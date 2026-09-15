# SPDX-License-Identifier: GPL-3.0-or-later
import stat
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest import mock

from btrfs_patrol import btrfs, selinux, setup, system
from btrfs_patrol import config as config_mod
from btrfs_patrol.errors import PatrolError
from btrfs_patrol.setup import SetupPlan, config_text, fstab_entry, snapshot_mount_options
from btrfs_patrol.system import Mount

FEDORA_FSTAB = """\
# /etc/fstab
UUID=FFE9-D9AE  /boot  vfat   umask=0077                              0 2
UUID=6b2c5be9   /      btrfs  noatime,compress=zstd:1,space_cache=v2  0 0
UUID=6b2c5be9   /home  btrfs  noatime,compress=zstd:1,subvol=home     0 0
"""
FEDORA_MOUNTS = [
    Mount("/root", "/", "btrfs", "/dev/vda2"),
    Mount("/home", "/home", "btrfs", "/dev/vda2"),
    Mount("/", "/boot", "vfat", "/dev/vda1"),
]
SNAPSHOTS_LINE = "UUID=6b2c5be9  /.snapshots  btrfs  subvol=snapshots,noatime  0 0"


class FstabTests(unittest.TestCase):
    def test_fstab_entry(self):
        self.assertEqual(
            fstab_entry(FEDORA_FSTAB, Path("/"))[3], "noatime,compress=zstd:1,space_cache=v2"
        )
        self.assertIsNone(fstab_entry(FEDORA_FSTAB, Path("/.snapshots")))
        commented = "# UUID=x /.snapshots btrfs subvol=snapshots 0 0\n"
        self.assertIsNone(fstab_entry(commented, Path("/.snapshots")))

    def test_snapshot_mount_options_follow_the_root(self):
        root = fstab_entry(FEDORA_FSTAB, Path("/"))
        self.assertEqual(
            snapshot_mount_options(root, "snapshots"),
            "subvol=snapshots,noatime,compress=zstd:1,space_cache=v2",
        )
        by_name = "UUID=x / btrfs defaults,subvol=root,subvolid=256 0 0".split()
        self.assertEqual(snapshot_mount_options(by_name, "snapshots"), "subvol=snapshots,noatime")
        self.assertEqual(snapshot_mount_options(None, "@snapshots"), "subvol=@snapshots,noatime")


class TimerTests(unittest.TestCase):
    def test_only_a_disabled_timer_is_enabled(self):
        self.assertTrue(setup.timer_needs_enabling("disabled"))
        for state in (None, "enabled", "masked", "static", "enabled-runtime"):
            with self.subTest(state=state):
                self.assertFalse(setup.timer_needs_enabling(state))

    def test_unit_file_state(self):
        def fake_run(stdout):
            return mock.Mock(stdout=stdout, returncode=0)

        with mock.patch("subprocess.run", return_value=fake_run("disabled\n")):
            self.assertEqual(system.unit_file_state(setup.TIMER), "disabled")
        with mock.patch("subprocess.run", return_value=fake_run("not-found\n")):
            self.assertIsNone(system.unit_file_state(setup.TIMER))
        with mock.patch("subprocess.run", side_effect=FileNotFoundError):
            self.assertIsNone(system.unit_file_state(setup.TIMER))


class ConfigTextTests(unittest.TestCase):
    def test_example_matches_the_defaults(self):
        example = config_mod.parse(tomllib.loads(config_mod.example_text()))
        self.assertEqual(example, config_mod.parse({}))

    def test_root_subvolume_is_filled_in(self):
        for name in ("root", "@"):
            with self.subTest(name=name):
                text = config_text(name)
                self.assertEqual(config_mod.parse(tomllib.loads(text)).root_subvolume, name)
                self.assertIn("# btrfs-patrol configuration", text)


