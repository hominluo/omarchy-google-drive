"""Filesystem-safety tests for gdrive-sync.py.

Every write the backend makes must survive a hostile pathname: a symlink
planted where a temp or a target would go must never redirect the write, and
cleanup must delete exactly the directory it verified, not whatever the name
points at afterwards.
"""

import contextlib
import importlib.util
import io
import os
import stat
import sys
import tempfile
import textwrap
import unittest
from argparse import Namespace
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent


def load_module():
  spec = importlib.util.spec_from_file_location("gdrive_sync", ROOT / "gdrive-sync.py")
  module = importlib.util.module_from_spec(spec)
  spec.loader.exec_module(module)
  return module


mod = load_module()


class TempDirCase(unittest.TestCase):
  def setUp(self):
    self.tmp = Path(tempfile.mkdtemp(prefix="gdrive-test-"))
    self.addCleanup(self._cleanup)

  def _cleanup(self):
    import shutil
    shutil.rmtree(self.tmp, ignore_errors=True)


class WriteAtomicTests(TempDirCase):
  def test_writes_content_privately_and_leaves_no_temp(self):
    target = self.tmp / "state" / "state.json"
    mod.write_atomic(target, '{"a": 1}\n')
    self.assertEqual(target.read_text(), '{"a": 1}\n')
    self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
    self.assertEqual([p.name for p in target.parent.iterdir()], ["state.json"])
    self.assertEqual(stat.S_IMODE(target.parent.stat().st_mode), 0o700)

  def test_mode_is_honoured(self):
    target = self.tmp / "unit.service"
    mod.write_atomic(target, "[Unit]\n", mode=0o644)
    self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o644)

  def test_symlinked_destination_is_replaced_not_followed(self):
    victim = self.tmp / "victim"
    victim.write_text("keep me")
    target = self.tmp / "state.json"
    target.symlink_to(victim)
    mod.write_atomic(target, "new")
    self.assertFalse(target.is_symlink())
    self.assertEqual(target.read_text(), "new")
    self.assertEqual(victim.read_text(), "keep me")

  def test_symlinked_parent_is_refused(self):
    real = self.tmp / "real"
    real.mkdir()
    link = self.tmp / "link"
    link.symlink_to(real)
    with self.assertRaises(RuntimeError):
      mod.write_atomic(link / "state.json", "x")
    self.assertEqual(list(real.iterdir()), [])

  def test_temp_name_collision_is_retried_and_left_alone(self):
    target = self.tmp / "state.json"
    planted = self.tmp / ".state.json.aaaa.tmp"
    planted.write_text("someone else's")
    with mock.patch.object(mod.secrets, "token_hex", side_effect=["aaaa", "bbbb"]):
      mod.write_atomic(target, "ours")
    self.assertEqual(target.read_text(), "ours")
    self.assertEqual(planted.read_text(), "someone else's")
    self.assertEqual(sorted(p.name for p in self.tmp.iterdir()), [".state.json.aaaa.tmp", "state.json"])

  def test_write_json_round_trips(self):
    target = self.tmp / "selection.json"
    mod.write_json(target, {"folders": ["b", "a"], "rootFiles": True})
    self.assertEqual(mod.read_json(target, None), {"folders": ["b", "a"], "rootFiles": True})
    self.assertTrue(target.read_text().endswith("\n"))


class LockFileTests(TempDirCase):
  def test_lock_refuses_symlink(self):
    victim = self.tmp / "victim"
    victim.write_text("keep")
    lock = self.tmp / "sync.lock"
    lock.symlink_to(victim)
    with self.assertRaises(RuntimeError):
      mod.open_lock_file(lock)
    self.assertEqual(victim.read_text(), "keep")

  def test_lock_is_created_privately(self):
    lock = self.tmp / "sync.lock"
    with mod.open_lock_file(lock):
      self.assertEqual(stat.S_IMODE(lock.stat().st_mode), 0o600)


class RemoveTreeTests(TempDirCase):
  def test_removes_nested_tree_but_never_follows_symlinks(self):
    outside = self.tmp / "outside"
    outside.mkdir()
    (outside / "precious").write_text("do not delete")
    victim_file = self.tmp / "victim-file"
    victim_file.write_text("do not delete either")

    tree = self.tmp / "tree"
    (tree / "a" / "b").mkdir(parents=True)
    (tree / "a" / "b" / "file").write_text("x")
    (tree / "top").write_text("y")
    (tree / "a" / "dirlink").symlink_to(outside)
    (tree / "filelink").symlink_to(victim_file)

    fd = os.open(tree, os.O_RDONLY | os.O_DIRECTORY)
    try:
      mod.remove_tree_contents(fd, os.fstat(fd).st_dev)
    finally:
      os.close(fd)

    self.assertEqual(list(tree.iterdir()), [])
    self.assertEqual((outside / "precious").read_text(), "do not delete")
    self.assertEqual(victim_file.read_text(), "do not delete either")


FAKE_RCLONE = textwrap.dedent("""\
  #!/bin/sh
  # Records how it was called, resolves the source path from inside the child
  # (where /proc/self/fd/N is meaningful), then optionally swaps the target
  # directory out from under its name before returning.
  printf '%s\\n' "$@" > "$FAKE_LOG"
  readlink -f "$2" >> "$FAKE_LOG"
  if [ -n "$FAKE_SWAP" ]; then
    mv "$FAKE_SWAP" "$FAKE_SWAP.moved"
    mkdir "$FAKE_SWAP"
    echo planted > "$FAKE_SWAP/planted"
  fi
  if [ -n "$FAKE_FAIL" ]; then
    echo "ERROR : Local file system at $2: 1 differences found" >&2
    exit 1
  fi
  exit 0
  """)


