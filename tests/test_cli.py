# SPDX-License-Identifier: GPL-3.0-or-later
import contextlib
import io
import os
import platform
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from support import make_snapshot

from btrfs_patrol import boot, btrfs, selinux, system
from btrfs_patrol import config as config_mod
from btrfs_patrol.cli import (
    App,
    build_parser,
    cmd_delete,
    cmd_prune_kernels,
    cmd_snapshot,
    main,
)
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

    def test_delete_refuses_the_snapshot_the_system_is_running_from(self):
        """After a rollback / is mounted FROM snapshot 8, and the tool names it."""
        self.store.path(8).mkdir()
        self.store.save(make_snapshot(8, kind="rollback", keep=True))
        self.store.subvolume(8).mkdir()
        args = build_parser().parse_args(["delete", "8", "--yes"])
        with mock.patch.object(system, "read_mounts", return_value=self.pending_mounts()):
            with self.assertRaisesRegex(PatrolError, "running from"):
                cmd_delete(self.app(io.StringIO()), args)
        self.assertIn(8, self.store.ids(), "it must still be there")

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


class SubvolumeCliTests(unittest.TestCase):
    """Commands with [subvolumes] tables: /home mounted on its own, and a log subvolume inside root."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name)
        self.snapshots_dir = base / "snapshots"
        self.snapshots_dir.mkdir()
        self.log_dir = base / "log"
        self.log_dir.mkdir()
        self.config = base / "config.toml"
        self.config.write_text(
            f'[filesystem]\nsnapshots_dir = "{self.snapshots_dir}"\n'
            '[subvolumes.home]\npath = "/home"\nmax_snapshots = 1\n'
            f'[subvolumes.log]\npath = "{self.log_dir}"\ntimer = false\n'
        )
        self.store = SnapshotStore(self.snapshots_dir)
        for name in ("enabled", "exclusion_missing"):
            patcher = mock.patch.object(selinux, name, return_value=False)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch.multiple(btrfs, create_snapshot=mock.DEFAULT, delete_subvolume=mock.DEFAULT)
        mocks = patcher.start()
        self.addCleanup(patcher.stop)
        self.create_snapshot = mocks["create_snapshot"]
        self.create_snapshot.side_effect = lambda source, destination, readonly=False: destination.mkdir()
        mocks["delete_subvolume"].side_effect = lambda path: path.rmdir()
        self.mounts = [
            Mount("/root", "/", "btrfs", "/dev/vda2"),
            Mount("/home", "/home", "btrfs", "/dev/vda2"),
            Mount("/snapshots", str(self.snapshots_dir), "btrfs", "/dev/vda2"),
        ]
        patcher = mock.patch.object(system, "read_mounts", side_effect=lambda: self.mounts)
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(["--config", str(self.config), "--color", "never", *argv])
        return code, out.getvalue(), err.getvalue()

    def snapshot(self, *argv):
        out = io.StringIO()
        app = App(config_mod.load(self.config), self.store, Console("never", out=out))
        code = cmd_snapshot(app, build_parser().parse_args(["snapshot", *argv]))
        return code, out.getvalue()

    def taken(self):
        return [(s.id, s.subvolume) for s in self.store.load_all()]

    def test_manual_snapshot_takes_every_subvolume(self):
        code, out = self.snapshot("-d", "before the upgrade")
        self.assertEqual(code, 0)
        self.assertEqual(self.taken(), [(1, "root"), (2, "home"), (3, "log")])
        self.assertIn("created snapshot 2 of home", out)
        self.assertEqual(
            [c.args[0] for c in self.create_snapshot.call_args_list],
            [Path("/"), Path("/home"), self.log_dir],
        )

    def test_snapshot_of_named_subvolumes(self):
        self.snapshot("--subvolume", "home", "-s", "home")
        self.assertEqual(self.taken(), [(1, "home")])
        with self.assertRaisesRegex(PatrolError, "no subvolume named 'data'.*root, home, log"):
            self.snapshot("-s", "data")

    def test_timer_skips_subvolumes_without_the_timer(self):
        self.snapshot("--kind", "timer")
        self.assertEqual(self.taken(), [(1, "root"), (2, "home")])

    def test_each_subvolume_is_pruned_by_its_own_limit(self):
        self.snapshot("-s", "home")
        self.snapshot("-s", "home")
        self.snapshot("-s", "root")
        self.assertEqual(self.taken(), [(2, "home"), (3, "root")])

    def test_pending_rollback_of_home(self):
        self.mounts[1] = Mount("/snapshots/9/snapshot", "/home", "btrfs", "/dev/vda2")
        with self.assertRaisesRegex(PatrolError, "/home is still the previous state.*reboot first"):
            self.snapshot()
        self.assertEqual(self.store.ids(), [])
        code, out = self.snapshot("--kind", "timer")
        self.assertEqual(code, 0)
        self.assertIn("skipping the scheduled snapshot of home", out)
        self.assertEqual(self.taken(), [(1, "root")])

    def test_list_shows_the_subvolume(self):
        self.snapshot("-s", "home")
        code, out, err = self.run_cli("list")
        self.assertEqual(code, 0, err)
        self.assertIn("SUBVOLUME", out)
        self.assertIn(" home ", out)

    def test_check_reports_a_directory_that_is_not_a_subvolume(self):
        with mock.patch.object(btrfs, "is_subvolume", return_value=False):
            code, _, err = self.run_cli("check")
        self.assertEqual(code, 1)
        self.assertIn(f"{self.log_dir} (subvolume 'log') is a directory, not a btrfs subvolume", err)

    def test_check_warns_about_a_subvolume_inside_root(self):
        with mock.patch.object(btrfs, "is_subvolume", return_value=True):
            code, out, err = self.run_cli("check")
        self.assertEqual(code, 0, err)
        self.assertIn(f"{self.log_dir} is a subvolume inside the root subvolume", err)
        self.assertNotIn("/home is a subvolume inside", err)
        self.assertIn("look good", out)

    def test_check_reports_home_on_another_filesystem(self):
        self.mounts[1] = Mount("/", "/home", "xfs", "/dev/vdb1")
        with mock.patch.object(btrfs, "is_subvolume", return_value=True):
            code, _, err = self.run_cli("check")
        self.assertEqual(code, 1)
        self.assertIn("/home (subvolume 'home') is not on the btrfs filesystem mounted at /", err)

    def test_check_warns_about_snapshots_of_unconfigured_subvolumes(self):
        snapshot = make_snapshot(4, subvolume="data")
        self.store.path(4).mkdir()
        self.store.save(snapshot)
        self.store.subvolume(4).mkdir()
        with mock.patch.object(btrfs, "is_subvolume", return_value=True):
            code, _, err = self.run_cli("check")
        self.assertEqual(code, 0, err)
        self.assertIn("snapshot(s) 4 are of the subvolume 'data'", err)


if __name__ == "__main__":
    unittest.main()


class PruneKernelsTests(unittest.TestCase):
    """prune-kernels removes boot entries for kernels with no modules."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name)
        snapshots_dir = base / "snapshots"
        snapshots_dir.mkdir()
        config = base / "config.toml"
        config.write_text(f'[filesystem]\nsnapshots_dir = "{snapshots_dir}"\n')
        self.out = io.StringIO()
        # Console binds its streams at construction, so give it both explicitly:
        # info goes to out, warnings to err.
        self.err = io.StringIO()
        self.app = App(config_mod.load(config), SnapshotStore(snapshots_dir),
                       Console("never", out=self.out, err=self.err))

    def run_command(self, *argv, stale=frozenset(), left=frozenset()):
        """Run the command; returns stdout and stderr together, warnings included.

        stale is what the first scan finds; left is what a second scan still
        finds afterwards, standing for entries kernel-install did not remove.
        """
        args = build_parser().parse_args(["prune-kernels", *argv])
        scans = [set(stale), set(left)]
        with mock.patch.object(boot, "stale_kernel_versions", side_effect=scans):
            with mock.patch.object(boot, "remove_boot_entry") as remove:
                code = cmd_prune_kernels(self.app, args)
        return code, self.out.getvalue() + self.err.getvalue(), remove

    def test_nothing_stale(self):
        code, out, remove = self.run_command()
        self.assertEqual(code, 0)
        self.assertIn("nothing to remove", out)
        remove.assert_not_called()

    def test_dry_run_lists_but_removes_nothing(self):
        code, out, remove = self.run_command("-n", stale={"9.9.9-1.fc99.x86_64"})
        self.assertEqual(code, 0)
        self.assertIn("9.9.9-1.fc99.x86_64", out)
        self.assertIn("dry run", out)
        remove.assert_not_called()

    def test_removes_every_stale_entry(self):
        stale = {"9.9.9-1.fc99.x86_64", "9.9.8-1.fc99.x86_64"}
        code, _, remove = self.run_command("-y", stale=stale)
        self.assertEqual(code, 0)
        self.assertEqual(sorted(call.args[0] for call in remove.call_args_list), sorted(stale))

    def test_never_removes_the_running_kernel(self):
        code, out, remove = self.run_command("-y", stale={platform.release()})
        self.assertEqual(code, 0)
        remove.assert_not_called()
        self.assertIn("nothing to remove", out)

    def test_warns_that_rolling_forward_needs_the_entries_back(self):
        _, out, _ = self.run_command("-n", stale={"9.9.9-1.fc99.x86_64"})
        self.assertIn("kernel-install add-all", out)

    def test_reports_an_entry_kernel_install_could_not_remove(self):
        """kernel-install exits 0 even when it removes nothing, so verify instead."""
        version = "9.9.9-1.fc99.x86_64"
        code, out, remove = self.run_command("-y", stale={version}, left={version})
        self.assertEqual(code, 1)
        remove.assert_called_once_with(version)
        self.assertIn("has to be removed by hand", out)
        self.assertNotIn(f"removed the boot entry for {version}", out)

    def test_reports_only_the_entries_that_really_went(self):
        gone, stuck = "9.9.9-1.fc99.x86_64", "9.9.8-1.fc99.x86_64"
        code, out, _ = self.run_command("-y", stale={gone, stuck}, left={stuck})
        self.assertEqual(code, 1)
        self.assertIn(f"removed the boot entry for {gone}", out)
        self.assertNotIn(f"removed the boot entry for {stuck}", out)


