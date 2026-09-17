#!/usr/bin/env python3
"""rclone bisync backend for the Omarchy Google Drive folder widget.

Keeps a real on-disk folder two-way synced with a chosen subset of a Google
Drive remote, so files are actually on disk and work offline. Whatever is not
selected is never downloaded; it stays reachable through the optional
browse mount, which is an on-demand FUSE view of the whole remote.

The heavy lifting is rclone's. This script owns the selection, turns it into
an rclone filters file, drives bisync, and reports state as JSON for the
Quickshell widget. It never reads or writes rclone's credentials.
"""

from __future__ import annotations

import argparse
import errno
import fcntl
import hashlib
import json
import os
import re
import secrets
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

STATE_DIR = Path(os.environ.get("XDG_STATE_HOME") or (Path.home() / ".local" / "state")) / "omarchy-gdrive"
SELECTION_PATH = STATE_DIR / "selection.json"
FILTERS_PATH = STATE_DIR / "filters.txt"
STATE_PATH = STATE_DIR / "state.json"
LOG_PATH = STATE_DIR / "sync.log"
WORKDIR = STATE_DIR / "workdir"
LOCK_PATH = STATE_DIR / "sync.lock"
UNIT_DIR = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")) / "systemd" / "user"

SERVICE = "omarchy-gdrive-sync.service"
BROWSE_SERVICE = "omarchy-gdrive-browse.service"
TIMER = "omarchy-gdrive-sync.timer"

REMOTE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]*$")
GLOB_SPECIALS = set("\\*?[]{}")
SKIP_LOCAL = {".rclone-bisync", "lost+found"}


# ---------------------------------------------------------------- primitives

def clean_text(value: str, limit: int = 400) -> str:
  text = " ".join((value or "").split())
  return text if len(text) <= limit else text[: limit - 1] + "…"


def run(command: list[str], timeout: float = 20, pass_fds: tuple[int, ...] = ()) -> tuple[int, str, str]:
  try:
    done = subprocess.run(command, check=False, capture_output=True, text=True, timeout=timeout, pass_fds=pass_fds)
  except FileNotFoundError as error:
    return 127, "", str(error)
  except subprocess.TimeoutExpired as error:
    out = error.stdout if isinstance(error.stdout, str) else (error.stdout or b"").decode(errors="replace")
    err = error.stderr if isinstance(error.stderr, str) else (error.stderr or b"").decode(errors="replace")
    return 124, out or "", err or "Command timed out"
  return done.returncode, done.stdout.strip(), done.stderr.strip()


def rclone_bin() -> str | None:
  return shutil.which("rclone")


def normalize_remote(value: str) -> str:
  remote = (value or "gdrive").strip().removesuffix(":").strip()
  if not REMOTE_RE.fullmatch(remote):
    raise ValueError("Remote name may only contain letters, numbers, spaces, dots, underscores, and hyphens")
  return remote


def normalize_path(value: str, fallback: str) -> Path:
  path = Path(os.path.expandvars(os.path.expanduser(value or fallback)))
  path = path if path.is_absolute() else (Path.home() / path)
  resolved = Path(os.path.normpath(str(path)))
  home = Path.home().resolve()
  if resolved in (Path("/"), home) or home not in resolved.parents:
    raise ValueError("Choose a folder inside your home directory, not the home directory itself")
  return resolved


def read_json(path: Path, fallback: Any) -> Any:
  try:
    with path.open(encoding="utf-8") as handle:
      return json.load(handle)
  except (OSError, json.JSONDecodeError):
    return fallback


def open_owned_dir(path: Path) -> int:
  """Open a directory we own, refusing a symlink standing in its place.
  Every write below happens relative to a descriptor from here, so another
  process racing the pathname cannot redirect it."""
  path.mkdir(parents=True, exist_ok=True, mode=0o700)
  try:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
  except OSError as error:
    if error.errno in (errno.ELOOP, errno.ENOTDIR):
      raise RuntimeError(f"{path} is not a plain directory; refusing to write there") from error
    raise
  info = os.fstat(fd)
  if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
    os.close(fd)
    raise RuntimeError(f"{path} is not a directory owned by you; refusing to write there")
  return fd


def write_atomic(path: Path, text: str, mode: int = 0o600) -> None:
  """Replace `path` with `text` without resolving a pathname twice: an
  unpredictable O_EXCL|O_NOFOLLOW temp inside the verified parent, fsync,
  then a rename relative to that same directory descriptor."""
  dfd = open_owned_dir(path.parent)
  try:
    for _ in range(32):
      tmp = f".{path.name}.{secrets.token_hex(8)}.tmp"
      try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, mode, dir_fd=dfd)
        break
      except FileExistsError:
        continue
    else:
      raise RuntimeError(f"could not create a temporary file beside {path}")
    try:
      info = os.fstat(fd)
      if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_nlink != 1:
        raise RuntimeError(f"unexpected file at {path.parent / tmp}; refusing to write")
      handle = os.fdopen(fd, "w", encoding="utf-8")
    except BaseException:
      os.close(fd)
      _unlink_quiet(tmp, dfd)
      raise
    try:
      with handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
      os.replace(tmp, path.name, src_dir_fd=dfd, dst_dir_fd=dfd)
      os.fsync(dfd)
    except BaseException:
      _unlink_quiet(tmp, dfd)
      raise
  finally:
    os.close(dfd)