class CleanupTests(TempDirCase):
  def setUp(self):
    super().setUp()
    self.home = self.tmp / "home"
    self.folder = self.home / "Google Drive"
    (self.folder / "Keep" / "sub").mkdir(parents=True)
    (self.folder / "Keep" / "sub" / "f").write_text("k")
    (self.folder / "Drop" / "sub").mkdir(parents=True)
    (self.folder / "Drop" / "sub" / "f").write_text("d" * 10)
    (self.folder / "Drop" / "g").write_text("d" * 5)
    self.rclone = self.tmp / "rclone"
    self.rclone.write_text(FAKE_RCLONE)
    self.rclone.chmod(0o755)
    self.log = self.tmp / "rclone.log"
    state = self.tmp / "state"
    self.patches = [
      mock.patch.object(mod.Path, "home", return_value=self.home),
      mock.patch.object(mod, "rclone_bin", return_value=str(self.rclone)),
      mock.patch.object(mod, "SELECTION_PATH", state / "selection.json"),
      mock.patch.object(mod, "LOCK_PATH", state / "sync.lock"),
      mock.patch.dict(os.environ, {"FAKE_LOG": str(self.log), "FAKE_SWAP": "", "FAKE_FAIL": ""}),
    ]
    for patch in self.patches:
      patch.start()
      self.addCleanup(patch.stop)
    mod.save_selection({"folders": ["Keep"], "rootFiles": True})

  def cleanup(self, only=()):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
      mod.cmd_cleanup(Namespace(remote="gdrive", folder=str(self.folder), only=list(only)))
    return mod.json.loads(out.getvalue())

  def test_check_runs_through_the_descriptor_and_deletes_only_deselected(self):
    result = self.cleanup()
    self.assertEqual(result["removed"], ["Drop"])
    self.assertEqual(result["freedBytes"], 15)
    self.assertTrue(result["ok"])
    self.assertFalse((self.folder / "Drop").exists())
    self.assertTrue((self.folder / "Keep" / "sub" / "f").exists())
    lines = self.log.read_text().splitlines()
    self.assertEqual(lines[0], "check")
    self.assertRegex(lines[1], r"^/proc/self/fd/\d+$")
    self.assertEqual(lines[2], "gdrive:Drop")
    self.assertEqual(lines[-1], str((self.folder / "Drop").resolve()))

  def test_failed_check_removes_nothing_and_names_the_real_path(self):
    os.environ["FAKE_FAIL"] = "1"
    result = self.cleanup()
    self.assertEqual(result["removed"], [])
    self.assertFalse(result["ok"])
    self.assertEqual(result["refused"][0]["name"], "Drop")
    self.assertIn(str(self.folder / "Drop"), result["refused"][0]["reason"])
    self.assertNotIn("/proc/self/fd", result["refused"][0]["reason"])
    self.assertTrue((self.folder / "Drop" / "g").exists())

  def test_directory_swapped_during_check_is_not_deleted(self):
    os.environ["FAKE_SWAP"] = str(self.folder / "Drop")
    result = self.cleanup()
    self.assertEqual(result["removed"], [])
    # Refused, and says so: the directory it verified was emptied first.
    self.assertEqual(len(result["refused"]), 1)
    self.assertEqual(result["refused"][0]["name"], "Drop")
    self.assertEqual(result["refused"][0]["reason"], "folder changed during cleanup")
    self.assertTrue(result["refused"][0].get("partial"))
    # The impostor under the name survives untouched…
    self.assertEqual((self.folder / "Drop" / "planted").read_text(), "planted\n")
    # …and only the directory rclone actually verified was emptied.
    moved = self.folder / "Drop.moved"
    self.assertTrue(moved.is_dir())
    self.assertEqual(list(moved.iterdir()), [])

  def test_symlink_in_place_of_a_folder_is_skipped(self):
    outside = self.tmp / "outside"
    outside.mkdir()
    (outside / "precious").write_text("p")
    (self.folder / "Linked").symlink_to(outside)
    result = self.cleanup(only=["Linked", "Drop"])
    self.assertEqual(result["removed"], ["Drop"])
    self.assertTrue((self.folder / "Linked").is_symlink())
    self.assertEqual((outside / "precious").read_text(), "p")


if __name__ == "__main__":
  unittest.main()


# ---------------------------------------------------------------- round 2: bounded remote input

def write_script(path, body):
  path.write_text(textwrap.dedent(body))
  path.chmod(0o755)
  return str(path)


class RunnerTests(TempDirCase):
  def test_output_over_cap_is_refused_whole(self):
    flood = write_script(self.tmp / "flood", """\
      #!/bin/sh
      head -c 3000000 /dev/zero | tr '\\0' a
      """)
    import time
    started = time.monotonic()
    code, out, err = mod.run([flood], max_stdout=1024)
    self.assertEqual(code, mod.EXIT_TOO_LARGE)
    self.assertEqual(out, "")
    self.assertIn("too much output", err)
    self.assertLess(time.monotonic() - started, 5)

  def test_deadline_returns_nothing(self):
    slow = write_script(self.tmp / "slow", """\
      #!/bin/sh
      printf partial
      sleep 5
      """)
    code, out, err = mod.run([slow], timeout=0.3)
    self.assertEqual(code, mod.EXIT_TIMEOUT)
    self.assertEqual(out, "")
    self.assertIn("timed out", err)

  def test_process_group_is_reaped(self):
    orphan = write_script(self.tmp / "orphan", """\
      #!/bin/sh
      sleep 300 </dev/null >/dev/null 2>&1 &
      echo $!
      """)
    import time
    code, out, _ = mod.run([orphan])
    self.assertEqual(code, 0)
    pid = int(out)
    for _ in range(40):
      try:
        os.kill(pid, 0)
      except ProcessLookupError:
        break
      try:
        with open(f"/proc/{pid}/stat") as handle:
          if handle.read().split(")")[-1].split()[0] == "Z":
            break
      except OSError:
        break
      time.sleep(0.05)
    else:
      self.fail("background child of the runner outlived it")
    self.assertEqual(mod._LIVE, set())

  def test_missing_binary(self):
    code, _, _ = mod.run([str(self.tmp / "nonexistent")])
    self.assertEqual(code, 127)


