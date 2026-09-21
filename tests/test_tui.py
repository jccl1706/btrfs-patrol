# SPDX-License-Identifier: GPL-3.0-or-later
"""The terminal interface's controller: what each key does, against a real store.

Nothing here opens a terminal. `Controller` is the half of tui.py that decides
and acts; `run()` is the half that talks to curses, and it is deliberately thin
enough that this covers what matters.

btrfs itself is faked, as tests/test_snapshots.py does: a snapshot is a
directory, so the store's bookkeeping is real while nothing needs a filesystem
that can make subvolumes.
"""

import curses
import io
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from btrfs_patrol import btrfs
from btrfs_patrol import config as config_mod
from btrfs_patrol import tui
from btrfs_patrol.cli import App
from btrfs_patrol.errors import PatrolError
from btrfs_patrol.output import Console
from btrfs_patrol.screen import ALL, Mode, Screen
from btrfs_patrol.snapshots import Snapshot, SnapshotStore

KERNEL = "7.2.4-200.fc44.x86_64"


def fake_create_snapshot(source, destination, readonly=False):
    destination.mkdir()


def fake_delete_subvolume(path):
    path.rmdir()


class ControllerTestCase(unittest.TestCase):
    """A controller over a real store in a temporary directory."""

    subvolumes = None

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.store = SnapshotStore(self.dir)

        patcher = mock.patch.multiple(
            btrfs, create_snapshot=mock.DEFAULT, delete_subvolume=mock.DEFAULT
        )
        mocks = patcher.start()
        self.addCleanup(patcher.stop)
        mocks["create_snapshot"].side_effect = fake_create_snapshot
        mocks["delete_subvolume"].side_effect = fake_delete_subvolume

        # Nothing in these tests should reach the boot partition or the mount
        # table; each is covered where it belongs.
        for name in ("sync_boot_entries", "mounted_snapshots", "pending_rollbacks"):
            target = mock.patch.object(
                tui, name, return_value={} if name != "sync_boot_entries" else None
            )
            setattr(self, name, target.start())
            self.addCleanup(target.stop)
        require = mock.patch.object(tui.system, "require_mounted", return_value=None)
        require.start()
        self.addCleanup(require.stop)

        data = {"subvolumes": self.subvolumes} if self.subvolumes else {}
        self.config = config_mod.parse(data)
        self.app = App(
            config=self.config,
            store=self.store,
            console=Console("never", out=io.StringIO(), err=io.StringIO()),
        )
        self.screen = Screen(config=self.config, snapshots=[], width=80, height=20)
        self.controller = tui.Controller(app=self.app, screen=self.screen)

    def add(self, description="", keep=False, subvolume="root"):
        with self.store.lock():
            return self.store.create(
                self.dir, kind="manual", description=description, keep=keep, subvolume=subvolume
            )

    def press(self, action, character=""):
        return self.controller.dispatch(action, character)

    def type_text(self, text):
        for character in text:
            self.press(None, character)


class KeyMappingTests(unittest.TestCase):
    def test_arrows_and_pages(self):
        self.assertEqual(tui.action_for(curses.KEY_UP), tui.UP)
        self.assertEqual(tui.action_for(curses.KEY_DOWN), tui.DOWN)
        self.assertEqual(tui.action_for(curses.KEY_NPAGE), tui.PAGE_DOWN)
        self.assertEqual(tui.action_for(curses.KEY_HOME), tui.HOME)

    def test_letters(self):
        self.assertEqual(tui.action_for(ord("q")), tui.QUIT)
        self.assertEqual(tui.action_for(ord("x")), tui.DELETE)
        self.assertEqual(tui.action_for(ord("/")), tui.FILTER)

    def test_tab_and_escape(self):
        self.assertEqual(tui.action_for(9), tui.NEXT_SUBVOLUME)
        self.assertEqual(tui.action_for(curses.KEY_BTAB), tui.PREV_SUBVOLUME)
        self.assertEqual(tui.action_for(27), tui.CANCEL)

    def test_a_key_that_means_nothing_here(self):
        self.assertIsNone(tui.action_for(ord("Z")))
        self.assertIsNone(tui.action_for(-1))