def _unlink_quiet(name: str, dir_fd: int) -> None:
  try:
    os.unlink(name, dir_fd=dir_fd)
  except OSError:
    pass


def open_lock_file(path: Path):
  """A lock file that is created O_NOFOLLOW and checked to be our own
  regular file before anything flocks it."""
  dfd = open_owned_dir(path.parent)
  try:
    fd = os.open(path.name, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=dfd)
  except OSError as error:
    if error.errno == errno.ELOOP:
      raise RuntimeError(f"{path} is a symlink; refusing to lock through it") from error
    raise
  finally:
    os.close(dfd)
  info = os.fstat(fd)
  if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid():
    os.close(fd)
    raise RuntimeError(f"{path} is not a regular file owned by you")
  return os.fdopen(fd, "r+")


def remove_tree_contents(dfd: int, dev: int) -> None:
  """Empty the directory behind `dfd` relative to that descriptor: symlinks
  are unlinked rather than followed, names are never re-resolved from the
  root, and a second filesystem is never crossed."""
  with os.scandir(dfd) as scan:
    entries = [(entry.name, entry.is_dir(follow_symlinks=False)) for entry in scan]
  for name, is_dir in entries:
    if not is_dir:
      os.unlink(name, dir_fd=dfd)
      continue
    child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=dfd)
    try:
      if os.fstat(child).st_dev != dev:
        raise RuntimeError(f"{name} is on another filesystem; refusing to remove it")
      remove_tree_contents(child, dev)
    finally:
      os.close(child)
    os.rmdir(name, dir_fd=dfd)


def write_json(path: Path, payload: Any) -> None:
  write_atomic(path, json.dumps(payload, indent=2, sort_keys=True) + "\n")


# ---------------------------------------------------------------- selection

def load_selection() -> dict[str, Any]:
  data = read_json(SELECTION_PATH, {})
  if not isinstance(data, dict):
    data = {}
  folders = data.get("folders")
  folders = [str(name) for name in folders if str(name).strip()] if isinstance(folders, list) else []
  return {
    "folders": sorted(dict.fromkeys(folders), key=str.casefold),
    "rootFiles": data.get("rootFiles", True) is not False,
  }


def save_selection(selection: dict[str, Any]) -> None:
  write_json(SELECTION_PATH, selection)


def escape_glob(name: str) -> str:
  return "".join("\\" + ch if ch in GLOB_SPECIALS else ch for ch in name)


def build_filters(selection: dict[str, Any]) -> str:
  """Turn the selection into rclone filter rules. First match wins."""
  lines = [
    "# Generated by the Omarchy Google Drive widget. Edit the selection in the",
    "# panel instead; this file is rewritten on every change.",
  ]
  for name in selection["folders"]:
    lines.append("+ /" + escape_glob(name) + "/**")
  if selection["rootFiles"]:
    lines.append("+ /*")
  lines.append("- **")
  return "\n".join(lines) + "\n"


def filters_hash() -> str:
  try:
    return hashlib.sha256(FILTERS_PATH.read_bytes()).hexdigest()
  except OSError:
    return ""


def needs_resync(message: str) -> bool:
  """bisync reports a stale filters file only in --log-file, never on stderr."""
  if "must run --resync" in message.lower():
    return True
  try:
    tail = LOG_PATH.read_text(encoding="utf-8", errors="replace")[-8000:]
  except OSError:
    return False
  return "must run --resync" in tail.lower()


def sync_filters_file(selection: dict[str, Any]) -> bool:
  """Write filters.txt. Returns True when the contents actually changed."""
  desired = build_filters(selection)
  try:
    current = FILTERS_PATH.read_text(encoding="utf-8")
  except OSError:
    current = ""
  if current == desired:
    return False
  write_atomic(FILTERS_PATH, desired)
  return True


# ---------------------------------------------------------------- run state

def load_state() -> dict[str, Any]:
  data = read_json(STATE_PATH, {})
  return data if isinstance(data, dict) else {}


def patch_state(**fields: Any) -> dict[str, Any]:
  state = load_state()
  state.update(fields)
  write_json(STATE_PATH, state)
  return state


def service_active() -> bool:
  code, out, _ = run(["systemctl", "--user", "is-active", SERVICE], timeout=6)
  return out.strip() in ("active", "activating") or code == 0