class ListingStreamTests(unittest.TestCase):
  def parse(self, *chunks, **kwargs):
    return list(mod.iter_json_array(list(chunks), **kwargs))

  def test_rows_split_across_chunks(self):
    text = '[\n{"Name": "é"},\n{"Name": "b", "Size": 3}\n]\n'.encode("utf-8")
    rows = self.parse(text[:5], text[5:12], text[12:])
    self.assertEqual([row["Name"] for row in rows], ["é", "b"])

  def test_not_an_array_or_truncated(self):
    with self.assertRaises(mod.ListingUnreadable):
      self.parse(b"[1]")
    with self.assertRaises(mod.ListingUnreadable):
      self.parse(b'[{"Name":"x"}')
    with self.assertRaises(mod.ListingUnreadable):
      self.parse(b'{"Name":"x"}')
    with self.assertRaises(mod.ListingUnreadable):
      self.parse(b'[]{"more": 1}')

  def test_entry_and_item_caps(self):
    many = ("[" + ",".join('{"Name":"f%d"}' % i for i in range(6)) + "]").encode()
    with self.assertRaises(mod.ListingTooLarge):
      self.parse(many, max_entries=5)
    self.assertEqual(len(self.parse(many, max_entries=6)), 6)
    huge = ('[{"Name":"' + "x" * 200 + '"}]').encode()
    with self.assertRaises(mod.ListingTooLarge):
      self.parse(huge, max_item_bytes=100)

  def test_odd_numbers(self):
    rows = self.parse(b'[{"Size": Infinity}, {"Size": NaN}]')
    self.assertEqual([row["Size"] for row in rows], [None, None])
    with self.assertRaises(mod.ListingUnreadable):
      self.parse(('[{"Size": ' + "9" * 5000 + '}]').encode())


class RemoteFoldersTests(TempDirCase):
  def fake(self, body, exit_code=0):
    self.json = self.tmp / "listing.json"
    self.json.write_bytes(body)
    script = write_script(self.tmp / "rclone", """\
      #!/bin/sh
      cat "$FAKE_JSON"
      exit ${FAKE_EXIT:-0}
      """)
    os.environ["FAKE_JSON"] = str(self.json)
    os.environ["FAKE_EXIT"] = str(exit_code)
    self.addCleanup(os.environ.pop, "FAKE_JSON", None)
    self.addCleanup(os.environ.pop, "FAKE_EXIT", None)
    return script

  def test_valid_names_only_sorted(self):
    rclone = self.fake(b'[{"Name":"b"},{"Name":"A"},{"Name":"bad\\nname"},{"Name":"a/b"},{"Name":" lead"},{"Size":7},{"Name":""}]')
    with contextlib.redirect_stderr(io.StringIO()):
      names, error = mod.remote_folders(rclone, "gdrive")
    self.assertEqual((names, error), ([" lead", "A", "b"], ""))

  def test_too_many_folders_is_refused_whole(self):
    rclone = self.fake(("[" + ",".join('{"Name":"f%d"}' % i for i in range(mod.MAX_LIST_ENTRIES + 1)) + "]").encode())
    names, error = mod.remote_folders(rclone, "gdrive")
    self.assertEqual(names, [])
    self.assertIn("more than", error)

  def test_rclone_failure_reports_stderr(self):
    rclone = write_script(self.tmp / "rclone", """\
      #!/bin/sh
      echo "Failed to create file system: token expired" >&2
      exit 3
      """)
    names, error = mod.remote_folders(rclone, "gdrive")
    self.assertEqual(names, [])
    self.assertIn("token expired", error)

  def test_root_files_use_rclone_size(self):
    log = self.tmp / "argv.log"
    rclone = write_script(self.tmp / "rclone", """\
      #!/bin/sh
      printf '%s\\n' "$@" > "$FAKE_LOG"
      echo '{"count":3,"bytes":12,"sizeless":1}'
      """)
    with mock.patch.dict(os.environ, {"FAKE_LOG": str(log)}):
      self.assertEqual(mod.remote_root_files(rclone, "gdrive"), (3, 12, ""))
    argv = log.read_text().splitlines()
    self.assertEqual(argv[:3], ["size", "gdrive:", "--json"])
    self.assertEqual(argv[3:], ["--filter", "+ /*", "--filter", "- **"])


class NameValidatorTests(unittest.TestCase):
  def test_table(self):
    for good in ("Docs", "a b.c", " lead", "trail ", "x" * mod.MAX_NAME_BYTES, "naïve", "日本語"):
      self.assertTrue(mod.valid_drive_name(good), good)
    for bad in ("", "   ", ".", "..", "a/b", "a\nb", "a\rb", "a\x00b", "a\x7fb", "x" * (mod.MAX_NAME_BYTES + 1),
                "é" * 200, 123, None, ["a"]):
      self.assertFalse(mod.valid_drive_name(bad), repr(bad))


