# SPDX-License-Identifier: GPL-3.0-or-later
import unittest
from pathlib import Path

from btrfs_patrol.system import Mount, containing_mount, find_mount, parse_mountinfo

MOUNTINFO = """\
22 1 0:21 /root / rw,noatime shared:1 - btrfs /dev/mapper/vg0-root rw,compress=zstd:1,subvolid=256,subvol=/root
45 22 0:21 /home /home rw,noatime shared:30 - btrfs /dev/mapper/vg0-root rw,compress=zstd:1,subvolid=257,subvol=/home
46 22 259:1 / /boot rw,relatime shared:32 - vfat /dev/nvme0n1p1 rw,umask=0077
47 22 0:21 /snapshots /.snap\\040shots rw - btrfs /dev/mapper/vg0-root rw
48 45 0:40 / /home rw,nosuid - tmpfs tmpfs rw
"""


class MountinfoTests(unittest.TestCase):
    def setUp(self):
        self.mounts = parse_mountinfo(MOUNTINFO)

    def test_parses_fields(self):
        self.assertEqual(
            self.mounts[0], Mount("/root", "/", "btrfs", "/dev/mapper/vg0-root")
        )
        self.assertEqual(self.mounts[2].fstype, "vfat")

    def test_unescapes_spaces_and_handles_no_optional_fields(self):
        self.assertEqual(self.mounts[3].mount_point, "/.snap shots")
        self.assertEqual(find_mount(Path("/.snap shots"), self.mounts).root, "/snapshots")

    def test_find_mount_returns_topmost(self):
        self.assertEqual(find_mount(Path("/home"), self.mounts).fstype, "tmpfs")
        self.assertIsNone(find_mount(Path("/var"), self.mounts))

    def test_containing_mount_is_the_deepest_and_topmost(self):
        self.assertEqual(containing_mount(Path("/home/jc/notes"), self.mounts).fstype, "tmpfs")
        self.assertEqual(containing_mount(Path("/var/log"), self.mounts).root, "/root")
        self.assertEqual(containing_mount(Path("/homework"), self.mounts).root, "/root")
        self.assertEqual(containing_mount(Path("/boot"), self.mounts).fstype, "vfat")
        self.assertEqual(containing_mount(Path("/.snap shots/1"), self.mounts).root, "/snapshots")
        self.assertIsNone(containing_mount(Path("/var"), []))


if __name__ == "__main__":
    unittest.main()