class NavigationTests(ControllerTestCase):
    def setUp(self):
        super().setUp()
        for i in range(4):
            self.add(description=f"snapshot {i}")
        self.controller.reload()

    def test_quit_stops_the_loop(self):
        self.assertFalse(self.press(tui.QUIT))

    def test_anything_else_keeps_it_running(self):
        self.assertTrue(self.press(tui.DOWN))

    def test_moving(self):
        first = self.screen.current.id
        self.press(tui.DOWN)
        self.assertNotEqual(self.screen.current.id, first)
        self.press(tui.UP)
        self.assertEqual(self.screen.current.id, first)

    def test_end_and_home(self):
        self.press(tui.END)
        self.assertEqual(self.screen.current.id, max(s.id for s in self.screen.visible))
        self.press(tui.HOME)
        self.assertEqual(self.screen.current.id, min(s.id for s in self.screen.visible))

    def test_enter_opens_the_detail_page_and_any_key_leaves_it(self):
        self.press(tui.OPEN)
        self.assertIs(self.screen.mode, Mode.DETAIL)
        self.assertIn("description", "\n".join(self.screen.render()))
        self.press(tui.DOWN)
        self.assertIs(self.screen.mode, Mode.BROWSE)

    def test_help_opens_and_closes(self):
        self.press(tui.HELP)
        self.assertIs(self.screen.mode, Mode.HELP)
        self.press(tui.QUIT)  # any key leaves help, and does NOT quit
        self.assertIs(self.screen.mode, Mode.BROWSE)

    def test_help_swallows_the_key_rather_than_quitting(self):
        self.press(tui.HELP)
        self.assertTrue(self.press(tui.QUIT))


class KeepTests(ControllerTestCase):
    def setUp(self):
        super().setUp()
        self.snapshot = self.add(description="one")
        self.controller.reload()

    def test_k_toggles_and_persists(self):
        self.press(tui.KEEP)
        self.assertTrue(self.store.load(self.snapshot.id).keep)
        self.press(tui.KEEP)
        self.assertFalse(self.store.load(self.snapshot.id).keep)

    def test_it_says_what_happened(self):
        self.press(tui.KEEP)
        self.assertIn("will be kept", self.screen.message)

    def test_the_cursor_stays_on_the_same_snapshot(self):
        self.press(tui.KEEP)
        self.assertEqual(self.screen.current.id, self.snapshot.id)


class DescribeTests(ControllerTestCase):
    def setUp(self):
        super().setUp()
        self.snapshot = self.add(description="before")
        self.controller.reload()

    def test_d_edits_starting_from_what_is_there(self):
        self.press(tui.DESCRIBE)
        self.assertIs(self.screen.mode, Mode.INPUT)
        self.assertEqual(self.screen.input_buffer, "before")

    def test_accepting_saves(self):
        self.press(tui.DESCRIBE)
        for _ in range(len("before")):
            self.press(tui.BACKSPACE)
        self.type_text("after")
        self.press(tui.ACCEPT)
        self.assertEqual(self.store.load(self.snapshot.id).description, "after")
        self.assertIs(self.screen.mode, Mode.BROWSE)

    def test_cancelling_changes_nothing(self):
        self.press(tui.DESCRIBE)
        self.type_text("junk")
        self.press(tui.CANCEL)
        self.assertEqual(self.store.load(self.snapshot.id).description, "before")
        self.assertIs(self.screen.mode, Mode.BROWSE)


class NewSnapshotTests(ControllerTestCase):
    subvolumes = {"home": {"path": "/home"}}

    def test_n_takes_one_with_the_typed_description(self):
        self.press(tui.NEW)
        self.type_text("by hand")
        self.press(tui.ACCEPT)
        [snapshot] = self.store.load_all()
        self.assertEqual(snapshot.description, "by hand")
        self.assertEqual(snapshot.subvolume, "root")
        self.assertIn("took snapshot", self.screen.message)

    def test_it_is_of_the_subvolume_being_shown(self):
        self.screen.subvolume = "home"
        self.press(tui.NEW)
        self.type_text("of home")
        self.press(tui.ACCEPT)
        [snapshot] = self.store.load_all()
        self.assertEqual(snapshot.subvolume, "home")

    def test_showing_everything_takes_one_of_root_rather_than_several(self):
        # One key should not take three snapshots.
        self.screen.subvolume = ALL
        self.press(tui.NEW)
        self.press(tui.ACCEPT)
        self.assertEqual([s.subvolume for s in self.store.load_all()], ["root"])

    def test_a_pending_rollback_refuses_rather_than_crashing(self):
        self.pending_rollbacks.return_value = {"root": 7}
        self.press(tui.NEW)
        self.press(tui.ACCEPT)
        self.assertEqual(self.store.load_all(), [])
        self.assertTrue(self.screen.message_is_error)
        self.assertIn("reboot first", self.screen.message)

    def test_an_unmounted_store_refuses(self):
        with mock.patch.object(
            tui.system, "require_mounted", side_effect=PatrolError("not mounted")
        ):
            self.press(tui.NEW)
            self.press(tui.ACCEPT)
        self.assertEqual(self.store.load_all(), [])
        self.assertTrue(self.screen.message_is_error)

    def test_the_cursor_lands_on_the_new_snapshot(self):
        self.add(description="older")
        self.controller.reload()
        self.press(tui.NEW)
        self.type_text("newest")
        self.press(tui.ACCEPT)
        self.assertEqual(self.screen.current.description, "newest")


