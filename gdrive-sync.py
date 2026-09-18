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
import codecs
import ctypes
import errno
import fcntl
import hashlib
import itertools
import json
import os
import re
import secrets
import selectors
import shutil
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Iterable, Iterator

STATE_DIR = Path(os.environ.get("XDG_STATE_HOME") or (Path.home() / ".local" / "state")) / "omarchy-gdrive"
SELECTION_PATH = STATE_DIR / "selection.json"
FILTERS_PATH = STATE_DIR / "filters.txt"
STATE_PATH = STATE_DIR / "state.json"
CACHE_PATH = STATE_DIR / "cache.json"
LOG_PATH = STATE_DIR / "sync.log"
MOUNT_LOG_PATH = STATE_DIR / "mount.log"
WORKDIR = STATE_DIR / "workdir"
LOCK_PATH = STATE_DIR / "sync.lock"
UNIT_DIR = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")) / "systemd" / "user"

SERVICE = "omarchy-gdrive-sync.service"
BROWSE_SERVICE = "omarchy-gdrive-browse.service"
TIMER = "omarchy-gdrive-sync.timer"

REMOTE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]*$")
GLOB_SPECIALS = set("\\*?[]{}")
SKIP_LOCAL = {".rclone-bisync", "lost+found"}
CONTROL = re.compile(r"[\x00-\x1f\x7f]")
# A Drive name lands in a filter rule, an argv and the panel; a byte that is
# not fit for all three (a newline, a slash, a control) keeps it out of all.
NAME_BAD = re.compile(r"[\x00-\x1f\x7f/]")
ANSI = re.compile(r"\x1b\[[0-9;]*m")

# Everything a child process or the remote sends is read under a cap. A
# listing that blows one is refused whole rather than shown in part.
MAX_OUTPUT = 1 << 20            # default per-pipe cap for run()
MAX_LIST_BYTES = 8 << 20        # rclone lsjson transfer cap
MAX_LIST_ENTRIES = 5000         # top-level folders the widget will show
MAX_ITEM_BYTES = 64 << 10       # one lsjson row
MAX_NAME_BYTES = 255            # NAME_MAX: rclone could not create a longer local name anyway
CHECK_STDERR_BYTES = 256 << 10  # rclone check prints one line per differing file
LOG_ROTATE_BYTES = 2 << 20
LOG_TAIL_BYTES = 16384
MAX_WALK_ENTRIES = 100_000
MAX_WALK_SECONDS = 2.0
ABOUT_CACHE_SECONDS = 60
EXIT_TIMEOUT = 124
EXIT_TOO_LARGE = 125
# bisync's "must run --resync" abort is a FatalError (exit 7 in current
# rclone; the docs say 2), never a plain error.
RCLONE_RESYNC_CODES = (2, 7)


# ---------------------------------------------------------------- primitives

def clean_text(value: str, limit: int = 400) -> str:
  text = " ".join((value or "").split())
  return text if len(text) <= limit else text[: limit - 1] + "…"


try:
  _PRCTL = ctypes.CDLL(None, use_errno=True).prctl
  _PRCTL.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong]
except (OSError, AttributeError):
  _PRCTL = None


def _die_with_parent() -> None:
  """Runs in the child between fork and exec: PR_SET_PDEATHSIG, so an rclone
  outlives neither this helper nor the watchdog that kills it. The helper is
  single-threaded and the symbol was resolved in the parent, which is what
  makes a preexec_fn safe here."""
  if _PRCTL is not None:
    _PRCTL(1, int(signal.SIGKILL), 0, 0, 0)


class ChildTimeout(RuntimeError):
  code = EXIT_TIMEOUT


class ChildOutputTooLarge(RuntimeError):
  code = EXIT_TOO_LARGE


_LIVE: set[subprocess.Popen] = set()


