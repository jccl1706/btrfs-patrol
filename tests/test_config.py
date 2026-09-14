# SPDX-License-Identifier: GPL-3.0-or-later
import tempfile
import unittest
from pathlib import Path

from btrfs_patrol import config
from btrfs_patrol.errors import PatrolError


class ParseTests(unittest.TestCase):
    def test_defaults_match_fedora_layout(self):
        c = config.parse({})
        self.assertIsNone(c.device)
        self.assertEqual(c.root_subvolume, "root")
        self.assertEqual(c.snapshots_subvolume, "snapshots")
        self.assertEqual(c.snapshots_dir, Path("/.snapshots"))
        self.assertEqual(c.max_snapshots, 50)
        self.assertTrue(c.dnf_pre_snapshot)
        self.assertFalse(c.dnf_post_snapshot)

    def test_explicit_device(self):
        c = config.parse({"filesystem": {"device": "/dev/nvme0n1p3"}})
        self.assertEqual(c.device, "/dev/nvme0n1p3")

    def test_unknown_names_are_rejected(self):
        with self.assertRaisesRegex(PatrolError, "max_snapshot"):
            config.parse({"retention": {"max_snapshot": 5}})
        with self.assertRaisesRegex(PatrolError, "retentoin"):
            config.parse({"retentoin": {}})

    def test_wrong_types_are_rejected(self):
        for data in (
            {"retention": {"max_snapshots": "50"}},
            {"retention": {"max_snapshots": True}},
            {"dnf": {"pre_snapshot": 1}},
            {"filesystem": "root"},
        ):
            with self.subTest(data=data), self.assertRaises(PatrolError):
                config.parse(data)

    def test_snapshots_inside_root_are_rejected(self):
        with self.assertRaisesRegex(PatrolError, "inside the root subvolume"):
            config.parse({"filesystem": {"snapshots_subvolume": "root/.snapshots"}})

    def test_invalid_values_are_rejected(self):
        for data in (
            {"retention": {"max_snapshots": 0}},
            {"output": {"color": "sometimes"}},
            {"filesystem": {"snapshots_dir": "relative/path"}},
            {"filesystem": {"root_subvolume": "/root"}},
            {"filesystem": {"snapshots_subvolume": "../elsewhere"}},
            {"filesystem": {"root_subvolume": ""}},
        ):
            with self.subTest(data=data), self.assertRaises(PatrolError):
                config.parse(data)


class LoadTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)

    def test_loads_file(self):
        path = self.dir / "config.toml"
        path.write_text('[retention]\nmax_snapshots = 7\n')
        self.assertEqual(config.load(path).max_snapshots, 7)

    def test_missing_file(self):
        with self.assertRaisesRegex(PatrolError, "not found"):
            config.load(self.dir / "missing.toml")

    def test_invalid_toml(self):
        path = self.dir / "config.toml"
        path.write_text("[filesystem\n")
        with self.assertRaises(PatrolError):
            config.load(path)


if __name__ == "__main__":
    unittest.main()