class DeleteTests(ControllerTestCase):
    def setUp(self):
        super().setUp()
        self.one = self.add(description="one")
        self.two = self.add(description="two")
        self.controller.reload()

    def test_x_asks_before_deleting(self):
        self.press(tui.DELETE)
        self.assertIs(self.screen.mode, Mode.CONFIRM)
        self.assertEqual(len(self.store.load_all()), 2)
        self.assertIn(f"Delete snapshot {self.screen.current.id}", self.screen.message)

    def test_confirming_deletes(self):
        target = self.screen.current.id
        self.press(tui.DELETE)
        self.press(tui.YES)
        self.assertNotIn(target, self.store.ids())
        self.assertIn(f"deleted snapshot {target}", self.screen.message)

    def test_declining_keeps_it(self):
        self.press(tui.DELETE)
        self.press(tui.NO)
        self.assertEqual(len(self.store.load_all()), 2)
        self.assertIs(self.screen.mode, Mode.BROWSE)

    def test_the_confirmation_warns_that_it_is_kept(self):
        with self.store.lock():
            kept = self.store.load(self.one.id)
            kept.keep = True
            self.store.save(kept)
        self.controller.reload(keep_id=self.one.id)
        self.press(tui.DELETE)
        self.assertIn("(kept)", self.screen.message)

    def test_it_refuses_the_snapshot_the_system_is_running_from(self):
        # The same rule cmd_delete enforces: deleting it aims
        # 'btrfs subvolume delete' at the running system.
        self.mounted_snapshots.return_value = {"root": self.screen.current.id}
        self.press(tui.DELETE)
        self.assertIs(self.screen.mode, Mode.BROWSE)
        self.assertTrue(self.screen.message_is_error)
        self.assertIn("running from", self.screen.message)
        self.assertEqual(len(self.store.load_all()), 2)

    def test_deleting_the_last_one_leaves_an_empty_screen_that_still_renders(self):
        self.press(tui.DELETE)
        self.press(tui.YES)
        self.press(tui.DELETE)
        self.press(tui.YES)
        self.assertEqual(self.store.load_all(), [])
        self.assertIsNone(self.screen.current)
        self.screen.render()


class FilterTests(ControllerTestCase):
    def setUp(self):
        super().setUp()
        self.add(description="one")
        self.add(description="two")
        self.controller.reload()

    def test_slash_collects_a_selector_and_applies_it(self):
        self.press(tui.FILTER)
        self.type_text("1")
        self.press(tui.ACCEPT)
        self.assertEqual(self.screen.query, "1")
        self.assertEqual([s.id for s in self.screen.visible], [1])

    def test_it_narrows_as_it_is_typed(self):
        self.press(tui.FILTER)
        self.type_text("2")
        self.assertEqual([s.id for s in self.screen.visible], [2])

    def test_escape_clears_it_again(self):
        self.press(tui.FILTER)
        self.type_text("1")
        self.press(tui.CANCEL)
        self.assertEqual(self.screen.query, "")
        self.assertEqual(len(self.screen.visible), 2)

    def test_escape_while_browsing_clears_an_applied_filter(self):
        self.press(tui.FILTER)
        self.type_text("1")
        self.press(tui.ACCEPT)
        self.press(tui.CANCEL)
        self.assertEqual(self.screen.query, "")


class ReloadTests(ControllerTestCase):
    def test_r_picks_up_a_snapshot_taken_elsewhere(self):
        self.add(description="one")
        self.controller.reload()
        self.assertEqual(len(self.screen.visible), 1)
        self.add(description="taken by dnf")
        self.press(tui.RELOAD)
        self.assertEqual(len(self.screen.visible), 2)
        self.assertIn("reloaded", self.screen.message)


if __name__ == "__main__":
    unittest.main()


class FakePlan:
    """Stands in for a RollbackPlan: the controller only reads and executes it."""

    def __init__(self, target, warnings=(), fail=None):
        self.target = target
        self.warnings = list(warnings)
        self.name = "root"
        self.path = Path("/")
        self.mounted = True
        self.fail = fail
        self.executed = False

    def describe(self):
        return ["  root subvolume:     root (ID 256) on /dev/vda2", "  running kernel:     ok"]

    def execute(self):
        if self.fail:
            raise self.fail
        self.executed = True
        return Snapshot(
            id=99, created=datetime(2026, 9, 21, 12, 0, 0), kernel=KERNEL,
            kind="rollback", description="state before rollback", keep=True,
        )


