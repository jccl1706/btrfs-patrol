# SPDX-License-Identifier: GPL-3.0-or-later
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from btrfs_patrol import selinux, system


class ExclusionTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name) / "selinux"
        self.exclude = self.dir / "fixfiles_exclude_dirs"
        for patcher in (
            mock.patch.object(selinux, "SELINUX_DIR", self.dir),
            mock.patch.object(selinux, "EXCLUDE_FILE", self.exclude),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_without_a_policy_nothing_is_missing(self):
        self.assertFalse(selinux.exclusion_missing(Path("/.snapshots")))

    def test_missing_until_added(self):
        self.dir.mkdir()
        (self.dir / "config").write_text("SELINUX=enforcing\n")
        self.assertTrue(selinux.exclusion_missing(Path("/.snapshots")))
        self.exclude.write_text("/var/lib/big")  # no trailing newline
        selinux.add_exclusion(Path("/.snapshots"))
        self.assertEqual(self.exclude.read_text(), "/var/lib/big\n/.snapshots\n")
        self.assertFalse(selinux.exclusion_missing(Path("/.snapshots")))

    def test_added_to_a_missing_file(self):
        self.dir.mkdir()
        selinux.add_exclusion(Path("/.snapshots"))
        self.assertEqual(self.exclude.read_text(), "/.snapshots\n")

    def test_matching_ignores_spacing_and_a_trailing_slash(self):
        self.assertTrue(selinux.excluded(Path("/.snapshots"), "  /.snapshots/  \n"))
        self.assertFalse(selinux.excluded(Path("/.snapshots"), "/.snapshots-old\n/snapshots\n"))


class EnabledTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name)
        self.enforce = base / "enforce"
        self.current = base / "current"
        for patcher in (
            mock.patch.object(selinux, "SELINUXFS_ENFORCE", self.enforce),
            mock.patch.object(selinux, "PROC_ATTR_CURRENT", self.current),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_disabled_with_selinuxfs_mounted(self):
        # Fedora with SELinux disabled: the enforce file exists, but no policy is loaded.
        self.enforce.write_text("0")
        self.current.write_text("kernel\0")
        self.assertFalse(selinux.enabled())

    def test_enabled_permissive_or_enforcing(self):
        self.enforce.write_text("0")
        self.current.write_text("unconfined_u:unconfined_r:unconfined_t:s0\0")
        self.assertTrue(selinux.enabled())

    def test_no_selinuxfs(self):
        self.current.write_text("unconfined_u:unconfined_r:unconfined_t:s0\0")
        self.assertFalse(selinux.enabled())


class StoreTests(unittest.TestCase):
    def test_store_files_but_never_snapshot_contents(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = Path(tmp.name)
        (store / "1/snapshot/etc").mkdir(parents=True)
        (store / "1/info.json").write_text("{}")
        (store / "2").mkdir()  # an entry whose metadata isn't written yet
        (store / ".next-id").write_text("3\n")
        (store / ".lock").touch()
        (store / "notes").mkdir()
        self.assertEqual(
            selinux.store_paths(store),
            [store, store / ".lock", store / ".next-id", store / "1", store / "1/info.json", store / "2"],
        )

    def test_mislabeled_reads_a_restorecon_dry_run(self):
        output = "Would relabel /s from a_t to b_t\nWould relabel /s/1 from a_t to b_t\n"
        paths = [Path("/s"), Path("/s/1")]
        with mock.patch.object(system, "run", return_value=output) as run:
            self.assertEqual(len(selinux.mislabeled(paths)), 2)
        run.assert_called_once_with("restorecon", "-n", "-v", *paths, missing=mock.ANY)

    def test_label_never_recurses(self):
        with mock.patch.object(system, "run", return_value="") as run:
            selinux.label([Path("/s")])
        self.assertNotIn("-R", run.call_args.args)


if __name__ == "__main__":
    unittest.main()
