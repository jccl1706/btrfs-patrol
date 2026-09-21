# SPDX-License-Identifier: GPL-3.0-or-later
"""Reading a btrfs send stream as a list of changes.

The fixture in data/ is a REAL stream, captured from two snapshots whose
differences were arranged in advance: a file added, one deleted, one modified,
one renamed, a directory added and one removed, a symlink created and a mode
changed. Every expectation here is that known list, so these tests say whether
the interpretation is right rather than whether it still does what it did.
"""

import unittest
from pathlib import Path

from btrfs_patrol.diff import (
    Change,
    Comparison,
    Entry,
    interpret,
    parse,
    parse_fields,
    strip_subvolume,
)

DUMP = (Path(__file__).resolve().parent / "data" / "receive-dump-known-changes.txt").read_text()


def paths(entries, change):
    return sorted(e.path for e in entries if e.change is change)


class KnownStreamTests(unittest.TestCase):
    """Against the real stream, with the changes that were actually made."""

    def setUp(self):
        self.entries = interpret(DUMP)

    def test_the_added_files_are_the_ones_that_were_added(self):
        self.assertEqual(
            paths(self.entries, Change.ADDED),
            ["difftest/added-dir", "difftest/added.txt", "difftest/link.txt"],
        )

    def test_the_removed_files_are_the_ones_that_were_removed(self):
        self.assertEqual(
            paths(self.entries, Change.REMOVED),
            ["difftest/deleted-dir", "difftest/deleted.txt"],
        )

    def test_the_modified_file_is_the_one_that_was_modified(self):
        self.assertEqual(paths(self.entries, Change.MODIFIED), ["difftest/modified.txt"])

    def test_the_rename_is_one_entry_naming_both_ends(self):
        [renamed] = [e for e in self.entries if e.change is Change.RENAMED]
        self.assertEqual(renamed.old_path, "difftest/oldname.txt")
        self.assertEqual(renamed.path, "difftest/newname.txt")

    def test_a_rename_is_not_also_an_addition_and_a_deletion(self):
        # It arrives as link + unlink; read literally that is two changes.
        self.assertNotIn("difftest/newname.txt", paths(self.entries, Change.ADDED))
        self.assertNotIn("difftest/oldname.txt", paths(self.entries, Change.REMOVED))

    def test_the_mode_change_is_reported_as_attributes(self):
        self.assertEqual(paths(self.entries, Change.ATTRS), ["difftest/unchanged.txt"])

    def test_no_temporary_names_reach_the_output(self):
        # New files are created as o<inode>-<transid>-<n> and renamed into
        # place; that name is nobody's filename.
        for entry in self.entries:
            self.assertNotRegex(entry.path, r"\bo\d+-\d+-\d+\b")
            self.assertNotRegex(entry.old_path or "x", r"\bo\d+-\d+-\d+\b")

    def test_nothing_keeps_the_subvolume_prefix(self):
        for entry in self.entries:
            self.assertFalse(entry.path.startswith("./"), entry.path)
            self.assertFalse(entry.path.startswith("snapshot/"), entry.path)

    def test_a_binary_that_was_merely_run_is_not_a_change(self):
        # /usr/bin/rmdir has a utimes line because running rmdir is how a
        # directory got deleted. Its atime moved; nothing about it changed.
        self.assertNotIn("usr/bin/rmdir", [e.path for e in self.entries])

    def test_a_parent_directory_is_not_a_change_because_a_child_changed(self):
        self.assertNotIn("difftest", [e.path for e in self.entries])

    def test_the_whole_list_is_exactly_what_was_done(self):
        self.assertEqual(
            sorted(e.describe() for e in self.entries),
            sorted(
                [
                    "+ difftest/added-dir",
                    "+ difftest/added.txt",
                    "+ difftest/link.txt",
                    "- difftest/deleted-dir",
                    "- difftest/deleted.txt",
                    "~ difftest/modified.txt",
                    "> difftest/oldname.txt -> difftest/newname.txt",
                    "a difftest/unchanged.txt",
                ]
            ),
        )


