# SPDX-License-Identifier: GPL-3.0-or-later
import io
import os
import unittest
from unittest import mock

from support import make_snapshot

from btrfs_patrol.output import Style, color_enabled, format_table


class FakeTerminal(io.StringIO):
    def isatty(self):
        return True


class FormatTableTests(unittest.TestCase):
    def setUp(self):
        self.snapshots = [
            make_snapshot(1, description="short", keep=True),
            make_snapshot(12, kind="dnf-pre", description="x" * 40),
        ]

    def test_truncates_descriptions_to_fit(self):
        lines = format_table(self.snapshots, Style(False), width=80).splitlines()
        self.assertEqual(len(lines), 3)
        self.assertTrue(all(len(line) <= 80 for line in lines), lines)
        self.assertTrue(lines[2].endswith("…"))

    def test_wraps_descriptions_when_asked(self):
        lines = format_table(self.snapshots, Style(False), width=80, wrap=True).splitlines()
        self.assertEqual(len(lines), 4)
        self.assertTrue(all(len(line) <= 80 for line in lines), lines)
        self.assertEqual(lines[3].strip(), "x" * (40 - len(lines[2].split()[-1])))

    def test_marks_kept_snapshots(self):
        lines = format_table(self.snapshots, Style(False), width=80).splitlines()
        self.assertTrue(lines[1].startswith("*1"))
        self.assertTrue(lines[2].startswith("12"))

    def test_colors(self):
        self.assertNotIn("\033[", format_table(self.snapshots, Style(False), width=80))
        self.assertIn("\033[", format_table(self.snapshots, Style(True), width=80))

    def test_empty(self):
        self.assertEqual(len(format_table([], Style(False), width=80).splitlines()), 1)


class ColorEnabledTests(unittest.TestCase):
    def test_explicit_settings(self):
        self.assertTrue(color_enabled("always", io.StringIO()))
        self.assertFalse(color_enabled("never", FakeTerminal()))

    def test_auto(self):
        with mock.patch.dict(os.environ, {"TERM": "xterm"}, clear=True):
            self.assertFalse(color_enabled("auto", io.StringIO()))
            self.assertTrue(color_enabled("auto", FakeTerminal()))
        with mock.patch.dict(os.environ, {"TERM": "xterm", "NO_COLOR": "1"}, clear=True):
            self.assertFalse(color_enabled("auto", FakeTerminal()))


if __name__ == "__main__":
    unittest.main()