def systemd_quote(value: str) -> str:
  """Quote a value for a systemd Exec= line; paths here contain spaces."""
  return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def unit_sources(remote: str, folder: Path, mount: Path) -> dict[str, str]:
  helper = systemd_quote(str(Path(__file__).resolve()))
  # Pin the system interpreter. A systemd user unit does not inherit the
  # PATH that version managers (mise, pyenv, asdf) install their shims on, and
  # those paths move on every version bump — resolving python3 from PATH here
  # bakes a path that silently stops existing.
  python = "/usr/bin/python3" if Path("/usr/bin/python3").exists() else (shutil.which("python3") or "/usr/bin/python3")
  return {
    SERVICE: f"""[Unit]
Description=Omarchy Google Drive folder sync (rclone bisync)
Documentation=https://rclone.org/bisync/

[Service]
Type=oneshot
ExecStart={python} {helper} run --remote {systemd_quote(remote)} --folder {systemd_quote(str(folder))}
# A first baseline over a large folder can take a while; bisync holds its own
# lock, and Type=oneshot keeps the timer from starting a second run.
TimeoutStartSec=7200
# Stay out of the way of interactive work.
Nice=10
IOSchedulingClass=idle
""",
    TIMER: """[Unit]
Description=Sync the Omarchy Google Drive folder

[Timer]
OnBootSec=2min
# Measured from when the last run finished, so a long sync never overlaps the
# next trigger. The interval drop-in overrides this.
OnUnitInactiveSec=10min
AccuracySec=30s
Unit=omarchy-gdrive-sync.service

[Install]
WantedBy=timers.target
""",
    BROWSE_SERVICE: f"""[Unit]
Description=Omarchy Google Drive browse mount (read-only, on demand)
Documentation=https://rclone.org/commands/rclone_mount/

[Service]
# The mount command returns once the FUSE mount is live; the rclone daemon it
# starts is deliberately session-independent, so the unit stays active to hold
# the ExecStop that tears it down.
Type=oneshot
RemainAfterExit=yes
ExecStart={python} {helper} mount --remote {systemd_quote(remote)} --mount {systemd_quote(str(mount))}
ExecStop={python} {helper} unmount --mount {systemd_quote(str(mount))}
TimeoutStartSec=180

[Install]
WantedBy=default.target
""",
  }


def ensure_units(remote: str, folder: Path, mount: Path) -> bool:
  """Write the plugin's own systemd units. Called before anything that needs
  them, so a fresh `omarchy plugin add` works with no manual setup. Rewrites
  only on change, and never touches the interval drop-in beside the timer."""
  changed = False
  for name, text in unit_sources(remote, folder, mount).items():
    path = UNIT_DIR / name
    try:
      current = path.read_text(encoding="utf-8")
    except OSError:
      current = ""
    if current != text:
      write_atomic(path, text, mode=0o644)
      changed = True
  if changed:
    run(["systemctl", "--user", "daemon-reload"], timeout=30)
  return changed


def unit_enabled(unit: str) -> bool:
  _, out, _ = run(["systemctl", "--user", "is-enabled", unit], timeout=6)
  return out.strip() in ("enabled", "enabled-runtime")


def timer_enabled() -> bool:
  return unit_enabled(TIMER)


# ---------------------------------------------------------------- remote I/O

def configured_remotes(rclone: str) -> tuple[set[str], str]:
  code, out, err = run([rclone, "listremotes"], timeout=10)
  if code != 0:
    return set(), clean_text(err or out or "Could not read rclone configuration")
  return {line.strip().removesuffix(":") for line in out.splitlines() if line.strip()}, ""


def remote_folders(rclone: str, remote: str) -> tuple[list[str], str]:
  code, out, err = run([rclone, "lsjson", f"{remote}:", "--dirs-only", "--no-modtime"], timeout=90)
  if code != 0:
    return [], clean_text(err or out or "Could not list Google Drive folders")
  try:
    rows = json.loads(out or "[]")
  except json.JSONDecodeError:
    return [], "rclone returned an unreadable folder listing"
  names = [str(row.get("Name") or "") for row in rows if isinstance(row, dict)]
  return sorted((n for n in names if n), key=str.casefold), ""


def remote_root_files(rclone: str, remote: str) -> tuple[int, int, str]:
  """Loose files at the top of the Drive — the ones in no folder at all."""
  code, out, err = run([rclone, "lsjson", f"{remote}:", "--files-only", "--no-modtime"], timeout=90)
  if code != 0:
    return 0, 0, clean_text(err or out or "Could not list Google Drive files")
  try:
    rows = json.loads(out or "[]")
  except json.JSONDecodeError:
    return 0, 0, "rclone returned an unreadable file listing"
  count = 0
  total = 0
  for row in rows:
    if not isinstance(row, dict):
      continue
    count += 1
    size = row.get("Size")
    # Google-native docs report -1; they are skipped by the sync anyway.
    if isinstance(size, (int, float)) and size > 0:
      total += int(size)
  return count, total, ""


