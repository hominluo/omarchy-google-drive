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
    self.assertEqual(result["refused"], [{"name": "Drop", "reason": "folder changed during cleanup"}])
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
