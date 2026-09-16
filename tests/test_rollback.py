# SPDX-License-Identifier: GPL-3.0-or-later
import platform
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from support import make_snapshot

from btrfs_patrol import boot, btrfs, rollback, system
from btrfs_patrol import config as config_mod
from btrfs_patrol.btrfs import Subvolume
from btrfs_patrol.errors import PatrolError
from btrfs_patrol.rollback import (
    DEFAULT_SUBVOLUME,
    RollbackPlan,
    boot_method,
    fstab_method,
    nested_subvolumes,
    pending_rollback,
    pending_rollback_message,
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


class FstabMethodTests(unittest.TestCase):
    def test_mounted_by_name(self):
        self.assertEqual(fstab_method(Path("/home"), "home", FEDORA_FSTAB), "subvol=home in /etc/fstab")

    def test_refused_setups(self):
        for fstab, message in (
            (FEDORA_FSTAB.replace("noatime,subvol=home", "noatime,subvolid=257"), "subvolid"),
            (FEDORA_FSTAB.replace("noatime,subvol=home", "noatime"), "no subvol= option"),
            (FEDORA_FSTAB.replace("subvol=home", "subvol=@home"), "subvol=@home"),
            (FEDORA_FSTAB.replace("/home ", "/srv  "), "no line for /home"),
        ):
            with self.subTest(fstab=fstab), self.assertRaisesRegex(PatrolError, message):
                fstab_method(Path("/home"), "home", fstab)


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

    def test_message_names_the_path(self):
        self.assertIn("/ is still the previous system, kept as snapshot 8", pending_rollback_message(8))
        self.assertIn(
            "/home is still the previous state, kept as snapshot 8",
            pending_rollback_message(8, Path("/home")),
        )


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
            is_subvolume=mock.DEFAULT,
        )
        self.btrfs = patcher.start()
        self.addCleanup(patcher.stop)
        self.btrfs["create_snapshot"].side_effect = (
            lambda source, destination, readonly=False: shutil.copytree(source, destination)
        )
        self.btrfs["delete_subvolume"].side_effect = shutil.rmtree
        self.btrfs["subvolume_id"].return_value = 300
        # The restored root is a subvolume unless a test says otherwise; the fake
        # create_snapshot above is a copytree, which cannot make a real one.
        self.btrfs["is_subvolume"].return_value = True
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

    def test_a_restored_root_that_is_not_a_subvolume_is_refused_and_undone(self):
        """'btrfs subvolume snapshot' into an existing directory nests and exits 0."""
        self.btrfs["is_subvolume"].return_value = False
        with self.assertRaisesRegex(PatrolError, "is not a subvolume after restoring"):
            self.plan().execute()
        # The live root must be back where it was, not left inside the store.
        self.assert_untouched()

    def test_the_default_subvolume_is_not_touched_when_the_restore_is_bad(self):
        self.btrfs["is_subvolume"].return_value = False
        with self.assertRaises(PatrolError):
            self.plan().execute()
        self.assertEqual(self.default["id"], 256, "the default must not have moved")

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

    def with_home(self):
        return config_mod.parse({
            "filesystem": {"snapshots_dir": str(self.store.directory)},
            "subvolumes": {"home": {"path": "/home"}, "log": {"path": "/var/log"}},
        })

    def test_rollback_of_another_subvolume(self):
        home = self.top / "home"
        (home / "jc").mkdir(parents=True)
        (home / "jc/notes").write_text("current")
        target = make_snapshot(5, subvolume="home")
        self.store.path(5).mkdir()
        self.store.save(target)
        (self.store.subvolume(5) / "jc").mkdir(parents=True)
        (self.store.subvolume(5) / "jc/notes").write_text("old")
        config = self.with_home()
        plan = RollbackPlan(
            config, self.store, target, self.top, "/dev/vda2", 257, "subvol=home in /etc/fstab",
            [], None, subvolume=config.find("home"), subvolume_path="home",
        )
        self.assertIn("takes effect:       at the next reboot", "\n".join(plan.describe()))
        saved = plan.execute()
        self.assertEqual((saved.id, saved.subvolume, saved.kind, saved.keep), (6, "home", "rollback", True))
        self.assertEqual((home / "jc/notes").read_text(), "old")
        self.assertEqual((self.store.subvolume(6) / "jc/notes").read_text(), "current")
        # Root and the default subvolume are left alone.
        self.assertEqual((self.root / "etc/state").read_text(), "current")
        self.btrfs["set_default_subvolume"].assert_not_called()

    def test_a_subvolume_inside_root_takes_effect_at_once(self):
        config = self.with_home()
        plan = RollbackPlan(
            config, self.store, make_snapshot(1, subvolume="log"), self.top, "/dev/vda2", 270,
            "its path inside the 'root' subvolume", [], None,
            subvolume=config.find("log"), subvolume_path="root/var/log", mounted=False,
        )
        text = "\n".join(plan.describe())
        self.assertIn("log, root/var/log (ID 270)", text)
        self.assertIn("takes effect:       at once", text)


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


