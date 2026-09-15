# SPDX-License-Identifier: GPL-3.0-or-later
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from support import make_snapshot

from btrfs_patrol import btrfs
from btrfs_patrol.errors import PatrolError
from btrfs_patrol.snapshots import (
    COUNTER_FILE,
    INFO_FILE,
    Snapshot,
    SnapshotStore,
    select_for_pruning,
)


def fake_create_snapshot(source, destination, readonly=False):
    destination.mkdir()


def fake_delete_subvolume(path):
    path.rmdir()


class SnapshotJsonTests(unittest.TestCase):
    def test_round_trip(self):
        snapshot = make_snapshot(3, kind="dnf-pre", description="upgrade", keep=True)
        self.assertEqual(Snapshot.from_json(3, snapshot.to_json()), snapshot)

    def test_invalid_metadata(self):
        good = make_snapshot(3).to_json()
        for change in (
            {"format": 99},
            {"created": "yesterday"},
            {"created": None},
        ):
            data = {**good, **change}
            with self.subTest(change=change), self.assertRaises(PatrolError):
                Snapshot.from_json(3, data)
        del good["kernel"]
        with self.assertRaises(PatrolError):
            Snapshot.from_json(3, good)
        with self.assertRaises(PatrolError):
            Snapshot.from_json(3, [])

    def test_root_snapshots_keep_format_1(self):
        data = make_snapshot(3).to_json()
        self.assertEqual(data["format"], 1)
        self.assertNotIn("subvolume", data)
        self.assertEqual(Snapshot.from_json(3, data).subvolume, "root")

    def test_other_subvolumes_are_named_in_format_2(self):
        snapshot = make_snapshot(3, subvolume="home")
        data = snapshot.to_json()
        self.assertEqual((data["format"], data["subvolume"]), (2, "home"))
        self.assertEqual(Snapshot.from_json(3, data), snapshot)

    def test_format_2_needs_a_subvolume(self):
        data = make_snapshot(3, subvolume="home").to_json()
        for change in ({"subvolume": ""}, {"subvolume": 7}):
            with self.subTest(change=change), self.assertRaises(PatrolError):
                Snapshot.from_json(3, {**data, **change})
        del data["subvolume"]
        with self.assertRaises(PatrolError):
            Snapshot.from_json(3, data)


class PruneSelectionTests(unittest.TestCase):
    def test_kept_snapshots_are_never_pruned_and_do_not_count(self):
        snapshots = [
            make_snapshot(1, keep=True),
            make_snapshot(2),
            make_snapshot(3),
            make_snapshot(4, keep=True),
            make_snapshot(5),
        ]
        self.assertEqual([s.id for s in select_for_pruning(snapshots, 2)], [2])

    def test_nothing_to_prune(self):
        self.assertEqual(select_for_pruning([make_snapshot(1)], 1), [])

    def test_each_subvolume_is_counted_on_its_own(self):
        snapshots = [
            make_snapshot(1),
            make_snapshot(2, subvolume="home"),
            make_snapshot(3),
            make_snapshot(4, subvolume="home"),
            make_snapshot(5, subvolume="home"),
        ]
        self.assertEqual([s.id for s in select_for_pruning(snapshots, 1)], [1])
        self.assertEqual([s.id for s in select_for_pruning(snapshots, 1, "home")], [2, 4])


class SnapshotStoreTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.store = SnapshotStore(self.dir)
        patcher = mock.patch.multiple(
            btrfs,
            create_snapshot=mock.DEFAULT,
            delete_subvolume=mock.DEFAULT,
        )
        mocks = patcher.start()
        self.addCleanup(patcher.stop)
        mocks["create_snapshot"].side_effect = fake_create_snapshot
        mocks["delete_subvolume"].side_effect = fake_delete_subvolume
        self.btrfs = mocks

    def create(self, **kwargs):
        with self.store.lock():
            return self.store.create(Path("/"), kind=kwargs.pop("kind", "manual"), **kwargs)

    def test_ids_ignore_other_entries(self):
        for name in ("3", "10", "notes"):
            (self.dir / name).mkdir()
        (self.dir / "7").write_text("not a directory")
        (self.dir / ".lock").touch()
        (self.dir / COUNTER_FILE).write_text("11\n")
        self.assertEqual(self.store.ids(), [3, 10])

    def test_missing_directory(self):
        with self.assertRaisesRegex(PatrolError, "not found"):
            SnapshotStore(self.dir / "missing").ids()

    def test_save_and_load(self):
        snapshot = make_snapshot(4, description="hello")
        self.store.path(4).mkdir()
        self.store.save(snapshot)
        self.assertEqual(self.store.load(4), snapshot)
        self.assertEqual(sorted(p.name for p in self.store.path(4).iterdir()), [INFO_FILE])

    def test_load_errors(self):
        self.store.path(1).mkdir()
        with self.assertRaisesRegex(PatrolError, "has no info.json"):
            self.store.load(1)
        (self.store.path(1) / INFO_FILE).write_text("{")
        with self.assertRaisesRegex(PatrolError, "invalid JSON"):
            self.store.load(1)

    def test_create_numbers_snapshots_and_writes_metadata(self):
        first = self.create(description="one")
        second = self.create(kind="timer", keep=True)
        self.assertEqual((first.id, second.id), (1, 2))
        self.assertEqual(self.store.load_all(), [first, second])
        self.btrfs["create_snapshot"].assert_called_with(
            Path("/"), self.store.subvolume(2), readonly=True
        )
        data = json.loads((self.store.path(2) / INFO_FILE).read_text())
        self.assertEqual(data["kind"], "timer")
        self.assertEqual((self.dir / COUNTER_FILE).read_text(), "3\n")

    def test_failed_create_leaves_nothing_behind(self):
        self.btrfs["create_snapshot"].side_effect = PatrolError("boom")
        with self.assertRaisesRegex(PatrolError, "boom"):
            self.create()
        self.assertEqual(self.store.ids(), [])

    def test_ids_are_never_reused(self):
        self.create()
        newest = self.create()
        with self.store.lock():
            self.store.delete(newest.id)
        self.assertEqual(self.create().id, 3)

    def test_failed_create_does_not_reuse_its_id(self):
        self.btrfs["create_snapshot"].side_effect = PatrolError("boom")
        with self.assertRaises(PatrolError):
            self.create()
        self.btrfs["create_snapshot"].side_effect = fake_create_snapshot
        self.assertEqual(self.create().id, 2)

    def test_counter_catches_up_with_existing_snapshots(self):
        # A store from before the counter existed, or a stale counter restored from a backup.
        self.store.path(5).mkdir()
        self.store.save(make_snapshot(5))
        (self.dir / COUNTER_FILE).write_text("2\n")
        self.assertEqual(self.create().id, 6)
        self.assertEqual((self.dir / COUNTER_FILE).read_text(), "7\n")

    def test_invalid_counter(self):
        (self.dir / COUNTER_FILE).write_text("banana\n")
        with self.assertRaisesRegex(PatrolError, "expected a snapshot ID"):
            self.create()
        self.assertEqual(self.store.ids(), [])

    def test_delete(self):
        snapshot = self.create()
        with self.store.lock():
            self.store.delete(snapshot.id)
        self.assertFalse(self.store.path(snapshot.id).exists())
        self.btrfs["delete_subvolume"].assert_called_once_with(self.store.subvolume(snapshot.id))

    def test_prune_deletes_oldest_unkept(self):
        self.create(keep=True)
        for _ in range(3):
            self.create(kind="timer")
        with self.store.lock():
            pruned = self.store.prune(max_snapshots=2)
        self.assertEqual([s.id for s in pruned], [2])
        self.assertEqual(self.store.ids(), [1, 3, 4])

    def test_create_for_another_subvolume(self):
        snapshot = self.create(subvolume="home")
        self.assertEqual(self.store.load(snapshot.id).subvolume, "home")
        data = json.loads((self.store.path(snapshot.id) / INFO_FILE).read_text())
        self.assertEqual(data["subvolume"], "home")

    def test_prune_leaves_other_subvolumes_alone(self):
        self.create()
        self.create(subvolume="home")
        self.create()
        with self.store.lock():
            pruned = self.store.prune(max_snapshots=1)
        self.assertEqual([s.id for s in pruned], [1])
        self.assertEqual(self.store.ids(), [2, 3])

    def test_lock_requires_directory(self):
        with self.assertRaisesRegex(PatrolError, "not found"):
            with SnapshotStore(self.dir / "missing").lock():
                pass


if __name__ == "__main__":
    unittest.main()