class Child:
  """A child process read under a deadline and per-pipe byte caps, in its
  own session so the whole group can be killed. Yields stdout as it comes;
  stderr is collected. Hitting any limit kills the group and raises, and
  the caller gets nothing rather than a partial result."""

  def __init__(self, command: list[str], *, timeout: float = 20.0, max_stdout: int = MAX_OUTPUT,
               max_stderr: int = MAX_OUTPUT, pass_fds: tuple[int, ...] = ()):
    self.command = command
    self.timeout = timeout
    self.max_stdout = max_stdout
    self.max_stderr = max_stderr
    self.pass_fds = pass_fds
    self.proc: subprocess.Popen | None = None
    self.stderr = bytearray()
    self.returncode: int | None = None
    self.deadline = 0.0

  def __enter__(self) -> "Child":
    self.proc = subprocess.Popen(
      self.command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
      start_new_session=True, pass_fds=self.pass_fds, preexec_fn=_die_with_parent)
    self.deadline = time.monotonic() + self.timeout
    _LIVE.add(self.proc)
    return self

  def __exit__(self, *exc: object) -> None:
    proc = self.proc
    if proc is None:
      return
    self._killpg()
    if proc.poll() is None:
      try:
        proc.wait(timeout=5)
      except subprocess.TimeoutExpired:
        pass
    for pipe in (proc.stdout, proc.stderr):
      if pipe is not None:
        pipe.close()
    _LIVE.discard(proc)

  def _killpg(self) -> None:
    proc = self.proc
    if proc is None:
      return
    try:
      os.killpg(proc.pid, signal.SIGKILL)
    except OSError:
      pass  # already gone, or not ours to signal

  def _abort(self) -> None:
    self._killpg()
    if self.proc is not None:
      try:
        self.proc.wait(timeout=5)
      except subprocess.TimeoutExpired:
        pass

  def chunks(self) -> Iterator[bytes]:
    proc = self.proc
    assert proc is not None and proc.stdout is not None and proc.stderr is not None
    out_fd, err_fd = proc.stdout.fileno(), proc.stderr.fileno()
    limit = {out_fd: self.max_stdout, err_fd: self.max_stderr}
    seen = {out_fd: 0, err_fd: 0}
    with selectors.DefaultSelector() as sel:
      for fd in limit:
        sel.register(fd, selectors.EVENT_READ)
      while limit:
        wait = self.deadline - time.monotonic()
        if wait <= 0:
          self._abort()
          raise ChildTimeout()
        for key, _ in sel.select(wait):
          try:
            data = os.read(key.fd, 65536)
          except OSError:
            data = b""
          if not data:
            sel.unregister(key.fd)
            del limit[key.fd]
            continue
          seen[key.fd] += len(data)
          if seen[key.fd] > limit[key.fd]:
            self._abort()
            raise ChildOutputTooLarge()
          if key.fd == err_fd:
            self.stderr += data
          else:
            yield data
    try:
      self.returncode = proc.wait(timeout=max(0.0, self.deadline - time.monotonic()))
    except subprocess.TimeoutExpired:
      self._abort()
      raise ChildTimeout()
    # The leader has exited with its own code; anything it left behind in
    # the session goes now.
    self._killpg()

  def stderr_text(self) -> str:
    return bytes(self.stderr).decode("utf-8", "replace").strip()

  def failure(self) -> str:
    """After a read was abandoned: the child's own complaint if it exited
    on its own with an error, else "". Stops the child first."""
    proc = self.proc
    if proc is None:
      return ""
    self._abort()
    room = max(0, self.max_stderr - len(self.stderr))
    if proc.stderr is not None and room:
      try:
        self.stderr += os.read(proc.stderr.fileno(), room)
      except OSError:
        pass
    code = proc.returncode
    if code is None or code == 0 or code < 0:
      return ""
    return self.stderr_text()


def run(command: list[str], timeout: float = 20, pass_fds: tuple[int, ...] = (), *,
        max_stdout: int = MAX_OUTPUT, max_stderr: int = MAX_OUTPUT) -> tuple[int, str, str]:
  """(code, stdout, stderr). A child that overruns the deadline or a cap is
  killed with its group and reported as 124 / 125 with empty output."""
  out = bytearray()
  try:
    with Child(command, timeout=timeout, max_stdout=max_stdout, max_stderr=max_stderr, pass_fds=pass_fds) as child:
      for data in child.chunks():
        out += data
      code = child.returncode or 0
      err = child.stderr_text()
  except FileNotFoundError as error:
    return 127, "", str(error)
  except OSError as error:
    return 126, "", str(error)
  except ChildTimeout:
    return EXIT_TIMEOUT, "", f"Command timed out after {timeout:g}s"
  except ChildOutputTooLarge:
    return EXIT_TOO_LARGE, "", "Command produced too much output"
  return code, bytes(out).decode("utf-8", "replace").strip(), err