class PrepareOtherSubvolumeTests(unittest.TestCase):
    """The checks prepare() makes before mounting anything, for a subvolume other than root."""

    ROOT_MOUNT = Mount("/root", "/", "btrfs", "/dev/vda2")

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        snapshots = self.dir / "snapshots"
        snapshots.mkdir()
        self.store = SnapshotStore(snapshots)
        self.config = config_mod.parse({
            "filesystem": {"snapshots_dir": str(snapshots)},
            "subvolumes": {"home": {"path": "/home"}},
        })
        self.target = self.add(1, "home")

    def add(self, snapshot_id, subvolume):
        snapshot = make_snapshot(snapshot_id, subvolume=subvolume)
        self.store.path(snapshot_id).mkdir()
        self.store.save(snapshot)
        self.store.subvolume(snapshot_id).mkdir()
        return snapshot

    def prepare(self, mounts, target=None):
        with mock.patch.object(system, "read_mounts", return_value=mounts):
            with rollback.prepare(self.config, self.store, target or self.target):
                pass

    def test_refuses_a_subvolume_that_is_not_configured(self):
        with self.assertRaisesRegex(PatrolError, "'data', which isn't in the configuration"):
            self.prepare([self.ROOT_MOUNT], self.add(2, "data"))

    def test_refuses_while_root_waits_for_a_reboot(self):
        mounts = [Mount("/snapshots/8/snapshot", "/", "btrfs", "/dev/vda2"), Mount("/home", "/home", "btrfs", "/dev/vda2")]
        with self.assertRaisesRegex(PatrolError, "/ is still the previous system.*reboot first"):
            self.prepare(mounts)

    def test_refuses_while_home_waits_for_a_reboot(self):
        mounts = [self.ROOT_MOUNT, Mount("/snapshots/9/snapshot", "/home", "btrfs", "/dev/vda2")]
        with self.assertRaisesRegex(PatrolError, "/home is still the previous state.*reboot first"):
            self.prepare(mounts)

    def test_refuses_home_on_another_filesystem(self):
        mounts = [self.ROOT_MOUNT, Mount("/", "/home", "xfs", "/dev/vdb1")]
        with self.assertRaisesRegex(PatrolError, "not on the btrfs filesystem mounted at /"):
            self.prepare(mounts)

    def test_refuses_home_mounted_by_subvolume_id(self):
        fstab = self.dir / "fstab"
        fstab.write_text(FEDORA_FSTAB.replace("noatime,subvol=home", "noatime,subvolid=257"))
        mounts = [self.ROOT_MOUNT, Mount("/home", "/home", "btrfs", "/dev/vda2")]
        with mock.patch.object(rollback, "FSTAB", fstab):
            with self.assertRaisesRegex(PatrolError, "subvolid"):
                self.prepare(mounts)


if __name__ == "__main__":
    unittest.main()