def storage_usage(rclone: str, remote: str) -> tuple[int, int, bool, str]:
  code, out, err = run([rclone, "about", f"{remote}:", "--json"], timeout=25)
  if code != 0:
    return 0, 0, False, clean_text(err or out or "Storage usage is unavailable")
  try:
    data = json.loads(out)
  except json.JSONDecodeError:
    return 0, 0, False, "rclone returned invalid storage information"
  total = max(0, int(data.get("total") or 0))
  used = data.get("used")
  if used is None and total > 0 and data.get("free") is not None:
    used = total - int(data.get("free") or 0)
  return max(0, int(used or 0)), total, total > 0, ""


def directory_bytes(path: Path) -> int:
  total = 0
  stack = [path]
  while stack:
    current = stack.pop()
    try:
      entries = list(os.scandir(current))
    except OSError:
      continue
    for entry in entries:
      try:
        if entry.is_dir(follow_symlinks=False):
          stack.append(Path(entry.path))
        elif entry.is_file(follow_symlinks=False):
          total += entry.stat(follow_symlinks=False).st_size
      except OSError:
        continue
  return total


def local_top_level(folder: Path) -> dict[str, int]:
  """Bytes on disk per top-level entry of the synced folder."""
  sizes: dict[str, int] = {}
  try:
    entries = list(os.scandir(folder))
  except OSError:
    return sizes
  for entry in entries:
    if entry.name in SKIP_LOCAL:
      continue
    try:
      if entry.is_dir(follow_symlinks=False):
        sizes[entry.name] = directory_bytes(Path(entry.path))
    except OSError:
      continue
  return sizes


# ---------------------------------------------------------------- mount side

def path_is_live(path: Path) -> bool:
  """A FUSE mount whose daemon died stays in the mount table, but every call
  against it fails with ENOTCONN. That is what an unclean shutdown leaves."""
  try:
    os.stat(path)
    return True
  except OSError as error:
    return error.errno not in (errno.ENOTCONN, errno.EIO, errno.EREMOTEIO)


def mount_info(path: Path) -> tuple[bool, bool, str, bool]:
  """(mounted, mounted_by_rclone, fstype, alive)."""
  findmnt = shutil.which("findmnt")
  if not findmnt:
    return False, False, "", True
  code, out, _ = run([findmnt, "-rn", "-M", str(path), "-o", "FSTYPE"], timeout=8)
  if code != 0 or not out:
    return False, False, "", True
  fs_type = out.splitlines()[0].split()[0]
  by_rclone = "rclone" in fs_type.lower()
  return True, by_rclone, fs_type, path_is_live(path) if by_rclone else True


def detach_stale_mount(path: Path) -> bool:
  """Lazily detach a dead mount so a fresh daemon can claim the path."""
  fusermount = shutil.which("fusermount3") or shutil.which("fusermount")
  if not fusermount:
    return False
  return run([fusermount, "-uz", str(path)], timeout=15)[0] == 0


def mount_browse(remote: str, mount_path: Path) -> None:
  rclone = rclone_bin()
  if not rclone:
    raise RuntimeError("rclone is not installed")
  remotes, error = configured_remotes(rclone)
  if error:
    raise RuntimeError(error)
  if remote not in remotes:
    raise RuntimeError(f"rclone remote '{remote}' is not configured")

  mounted, by_rclone, fs_type, alive = mount_info(mount_path)
  if by_rclone and alive:
    return
  if by_rclone and not alive:
    # Daemon gone (crash, power loss, unclean reboot). Bury it, then remount.
    detach_stale_mount(mount_path)
    mounted, by_rclone, fs_type, alive = mount_info(mount_path)
  if mounted and not by_rclone:
    raise RuntimeError(f"{mount_path} is already mounted as {fs_type}")
  mount_path.mkdir(parents=True, exist_ok=True)
  if any(mount_path.iterdir()):
    raise RuntimeError(f"Browse folder is not empty: {mount_path}")

  STATE_DIR.mkdir(parents=True, exist_ok=True)
  command = [
    rclone, "mount", f"{remote}:", str(mount_path),
    "--daemon",
    "--read-only",
    "--vfs-cache-mode", "full",
    "--vfs-cache-max-age", "6h",
    "--dir-cache-time", "5m",
    "--poll-interval", "1m",
    "--log-file", str(STATE_DIR / "mount.log"),
    "--log-level", "INFO",
  ]
  # rclone --daemon forks and keeps the inherited pipes open, so never wait on
  # its stdout; poll the mount table for the result instead.
  try:
    subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
  except OSError as error:
    raise RuntimeError(f"Could not start rclone mount: {error}") from error

  deadline = time.monotonic() + 20
  while time.monotonic() < deadline:
    state = mount_info(mount_path)
    if state[1] and state[3]:
      return
    time.sleep(0.4)
  raise RuntimeError("rclone started but the browse mount did not appear")