def kill_live_children() -> None:
  for proc in list(_LIVE):
    try:
      os.killpg(proc.pid, signal.SIGKILL)
    except OSError:
      pass


def _on_signal(signum: int, _frame: object) -> None:
  kill_live_children()
  raise SystemExit(128 + signum)


class ListingTooLarge(RuntimeError):
  pass


class ListingUnreadable(RuntimeError):
  pass


def iter_json_array(chunks: Iterable[bytes], *, max_entries: int | None = None,
                    max_item_bytes: int | None = None) -> Iterator[Any]:
  """Stream the objects of a JSON array (rclone lsjson's output) one at a
  time, holding at most one item's bytes in memory. Every row must be an
  object; the count and the size of a row are capped."""
  max_entries = MAX_LIST_ENTRIES if max_entries is None else max_entries
  max_item = MAX_ITEM_BYTES if max_item_bytes is None else max_item_bytes
  decoder = json.JSONDecoder(parse_constant=lambda _: None)      # Infinity / NaN -> None
  utf8 = codecs.getincrementaldecoder("utf-8")(errors="replace")
  buf = ""
  opened = False
  closed = False
  count = 0
  for raw in itertools.chain(chunks, (b"",)):
    buf += utf8.decode(raw, final=not raw)
    while True:
      buf = buf.lstrip()
      if not buf:
        break
      if closed:
        raise ListingUnreadable("data after the end of the listing")
      if not opened:
        if buf[0] != "[":
          raise ListingUnreadable("listing does not start with an array")
        buf = buf[1:]
        opened = True
        continue
      if buf[0] == "]":
        # Keep draining: the producer must reach EOF so its exit code is known.
        closed = True
        buf = buf[1:]
        continue
      if buf[0] == ",":
        buf = buf[1:]
        continue
      if buf[0] != "{":
        raise ListingUnreadable("listing row is not an object")
      try:
        item, end = decoder.raw_decode(buf)
      except json.JSONDecodeError:
        if len(buf) > max_item:
          raise ListingTooLarge("a listing entry is too long")
        break                                                    # need more bytes
      except ValueError:
        raise ListingUnreadable("listing row is not readable")    # e.g. an absurdly long number
      if end > max_item:
        raise ListingTooLarge("a listing entry is too long")
      buf = buf[end:]
      count += 1
      if count > max_entries:
        raise ListingTooLarge(f"more than {max_entries} entries")
      yield item
  if not closed:
    raise ListingUnreadable("listing ended early")


def valid_drive_name(name: object) -> bool:
  """A Drive folder name the widget will list, select and write into a
  filter rule. Anything else is left out entirely."""
  if not isinstance(name, str) or not name or name in (".", "..") or name != name.strip():
    return False
  if NAME_BAD.search(name):
    return False
  try:
    return len(name.encode("utf-8")) <= MAX_NAME_BYTES
  except UnicodeEncodeError:
    return False


def clamp_int(value: object, limit: int = 1 << 62) -> int:
  """A non-negative integer from JSON, else 0 (bool, float, str all count as 0)."""
  if isinstance(value, bool) or not isinstance(value, int):
    return 0
  return value if 0 <= value <= limit else 0


def tail_bytes(path: Path, limit: int) -> str:
  """The last `limit` bytes of a regular file, without reading the rest."""
  try:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
  except OSError:
    return ""
  try:
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode):
      return ""
    os.lseek(fd, max(0, info.st_size - limit), os.SEEK_SET)
    chunks = []
    remaining = limit
    while remaining > 0:
      data = os.read(fd, min(65536, remaining))
      if not data:
        break
      chunks.append(data)
      remaining -= len(data)
    return b"".join(chunks).decode("utf-8", "replace")
  finally:
    os.close(fd)


