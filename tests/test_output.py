# SPDX-License-Identifier: GPL-3.0-or-later
import io
import os
import unittest
from unittest import mock

from support import make_snapshot

from btrfs_patrol.output import (
    Style,
    color_enabled,
    display_width,
    format_table,
    printable,
    truncate,
    wrap_text,
)


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

    def test_subvolume_column_only_when_asked(self):
        snapshots = [*self.snapshots, make_snapshot(13, subvolume="home")]
        self.assertNotIn("SUBVOLUME", format_table(snapshots, Style(False), width=120))
        lines = format_table(snapshots, Style(False), width=120, show_subvolume=True).splitlines()
        self.assertIn("SUBVOLUME", lines[0])
        self.assertEqual(lines[1].split()[3], "root")
        self.assertEqual(lines[3].split()[3], "home")
        self.assertTrue(all(len(line) <= 120 for line in lines), lines)

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


class PrintableTests(unittest.TestCase):
    """Descriptions are written by people and by dnf, and printed to a terminal."""

    def test_a_newline_cannot_split_a_row(self):
        self.assertEqual(printable("line one\nline two"), "line one line two")

    def test_escape_sequences_are_not_left_executable(self):
        rendered = printable("\033[31mRED\033[0m and \033[2J")
        self.assertNotIn("\033", rendered, "the terminal must not obey a description")

    def test_bidi_overrides_are_removed(self):
        self.assertEqual(printable("safe\u202Egnahc"), "safegnahc")

    def test_ordinary_text_is_untouched(self):
        self.assertEqual(printable("upgrade kernel-core, mesa +2 more"),
                         "upgrade kernel-core, mesa +2 more")


class DisplayWidthTests(unittest.TestCase):
    """len() is not the number of terminal columns."""

    CJK = "安装 GNOME 49 之前的系统快照"

    def test_wide_characters_count_two_columns(self):
        # The point is the gap between the two, which is what len() got wrong.
        self.assertEqual(len(self.CJK), 19)
        self.assertEqual(display_width(self.CJK), 28)
        self.assertGreater(display_width(self.CJK), len(self.CJK))

    def test_combining_marks_count_nothing(self):
        self.assertEqual(display_width("e\u0301"), 1)

    def test_truncation_respects_columns_not_code_points(self):
        cut = truncate(self.CJK, 12)
        self.assertLessEqual(display_width(cut), 12)
        self.assertTrue(cut.endswith("…"))

    def test_a_table_row_fits_the_requested_width(self):
        snapshots = [make_snapshot(1, description=self.CJK * 4)]
        lines = format_table(snapshots, Style(False), width=80).splitlines()
        for line in lines:
            self.assertLessEqual(display_width(line), 80, line)

    def test_wrapping_respects_columns(self):
        for line in wrap_text(self.CJK * 3, 20):
            self.assertLessEqual(display_width(line), 20, line)