class FilterInjectionTests(TempDirCase):
  def test_selection_file_names_are_validated(self):
    with mock.patch.object(mod, "SELECTION_PATH", self.tmp / "selection.json"):
      mod.write_json(mod.SELECTION_PATH, {"folders": ["x\n- /Docs/**", "ok", "-dash"]})
      with contextlib.redirect_stderr(io.StringIO()) as err:
        self.assertEqual(mod.load_selection()["folders"], ["-dash", "ok"])
      self.assertIn("ignoring 1 invalid", err.getvalue())

  def test_filters_never_carry_an_injected_rule(self):
    text = mod.build_filters({"folders": ["x\n!", "ok"], "rootFiles": False})
    rules = [line for line in text.splitlines() if not line.startswith("#")]
    self.assertEqual(rules, ["+ /ok/**", "- **"])

  def test_select_refuses_bad_names_and_accepts_dash_form(self):
    with mock.patch.object(mod, "SELECTION_PATH", self.tmp / "selection.json"), \
         mock.patch.object(mod, "FILTERS_PATH", self.tmp / "filters.txt"):
      with self.assertRaises(ValueError):
        mod.cmd_select(Namespace(add=["x\n!"], remove=[], set=None, root_files=None))
      args = mod.parser().parse_args(["select", "--add=-lead", "--remove=a=b"])
      self.assertEqual((args.add, args.remove), (["-lead"], ["a=b"]))
      with contextlib.redirect_stdout(io.StringIO()):
        mod.cmd_select(args)
      self.assertEqual(mod.load_selection()["folders"], ["-lead"])
      self.assertIn("+ /-lead/**", (self.tmp / "filters.txt").read_text())


class SystemdQuoteTests(unittest.TestCase):
  def test_specifiers_and_expansions_are_literal(self):
    self.assertEqual(mod.systemd_quote('a%b$c"d\\e'), '"a%%b$$c\\"d\\\\e"')
    with self.assertRaises(ValueError):
      mod.systemd_quote("a\nExecStartPre=/bin/true")
    with self.assertRaises(ValueError):
      mod.normalize_path("~/x\ny", "~/Google Drive")


class LogTests(TempDirCase):
  def test_tail_bytes(self):
    path = self.tmp / "sync.log"
    path.write_bytes(b"a" * 10000 + b"END")
    self.assertEqual(mod.tail_bytes(path, 100), "a" * 97 + "END")
    self.assertEqual(mod.tail_bytes(self.tmp / "missing", 100), "")
    link = self.tmp / "link.log"
    link.symlink_to(path)
    self.assertEqual(mod.tail_bytes(link, 100), "")

  def test_rotate(self):
    path = self.tmp / "sync.log"
    path.write_bytes(b"x" * 101)
    (self.tmp / "sync.log.1").write_bytes(b"older")
    mod.rotate_log(path, limit=100)
    self.assertFalse(path.exists())
    self.assertEqual((self.tmp / "sync.log.1").read_bytes(), b"x" * 101)
    path.write_bytes(b"small")
    mod.rotate_log(path, limit=100)
    self.assertEqual(path.read_bytes(), b"small")
    victim = self.tmp / "victim"
    victim.write_text("keep")
    (self.tmp / "mount.log").symlink_to(victim)
    mod.rotate_log(self.tmp / "mount.log", limit=1)
    self.assertFalse((self.tmp / "mount.log").exists())
    self.assertEqual(victim.read_text(), "keep")

  def test_needs_resync_is_gated_on_the_exit_code_and_rclones_own_line(self):
    path = self.tmp / "sync.log"
    with mock.patch.object(mod, "LOG_PATH", path):
      path.write_text("2026/09/18 00:00:00 ERROR : Bisync aborted. Must run --resync to recover.\n")
      self.assertTrue(mod.needs_resync(7))
      self.assertTrue(mod.needs_resync(2))
      self.assertFalse(mod.needs_resync(0))
      self.assertFalse(mod.needs_resync(1))
      path.write_text("2026/09/18 00:00:00 INFO  : Bisync aborted. Must run --resync to recover.txt: Copied (new)\n")
      self.assertFalse(mod.needs_resync(7))
      path.write_text("\x1b[31m2026/09/18 00:00:00 ERROR : Bisync interrupted. Must run --resync to recover.\x1b[0m\n")
      self.assertTrue(mod.needs_resync(7))
      # With --resilient the critical error is still rclone's own line; the
      # "aborted" line after it changes wording (verified with rclone 1.75).
      path.write_text("2026/09/18 00:00:00 ERROR : Bisync critical error: filters file has changed (must run --resync): /x/filters.txt\n"
                      "2026/09/18 00:00:00 ERROR : Bisync aborted. Error is retryable without --resync due to --resilient mode.\n")
      self.assertTrue(mod.needs_resync(7))
      path.write_text("2026/09/18 00:00:00 INFO  : evil (must run --resync): Copied (new)\n"
                      "2026/09/18 00:00:00 ERROR : Bisync aborted. Error is retryable without --resync due to --resilient mode.\n")
      self.assertFalse(mod.needs_resync(7))


class WalkBudgetTests(TempDirCase):
  def test_budget_cuts_the_walk_short(self):
    root = self.tmp / "tree"
    for i in range(30):
      sub = root / f"d{i}"
      sub.mkdir(parents=True)
      for j in range(10):
        (sub / f"f{j}").write_bytes(b"x" * 10)
    total, approx = mod.directory_bytes(root, mod.WalkBudget(max_entries=50, max_seconds=10))
    self.assertTrue(approx)
    self.assertGreater(total, 0)
    total, approx = mod.directory_bytes(root, mod.WalkBudget(max_entries=100000, max_seconds=10))
    self.assertEqual((total, approx), (3000, False))
    (root / "bad\nname").mkdir()
    sizes, _files, _approx = mod.local_top_level(root)
    self.assertNotIn("bad\nname", sizes)
    self.assertEqual(len(sizes), 30)
    # One folder exhausting its budget never zeroes the ones after it.
    with mock.patch.object(mod, "MAX_WALK_ENTRIES", 5):
      sizes, _files, approx = mod.local_top_level(root)
    self.assertTrue(approx)
    self.assertEqual(len(sizes), 30)
    self.assertTrue(all(size > 0 for size in sizes.values()), sizes)

  def test_signal_handler_terms_before_it_kills(self):
    import signal
    import subprocess
    import time
    script = write_script(self.tmp / "trap", """\
      #!/bin/sh
      trap 'echo bye > "$FAKE_OUT"; exit 0' TERM
      while :; do sleep 0.1; done
      """)
    out = self.tmp / "out"
    proc = subprocess.Popen([script], start_new_session=True, env={**os.environ, "FAKE_OUT": str(out)})
    mod._LIVE.add(proc)
    time.sleep(0.3)                      # let the script install its trap first
    try:
      started = time.monotonic()
      mod.kill_live_children(grace=5.0)
      self.assertLess(time.monotonic() - started, 4)
      self.assertEqual(out.read_text().strip(), "bye")      # it got TERM and exited on its own
    finally:
      mod._LIVE.discard(proc)
      proc.wait(timeout=5)