class UnmountedSnapshotStoreTests(unittest.TestCase):
    """cmd_snapshot must refuse rather than write into the parent subvolume."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name)
        self.snapshots_dir = base / "snapshots"
        self.snapshots_dir.mkdir()
        self.config_path = base / "config.toml"
        self.config_path.write_text(f'[filesystem]\nsnapshots_dir = "{self.snapshots_dir}"\n')
        self.out, self.err = io.StringIO(), io.StringIO()
        self.app = App(config_mod.load(self.config_path), SnapshotStore(self.snapshots_dir),
                       Console("never", out=self.out, err=self.err))

    def run_snapshot(self, mounts):
        args = build_parser().parse_args(["snapshot", "-d", "x"])
        with mock.patch.object(system, "read_mounts", return_value=mounts):
            with mock.patch.object(btrfs, "create_snapshot") as create:
                with self.assertRaises(PatrolError) as caught:
                    cmd_snapshot(self.app, args)
        return str(caught.exception), create

    def test_refuses_when_the_store_is_not_mounted(self):
        message, create = self.run_snapshot([Mount("/root", "/", "btrfs", "/dev/vda2")])
        self.assertIn("is not a mount point", message)
        create.assert_not_called()
        self.assertEqual(list(self.snapshots_dir.iterdir()), [], "nothing written to the directory")

    def test_refuses_when_a_different_subvolume_is_mounted_there(self):
        mounts = [
            Mount("/root", "/", "btrfs", "/dev/vda2"),
            Mount("/wrong", str(self.snapshots_dir), "btrfs", "/dev/vda2"),
        ]
        message, create = self.run_snapshot(mounts)
        self.assertIn("is subvolume '/wrong'", message)
        create.assert_not_called()
