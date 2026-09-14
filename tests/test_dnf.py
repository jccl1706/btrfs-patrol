# SPDX-License-Identifier: GPL-3.0-or-later
import contextlib
import io
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from btrfs_patrol import btrfs, dnf
from btrfs_patrol.errors import PatrolError
from btrfs_patrol.output import Console

# The actions plugin reads every stdout line as an instruction, so these are the only
# lines the hook may print there.
PLUGIN_LINE = re.compile(r"log\.(INFO|WARNING)=btrfs-patrol: .*")


def fake_create_snapshot(source, destination, readonly=False):
    destination.mkdir()


class DnfHookTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name)
        self.snapshots_dir = base / "snapshots"
        self.snapshots_dir.mkdir()
        self.config = base / "config.toml"
        self.write_config()
        # Never run the real btrfs from tests.
        patcher = mock.patch.multiple(btrfs, create_snapshot=mock.DEFAULT, delete_subvolume=mock.DEFAULT)
        mocks = patcher.start()
        self.addCleanup(patcher.stop)
        self.create_snapshot = mocks["create_snapshot"]
        self.create_snapshot.side_effect = fake_create_snapshot
        mocks["delete_subvolume"].side_effect = lambda path: path.rmdir()

    def write_config(self, extra=""):
        self.config.write_text(f'[filesystem]\nsnapshots_dir = "{self.snapshots_dir}"\n{extra}')

    def run_hook(self, phase):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out):
            code = dnf.run_hook(phase, self.config, Console("never", err=err))
        self.assertEqual(code, 0)
        lines = out.getvalue().splitlines()
        for line in lines:
            self.assertIsNotNone(PLUGIN_LINE.fullmatch(line), f"not a plugin instruction: {line!r}")
        return lines, err.getvalue()

    def snapshot_dirs(self):
        return sorted(p.name for p in self.snapshots_dir.iterdir() if p.name.isdigit())

    def test_pre_snapshot_is_logged_through_the_plugin(self):
        lines, err = self.run_hook("pre")
        self.assertEqual(lines, ["log.INFO=btrfs-patrol: created snapshot 1 (dnf-pre)"])
        self.assertEqual(err, "")
        self.assertEqual(self.snapshot_dirs(), ["1"])

    def test_post_snapshot_is_off_by_default(self):
        lines, err = self.run_hook("post")
        self.assertEqual((lines, err), ([], ""))
        self.assertEqual(self.snapshot_dirs(), [])

    def test_missing_config_is_silent(self):
        self.config.unlink()
        self.assertEqual(self.run_hook("pre"), ([], ""))

    def test_failure_is_a_single_warning_line(self):
        self.create_snapshot.side_effect = PatrolError("btrfs failed\nsecond line")
        lines, err = self.run_hook("pre")
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].startswith("log.WARNING="))
        self.assertIn("btrfs failed second line", lines[0])
        self.assertIn("btrfs failed", err)
        self.assertEqual(self.snapshot_dirs(), [])

    def test_pruning_is_reported(self):
        self.write_config("[retention]\nmax_snapshots = 1\n")
        self.run_hook("pre")
        lines, _ = self.run_hook("pre")
        self.assertEqual(lines, ["log.INFO=btrfs-patrol: created snapshot 2 (dnf-pre), pruned 1"])


if __name__ == "__main__":
    unittest.main()
