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


class SubvolumeTests(unittest.TestCase):
    def test_root_alone_by_default(self):
        c = config.parse({})
        self.assertEqual(c.subvolumes, ())
        self.assertEqual([s.name for s in c.managed()], ["root"])
        self.assertEqual(c.root, config.ManagedSubvolume("root", Path("/"), 50, timer=True, dnf=True))

    def test_defaults(self):
        c = config.parse({"retention": {"max_snapshots": 30}, "subvolumes": {"home": {"path": "/home"}}})
        [home] = c.subvolumes
        self.assertEqual(home, config.ManagedSubvolume("home", Path("/home"), 30, timer=True, dnf=False))
        self.assertEqual([s.name for s in c.managed()], ["root", "home"])
        self.assertEqual(c.find("home"), home)
        self.assertEqual(c.find("root"), c.root)
        self.assertIsNone(c.find("data"))

    def test_explicit_options(self):
        c = config.parse({"subvolumes": {"log": {
            "path": "/var/log", "max_snapshots": 5, "timer": False, "dnf": True,
        }}})
        self.assertEqual(
            c.subvolumes, (config.ManagedSubvolume("log", Path("/var/log"), 5, timer=False, dnf=True),)
        )

    def test_invalid_subvolumes(self):
        for subvolumes, message in (
            ({"root": {"path": "/srv"}}, "root subvolume's"),
            ({"my home": {"path": "/home"}}, "letters, digits"),
            ({"home": {}}, "needs a path"),
            ({"home": {"path": "home"}}, "absolute path"),
            ({"home": {"path": "/home/../etc"}}, "without '..'"),
            ({"home": {"path": "/"}}, "always snapshotted"),
            ({"snap": {"path": "/.snapshots/1"}}, "inside the snapshots directory"),
            ({"home": {"path": "/home", "max_snapshots": 0}}, "at least 1"),
            ({"home": {"path": "/home"}, "home2": {"path": "/home"}}, "already"),
            ({"home": {"path": "/home", "keep": True}}, "unknown option"),
            ({"home": {"path": "/home", "timer": "yes"}}, "must be a bool"),
            ({"home": "/home"}, "must be a table"),
        ):
            with self.subTest(subvolumes=subvolumes), self.assertRaisesRegex(PatrolError, message):
                config.parse({"subvolumes": subvolumes})
        with self.assertRaisesRegex(PatrolError, "one \\[subvolumes.<name>\\] table"):
            config.parse({"subvolumes": ["home"]})

    def test_example_configuration_has_none(self):
        import tomllib

        self.assertEqual(config.parse(tomllib.loads(config.example_text())).subvolumes, ())


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


class ReservedAndDuplicateNameTests(unittest.TestCase):
    """Selectors match subvolume names case-insensitively, so names must too."""

    def test_the_root_name_is_reserved_whatever_its_case(self):
        for name in ("ROOT", "Root", "rOOt"):
            with self.subTest(name=name):
                with self.assertRaisesRegex(PatrolError, "is the root subvolume's"):
                    config.parse({"subvolumes": {name: {"path": "/srv"}}})

    def test_two_names_differing_only_in_case_are_refused(self):
        with self.assertRaisesRegex(PatrolError, "differs only in case"):
            config.parse({
                "subvolumes": {"home": {"path": "/home"}, "Home": {"path": "/srv"}}
            })

    def test_whitespace_in_the_snapshots_dir_is_refused(self):
        """/etc/fstab separates fields with whitespace."""
        with self.assertRaisesRegex(PatrolError, "must not contain whitespace"):
            config.parse({"filesystem": {"snapshots_dir": "/.snap shots"}})