class ParsingTests(unittest.TestCase):
    def test_a_plain_line(self):
        [(command, path, fields)] = parse("unlink          ./snapshot/difftest/deleted.txt")
        self.assertEqual(command, "unlink")
        self.assertEqual(path, "./snapshot/difftest/deleted.txt")
        self.assertEqual(fields, {})

    def test_fields_are_read(self):
        [(command, path, fields)] = parse("chmod  ./snapshot/x mode=600")
        self.assertEqual(command, "chmod")
        self.assertEqual(path, "./snapshot/x")
        self.assertEqual(fields["mode"], "600")

    def test_a_value_containing_spaces(self):
        # An SELinux context has them, so splitting on whitespace loses the end.
        line = "set_xattr  ./snapshot/x  name=security.selinux value=a b c len=35"
        [(_, _, fields)] = parse(line)
        self.assertEqual(fields["value"], "a b c")
        self.assertEqual(fields["len"], "35")

    def test_blank_lines_are_skipped(self):
        self.assertEqual(parse("\n\n"), [])

    def test_parse_fields_on_its_own(self):
        self.assertEqual(parse_fields("uuid=abc transid=55"), {"uuid": "abc", "transid": "55"})
        self.assertEqual(parse_fields(""), {})


class StripSubvolumeTests(unittest.TestCase):
    def test_removes_the_leading_component(self):
        self.assertEqual(strip_subvolume("./snapshot/difftest/x"), "difftest/x")

    def test_the_subvolume_itself_becomes_empty(self):
        self.assertEqual(strip_subvolume("./snapshot"), "")

    def test_a_path_with_no_prefix_is_left_alone_but_shortened(self):
        self.assertEqual(strip_subvolume("./x/y"), "y")


class ComparisonTests(unittest.TestCase):
    def setUp(self):
        self.comparison = Comparison(older_id=1, newer_id=2, entries=interpret(DUMP))

    def test_summary_counts_each_kind(self):
        summary = self.comparison.summary()
        self.assertIn("3 added", summary)
        self.assertIn("2 removed", summary)
        self.assertIn("1 renamed", summary)

    def test_an_identical_pair_says_so(self):
        empty = Comparison(older_id=1, newer_id=2, entries=[])
        self.assertTrue(empty.empty)
        self.assertEqual(empty.summary(), "no differences")
        self.assertEqual(empty.lines(), [])

    def test_lines_are_grouped_by_kind_then_sorted(self):
        lines = self.comparison.lines()
        self.assertTrue(lines[0].startswith("+"))
        self.assertTrue(lines[-1].startswith("a"))
        added = [line for line in lines if line.startswith("+")]
        self.assertEqual(added, sorted(added))

    def test_of_returns_only_that_kind(self):
        self.assertTrue(all(e.change is Change.ADDED for e in self.comparison.of(Change.ADDED)))


class EntryTests(unittest.TestCase):
    def test_a_rename_names_both_ends(self):
        entry = Entry(Change.RENAMED, "new", old_path="old")
        self.assertEqual(entry.describe(), "> old -> new")

    def test_everything_else_names_one(self):
        self.assertEqual(Entry(Change.ADDED, "x").describe(), "+ x")
        self.assertEqual(Entry(Change.REMOVED, "x").describe(), "- x")
        self.assertEqual(Entry(Change.MODIFIED, "x").describe(), "~ x")


class EmptyStreamTests(unittest.TestCase):
    def test_a_stream_with_only_a_header_finds_nothing(self):
        self.assertEqual(interpret("snapshot  ./snapshot  uuid=x transid=1"), [])

    def test_an_empty_string(self):
        self.assertEqual(interpret(""), [])


if __name__ == "__main__":
    unittest.main()