class StateDirTests(TempDirCase):
  def test_modes_are_tightened_each_run(self):
    state = self.tmp / "state"
    work = state / "workdir"
    work.mkdir(parents=True)
    os.chmod(state, 0o755)
    os.chmod(work, 0o755)
    (state / "filters.txt").write_text("x")
    os.chmod(state / "filters.txt", 0o644)
    (work / "a.lst").write_text("x")
    os.chmod(work / "a.lst", 0o644)
    with mock.patch.object(mod, "STATE_DIR", state), mock.patch.object(mod, "WORKDIR", work):
      mod.ensure_state_dir()
    for path in (state, work):
      self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700)
    for path in (state / "filters.txt", work / "a.lst"):
      self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
    link = self.tmp / "linked"
    link.symlink_to(state)
    with mock.patch.object(mod, "STATE_DIR", link), mock.patch.object(mod, "WORKDIR", work):
      with self.assertRaises(RuntimeError):
        mod.ensure_state_dir()


class StorageCacheTests(TempDirCase):
  def test_about_is_asked_once_a_minute(self):
    calls = []

    def fake_run(command, *args, **kwargs):
      calls.append(command)
      return 0, '{"total": 100, "used": 40}', ""
    cache = {}
    with mock.patch.object(mod, "run", fake_run):
      first = mod.storage_usage("rclone", "gdrive", cache)
      second = mod.storage_usage("rclone", "gdrive", cache)
    self.assertEqual(first, (40, 100, True, ""))
    self.assertEqual(second, first)
    self.assertEqual(len(calls), 1)
    with mock.patch.object(mod, "run", lambda *a, **k: (0, '{"total": Infinity, "used": [1]}', "")):
      self.assertEqual(mod.storage_usage("rclone", "other", {}), (0, 0, False, ""))


# ---------------------------------------------------------------- round 3: audit fixes

import fcntl
import re


class NormalizePathTests(TempDirCase):
  def setUp(self):
    super().setUp()
    self.home = self.tmp / "home"
    self.home.mkdir()

  def test_inside_home_is_accepted_and_outside_is_refused(self):
    with mock.patch.dict(os.environ, {"HOME": str(self.home)}):
      self.assertEqual(mod.normalize_path("~/Google Drive", "x"), self.home / "Google Drive")
      self.assertEqual(mod.normalize_path(str(self.home / "nested" / ".." / "y"), "x"), self.home / "y")
      for bad in ("/", "~", str(self.home), "/etc", str(self.tmp / "outside")):
        with self.assertRaises(ValueError):
          mod.normalize_path(bad, "x")

  def test_a_home_reached_through_a_symlink_still_accepts_its_paths(self):
    # $HOME is /home/u while /home is a link to /var/home: the path as written
    # and the resolved one can never be prefixes of each other.
    real = self.tmp / "var" / "home" / "u"
    real.mkdir(parents=True)
    (self.tmp / "home-link").mkdir()
    link = self.tmp / "home-link" / "u"
    link.symlink_to(real)
    with mock.patch.dict(os.environ, {"HOME": str(link)}):
      self.assertEqual(mod.normalize_path("~/Google Drive", "x"), link / "Google Drive")
      self.assertEqual(mod.normalize_path(str(link / "Google Drive"), "x"), link / "Google Drive")
      # The resolved form is still inside home, so it is accepted too.
      self.assertEqual(mod.normalize_path(str(real / "Google Drive"), "x"), real / "Google Drive")
      with self.assertRaises(ValueError):
        mod.normalize_path(str(self.tmp / "elsewhere"), "x")


class PayloadCase(TempDirCase):
  def setUp(self):
    super().setUp()
    self.home = self.tmp / "home"
    self.folder = self.home / "Google Drive"
    self.folder.mkdir(parents=True)
    self.state = self.tmp / "state"
    self.state.mkdir()
    self.units = self.tmp / "units"
    for patch in (
      mock.patch.object(mod.Path, "home", return_value=self.home),
      mock.patch.object(mod, "SELECTION_PATH", self.state / "selection.json"),
      mock.patch.object(mod, "STATE_PATH", self.state / "state.json"),
      mock.patch.object(mod, "CACHE_PATH", self.state / "cache.json"),
      mock.patch.object(mod, "LOCK_PATH", self.state / "sync.lock"),
      mock.patch.object(mod, "UNIT_DIR", self.units),
      mock.patch.object(mod, "rclone_bin", return_value="/bin/true"),
      mock.patch.object(mod, "remote_root_files", return_value=(0, 0, "")),
    ):
      patch.start()
      self.addCleanup(patch.stop)
    self.saved_filters = self.state / "filters.txt"
    filters = mock.patch.object(mod, "FILTERS_PATH", self.saved_filters)
    filters.start()
    self.addCleanup(filters.stop)


