# SPDX-License-Identifier: GPL-3.0-or-later
"""The settings page's configuration writer.

Tested here rather than in the front end's own suite because it is the piece
that runs as root, and because it imports nothing from Qt - it is the only part
of the GUI that can be tested without a display or PySide6 installed.
"""
import contextlib
import io
import tempfile
import unittest
from pathlib import Path

from btrfs_patrol import config
from btrfs_patrol.gui import confwrite


class ApplyTests(unittest.TestCase):
    ORIGINAL = (
        "# keep me\n"
        "[filesystem]\n"
        "device = \"auto\"\n"
        "\n"
        "[retention]\n"
        "# and me\n"
        "max_snapshots = 50\n"
        "\n"
        "[dnf]\n"
        "pre_snapshot = true\n"
    )

    def test_changes_the_value_in_place(self):
        out = confwrite.apply(self.ORIGINAL, {("retention", "max_snapshots"): 25})
        self.assertIn("max_snapshots = 25\n", out)
        self.assertNotIn("max_snapshots = 50", out)

    def test_keeps_the_comments(self):
        """The reason this edits lines instead of re-emitting the document."""
        out = confwrite.apply(self.ORIGINAL, {("retention", "max_snapshots"): 25})
        self.assertIn("# keep me", out)
        self.assertIn("# and me", out)

    def test_adds_an_option_the_file_never_had(self):
        out = confwrite.apply(self.ORIGINAL, {("dnf", "post_snapshot"): True})
        self.assertIn("post_snapshot = true", out)
        self.assertIn("pre_snapshot = true", out)

    def test_adds_a_whole_section(self):
        out = confwrite.apply(self.ORIGINAL, {("boot", "entries"): 3})
        self.assertIn("[boot]", out)
        self.assertIn("entries = 3", out)

    def test_leaves_other_sections_alone(self):
        """A key that exists in two tables must only change in the one named."""
        text = "[retention]\nmax_snapshots = 50\n\n[subvolumes.var]\nmax_snapshots = 10\n"
        out = confwrite.apply(text, {("retention", "max_snapshots"): 5})
        self.assertIn("max_snapshots = 5\n", out)
        self.assertIn("max_snapshots = 10\n", out)

    def test_the_result_still_parses(self):
        out = confwrite.apply(self.ORIGINAL, {
            ("retention", "max_snapshots"): 25,
            ("boot", "entries"): 2,
            ("output", "color"): "never",
        })
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text(out)
            parsed = config.load(path)
        self.assertEqual(parsed.max_snapshots, 25)
        self.assertEqual(parsed.boot_entries, 2)
        self.assertEqual(parsed.color, "never")


class ValueTests(unittest.TestCase):
    def test_types_come_from_the_packages_own_schema(self):
        self.assertEqual(confwrite._parse_value("retention", "max_snapshots", "7"), 7)
        self.assertIs(confwrite._parse_value("dnf", "pre_snapshot", "false"), False)

    def test_refuses_an_option_that_does_not_exist(self):
        with self.assertRaises(ValueError):
            confwrite._parse_value("retention", "nonsense", "1")

    def test_refuses_the_wrong_type(self):
        with self.assertRaises(ValueError):
            confwrite._parse_value("retention", "max_snapshots", "lots")
        with self.assertRaises(ValueError):
            confwrite._parse_value("dnf", "pre_snapshot", "yes")

    def test_refuses_a_colour_that_is_not_one(self):
        with self.assertRaises(ValueError):
            confwrite._parse_value("output", "color", "purple")


class MainTests(unittest.TestCase):
    def _config(self, directory: str) -> Path:
        path = Path(directory) / "config.toml"
        path.write_text(ApplyTests.ORIGINAL)
        return path

    def test_writes_the_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self._config(directory)
            # It reports what it wrote on stdout; the test suite does not need to.
            with contextlib.redirect_stdout(io.StringIO()):
                code = confwrite.main(["-c", str(path), "retention.max_snapshots=9"])
            self.assertEqual(code, 0)
            self.assertEqual(config.load(path).max_snapshots, 9)

    def test_a_bad_setting_changes_nothing(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self._config(directory)
            before = path.read_text()
            self.assertEqual(confwrite.main(["-c", str(path), "retention.max_snapshots=lots"]), 2)
            self.assertEqual(path.read_text(), before)

    def test_a_setting_that_is_not_section_dot_option(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self._config(directory)
            self.assertEqual(confwrite.main(["-c", str(path), "max_snapshots=9"]), 2)


if __name__ == "__main__":
    unittest.main()