def unmount_browse(mount_path: Path) -> None:
  mounted, by_rclone, fs_type, _ = mount_info(mount_path)
  if not mounted:
    return
  if not by_rclone:
    raise RuntimeError(f"Refusing to unmount {mount_path}; it is {fs_type}, not rclone")
  fusermount = shutil.which("fusermount3") or shutil.which("fusermount")
  if not fusermount:
    raise RuntimeError("fusermount is not installed")
  code, out, err = run([fusermount, "-u", str(mount_path)], timeout=15)
  if code != 0:
    code, out, err = run([fusermount, "-uz", str(mount_path)], timeout=15)
  if code != 0:
    raise RuntimeError(clean_text(err or out or "Could not unmount the browse folder"))


# ---------------------------------------------------------------- the sync

def bisync_command(rclone: str, remote: str, folder: Path, resync: bool) -> list[str]:
  command = [
    rclone, "bisync", f"{remote}:", str(folder),
    "--filters-file", str(FILTERS_PATH),
    "--workdir", str(WORKDIR),
    "--drive-skip-gdocs",
    "--create-empty-src-dirs",
    "--resilient",
    "--recover",
    "--transfers", "8",
    "--checkers", "16",
    "--log-file", str(LOG_PATH),
    "--log-level", "INFO",
  ]
  # Conflict policy differs between a baseline run and a steady-state run.
  command += ["--resync", "--resync-mode", "newer"] if resync else ["--conflict-resolve", "newer"]
  return command


def bisync_running() -> bool:
  for entry in Path("/proc").iterdir():
    if not entry.name.isdigit():
      continue
    try:
      argv = (entry / "cmdline").read_bytes().split(b"\0")
    except OSError:
      continue
    if len(argv) >= 2 and argv[0].endswith(b"rclone") and argv[1] == b"bisync":
      return True
  return False


def clear_stale_bisync_lock() -> bool:
  """A bisync killed by a reboot leaves its lock behind, and every later run
  refuses to start. Safe to clear once no bisync process is alive."""
  locks = list(WORKDIR.glob("*.lck"))
  if not locks or bisync_running():
    return False
  for lock in locks:
    try:
      lock.unlink()
    except OSError:
      pass
  return True


NETWORK_HINTS = (
  "no such host", "dial tcp", "connection refused", "network is unreachable",
  "i/o timeout", "could not connect", "temporary failure in name resolution",
  "tls handshake timeout", "connection reset by peer",
)


def looks_offline(message: str) -> bool:
  text = message.lower()
  return any(hint in text for hint in NETWORK_HINTS)


def do_run(remote_value: str, folder_value: str, force_resync: bool) -> int:
  """Blocking sync. This is what the systemd service executes."""
  remote = normalize_remote(remote_value)
  folder = normalize_path(folder_value, "~/Google Drive")
  rclone = rclone_bin()
  os.close(open_owned_dir(STATE_DIR))
  os.close(open_owned_dir(WORKDIR))

  lock = open_lock_file(LOCK_PATH)
  try:
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
  except OSError as error:
    if error.errno in (errno.EACCES, errno.EAGAIN):
      print("another sync is already running", file=sys.stderr)
      return 0
    raise

  selection = load_selection()
  if not selection["folders"] and not selection["rootFiles"]:
    patch_state(lastResult="idle", lastError="", lastMessage="Nothing selected to sync yet.")
    return 0
  if not rclone:
    patch_state(lastResult="error", lastError="rclone is not installed")
    return 1

  remotes, error = configured_remotes(rclone)
  if error or remote not in remotes:
    patch_state(lastResult="error", lastError=error or f"rclone remote '{remote}' is not configured")
    return 1

  mounted, _, fs_type, _ = mount_info(folder)
  if mounted:
    patch_state(lastResult="error",
                lastError=f"{folder} is a {fs_type} mount; the synced folder must be a plain directory")
    return 1

  folder.mkdir(parents=True, exist_ok=True)
  if clear_stale_bisync_lock():
    print("cleared a bisync lock left by an interrupted run", file=sys.stderr)
  sync_filters_file(selection)
  filters_now = filters_hash()
  state = load_state()
  # bisync aborts when its filters file differs from the one the baseline was
  # built with, and it needs a baseline before its first run at all. Compare
  # against the hash recorded on the last good run rather than against what
  # we just wrote: `select` rewrites filters.txt as well.
  resync = bool(force_resync
                or not state.get("baseline")
                or state.get("filtersHash") != filters_now)

  started = time.time()
  patch_state(running=True, startedTs=started, resync=resync, lastError="")
  code, out, err = run(bisync_command(rclone, remote, folder, resync), timeout=7200)
  finished = time.time()

  ok = code == 0
  message = clean_text(err or out or "")
  if not ok and not resync and needs_resync(message):
    # Steady-state run rejected; retry once with a baseline instead of
    # leaving the folder stuck until someone notices.
    code, out, err = run(bisync_command(rclone, remote, folder, True), timeout=7200)
    finished = time.time()
    ok = code == 0
    resync = True
    message = clean_text(err or out or "")

  detail = message
  if not ok and not detail:
    try:
      detail = clean_text(LOG_PATH.read_text(encoding="utf-8", errors="replace")[-4000:])
    except OSError:
      detail = ""
  offline = not ok and looks_offline(detail)

  patch_state(
    running=False,
    startedTs=started,
    finishedTs=finished,
    durationSec=round(finished - started, 1),
    lastResult="ok" if ok else ("offline" if offline else "error"),
    lastError="" if ok else (message or f"bisync exited with code {code}"),
    lastMessage="" if not ok else message,
    baseline=True if ok else bool(state.get("baseline")),
    filtersHash=filters_now if ok else state.get("filtersHash", ""),
    resync=resync,
  )
  return 0 if (ok or offline) else 1