class FoldersPayloadTests(PayloadCase):
  """A folder that is still selected but no longer in the Drive listing."""

  def setUp(self):
    super().setUp()
    (self.folder / "Gone").mkdir()
    (self.folder / "Gone" / "f").write_text("x" * 10)
    (self.folder / "Old").mkdir()
    (self.folder / "Old" / "f").write_text("x" * 5)
    mod.save_selection({"folders": ["Gone"], "rootFiles": True})

  def payload(self, names):
    with mock.patch.object(mod, "remote_folders", return_value=(names, "")):
      return mod.folders_payload("gdrive", str(self.folder))

  def test_a_selected_folder_missing_from_drive_stays_selected_and_is_not_stale(self):
    payload = self.payload(["Other"])
    by_name = {row["name"]: row for row in payload["folders"]}
    self.assertTrue(by_name["Gone"]["selected"])
    self.assertTrue(by_name["Gone"]["missing"])
    self.assertFalse(by_name["Gone"]["stale"])
    self.assertFalse(by_name["Old"]["selected"])
    self.assertTrue(by_name["Old"]["stale"])
    # Only the deselected folder is "still on disk because you stopped syncing".
    self.assertEqual(payload["staleBytes"], 5)
    self.assertEqual(payload["staleCount"], 1)

  def test_a_selected_folder_that_is_listed_is_a_normal_row(self):
    payload = self.payload(["Gone"])
    row = {row["name"]: row for row in payload["folders"]}["Gone"]
    self.assertTrue(row["selected"])
    self.assertTrue(row["onDisk"])
    self.assertNotIn("missing", row)
    self.assertEqual(payload["staleCount"], 1)   # Old, deselected

  def test_loose_files_only_count_when_root_files_are_not_synced(self):
    (self.folder / "loose.txt").write_text("x" * 7)
    payload = self.payload(["Other"])
    self.assertEqual((payload["rootLocalBytes"], payload["rootLocalCount"]), (0, 0))
    mod.save_selection({"folders": ["Gone"], "rootFiles": False})
    payload = self.payload(["Other"])
    self.assertEqual((payload["rootLocalBytes"], payload["rootLocalCount"]), (7, 1))
    self.assertEqual(payload["staleBytes"], 5 + 7)
    self.assertEqual(payload["staleCount"], 2)


class UnitDriftTests(PayloadCase):
  def test_units_follow_the_configured_folder_and_are_written_only_on_change(self):
    with mock.patch.object(mod, "run") as runner:
      mod.ensure_units("gdrive", self.home / "One", self.home / "Browse")
      self.assertEqual(runner.call_count, 1)          # one daemon-reload after the batch
      service = (self.units / mod.SERVICE).read_text()
      self.assertIn(str(self.home / "One"), service)
      mod.ensure_units("gdrive", self.home / "One", self.home / "Browse")
      self.assertEqual(runner.call_count, 1)          # unchanged: no reload
      mod.ensure_units("gdrive", self.home / "Two", self.home / "Browse")
      self.assertEqual(runner.call_count, 2)
      self.assertIn(str(self.home / "Two"), (self.units / mod.SERVICE).read_text())

  def test_the_status_poll_carries_a_changed_folder_into_the_units(self):
    # Units were installed with the old path; the poll has to notice.
    with mock.patch.object(mod, "run", return_value=(0, "", "")):
      mod.ensure_units("gdrive", self.home / "Old", self.home / "Browse")
    with mock.patch.object(mod, "configured_remotes", return_value=({"gdrive"}, "")), \
         mock.patch.object(mod, "mount_info", return_value=(False, False, "", True)), \
         mock.patch.object(mod, "unit_enabled", return_value=False), \
         mock.patch.object(mod, "timer_enabled", return_value=False), \
         mock.patch.object(mod, "service_active", return_value=False), \
         mock.patch.object(mod, "storage_usage", return_value=(0, 0, False, "")), \
         mock.patch.object(mod, "run", return_value=(0, "", "")):
      mod.status_payload("gdrive", str(self.home / "New"), str(self.home / "Browse"))
    # Without a button press: the timer must not keep syncing the old path.
    self.assertIn(str(self.home / "New"), (self.units / mod.SERVICE).read_text())

  def test_a_status_poll_does_not_install_units_by_itself(self):
    with mock.patch.object(mod, "configured_remotes", return_value=({"gdrive"}, "")), \
         mock.patch.object(mod, "mount_info", return_value=(False, False, "", True)), \
         mock.patch.object(mod, "unit_enabled", return_value=False), \
         mock.patch.object(mod, "timer_enabled", return_value=False), \
         mock.patch.object(mod, "service_active", return_value=False), \
         mock.patch.object(mod, "storage_usage", return_value=(0, 0, False, "")), \
         mock.patch.object(mod, "run", return_value=(0, "", "")):
      mod.status_payload("gdrive", str(self.folder), str(self.home / "Browse"))
    self.assertFalse(self.units.exists())

  def test_the_interval_drop_in_is_rewritten_only_when_it_changes(self):
    with mock.patch.object(mod, "run") as runner:
      self.assertTrue(mod.write_timer_interval(25))
      self.assertEqual(runner.call_count, 1)
      path = self.units / (mod.TIMER + ".d") / "interval.conf"
      self.assertIn("OnUnitInactiveSec=25min", path.read_text())
      self.assertFalse(mod.write_timer_interval(25))
      self.assertEqual(runner.call_count, 1)          # no write, no daemon-reload
      self.assertTrue(mod.write_timer_interval(40))
      self.assertEqual(runner.call_count, 2)
      self.assertIn("OnUnitInactiveSec=40min", path.read_text())

  def test_the_status_poll_applies_the_interval_when_the_timer_is_on(self):
    with mock.patch.object(mod, "configured_remotes", return_value=({"gdrive"}, "")), \
         mock.patch.object(mod, "mount_info", return_value=(False, False, "", True)), \
         mock.patch.object(mod, "unit_enabled", return_value=False), \
         mock.patch.object(mod, "timer_enabled", return_value=True), \
         mock.patch.object(mod, "service_active", return_value=False), \
         mock.patch.object(mod, "storage_usage", return_value=(0, 0, False, "")), \
         mock.patch.object(mod, "run", return_value=(0, "", "")):
      mod.status_payload("gdrive", str(self.folder), str(self.home / "Browse"), 15)
      path = self.units / (mod.TIMER + ".d") / "interval.conf"
      self.assertIn("OnUnitInactiveSec=15min", path.read_text())


