# SPDX-License-Identifier: GPL-3.0-or-later
import unittest

from support import make_snapshot

from btrfs_patrol.errors import PatrolError
from btrfs_patrol.selectors import select

SNAPSHOTS = [
    make_snapshot(1, created="2026-09-01T08:00:00+00:00", description="Before GNOME 49", keep=True),
    make_snapshot(2, created="2026-09-02T16:05:00+00:00", kind="dnf-pre",
                  kernel="6.16.9-200.fc44.x86_64"),
    make_snapshot(5, created="2026-09-03T16:30:00+00:00", kind="timer"),
    make_snapshot(7, created="2026-10-04T09:00:00+00:00", description="fresh install"),
]


def ids(snapshots):
    return [s.id for s in snapshots]


class IdSelectorTests(unittest.TestCase):
    def test_single_id(self):
        self.assertEqual(ids(select("5", SNAPSHOTS)), [5])

    def test_ids_and_ranges_are_merged_and_sorted(self):
        self.assertEqual(ids(select("7, 1-5, 2", SNAPSHOTS)), [1, 2, 5, 7])

    def test_reversed_range(self):
        self.assertEqual(ids(select("5-1", SNAPSHOTS)), [1, 2, 5])

    def test_range_selects_only_existing_ids(self):
        self.assertEqual(ids(select("3-6", SNAPSHOTS)), [5])
        self.assertEqual(select("100-200", SNAPSHOTS), [])

    def test_unknown_id_is_an_error(self):
        with self.assertRaisesRegex(PatrolError, "no snapshot with ID 3"):
            select("3", SNAPSHOTS)

    def test_invalid_selectors(self):
        for selector in ("", "1,,2", "abc", "-3", "1-", "1-2-3", "²"):
            with self.subTest(selector=selector), self.assertRaises(PatrolError):
                select(selector, SNAPSHOTS)


class FieldSelectorTests(unittest.TestCase):
    def test_description_ignores_case(self):
        self.assertEqual(ids(select("description=gnome", SNAPSHOTS)), [1])

    def test_date_time_kernel_kind(self):
        self.assertEqual(ids(select("date=2026-09", SNAPSHOTS)), [1, 2, 5])
        self.assertEqual(ids(select("time=16:", SNAPSHOTS)), [2, 5])
        self.assertEqual(ids(select("kernel=6.16", SNAPSHOTS)), [2])
        self.assertEqual(ids(select("kind=dnf", SNAPSHOTS)), [2])

    def test_text_may_contain_commas(self):
        self.assertEqual(select("description=a,b", SNAPSHOTS), [])

    def test_keep(self):
        self.assertEqual(ids(select("keep=yes", SNAPSHOTS)), [1])
        self.assertEqual(ids(select("keep=no", SNAPSHOTS)), [2, 5, 7])
        with self.assertRaises(PatrolError):
            select("keep=maybe", SNAPSHOTS)

    def test_unknown_field_or_empty_text(self):
        with self.assertRaisesRegex(PatrolError, "unknown selector field"):
            select("comment=x", SNAPSHOTS)
        with self.assertRaises(PatrolError):
            select("kernel=", SNAPSHOTS)


if __name__ == "__main__":
    unittest.main()
