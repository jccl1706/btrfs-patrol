# SPDX-License-Identifier: GPL-3.0-or-later
import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from support import make_snapshot

from btrfs_patrol import config as config_mod
from btrfs_patrol import selinux, system
from btrfs_patrol.cli import App, build_parser, cmd_delete, cmd_snapshot, main
from btrfs_patrol.errors import PatrolError
from btrfs_patrol.output import Console
from btrfs_patrol.snapshots import SnapshotStore
from btrfs_patrol.system import Mount


class CliTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name)
        self.snapshots_dir = base / "snapshots"
        self.snapshots_dir.mkdir()
        self.config = base / "config.toml"
        self.config.write_text(f'[filesystem]\nsnapshots_dir = "{self.snapshots_dir}"\n')
        self.store = SnapshotStore(self.snapshots_dir)
        # Independent of the test machine's own SELinux state.
        self.selinux = {}
        for name in ("enabled", "exclusion_missing"):
            patcher = mock.patch.object(selinux, name, return_value=False)
            self.selinux[name] = patcher.start()
            self.addCleanup(patcher.stop)
        for snapshot in (
            make_snapshot(1, description="fresh install", keep=True),
            make_snapshot(2, kind="timer"),
        ):
            self.store.path(snapshot.id).mkdir()
            self.store.save(snapshot)

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(["--config", str(self.config), "--color", "never", *argv])
        return code, out.getvalue(), err.getvalue()

    def test_list(self):
        code, out, err = self.run_cli("list")
        self.assertEqual(code, 0, err)
        self.assertIn("fresh install", out)
        self.assertIn("2 snapshot(s), 1 kept", out)

    def test_list_with_selector(self):
        code, out, err = self.run_cli("list", "kind=timer")
        self.assertEqual(code, 0, err)
        self.assertIn("1 snapshot(s), 0 kept", out)

    def test_unknown_id(self):
        code, _, err = self.run_cli("list", "9")
        self.assertEqual(code, 1)
        self.assertIn("no snapshot with ID 9", err)

    def test_missing_config(self):
        self.config.unlink()
        code, _, err = self.run_cli("list")
        self.assertEqual(code, 1)
        self.assertIn("configuration file not found", err)

    @unittest.skipIf(os.geteuid() == 0, "root can read anything")
    def test_permission_denied_is_explained(self):
        self.snapshots_dir.chmod(0)
        self.addCleanup(self.snapshots_dir.chmod, 0o755)
        code, _, err = self.run_cli("list")
        self.assertEqual(code, 1)
        self.assertIn(f"permission denied: {self.snapshots_dir} (run as root?)", err)

    def test_delete_without_terminal_refuses_before_printing(self):
        # Call the handler directly: main() would stop at the root check first.
        out = io.StringIO()
        config = config_mod.load(self.config)
        app = App(config, self.store, Console("never", out=out))
        args = build_parser().parse_args(["delete", "1"])
        with mock.patch("sys.stdin", io.StringIO()):
            with self.assertRaisesRegex(PatrolError, "--yes"):
                cmd_delete(app, args)
        self.assertEqual(out.getvalue(), "")
        self.assertTrue(self.store.path(1).exists())

    def test_list_off_a_terminal_prints_whole_descriptions(self):
        description = "state before the big upgrade, " * 4
        snapshot = make_snapshot(3, description=description.strip())
        self.store.path(3).mkdir()
        self.store.save(snapshot)
        code, out, err = self.run_cli("list")
        self.assertEqual(code, 0, err)
        self.assertIn(description.strip(), out)
        self.assertNotIn("…", out)

    def pending_mounts(self):
        return [
            Mount("/snapshots/8/snapshot", "/", "btrfs", "/dev/vda2"),
            Mount("/snapshots", str(self.snapshots_dir), "btrfs", "/dev/vda2"),
        ]

    def test_check_during_a_pending_rollback_warns_but_passes(self):
        for snapshot_id in self.store.ids():
            self.store.subvolume(snapshot_id).mkdir()
        with mock.patch.object(system, "read_mounts", return_value=self.pending_mounts()):
            code, out, err = self.run_cli("check")
        self.assertEqual(code, 0, err)
        self.assertIn("waiting for a reboot", err)
        self.assertIn("snapshot 8", err)
        self.assertIn("look good", out)

    def test_check_warns_about_a_missing_relabel_exclusion(self):
        self.selinux["exclusion_missing"].return_value = True
        mounts = [
            Mount("/root", "/", "btrfs", "/dev/vda2"),
            Mount("/snapshots", str(self.snapshots_dir), "btrfs", "/dev/vda2"),
        ]
        for snapshot_id in self.store.ids():
            self.store.subvolume(snapshot_id).mkdir()
        with mock.patch.object(system, "read_mounts", return_value=mounts):
            code, out, err = self.run_cli("check")
        self.assertEqual(code, 0, err)
        self.assertIn("isn't excluded from full SELinux relabels", err)

    def app(self, out):
        return App(config_mod.load(self.config), self.store, Console("never", out=out))

    def test_snapshot_during_a_pending_rollback_is_refused(self):
        args = build_parser().parse_args(["snapshot"])
        with mock.patch.object(system, "read_mounts", return_value=self.pending_mounts()):
            with self.assertRaisesRegex(PatrolError, "reboot first"):
                cmd_snapshot(self.app(io.StringIO()), args)
        self.assertEqual(self.store.ids(), [1, 2])

    def test_timer_snapshot_during_a_pending_rollback_is_skipped(self):
        out = io.StringIO()
        args = build_parser().parse_args(["snapshot", "--kind", "timer"])
        with mock.patch.object(system, "read_mounts", return_value=self.pending_mounts()):
            self.assertEqual(cmd_snapshot(self.app(out), args), 0)
        self.assertIn("skipping the scheduled snapshot", out.getvalue())
        self.assertEqual(self.store.ids(), [1, 2])

    def test_dnf_hook_refuses_a_terminal(self):
        terminal = io.StringIO()
        terminal.isatty = lambda: True
        with mock.patch("sys.stdin", terminal), mock.patch("os.geteuid", return_value=0):
            code, _, err = self.run_cli("dnf-hook", "pre")
        self.assertEqual(code, 1)
        self.assertIn("run by dnf's actions plugin", err)

    @unittest.skipIf(os.geteuid() == 0, "running as root")
    def test_modifying_commands_require_root(self):
        for argv in (["snapshot"], ["delete", "1"], ["rollback", "1"], ["setup"], ["dnf-hook", "pre"]):
            with self.subTest(argv=argv):
                code, _, err = self.run_cli(*argv)
                self.assertEqual(code, 1)
                self.assertIn("must be run as root", err)


if __name__ == "__main__":
    unittest.main()