class CleanupLockTests(TempDirCase):
  def setUp(self):
    super().setUp()
    self.home = self.tmp / "home"
    self.folder = self.home / "Google Drive"
    (self.folder / "Drop").mkdir(parents=True)
    (self.folder / "Drop" / "f").write_text("d" * 4)
    self.rclone = self.tmp / "rclone"
    self.rclone.write_text(FAKE_RCLONE)
    self.rclone.chmod(0o755)
    self.state = self.tmp / "state"
    for patch in (
      mock.patch.object(mod.Path, "home", return_value=self.home),
      mock.patch.object(mod, "rclone_bin", return_value=str(self.rclone)),
      mock.patch.object(mod, "SELECTION_PATH", self.state / "selection.json"),
      mock.patch.object(mod, "LOCK_PATH", self.state / "sync.lock"),
      mock.patch.dict(os.environ, {"FAKE_LOG": str(self.tmp / "log"), "FAKE_SWAP": "", "FAKE_FAIL": ""}),
    ):
      patch.start()
      self.addCleanup(patch.stop)
    mod.save_selection({"folders": [], "rootFiles": True})

  def args(self):
    return Namespace(remote="gdrive", folder=str(self.folder), only=[])

  def test_a_running_sync_blocks_cleanup(self):
    with mod.open_lock_file(mod.LOCK_PATH) as held:
      fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
      with self.assertRaises(RuntimeError) as caught:
        mod.cmd_cleanup(self.args())
    self.assertIn("a sync is running", str(caught.exception))
    self.assertTrue((self.folder / "Drop" / "f").exists())

  def test_the_tree_is_emptied_while_the_lock_is_held(self):
    real = mod.remove_tree_contents
    seen = []

    def spy(dfd, dev, tally=None):
      other = mod.open_lock_file(mod.LOCK_PATH)
      try:
        try:
          fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
          seen.append("free")
        except OSError:
          seen.append("held")
      finally:
        other.close()
      return real(dfd, dev, tally)

    with mock.patch.object(mod, "remove_tree_contents", spy), \
         contextlib.redirect_stdout(io.StringIO()):
      self.assertEqual(mod.cmd_cleanup(self.args()), 0)
    self.assertEqual(seen, ["held"])   # nothing can write into it mid-removal

  def test_a_partial_removal_is_reported_as_such(self):
    with mock.patch.object(mod.os, "rmdir", side_effect=OSError(13, "no")), \
         contextlib.redirect_stdout(io.StringIO()) as out, \
         contextlib.redirect_stderr(io.StringIO()):
      self.assertEqual(mod.cmd_cleanup(self.args()), 1)
    refused = mod.json.loads(out.getvalue())["refused"]
    self.assertIn("no", refused[0]["reason"])
    self.assertTrue(refused[0]["partial"])
    self.assertFalse((self.folder / "Drop" / "f").exists())   # gone, as reported
    self.assertTrue((self.folder / "Drop").exists())

  def test_a_failure_inside_nested_directories_still_counts_as_partial(self):
    nested = self.folder / "Drop" / "sub"
    nested.mkdir()
    (nested / "deep").write_text("d")
    real = mod.os.rmdir

    def fail_inside(path, *args, **kwargs):
      if str(path) == "sub":
        raise OSError(13, "no")
      return real(path, *args, **kwargs)

    with mock.patch.object(mod.os, "rmdir", side_effect=fail_inside), \
         contextlib.redirect_stdout(io.StringIO()) as out, \
         contextlib.redirect_stderr(io.StringIO()):
      self.assertEqual(mod.cmd_cleanup(self.args()), 1)
    refused = mod.json.loads(out.getvalue())["refused"]
    self.assertEqual(refused[0]["name"], "Drop")
    self.assertTrue(refused[0]["partial"])   # deep had already been deleted
    self.assertFalse((nested / "deep").exists())


class CleanupFileTests(TempDirCase):
  def setUp(self):
    super().setUp()
    self.home = self.tmp / "home"
    self.folder = self.home / "Google Drive"
    self.folder.mkdir(parents=True)
    self.loose = self.folder / "loose.txt"
    self.loose.write_text("x" * 9)
    (self.folder / "nested").mkdir()
    (self.folder / "nested" / "f").write_text("x")
    self.log = self.tmp / "rclone.log"
    self.rclone = self.tmp / "rclone"
    self.rclone.write_text(FAKE_RCLONE)
    self.rclone.chmod(0o755)
    self.state = self.tmp / "state"
    for patch in (
      mock.patch.object(mod.Path, "home", return_value=self.home),
      mock.patch.object(mod, "rclone_bin", return_value=str(self.rclone)),
      mock.patch.object(mod, "SELECTION_PATH", self.state / "selection.json"),
      mock.patch.object(mod, "LOCK_PATH", self.state / "sync.lock"),
      mock.patch.dict(os.environ, {"FAKE_LOG": str(self.log), "FAKE_SWAP": "", "FAKE_FAIL": ""}),
    ):
      patch.start()
      self.addCleanup(patch.stop)
    mod.save_selection({"folders": ["nested"], "rootFiles": False})

  def cleanup(self):
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
      code = mod.cmd_cleanup(Namespace(remote="gdrive", folder=str(self.folder), only=[]))
    return code, mod.json.loads(out.getvalue())

  def test_a_deselected_loose_file_is_verified_then_removed(self):
    code, result = self.cleanup()
    self.assertEqual(code, 0)
    self.assertEqual(result["removed"], ["loose.txt"])
    self.assertEqual(result["freedBytes"], 9)
    self.assertFalse(self.loose.exists())
    lines = self.log.read_text().splitlines()
    argv, resolved = lines[:-1], lines[-1]   # the fake ends by resolving $2
    self.assertEqual(resolved, str(self.folder))
    self.assertEqual(argv[0], "check")
    self.assertIn("--drive-skip-gdocs", argv)
    self.assertEqual(argv[argv.index("--filter"):], ["--filter", "+ /loose.txt", "--filter", "- **"])
    self.assertEqual(argv[2], "gdrive:")   # the root of the Drive
    self.assertTrue((self.folder / "nested" / "f").exists())

  def test_a_failed_check_keeps_the_file(self):
    os.environ["FAKE_FAIL"] = "1"
    code, result = self.cleanup()
    self.assertEqual(code, 1)
    self.assertEqual(result["removed"], [])
    self.assertTrue(self.loose.exists())

  def test_files_are_left_alone_while_root_files_are_synced(self):
    mod.save_selection({"folders": ["nested"], "rootFiles": True})
    code, result = self.cleanup()
    self.assertEqual(code, 0)
    self.assertNotIn("loose.txt", result["removed"])
    self.assertTrue(self.loose.exists())


