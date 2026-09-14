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
from btrfs_patrol.cli import App, build_parser, cmd_delete, main
from btrfs_patrol.errors import PatrolError
from btrfs_patrol.output import Console
from btrfs_patrol.snapshots import SnapshotStore


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

    @unittest.skipIf(os.geteuid() == 0, "running as root")
    def test_modifying_commands_require_root(self):
        for argv in (["snapshot"], ["delete", "1"], ["rollback", "1"], ["dnf-hook", "pre"]):
            with self.subTest(argv=argv):
                code, _, err = self.run_cli(*argv)
                self.assertEqual(code, 1)
                self.assertIn("must be run as root", err)


if __name__ == "__main__":
    unittest.main()
