# SPDX-License-Identifier: GPL-3.0-or-later
import unittest
from unittest import mock

from btrfs_patrol import btrfs
from btrfs_patrol.btrfs import Subvolume, parse_subvolume_list
from btrfs_patrol.errors import PatrolError


class ParseSubvolumeListTests(unittest.TestCase):
    def test_parses_ids_and_paths(self):
        output = (
            "ID 256 gen 9123 top level 5 path root\n"
            "ID 257 gen 9120 top level 5 path home\n"
            "ID 300 gen 9000 top level 258 path snapshots/1/snapshot\n"
            "ID 301 gen 12 top level 5 path my data\n"
        )
        self.assertEqual(
            parse_subvolume_list(output),
            [
                Subvolume(256, "root"),
                Subvolume(257, "home"),
                Subvolume(300, "snapshots/1/snapshot"),
                Subvolume(301, "my data"),
            ],
        )

    def test_empty_output(self):
        self.assertEqual(parse_subvolume_list(""), [])

    def test_unexpected_output(self):
        with self.assertRaises(PatrolError):
            parse_subvolume_list("ERROR: can't access '/'\n")


class RunTests(unittest.TestCase):
    def test_missing_binary(self):
        with mock.patch.object(btrfs, "BTRFS", "btrfs-patrol-no-such-binary"):
            with self.assertRaisesRegex(PatrolError, "not found"):
                btrfs.run("subvolume", "list", "/")

    def test_failure_reports_stderr(self):
        # Stand in for btrfs with sh, so the arguments become a shell script.
        with mock.patch.object(btrfs, "BTRFS", "sh"):
            with self.assertRaisesRegex(PatrolError, "boom"):
                btrfs.run("-c", "echo boom >&2; exit 3")
            self.assertEqual(btrfs.run("-c", "echo ok"), "ok\n")


if __name__ == "__main__":
    unittest.main()
