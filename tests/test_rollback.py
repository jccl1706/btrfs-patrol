# SPDX-License-Identifier: GPL-3.0-or-later
import platform
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from support import make_snapshot

from btrfs_patrol import boot, btrfs, rollback
from btrfs_patrol import config as config_mod
from btrfs_patrol.btrfs import Subvolume
from btrfs_patrol.errors import PatrolError
from btrfs_patrol.rollback import (
    DEFAULT_SUBVOLUME,
    RollbackPlan,
    boot_method,
    nested_subvolumes,
    pending_rollback,
)
from btrfs_patrol.snapshots import SnapshotStore
from btrfs_patrol.system import Mount

FEDORA_FSTAB = """\
UUID=FFE9-D9AE  /boot  vfat   umask=0077                0 2
UUID=6b2c5be9   /      btrfs  noatime,compress=zstd:1   0 0
UUID=6b2c5be9   /home  btrfs  noatime,subvol=home       0 0
"""
FEDORA_CMDLINE = "root=UUID=6b2c5be9 rw rootfstype=btrfs quiet rhgb"


class BootMethodTests(unittest.TestCase):
    def test_default_subvolume(self):
        self.assertEqual(boot_method("root", FEDORA_FSTAB, FEDORA_CMDLINE), DEFAULT_SUBVOLUME)

    def test_by_name(self):
        fstab = FEDORA_FSTAB.replace("noatime,compress=zstd:1", "subvol=root,noatime")
        self.assertEqual(boot_method("root", fstab, FEDORA_CMDLINE), "subvol=root")
        cmdline = FEDORA_CMDLINE + " rootflags=subvol=/root"
        self.assertEqual(boot_method("root", FEDORA_FSTAB, cmdline), "subvol=root")

    def test_comments_are_ignored(self):
        fstab = "# UUID=x / btrfs subvolid=256 0 0\n" + FEDORA_FSTAB
        self.assertEqual(boot_method("root", fstab, FEDORA_CMDLINE), DEFAULT_SUBVOLUME)

    def test_refused_setups(self):
        for fstab, cmdline in (
            (FEDORA_FSTAB.replace("noatime,compress", "subvolid=256,compress"), FEDORA_CMDLINE),
            (FEDORA_FSTAB, FEDORA_CMDLINE + " rootflags=subvolid=256"),
            (FEDORA_FSTAB, FEDORA_CMDLINE + " rootflags=subvol=@"),
        ):
            with self.subTest(fstab=fstab, cmdline=cmdline), self.assertRaises(PatrolError):
                boot_method("root", fstab, cmdline)


class PendingRollbackTests(unittest.TestCase):
    def setUp(self):
        self.config = config_mod.parse({})

    def pending(self, root, fstype="btrfs"):
        return pending_rollback(self.config, Mount(root, "/", fstype, "/dev/vda2"))

    def test_root_moved_into_the_store(self):
        self.assertEqual(self.pending("/snapshots/8/snapshot"), 8)

    def test_not_pending(self):
        self.assertIsNone(self.pending("/root"))
        self.assertIsNone(self.pending("/snapshots/8/other"))
        self.assertIsNone(self.pending("/snapshots/x/snapshot"))
        self.assertIsNone(self.pending("/snapshots/8/snapshot", fstype="ext4"))
        self.assertIsNone(pending_rollback(self.config, None))


class NestedSubvolumeTests(unittest.TestCase):
    def test_outermost_subvolumes_inside_root(self):
        subvolumes = [
            Subvolume(256, "root"),
            Subvolume(257, "home"),
            Subvolume(258, "root/var/lib/portables"),
            Subvolume(259, "root/var/lib/machines"),
            Subvolume(260, "root/var/lib/machines/inner"),
            Subvolume(261, "snapshots/1/snapshot"),
            Subvolume(262, "rootfs/data"),
        ]
        self.assertEqual(
            nested_subvolumes(subvolumes, "root"), ["var/lib/machines", "var/lib/portables"]
        )