def rotate_log(path: Path, limit: int = LOG_ROTATE_BYTES) -> None:
  """Roll `path` to `path.1` once it exceeds `limit`, discarding the older
  roll. Only called while nothing is writing the log."""
  dfd = open_owned_dir(path.parent, fix_mode=0o700)
  try:
    try:
      info = os.stat(path.name, dir_fd=dfd, follow_symlinks=False)
    except FileNotFoundError:
      return
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid():
      _unlink_quiet(path.name, dfd)
      return
    if info.st_size > limit:
      os.replace(path.name, path.name + ".1", src_dir_fd=dfd, dst_dir_fd=dfd)
  finally:
    os.close(dfd)


def rclone_bin() -> str | None:
  return shutil.which("rclone")


def normalize_remote(value: str) -> str:
  remote = (value or "gdrive").strip().removesuffix(":").strip()
  if not REMOTE_RE.fullmatch(remote):
    raise ValueError("Remote name may only contain letters, numbers, spaces, dots, underscores, and hyphens")
  return remote


def normalize_path(value: str, fallback: str) -> Path:
  if CONTROL.search(value or ""):
    raise ValueError("Folder path contains control characters")
  path = Path(os.path.expandvars(os.path.expanduser(value or fallback)))
  path = path if path.is_absolute() else (Path.home() / path)
  resolved = Path(os.path.normpath(str(path)))
  home = Path.home().resolve()
  if resolved in (Path("/"), home) or home not in resolved.parents:
    raise ValueError("Choose a folder inside your home directory, not the home directory itself")
  if CONTROL.search(str(resolved)):
    raise ValueError("Folder path contains control characters")
  return resolved


def read_json(path: Path, fallback: Any) -> Any:
  try:
    with path.open(encoding="utf-8") as handle:
      return json.load(handle)
  except (OSError, json.JSONDecodeError):
    return fallback


def open_owned_dir(path: Path, *, fix_mode: int | None = None) -> int:
  """Open a directory we own, refusing a symlink standing in its place.
  Every write below happens relative to a descriptor from here, so another
  process racing the pathname cannot redirect it. With `fix_mode`, a
  directory that lets group or others in is tightened on the descriptor."""
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
  if fix_mode is not None and stat.S_IMODE(info.st_mode) & 0o077:
    try:
      os.fchmod(fd, fix_mode)
    except OSError:
      pass
  return fd