# ---------------------------------------------------------------- payloads

def folders_payload(remote_value: str, folder_value: str) -> dict[str, Any]:
  remote = normalize_remote(remote_value)
  folder = normalize_path(folder_value, "~/Google Drive")
  rclone = rclone_bin()
  selection = load_selection()
  if not rclone:
    return {"ok": False, "folders": [], "lastError": "rclone is not installed"}

  names, error = remote_folders(rclone, remote)
  if error:
    return {"ok": False, "folders": [], "lastError": error}

  local = local_top_level(folder)
  root_count, root_bytes, _ = remote_root_files(rclone, remote)
  chosen = set(selection["folders"])
  rows = [
    {
      "name": name,
      "selected": name in chosen,
      "localBytes": local.get(name, 0),
      "onDisk": name in local,
    }
    for name in names
  ]
  # A folder that was deselected but still occupies disk is worth surfacing.
  stale = [
    {"name": name, "selected": False, "localBytes": size, "onDisk": True, "stale": True}
    for name, size in sorted(local.items(), key=lambda item: item[0].casefold())
    if name not in {row["name"] for row in rows}
  ]
  return {
    "ok": True,
    "folders": rows + stale,
    "rootFiles": selection["rootFiles"],
    "rootFileCount": root_count,
    "rootFileBytes": root_bytes,
    "staleBytes": sum(row["localBytes"] for row in rows if not row["selected"] and row["onDisk"])
      + sum(row["localBytes"] for row in stale),
    "lastError": "",
  }


def status_payload(remote_value: str, folder_value: str, mount_value: str) -> dict[str, Any]:
  remote = normalize_remote(remote_value)
  folder = normalize_path(folder_value, "~/Google Drive")
  mount_path = normalize_path(mount_value, "~/GDrive-Browse")
  rclone = rclone_bin()
  selection = load_selection()
  state = load_state()

  payload: dict[str, Any] = {
    "ok": True,
    "installed": rclone is not None,
    "authenticated": False,
    "syncing": False,
    "statusText": "Not installed",
    "folderPath": str(folder),
    "mountPath": str(mount_path),
    "remoteName": remote,
    "selectedCount": len(selection["folders"]),
    "rootFiles": selection["rootFiles"],
    "localBytes": 0,
    "usedBytes": 0,
    "quotaBytes": 0,
    "usagePercent": 0,
    "quotaKnown": False,
    "browseMounted": False,
    "browseStale": False,
    "browseEnabled": False,
    "timerEnabled": False,
    "unitsInstalled": False,
    "lastResult": str(state.get("lastResult") or ""),
    "lastFinishedTs": int(state.get("finishedTs") or 0),
    "lastDurationSec": float(state.get("durationSec") or 0),
    "baseline": state.get("baseline") is True,
    "warning": "",
    "lastError": str(state.get("lastError") or ""),
  }

  if not rclone:
    return payload

  remotes, config_error = configured_remotes(rclone)
  payload["authenticated"] = remote in remotes
  browse_state = mount_info(mount_path)
  payload["browseMounted"] = browse_state[1] and browse_state[3]
  payload["browseStale"] = browse_state[1] and not browse_state[3]
  payload["browseEnabled"] = unit_enabled(BROWSE_SERVICE)
  payload["timerEnabled"] = timer_enabled()
  payload["unitsInstalled"] = (UNIT_DIR / SERVICE).exists() and (UNIT_DIR / TIMER).exists()
  payload["syncing"] = service_active()
  payload["localBytes"] = directory_bytes(folder) if folder.is_dir() else 0

  if config_error:
    payload["statusText"] = "Configuration unavailable"
    payload["lastError"] = config_error
    return payload
  if not payload["authenticated"]:
    payload["statusText"] = "Needs connection"
    return payload

  folder_mounted, _, fs_type, _ = mount_info(folder)
  if folder_mounted:
    payload["statusText"] = "Folder is a mount"
    payload["lastError"] = f"{folder} is mounted as {fs_type}; unmount it to sync into it"
    return payload

  used, total, quota_known, warning = storage_usage(rclone, remote)
  payload.update(usedBytes=used, quotaBytes=total, quotaKnown=quota_known,
                 usagePercent=(used / total * 100) if total > 0 else 0, warning=warning)

  if payload["syncing"]:
    payload["statusText"] = "Syncing…"
  elif payload["selectedCount"] == 0 and not selection["rootFiles"]:
    payload["statusText"] = "Nothing selected"
  elif payload["lastResult"] == "offline":
    payload["statusText"] = "Waiting for network"
  elif payload["lastResult"] == "error":
    payload["statusText"] = "Sync failed"
  elif payload["baseline"]:
    payload["statusText"] = "Synced"
  else:
    payload["statusText"] = "Ready to sync"
  return payload