class ErrorPayloadTests(PayloadCase):
  def test_a_failing_status_still_tells_the_panel_what_is_installed(self):
    argv = ["gdrive-sync.py", "status", "--folder", "/mnt/elsewhere", "--interval", "10"]
    out = io.StringIO()
    with mock.patch.object(sys, "argv", argv), \
         mock.patch.object(mod, "STATE_DIR", self.state), \
         mock.patch.object(mod, "WORKDIR", self.state / "workdir"), \
         contextlib.redirect_stdout(out):
      self.assertEqual(mod.main(), 0)
    payload = mod.json.loads(out.getvalue())
    self.assertFalse(payload["ok"])
    self.assertTrue(payload["installed"])          # rclone is not the problem
    self.assertEqual(payload["statusText"], "Unavailable")
    self.assertIn("inside your home directory", payload["lastError"])
    for key in ("authenticated", "syncing", "folderPath", "mountPath", "selectedCount",
                "localBytes", "browseMounted", "timerEnabled", "baseline", "warning"):
      self.assertIn(key, payload)

  def test_a_failing_folder_listing_keeps_its_shape(self):
    argv = ["gdrive-sync.py", "folders", "--folder", "/mnt/elsewhere"]
    out = io.StringIO()
    with mock.patch.object(sys, "argv", argv), \
         mock.patch.object(mod, "STATE_DIR", self.state), \
         mock.patch.object(mod, "WORKDIR", self.state / "workdir"), \
         contextlib.redirect_stdout(out):
      self.assertEqual(mod.main(), 0)
    payload = mod.json.loads(out.getvalue())
    self.assertEqual(payload["ok"], False)
    self.assertEqual(payload["folders"], [])


class BrowseToggleTests(TempDirCase):
  def setUp(self):
    super().setUp()
    self.home = self.tmp / "home"
    self.home.mkdir()
    self.calls = []
    for patch in (
      mock.patch.object(mod.Path, "home", return_value=self.home),
      mock.patch.object(mod, "units_from_args", return_value=None),
      mock.patch.object(mod, "unit_enabled", return_value=True),
    ):
      patch.start()
      self.addCleanup(patch.stop)

  def browse(self, enable=True, live=False, after_live=None):
    def fake_run(command, *args, **kwargs):
      self.calls.append(command)
      return 0, "", ""
    states = [live] if after_live is None else [live, after_live]
    with mock.patch.object(mod, "run", fake_run), \
         mock.patch.object(mod, "browse_live", side_effect=lambda path: states.pop(0) if states else live), \
         contextlib.redirect_stdout(io.StringIO()):
      args = Namespace(remote="gdrive", folder=str(self.home / "Drive"),
                       mount=str(self.home / "Browse"), enable=enable, disable=not enable)
      mod.cmd_browse(args)

  def test_an_enabled_unit_with_a_dead_mount_is_restarted(self):
    self.browse(enable=True, live=False, after_live=True)   # unit enabled, dead mount -> restart
    self.assertEqual(self.calls, [["systemctl", "--user", "restart", mod.BROWSE_SERVICE]])

  def test_a_disabled_unit_is_enabled_and_started(self):
    with mock.patch.object(mod, "unit_enabled", return_value=False):
      self.browse(enable=True, live=True)
    self.assertEqual(self.calls, [["systemctl", "--user", "enable", "--now", mod.BROWSE_SERVICE]])

  def test_a_mount_that_never_appears_is_an_error(self):
    with self.assertRaises(RuntimeError) as caught:
      self.browse(enable=True, live=False, after_live=False)
    self.assertIn("did not come up", str(caught.exception))


class QmlWatchdogTests(unittest.TestCase):
  """The panel must not kill work the backend is still allowed to do."""

  def interval(self, name):
    text = (ROOT / "Service.qml").read_text(encoding="utf-8")
    block = text.split(f"id: {name}", 1)[1].split("\n  }", 1)[0]
    return int(re.search(r"interval:\s*(\d+)", block).group(1))

  def test_status_and_folders_watchdogs_outlast_the_backend(self):
    # status: listremotes 10 + findmnt 2x8 + systemctl 3x6 + about 25 + walk 2
    #         + unit rewrite up to 30 + 25
    self.assertGreater(self.interval("statusWatchdog"), 126 * 1000)
    # folders: lsjson 90 + size 90 + walk 2
    self.assertGreater(self.interval("foldersWatchdog"), 182 * 1000)

  def test_the_control_watchdog_outlasts_a_bounded_cleanup(self):
    self.assertGreaterEqual(self.interval("controlWatchdog"), (mod.CLEANUP_TIMEOUT + 600) * 1000)