class ExecuteTests(unittest.TestCase):
    """RollbackPlan.execute on a directory tree standing in for the top-level subvolume."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.top = Path(tmp.name)

        self.root = self.top / "root"
        (self.root / "etc").mkdir(parents=True)
        (self.root / "etc/state").write_text("current")
        (self.root / "var/lib/portables").mkdir(parents=True)
        (self.root / "var/lib/portables/image.raw").write_text("nested data")

        snapshots = self.top / "snapshots"
        snapshots.mkdir()
        self.store = SnapshotStore(snapshots)
        self.target = make_snapshot(1, keep=True)
        self.store.path(1).mkdir()
        self.store.save(self.target)
        target_root = self.store.subvolume(1)
        (target_root / "etc").mkdir(parents=True)
        (target_root / "etc/state").write_text("old")
        # A snapshot holds an empty directory where a nested subvolume was.
        (target_root / "var/lib/portables").mkdir(parents=True)

        self.config = config_mod.parse({"filesystem": {"snapshots_dir": str(snapshots)}})
        self.default = {"id": 256}
        patcher = mock.patch.multiple(
            btrfs,
            create_snapshot=mock.DEFAULT,
            delete_subvolume=mock.DEFAULT,
            subvolume_id=mock.DEFAULT,
            set_default_subvolume=mock.DEFAULT,
            get_default_subvolume_id=mock.DEFAULT,
        )
        self.btrfs = patcher.start()
        self.addCleanup(patcher.stop)
        self.btrfs["create_snapshot"].side_effect = (
            lambda source, destination, readonly=False: shutil.copytree(source, destination)
        )
        self.btrfs["delete_subvolume"].side_effect = shutil.rmtree
        self.btrfs["subvolume_id"].return_value = 300
        self.btrfs["set_default_subvolume"].side_effect = (
            lambda subvolume_id, mount: self.default.update(id=subvolume_id)
        )
        self.btrfs["get_default_subvolume_id"].side_effect = lambda mount: self.default["id"]

    def plan(self, boots_by=DEFAULT_SUBVOLUME):
        return RollbackPlan(
            self.config, self.store, self.target, self.top, "/dev/vda2", 256,
            boots_by, ["var/lib/portables"], platform.release(),
        )

    def assert_untouched(self):
        self.assertEqual((self.root / "etc/state").read_text(), "current")
        self.assertEqual((self.root / "var/lib/portables/image.raw").read_text(), "nested data")
        self.assertEqual(self.store.ids(), [1])
        self.assertEqual(self.default["id"], 256)

    def test_rollback(self):
        saved = self.plan().execute()
        self.assertEqual((saved.id, saved.kind, saved.keep), (2, "rollback", True))
        self.assertEqual(self.store.load(2), saved)
        # The root now has the snapshot's content, and the old root is kept as snapshot 2.
        self.assertEqual((self.root / "etc/state").read_text(), "old")
        self.assertEqual((self.store.subvolume(2) / "etc/state").read_text(), "current")
        # The nested subvolume moved into the new root.
        self.assertEqual((self.root / "var/lib/portables/image.raw").read_text(), "nested data")
        self.assertFalse((self.store.subvolume(2) / "var/lib/portables").exists())
        self.assertEqual(self.default["id"], 300)
        self.btrfs["create_snapshot"].assert_called_once_with(self.store.subvolume(1), self.root)

    def test_boot_by_name_leaves_the_default_subvolume_alone(self):
        self.plan(boots_by="subvol=root").execute()
        self.btrfs["set_default_subvolume"].assert_not_called()
        self.assertEqual((self.root / "etc/state").read_text(), "old")

    def test_failed_snapshot_is_undone(self):
        self.btrfs["create_snapshot"].side_effect = PatrolError("no space left")
        with self.assertRaisesRegex(PatrolError, "no space left"):
            self.plan().execute()
        self.assert_untouched()

    def test_unchanged_default_subvolume_is_undone(self):
        self.btrfs["get_default_subvolume_id"].side_effect = lambda mount: 256
        with self.assertRaisesRegex(PatrolError, "default subvolume did not change"):
            self.plan().execute()
        self.assert_untouched()
        self.btrfs["delete_subvolume"].assert_called_once_with(self.root)

    def test_failed_undo_is_reported(self):
        self.btrfs["get_default_subvolume_id"].side_effect = lambda mount: 256
        self.btrfs["delete_subvolume"].side_effect = PatrolError("busy")
        with self.assertRaisesRegex(
            PatrolError, "undoing it failed too: could not delete the new root subvolume: busy"
        ):
            self.plan().execute()


class PrepareTests(unittest.TestCase):
    """The checks prepare() makes before mounting anything."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        snapshots = Path(tmp.name)
        self.store = SnapshotStore(snapshots)
        self.target = make_snapshot(1)
        self.store.path(1).mkdir()
        self.store.save(self.target)
        self.modules = self.store.subvolume(1) / "usr/lib/modules"
        self.modules.mkdir(parents=True)
        self.config = config_mod.parse({"filesystem": {"snapshots_dir": str(snapshots)}})

    def prepare(self):
        with rollback.prepare(self.config, self.store, self.target):
            pass

    def test_refuses_without_modules_for_the_running_kernel(self):
        (self.modules / "0.0.1-other").mkdir()
        with self.assertRaisesRegex(PatrolError, "no kernel modules for the running kernel"):
            self.prepare()

    def test_refuses_kernel_without_boot_entry(self):
        (self.modules / platform.release()).mkdir()
        with mock.patch.object(boot, "installed_kernel_versions", return_value={"0.0.1-other"}):
            with self.assertRaisesRegex(PatrolError, "has no boot entry"):
                self.prepare()


if __name__ == "__main__":
    unittest.main()