# ---------------------------------------------------------------- commands

def cmd_select(args: argparse.Namespace) -> None:
  selection = load_selection()
  folders = set(selection["folders"])
  if args.set is not None:
    folders = {name for name in args.set if name.strip()}
  folders.update(args.add or [])
  folders.difference_update(args.remove or [])
  selection["folders"] = sorted(folders, key=str.casefold)
  if args.root_files is not None:
    selection["rootFiles"] = args.root_files
  save_selection(selection)
  sync_filters_file(selection)
  print(json.dumps({"ok": True, "folders": selection["folders"], "rootFiles": selection["rootFiles"]}))


def cmd_cleanup(args: argparse.Namespace) -> None:
  """Delete local copies of deselected folders, but only after proving the
  files still exist on Drive. Nothing is removed on a failed check, and the
  check and the removal share one open directory descriptor, so whatever the
  name points at by the time the check finishes is never what gets deleted."""
  remote = normalize_remote(args.remote)
  folder = normalize_path(args.folder, "~/Google Drive")
  rclone = rclone_bin()
  if not rclone:
    raise RuntimeError("rclone is not installed")

  selection = load_selection()
  chosen = set(selection["folders"])
  local = local_top_level(folder)
  targets = [name for name in sorted(local, key=str.casefold) if name not in chosen]
  if args.only:
    targets = [name for name in targets if name in set(args.only)]
  if not targets:
    print(json.dumps({"ok": True, "removed": [], "freedBytes": 0}))
    return

  removed: list[str] = []
  freed = 0
  refused: list[dict[str, str]] = []
  folder_fd = open_owned_dir(folder)
  try:
    folder_dev = os.fstat(folder_fd).st_dev
    for name in targets:
      try:
        dfd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=folder_fd)
      except OSError:
        continue  # gone, or no longer a plain directory
      try:
        info = os.fstat(dfd)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_dev != folder_dev:
          refused.append({"name": name, "reason": "not a plain folder owned by you"})
          continue
        identity = (info.st_dev, info.st_ino)
        # rclone reads through the descriptor, so it verifies exactly the
        # directory emptied below, whatever the name resolves to meanwhile.
        fd_path = f"/proc/self/fd/{dfd}"
        code, out, err = run(
          [rclone, "check", fd_path, f"{remote}:{name}", "--one-way", "--drive-skip-gdocs"],
          timeout=1800,
          pass_fds=(dfd,),
        )
        if code != 0:
          reason = (err or out or "verification failed").replace(fd_path, str(folder / name))
          refused.append({"name": name, "reason": clean_text(reason)})
          continue
        try:
          remove_tree_contents(dfd, folder_dev)
          current = os.stat(name, dir_fd=folder_fd, follow_symlinks=False)
          if (current.st_dev, current.st_ino) != identity:
            raise RuntimeError("folder changed during cleanup")
          os.rmdir(name, dir_fd=folder_fd)
        except (OSError, RuntimeError) as error:
          refused.append({"name": name, "reason": clean_text(str(error))})
          continue
      finally:
        os.close(dfd)
      removed.append(name)
      freed += local.get(name, 0)
  finally:
    os.close(folder_fd)

  print(json.dumps({"ok": not refused, "removed": removed, "freedBytes": freed, "refused": refused}))


def write_timer_interval(minutes: int) -> None:
  """Override the shipped cadence with a drop-in, leaving the unit itself alone."""
  value = max(1, min(1440, int(minutes)))
  write_atomic(
    UNIT_DIR / (TIMER + ".d") / "interval.conf",
    "# Generated by the Omarchy Google Drive widget.\n"
    "[Timer]\n"
    f"OnUnitInactiveSec={value}min\n",
    mode=0o644,
  )
  run(["systemctl", "--user", "daemon-reload"], timeout=25)


def units_from_args(args: argparse.Namespace) -> None:
  ensure_units(
    normalize_remote(args.remote),
    normalize_path(args.folder, "~/Google Drive"),
    normalize_path(args.mount, "~/GDrive-Browse"),
  )


def cmd_browse(args: argparse.Namespace) -> None:
  """Enable/disable the browse mount as a unit, so the choice survives a
  reboot instead of living only in this session's mount table."""
  units_from_args(args)
  action = ["enable", "--now"] if args.enable else ["disable", "--now"]
  code, out, err = run(["systemctl", "--user", *action, BROWSE_SERVICE], timeout=90)
  if code != 0:
    raise RuntimeError(clean_text(err or out or "Could not change the browse mount"))
  print(json.dumps({"ok": True, "browseEnabled": unit_enabled(BROWSE_SERVICE)}))