def ensure_state_dir() -> None:
  """The state directory and everything in it are private to this user.
  Files an older release created 0644 (and rclone's own listings, which it
  writes with the umask on every run) are tightened each time."""
  for directory in (STATE_DIR, WORKDIR):
    dfd = open_owned_dir(directory, fix_mode=0o700)
    try:
      with os.scandir(dfd) as scan:
        names = [entry.name for entry in scan if entry.is_file(follow_symlinks=False)]
      for name in names:
        try:
          fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=dfd)
        except OSError:
          continue
        try:
          info = os.fstat(fd)
          if stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid() and stat.S_IMODE(info.st_mode) & 0o077:
            os.fchmod(fd, 0o600)
        except OSError:
          pass
        finally:
          os.close(fd)
    finally:
      os.close(dfd)


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
  folders = data.get("folders") if isinstance(data.get("folders"), list) else []
  kept = [name for name in folders if valid_drive_name(name)]
  if len(kept) != len(folders):
    print(f"ignoring {len(folders) - len(kept)} invalid folder name(s) in selection.json", file=sys.stderr)
  return {
    "folders": sorted(dict.fromkeys(kept), key=str.casefold)[:MAX_LIST_ENTRIES],
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
    if not valid_drive_name(name):
      continue  # never a rule from a name that could hold one
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


RESYNC_LINE = re.compile(
  r"^\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2}(?:\.\d+)? ERROR : Bisync (?:aborted|interrupted)\. Must run --resync to recover\.\s*$",
  re.M)


def needs_resync(code: int) -> bool:
  """bisync reports a stale filters file only in --log-file, never on stderr,
  so the exit code is the signal and rclone's own timestamped ERROR line in
  the log tail confirms it. A remote file name that happens to contain the
  words sits on an INFO line and never matches."""
  if code not in RCLONE_RESYNC_CODES:
    return False
  return RESYNC_LINE.search(ANSI.sub("", tail_bytes(LOG_PATH, LOG_TAIL_BYTES))) is not None


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
  """Quote a value for a systemd Exec= line; paths here contain spaces.
  `%` is a specifier and `$` an expansion to systemd, so both are doubled;
  a control character would end the line, so it is refused."""
  if CONTROL.search(value):
    raise ValueError("Path contains control characters")
  escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%").replace("$", "$$")
  return '"' + escaped + '"'


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
  """The Drive's top-level folders, streamed from rclone under hard caps.
  Anyone can share a folder into a Drive, so the count, the size of the
  listing and every name are bounded; past a bound there is no listing at
  all, never a partial one."""
  names: list[str] = []
  dropped = 0
  try:
    with Child([rclone, "lsjson", f"{remote}:", "--dirs-only", "--no-modtime"],
               timeout=90, max_stdout=MAX_LIST_BYTES) as child:
      try:
        for row in iter_json_array(child.chunks()):
          name = row.get("Name") if isinstance(row, dict) else None
          if valid_drive_name(name):
            names.append(name)
          else:
            dropped += 1
      except ListingUnreadable:
        # rclone that failed outright prints nothing on stdout: say why.
        return [], clean_text(child.failure() or "rclone returned an unreadable folder listing")
      if child.returncode != 0:
        return [], clean_text(child.stderr_text() or "Could not list Google Drive folders")
  except ListingTooLarge:
    return [], f"Google Drive has more than {MAX_LIST_ENTRIES} top-level folders (or an unusable entry); the widget can't list it"
  except ChildTimeout:
    return [], "Listing Google Drive folders timed out"
  except ChildOutputTooLarge:
    return [], "The Google Drive folder listing is too large to show"
  except OSError as error:
    return [], clean_text(str(error))
  if dropped:
    print(f"left out {dropped} Drive folder(s) whose names are not usable here", file=sys.stderr)
  return sorted(names, key=str.casefold), ""


def remote_root_files(rclone: str, remote: str) -> tuple[int, int, str]:
  """Loose files at the top of the Drive — the ones in no folder at all.
  `rclone size` with the same two rules the sync uses answers with one
  small object however many files there are."""
  code, out, err = run([rclone, "size", f"{remote}:", "--json", "--filter", "+ /*", "--filter", "- **"],
                       timeout=90, max_stdout=65536)
  if code != 0:
    return 0, 0, clean_text(err or out or "Could not count Google Drive files")
  try:
    data = json.loads(out or "{}", parse_constant=lambda _: None)
  except ValueError:
    return 0, 0, "rclone returned an unreadable file count"
  if not isinstance(data, dict):
    return 0, 0, "rclone returned an unreadable file count"
  return clamp_int(data.get("count")), clamp_int(data.get("bytes")), ""


def load_cache() -> dict[str, Any]:
  data = read_json(CACHE_PATH, {})
  return data if isinstance(data, dict) else {}


def save_cache(cache: dict[str, Any]) -> None:
  if cache != load_cache():
    write_json(CACHE_PATH, cache)


def storage_usage(rclone: str, remote: str, cache: dict[str, Any] | None = None) -> tuple[int, int, bool, str]:
  """(used, total, known, warning). Cached for a minute: the panel polls
  every few seconds while a sync runs, and that is not a reason to ask
  Google as often."""
  entry = (cache or {}).get("about")
  if isinstance(entry, dict) and entry.get("remote") == remote and time.time() - clamp_int(entry.get("ts")) < ABOUT_CACHE_SECONDS:
    return (clamp_int(entry.get("used")), clamp_int(entry.get("total")),
            entry.get("known") is True, str(entry.get("warning") or ""))
  code, out, err = run([rclone, "about", f"{remote}:", "--json"], timeout=25, max_stdout=65536)
  used = total = 0
  warning = ""
  if code != 0:
    warning = clean_text(err or out or "Storage usage is unavailable")
  else:
    try:
      data = json.loads(out or "{}", parse_constant=lambda _: None)
    except ValueError:
      data = None
    if not isinstance(data, dict):
      warning = "rclone returned invalid storage information"
    else:
      total = clamp_int(data.get("total"))
      used_value = data.get("used")
      if used_value is None and total > 0 and data.get("free") is not None:
        used_value = total - clamp_int(data.get("free"))
      used = clamp_int(used_value)
  known = total > 0
  if cache is not None:
    cache["about"] = {"remote": remote, "used": used, "total": total, "known": known, "warning": warning, "ts": int(time.time())}
  return used, total, known, warning


class WalkBudget:
  """How much of a local tree a status refresh may walk. Someone with edit
  rights on a synced shared folder decides how many files it holds; the
  panel's timer must not."""

  def __init__(self, max_entries: int | None = None, max_seconds: float | None = None):
    self.remaining = MAX_WALK_ENTRIES if max_entries is None else max_entries
    self.deadline = time.monotonic() + (MAX_WALK_SECONDS if max_seconds is None else max_seconds)
    self.exhausted = False

  def spend(self) -> bool:
    if self.exhausted:
      return False
    self.remaining -= 1
    if self.remaining < 0 or time.monotonic() > self.deadline:
      self.exhausted = True
      return False
    return True


def directory_bytes(path: Path, budget: WalkBudget | None = None) -> tuple[int, bool]:
  """(bytes, approximate): the bytes under `path`, and whether the walk was
  cut short by the budget."""
  budget = budget or WalkBudget()
  total = 0
  stack = [path]
  while stack and not budget.exhausted:
    current = stack.pop()
    try:
      scan = os.scandir(current)
    except OSError:
      continue
    with scan:
      for entry in scan:
        if not budget.spend():
          break
        try:
          if entry.is_dir(follow_symlinks=False):
            stack.append(Path(entry.path))
          elif entry.is_file(follow_symlinks=False):
            total += entry.stat(follow_symlinks=False).st_size
        except OSError:
          continue
  return total, budget.exhausted


def local_top_level(folder: Path) -> tuple[dict[str, int], bool]:
  """Bytes on disk per top-level entry of the synced folder, under one
  budget for the whole tree; (sizes, approximate)."""
  sizes: dict[str, int] = {}
  budget = WalkBudget()
  try:
    entries = list(os.scandir(folder))
  except OSError:
    return sizes, False
  for entry in entries:
    if entry.name in SKIP_LOCAL or not valid_drive_name(entry.name):
      continue
    try:
      if entry.is_dir(follow_symlinks=False):
        sizes[entry.name], _ = directory_bytes(Path(entry.path), budget)
    except OSError:
      continue
  return sizes, budget.exhausted


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
  # A real directory of ours, not a link to somewhere else, and empty.
  mount_fd = open_owned_dir(mount_path)
  try:
    with os.scandir(mount_fd) as scan:
      if any(True for _ in scan):
        raise RuntimeError(f"Browse folder is not empty: {mount_path}")
  finally:
    os.close(mount_fd)

  rotate_log(MOUNT_LOG_PATH)   # no mount daemon is alive at this point
  command = [
    rclone, "mount", f"{remote}:", str(mount_path),
    "--daemon",
    "--read-only",
    "--vfs-cache-mode", "full",
    "--vfs-cache-max-age", "6h",
    "--vfs-cache-max-size", "2G",
    "--dir-cache-time", "5m",
    "--poll-interval", "1m",
    "--log-file", str(MOUNT_LOG_PATH),
    "--log-level", "NOTICE",
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
  dfd = open_owned_dir(WORKDIR, fix_mode=0o700)
  try:
    with os.scandir(dfd) as scan:
      locks = [entry.name for entry in scan if entry.name.endswith(".lck") and entry.is_file(follow_symlinks=False)]
    if not locks or bisync_running():
      return False
    for name in locks:
      _unlink_quiet(name, dfd)
  finally:
    os.close(dfd)
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
  ensure_state_dir()

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

  # The sync root must be a real directory of ours; bisync would happily
  # follow a link planted there to wherever it points.
  os.close(open_owned_dir(folder))
  if clear_stale_bisync_lock():
    print("cleared a bisync lock left by an interrupted run", file=sys.stderr)
  rotate_log(LOG_PATH)   # under the flock: nothing else writes the log now
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
  if not ok and not resync and needs_resync(code):
    # Steady-state run rejected; retry once with a baseline instead of
    # leaving the folder stuck until someone notices.
    code, out, err = run(bisync_command(rclone, remote, folder, True), timeout=7200)
    finished = time.time()
    ok = code == 0
    resync = True
    message = clean_text(err or out or "")

  detail = message
  if not ok and not detail:
    detail = clean_text(ANSI.sub("", tail_bytes(LOG_PATH, 4000)))
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

  local, approx = local_top_level(folder)
  root_count, root_bytes, _ = remote_root_files(rclone, remote)
  chosen = set(selection["folders"])
  rows = [
    {
      "name": name,
      "selected": name in chosen,
      "localBytes": local.get(name, 0),
      "onDisk": name in local,
      "approx": approx,
    }
    for name in names
  ]
  # A folder that was deselected but still occupies disk is worth surfacing.
  stale = [
    {"name": name, "selected": False, "localBytes": size, "onDisk": True, "stale": True, "approx": approx}
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
    "localBytesApprox": approx,
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
    "localBytesApprox": False,
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
  cache = load_cache()
  cached = cache.get("localBytes") if isinstance(cache.get("localBytes"), dict) else None
  if payload["syncing"] and cached is not None:
    # The tree is churning and the panel polls every few seconds: the last
    # figure will do until the run ends.
    payload["localBytes"] = clamp_int(cached.get("bytes"))
    payload["localBytesApprox"] = True
  else:
    size, approx = directory_bytes(folder) if folder.is_dir() else (0, False)
    payload["localBytes"] = size
    payload["localBytesApprox"] = approx
    cache["localBytes"] = {"bytes": size, "approx": approx, "ts": int(time.time())}

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

  used, total, quota_known, warning = storage_usage(rclone, remote, cache)
  payload.update(usedBytes=used, quotaBytes=total, quotaKnown=quota_known,
                 usagePercent=(used / total * 100) if total > 0 else 0, warning=warning)
  try:
    save_cache(cache)
  except (OSError, RuntimeError):
    pass

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
  for name in folders:
    if not valid_drive_name(name):
      raise ValueError(f"Folder name not allowed: {clean_text(name, 60)!r}")
  folders.difference_update(args.remove or [])
  if len(folders) > MAX_LIST_ENTRIES:
    raise ValueError(f"At most {MAX_LIST_ENTRIES} folders can be selected")
  selection["folders"] = sorted(folders, key=str.casefold)
  if args.root_files is not None:
    selection["rootFiles"] = args.root_files
  save_selection(selection)
  sync_filters_file(selection)
  print(json.dumps({"ok": True, "folders": selection["folders"], "rootFiles": selection["rootFiles"]}))


def cmd_cleanup(args: argparse.Namespace) -> int:
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
  local, _ = local_top_level(folder)
  targets = [name for name in sorted(local, key=str.casefold) if name not in chosen]
  if args.only:
    targets = [name for name in targets if name in set(args.only)]
  if not targets:
    print(json.dumps({"ok": True, "removed": [], "freedBytes": 0}))
    return 0

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
          max_stderr=CHECK_STDERR_BYTES,   # one line per differing file, and the remote decides how many
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
  if refused:
    # The panel only reads stderr of a failed control command: say why.
    print(f"Kept {len(refused)} folder(s): {refused[0]['reason']}", file=sys.stderr)
    return 1
  return 0


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
  # The widget's watchdog stops a stuck helper with SIGTERM; whatever rclone
  # it was waiting on goes with it rather than running on unattended.
  for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
    signal.signal(signum, _on_signal)
  try:
    ensure_state_dir()
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
      return cmd_cleanup(args)
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