class PrepareTests(unittest.TestCase):
    """The checks prepare() makes before touching fstab or mounting anything."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.config_path = Path(tmp.name) / "config.toml"

    def prepare(self, mounts):
        with mock.patch.object(system, "read_mounts", return_value=mounts):
            with setup.prepare(self.config_path):
                pass

    def test_refuses_a_root_that_is_not_btrfs(self):
        with self.assertRaisesRegex(PatrolError, "not a btrfs filesystem"):
            self.prepare([Mount("/", "/", "ext4", "/dev/sda2")])

    def test_refuses_a_root_in_the_top_level_subvolume(self):
        with self.assertRaisesRegex(PatrolError, "top-level subvolume"):
            self.prepare([Mount("/", "/", "btrfs", "/dev/sda2")])

    def test_refuses_a_configuration_for_another_root(self):
        self.config_path.write_text('[filesystem]\nroot_subvolume = "@"\n')
        with self.assertRaisesRegex(PatrolError, "is '@', but / is 'root'"):
            self.prepare(FEDORA_MOUNTS)


class ExecuteTests(unittest.TestCase):
    """SetupPlan.execute with the filesystem, fstab and commands replaced."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name)
        self.top = base / "top"
        self.top.mkdir()
        # No trailing newline, to check that setup adds one before its line.
        self.fstab = base / "fstab"
        self.fstab.write_text(FEDORA_FSTAB.rstrip("\n"))
        self.config_path = base / "etc/btrfs-patrol/config.toml"
        self.text = config_text("root").replace(
            'snapshots_dir = "/.snapshots"', f'snapshots_dir = "{base / "mnt"}"'
        )
        self.config = config_mod.parse(tomllib.loads(self.text))

        for patcher in (
            mock.patch.object(setup, "FSTAB", self.fstab),
            mock.patch.object(btrfs, "create_subvolume", side_effect=lambda path: path.mkdir()),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        run_patcher = mock.patch.object(system, "run")
        self.run = run_patcher.start()
        self.addCleanup(run_patcher.stop)

    def plan(self, **changes):
        fields = dict(
            config_path=self.config_path, config=self.config, new_config_text=self.text,
            top=self.top, create_subvolume=True, create_mount_point=True,
            fstab_line=SNAPSHOTS_LINE, mount=True,
        )
        fields.update(changes)
        return SetupPlan(**fields)

    def test_everything_missing(self):
        plan = self.plan()
        self.assertEqual(len(plan.actions()), 5)
        plan.execute()
        self.assertEqual(config_mod.load(self.config_path), self.config)
        self.assertEqual(stat.S_IMODE((self.top / "snapshots").stat().st_mode), 0o700)
        self.assertTrue(self.config.snapshots_dir.is_dir())
        self.assertEqual(self.fstab.read_text(), FEDORA_FSTAB + SNAPSHOTS_LINE + "\n")
        backup = self.fstab.with_name("fstab" + setup.FSTAB_BACKUP_SUFFIX)
        self.assertEqual(backup.read_text(), FEDORA_FSTAB.rstrip("\n"))
        self.assertEqual(
            self.run.call_args_list,
            [mock.call("systemctl", "daemon-reload"), mock.call("mount", self.config.snapshots_dir)],
        )

    def test_enables_the_timer_after_mounting(self):
        plan = self.plan(
            new_config_text=None, create_subvolume=False, create_mount_point=False,
            fstab_line=None, enable_timer=True,
        )
        self.assertEqual(
            plan.actions(),
            [f"mount {self.config.snapshots_dir}",
             f"enable and start the daily snapshot timer ({setup.TIMER})"],
        )
        plan.execute()
        self.assertEqual(
            self.run.call_args_list,
            [mock.call("mount", self.config.snapshots_dir),
             mock.call("systemctl", "enable", "--now", setup.TIMER)],
        )

    def test_selinux_steps_come_after_mounting_and_before_the_timer(self):
        plan = self.plan(
            new_config_text=None, create_subvolume=False, create_mount_point=False,
            fstab_line=None, add_exclusion=True, label_store=True, enable_timer=True,
        )
        self.assertEqual(len(plan.actions()), 4)
        calls = []
        self.run.side_effect = lambda *args, **kwargs: calls.append(args[:2]) or ""
        snapshots_dir = self.config.snapshots_dir
        with mock.patch.object(selinux, "add_exclusion", side_effect=lambda d: calls.append(("exclude", d))), \
             mock.patch.object(selinux, "store_paths", return_value=[snapshots_dir]), \
             mock.patch.object(selinux, "label", side_effect=lambda p: calls.append(("label", tuple(p)))):
            plan.execute()
        self.assertEqual(
            calls,
            [("mount", snapshots_dir), ("exclude", snapshots_dir),
             ("label", (snapshots_dir,)), ("systemctl", "enable")],
        )

    def test_nothing_to_do(self):
        plan = self.plan(
            new_config_text=None, create_subvolume=False, create_mount_point=False,
            fstab_line=None, mount=False,
        )
        self.assertEqual(plan.actions(), [])
        plan.execute()
        self.assertFalse(self.config_path.exists())
        self.assertEqual(self.fstab.read_text(), FEDORA_FSTAB.rstrip("\n"))
        self.run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