def cmd_timer(args: argparse.Namespace) -> None:
  units_from_args(args)
  if args.interval:
    write_timer_interval(args.interval)
  action = ["enable", "--now"] if args.enable else ["disable", "--now"]
  code, out, err = run(["systemctl", "--user", *action, TIMER], timeout=20)
  if code != 0:
    raise RuntimeError(clean_text(err or out or "Could not change the sync timer"))
  print(json.dumps({"ok": True, "timerEnabled": timer_enabled()}))


def cmd_sync(args: argparse.Namespace) -> None:
  """Hand the run to systemd so it survives a shell restart and cannot
  overlap with the timer's own run."""
  units_from_args(args)
  command = ["systemctl", "--user", "start", SERVICE]
  if args.resync:
    patch_state(forceResync=True)
  code, out, err = run(command + ["--no-block"], timeout=20)
  if code != 0:
    raise RuntimeError(clean_text(err or out or "Could not start the sync service"))
  print(json.dumps({"ok": True, "started": True}))


def parser() -> argparse.ArgumentParser:
  result = argparse.ArgumentParser(description=__doc__)
  commands = result.add_subparsers(dest="command", required=True)

  status = commands.add_parser("status")
  status.add_argument("--remote", default="gdrive")
  status.add_argument("--folder", default="~/Google Drive")
  status.add_argument("--mount", default="~/GDrive-Browse")

  folders = commands.add_parser("folders")
  folders.add_argument("--remote", default="gdrive")
  folders.add_argument("--folder", default="~/Google Drive")

  select = commands.add_parser("select")
  select.add_argument("--add", action="append", default=[])
  select.add_argument("--remove", action="append", default=[])
  select.add_argument("--set", action="append", default=None)
  select.add_argument("--root-files", dest="root_files", action="store_true", default=None)
  select.add_argument("--no-root-files", dest="root_files", action="store_false", default=None)

  sync = commands.add_parser("sync")
  sync.add_argument("--resync", action="store_true")
  sync.add_argument("--remote", default="gdrive")
  sync.add_argument("--folder", default="~/Google Drive")
  sync.add_argument("--mount", default="~/GDrive-Browse")

  runner = commands.add_parser("run")
  runner.add_argument("--remote", default="gdrive")
  runner.add_argument("--folder", default="~/Google Drive")
  runner.add_argument("--resync", action="store_true")

  cleanup = commands.add_parser("cleanup")
  cleanup.add_argument("--remote", default="gdrive")
  cleanup.add_argument("--folder", default="~/Google Drive")
  cleanup.add_argument("--only", action="append", default=[])

  mount = commands.add_parser("mount")
  mount.add_argument("--remote", default="gdrive")
  mount.add_argument("--mount", default="~/GDrive-Browse")

  unmount = commands.add_parser("unmount")
  unmount.add_argument("--mount", default="~/GDrive-Browse")

  browse = commands.add_parser("browse")
  browse_group = browse.add_mutually_exclusive_group(required=True)
  browse_group.add_argument("--enable", action="store_true")
  browse_group.add_argument("--disable", action="store_true")
  browse.add_argument("--remote", default="gdrive")
  browse.add_argument("--folder", default="~/Google Drive")
  browse.add_argument("--mount", default="~/GDrive-Browse")

  timer = commands.add_parser("timer")
  group = timer.add_mutually_exclusive_group(required=True)
  group.add_argument("--enable", action="store_true")
  group.add_argument("--disable", action="store_true")
  timer.add_argument("--interval", type=int, default=0)
  timer.add_argument("--remote", default="gdrive")
  timer.add_argument("--folder", default="~/Google Drive")
  timer.add_argument("--mount", default="~/GDrive-Browse")

  return result


def main() -> int:
  args = parser().parse_args()
  try:
    if args.command == "status":
      print(json.dumps(status_payload(args.remote, args.folder, args.mount)))
    elif args.command == "folders":
      print(json.dumps(folders_payload(args.remote, args.folder)))
    elif args.command == "select":
      cmd_select(args)
    elif args.command == "sync":
      cmd_sync(args)
    elif args.command == "run":
      state = load_state()
      forced = args.resync or state.get("forceResync") is True
      if forced:
        patch_state(forceResync=False)
      return do_run(args.remote, args.folder, forced)
    elif args.command == "cleanup":
      cmd_cleanup(args)
    elif args.command == "mount":
      mount_browse(normalize_remote(args.remote), normalize_path(args.mount, "~/GDrive-Browse"))
    elif args.command == "unmount":
      unmount_browse(normalize_path(args.mount, "~/GDrive-Browse"))
    elif args.command == "browse":
      cmd_browse(args)
    elif args.command == "timer":
      cmd_timer(args)
  except (OSError, RuntimeError, ValueError) as error:
    if args.command in ("status", "folders"):
      print(json.dumps({"ok": False, "lastError": clean_text(str(error)), "folders": []}))
      return 0
    print(clean_text(str(error)), file=sys.stderr)
    return 1
  return 0


if __name__ == "__main__":
  raise SystemExit(main())
