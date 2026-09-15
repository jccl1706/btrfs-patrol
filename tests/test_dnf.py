# SPDX-License-Identifier: GPL-3.0-or-later
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from btrfs_patrol import btrfs, dnf, system
from btrfs_patrol.errors import PatrolError
from btrfs_patrol.output import Console
from btrfs_patrol.snapshots import SnapshotStore
from btrfs_patrol.system import Mount

OK_LOG = {"op": "reply", "requested_op": "log", "domain": "log", "status": "OK"}


def trans_reply(*packages):
    return {
        "op": "reply", "requested_op": "get", "domain": "trans_packages", "status": "OK",
        "return": {"trans_packages": [{"name": n, "action": a} for n, a in packages]},
    }


def fake_create_snapshot(source, destination, readonly=False):
    destination.mkdir()


class DescribeTransactionTests(unittest.TestCase):
    def test_groups_by_action_in_a_fixed_order(self):
        packages = [
            {"name": "tree", "action": "I"},
            {"name": "foo", "action": "E"},
            {"name": "kernel-core", "action": "U"},
            {"name": "kernel-core", "action": "O"},
            {"name": "bash", "action": "?"},
        ]
        self.assertEqual(
            dnf.describe_transaction(packages), "upgrade kernel-core; install tree; remove foo"
        )

    def test_long_lists_are_counted(self):
        packages = [{"name": f"pkg{i}", "action": "U"} for i in range(5)]
        packages.append({"name": "pkg0", "action": "U"})  # another arch of the same package
        self.assertEqual(dnf.describe_transaction(packages), "upgrade pkg0, pkg1, pkg2 +2 more")

    def test_nothing_describable(self):
        self.assertEqual(dnf.describe_transaction([{"name": "bash", "action": "?"}, {"x": 1}]), "")


class DnfHookTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name)
        self.snapshots_dir = base / "snapshots"
        self.snapshots_dir.mkdir()
        self.store = SnapshotStore(self.snapshots_dir)
        self.config = base / "config.toml"
        self.write_config()
        # Never run the real btrfs from tests.
        patcher = mock.patch.multiple(btrfs, create_snapshot=mock.DEFAULT, delete_subvolume=mock.DEFAULT)
        mocks = patcher.start()
        self.addCleanup(patcher.stop)
        self.create_snapshot = mocks["create_snapshot"]
        self.create_snapshot.side_effect = fake_create_snapshot
        mocks["delete_subvolume"].side_effect = lambda path: path.rmdir()
        self.mounts = [Mount("/root", "/", "btrfs", "/dev/vda2")]
        patcher = mock.patch.object(system, "read_mounts", side_effect=lambda: self.mounts)
        patcher.start()
        self.addCleanup(patcher.stop)

    def write_config(self, extra=""):
        self.config.write_text(f'[filesystem]\nsnapshots_dir = "{self.snapshots_dir}"\n{extra}')

    def run_hook(self, phase, *replies):
        stdin = io.StringIO("".join(json.dumps(r) + "\n" for r in replies))
        stdout, err = io.StringIO(), io.StringIO()
        code = dnf.run_hook(phase, self.config, Console("never", err=err), stdin=stdin, stdout=stdout)
        self.assertEqual(code, 0)
        requests = [json.loads(line) for line in stdout.getvalue().splitlines()]
        return requests, err.getvalue()

    def snapshot_ids(self):
        return self.store.ids()

    def test_pre_snapshot_describes_the_transaction(self):
        requests, err = self.run_hook(
            "pre", trans_reply(("tree", "I"), ("kernel-core", "U")), OK_LOG
        )
        self.assertEqual(
            requests[0],
            {"op": "get", "domain": "trans_packages", "args": {"output": ["name", "action"]}},
        )
        self.assertEqual(
            requests[1],
            {"op": "log", "args": {
                "level": "INFO",
                "message": "btrfs-patrol: created snapshot 1 (dnf-pre): upgrade kernel-core; install tree",
            }},
        )
        self.assertEqual(err, "")
        self.assertEqual(self.store.load(1).description, "upgrade kernel-core; install tree")

    def test_plugin_that_does_not_answer_still_gets_a_snapshot(self):
        requests, err = self.run_hook("pre")
        self.assertEqual(self.store.load(1).description, dnf.FALLBACK_DESCRIPTION)
        # After the first missing reply, nothing more is sent.
        self.assertEqual([r["op"] for r in requests], ["get"])
        self.assertEqual(err, "")

    def test_error_reply_falls_back_to_the_generic_description(self):
        error = {"op": "reply", "requested_op": "get", "domain": "trans_packages",
                 "status": "ERROR", "message": "not available here"}
        self.run_hook("pre", error, OK_LOG)
        self.assertEqual(self.store.load(1).description, dnf.FALLBACK_DESCRIPTION)

    def test_post_snapshot_is_off_by_default(self):
        requests, err = self.run_hook("post")
        self.assertEqual((requests, err), ([], ""))
        self.assertEqual(self.snapshot_ids(), [])

    def test_missing_config_is_silent(self):
        self.config.unlink()
        self.assertEqual(self.run_hook("pre"), ([], ""))

    def test_failure_is_a_single_warning(self):
        self.create_snapshot.side_effect = PatrolError("btrfs failed\nsecond line")
        requests, err = self.run_hook("pre", trans_reply(("tree", "I")), OK_LOG)
        self.assertEqual(requests[-1]["args"]["level"], "WARNING")
        self.assertIn("btrfs failed second line", requests[-1]["args"]["message"])
        self.assertIn("btrfs failed", err)
        self.assertEqual(self.snapshot_ids(), [])

    def test_pruning_is_reported(self):
        self.write_config("[retention]\nmax_snapshots = 1\n")
        self.run_hook("pre", trans_reply(("a", "I")), OK_LOG)
        requests, _ = self.run_hook("pre", trans_reply(("b", "I")), OK_LOG)
        self.assertEqual(
            requests[-1]["args"]["message"],
            "btrfs-patrol: created snapshot 2 (dnf-pre): install b; pruned 1",
        )

    def test_pending_rollback_skips_the_snapshot(self):
        self.mounts = [Mount("/snapshots/8/snapshot", "/", "btrfs", "/dev/vda2")]
        requests, err = self.run_hook("pre", OK_LOG)
        self.assertEqual([r["op"] for r in requests], ["log"])
        self.assertIn("waiting for a reboot", requests[0]["args"]["message"])
        self.assertIn("snapshot 8", err)
        self.assertEqual(self.snapshot_ids(), [])


if __name__ == "__main__":
    unittest.main()