class RollbackTests(ControllerTestCase):
    def setUp(self):
        super().setUp()
        self.snapshot = self.add(description="the good one")
        self.controller.reload()
        self.closed = False

    def arrange(self, plan=None, error=None):
        """Patch rollback.prepare, recording whether its context was closed."""
        outer = self

        import contextlib

        @contextlib.contextmanager
        def fake_prepare(config, store, target):
            if error is not None:
                raise error
            try:
                yield plan
            finally:
                outer.closed = True

        patcher = mock.patch.object(tui.rollback_mod, "prepare", fake_prepare)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_r_shows_the_plan_rather_than_acting(self):
        plan = FakePlan(self.snapshot, warnings=["/home is mounted from a snapshot"])
        self.arrange(plan)
        self.press(tui.ROLLBACK)
        self.assertIs(self.screen.mode, Mode.PAGE)
        text = "\n".join(self.screen.render())
        self.assertIn("root subvolume", text)
        self.assertIn("/home is mounted from a snapshot", text)
        self.assertFalse(plan.executed)

    def test_y_rolls_back_and_releases_the_mount(self):
        plan = FakePlan(self.snapshot)
        self.arrange(plan)
        self.press(tui.ROLLBACK)
        self.press(tui.YES)
        self.assertTrue(plan.executed)
        self.assertTrue(self.closed, "the top-level subvolume was left mounted")
        self.assertIn("reboot", "\n".join(self.screen.render()))

    def test_any_other_key_cancels_and_releases_the_mount(self):
        plan = FakePlan(self.snapshot)
        self.arrange(plan)
        self.press(tui.ROLLBACK)
        self.press(tui.NO)
        self.assertFalse(plan.executed)
        self.assertTrue(self.closed, "the top-level subvolume was left mounted")
        self.assertIs(self.screen.mode, Mode.BROWSE)
        self.assertIn("nothing rolled back", self.screen.message)

    def test_a_refused_rollback_says_why_and_stays_put(self):
        # prepare() refuses a snapshot whose kernel has no modules here.
        self.arrange(error=PatrolError("snapshot 1 has no kernel modules for 7.2.4"))
        self.press(tui.ROLLBACK)
        self.assertIs(self.screen.mode, Mode.BROWSE)
        self.assertTrue(self.screen.message_is_error)
        self.assertIn("no kernel modules", self.screen.message)

    def test_a_failure_while_executing_releases_the_mount_too(self):
        plan = FakePlan(self.snapshot, fail=PatrolError("the default subvolume did not change"))
        self.arrange(plan)
        self.press(tui.ROLLBACK)
        self.press(tui.YES)
        self.assertTrue(self.closed, "the top-level subvolume was left mounted")
        self.assertTrue(self.screen.message_is_error)
        self.assertIn("did not change", self.screen.message)

    def test_it_does_nothing_with_an_empty_list(self):
        self.arrange(FakePlan(self.snapshot))
        self.screen.replace([])
        self.press(tui.ROLLBACK)
        self.assertIs(self.screen.mode, Mode.BROWSE)


class ModeAwareKeyTests(unittest.TestCase):
    """The same key means different things in different modes.

    This is the layer the rollback bug slipped through: the controller tests
    hand YES straight to dispatch(), so a y that never became YES in the input
    loop passed every one of them.
    """

    def test_y_confirms_on_a_confirmation(self):
        self.assertEqual(tui.action_for(ord("y"), Mode.CONFIRM), tui.YES)
        self.assertEqual(tui.action_for(ord("Y"), Mode.CONFIRM), tui.YES)

    def test_y_confirms_on_a_rollback_plan_too(self):
        self.assertEqual(tui.action_for(ord("y"), Mode.PAGE), tui.YES)

    def test_anything_else_declines_rather_than_doing_nothing(self):
        for key in (ord("n"), ord("z"), 27):
            self.assertEqual(tui.action_for(key, Mode.PAGE), tui.NO)
            self.assertEqual(tui.action_for(key, Mode.CONFIRM), tui.NO)

    def test_y_is_not_an_answer_while_browsing(self):
        self.assertIsNone(tui.action_for(ord("y"), Mode.BROWSE))

    def test_typing_keys_are_recognised_while_entering_text(self):
        self.assertEqual(tui.action_for(10, Mode.INPUT), tui.ACCEPT)
        self.assertEqual(tui.action_for(27, Mode.INPUT), tui.CANCEL)
        self.assertEqual(tui.action_for(127, Mode.INPUT), tui.BACKSPACE)

    def test_a_letter_while_typing_is_not_an_action(self):
        # It is collected as a character instead; see _loop.
        self.assertIsNone(tui.action_for(ord("q"), Mode.INPUT))
