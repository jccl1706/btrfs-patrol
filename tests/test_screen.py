# SPDX-License-Identifier: GPL-3.0-or-later
"""The terminal interface's view model: movement, filtering, and what it renders.

Every one of these presses keys and asserts on plain strings. Nothing here
imports curses, which is the point of screen.py being a separate module.
"""

import unittest
from datetime import datetime

from btrfs_patrol import config as config_mod
from btrfs_patrol.output import display_width
from btrfs_patrol.screen import ALL, CHROME_HEIGHT, Glyphs, Ink, Mode, Screen
from btrfs_patrol.snapshots import Snapshot

KERNEL = "7.2.4-200.fc44.x86_64"


def snap(snapshot_id, subvolume="root", keep=False, description="", kind="manual", day=15):
    return Snapshot(
        id=snapshot_id,
        created=datetime(2026, 9, day, 11, 10, snapshot_id),
        kernel=KERNEL,
        kind=kind,
        description=description,
        keep=keep,
        subvolume=subvolume,
    )


def screen(snapshots=None, subvolumes=None, width=80, height=20):
    data = {"subvolumes": subvolumes} if subvolumes else {}
    return Screen(
        config=config_mod.parse(data),
        snapshots=list(snapshots if snapshots is not None else []),
        width=width,
        height=height,
    )


class MovementTests(unittest.TestCase):
    def setUp(self):
        self.screen = screen([snap(i, day=15 + i) for i in range(1, 6)])

    def test_starts_on_the_first_row(self):
        self.assertEqual(self.screen.current.id, 1)

    def test_moves_down_and_up(self):
        self.screen.move(2)
        self.assertEqual(self.screen.current.id, 3)
        self.screen.move(-1)
        self.assertEqual(self.screen.current.id, 2)

    def test_stops_at_the_ends_rather_than_wrapping(self):
        self.screen.move(-5)
        self.assertEqual(self.screen.current.id, 1)
        self.screen.move(99)
        self.assertEqual(self.screen.current.id, 5)

    def test_an_empty_list_has_no_current_snapshot(self):
        empty = screen([])
        self.assertIsNone(empty.current)
        empty.move(3)
        self.assertIsNone(empty.current)

    def test_the_view_scrolls_to_follow_the_cursor(self):
        small = screen([snap(i, day=1 + i % 28) for i in range(1, 30)], height=CHROME_HEIGHT + 3)
        small.move(10)
        self.assertLessEqual(small.top, small.cursor)
        self.assertLess(small.cursor, small.top + small.rows_available)

    def test_the_last_page_does_not_scroll_past_the_end(self):
        small = screen([snap(i, day=1 + i % 28) for i in range(1, 30)], height=CHROME_HEIGHT + 3)
        small.move(99)
        self.assertLessEqual(small.top + small.rows_available, len(small.visible) + 1)


class SubvolumeTests(unittest.TestCase):
    def setUp(self):
        self.screen = screen(
            [snap(1), snap(2), snap(3, subvolume="home")],
            subvolumes={"home": {"path": "/home"}},
        )

    def test_starts_showing_everything(self):
        self.assertEqual(self.screen.subvolume, ALL)
        self.assertEqual(len(self.screen.visible), 3)

    def test_cycles_through_the_configured_subvolumes(self):
        self.assertEqual(self.screen.subvolume_names, [ALL, "root", "home"])
        self.screen.cycle_subvolume()
        self.assertEqual(self.screen.subvolume, "root")
        self.screen.cycle_subvolume()
        self.assertEqual(self.screen.subvolume, "home")
        self.screen.cycle_subvolume()
        self.assertEqual(self.screen.subvolume, ALL)

    def test_cycles_backwards_too(self):
        self.screen.cycle_subvolume(-1)
        self.assertEqual(self.screen.subvolume, "home")

    def test_filters_the_list(self):
        self.screen.cycle_subvolume()  # root
        self.assertEqual([s.id for s in self.screen.visible], [1, 2])
        self.screen.cycle_subvolume()  # home
        self.assertEqual([s.id for s in self.screen.visible], [3])

    def test_a_subvolume_with_no_snapshots_can_still_be_selected(self):
        # Otherwise the one place you would look to find out why it has none
        # cannot be reached.
        none_yet = screen([snap(1)], subvolumes={"srv": {"path": "/srv"}})
        none_yet.subvolume = "srv"
        self.assertEqual(none_yet.visible, [])
        self.assertIn("srv", "\n".join(none_yet.render()))

    def test_switching_returns_to_the_top(self):
        self.screen.move(2)
        self.screen.cycle_subvolume()
        self.assertEqual(self.screen.cursor, 0)

    def test_the_subvolume_column_appears_only_when_it_says_something(self):
        self.assertTrue(self.screen.show_subvolume)
        self.screen.cycle_subvolume()  # root alone
        self.assertFalse(self.screen.show_subvolume)

    def test_a_root_only_system_never_shows_the_column(self):
        plain = screen([snap(1), snap(2)])
        self.assertFalse(plain.show_subvolume)


class FilterTests(unittest.TestCase):
    def setUp(self):
        self.screen = screen(
            [snap(1), snap(2, kind="dnf-pre"), snap(3, keep=True), snap(4, kind="dnf-pre")]
        )

    def test_a_selector_narrows_the_list(self):
        self.screen.query = "kind=dnf-pre"
        self.assertEqual([s.id for s in self.screen.visible], [2, 4])

    def test_an_id_selector_works_like_it_does_in_list(self):
        self.screen.query = "1,3"
        self.assertEqual([s.id for s in self.screen.visible], [1, 3])

    def test_a_broken_selector_shows_nothing_rather_than_everything(self):
        # "Nothing" reads as a filter that matched nothing; the whole list reads
        # as a filter that is not applied, which is a lie while one is typed.
        self.screen.query = "kind="
        self.assertEqual(self.screen.visible, [])

    def test_the_title_says_a_filter_is_on(self):
        self.screen.query = "kind=dnf-pre"
        self.assertIn("/kind=dnf-pre", self.screen.title_line())

    def test_an_empty_result_says_why(self):
        self.screen.query = "kind=nothing-like-this"
        self.assertIn("no snapshots match", "\n".join(self.screen.table_lines()))


class ReplaceTests(unittest.TestCase):
    """A reload keeps the user where they were, by ID rather than by position."""

    def setUp(self):
        self.screen = screen([snap(2, day=16), snap(3, day=17), snap(4, day=18)])
        self.screen.move(1)  # on #3

    def test_stays_on_the_same_snapshot_when_one_is_added_above(self):
        self.screen.replace([snap(1, day=15), snap(2, day=16), snap(3, day=17), snap(4, day=18)])
        self.assertEqual(self.screen.current.id, 3)

    def test_stays_put_when_one_below_is_removed(self):
        self.screen.replace([snap(2, day=16), snap(3, day=17)])
        self.assertEqual(self.screen.current.id, 3)

    def test_falls_back_sensibly_when_the_snapshot_under_the_cursor_is_gone(self):
        self.screen.replace([snap(2, day=16), snap(4, day=18)])
        self.assertIsNotNone(self.screen.current)
        self.assertIn(self.screen.current.id, (2, 4))

    def test_an_explicit_id_wins(self):
        self.screen.replace([snap(2, day=16), snap(3, day=17), snap(4, day=18)], keep_id=4)
        self.assertEqual(self.screen.current.id, 4)

    def test_emptying_the_list_is_not_an_error(self):
        self.screen.replace([])
        self.assertIsNone(self.screen.current)
        self.screen.render()


class ModeTests(unittest.TestCase):
    def setUp(self):
        self.screen = screen([snap(1, description="before the new kernel")])

    def test_confirming_shows_only_yes_and_no(self):
        self.screen.ask("Delete snapshot 1?", pending="delete")
        self.assertIs(self.screen.mode, Mode.CONFIRM)
        self.assertEqual(self.screen.key_line(), "y confirm   n cancel")
        self.assertIn("Delete snapshot 1?", "\n".join(self.screen.detail_lines()))

    def test_cancelling_returns_to_browsing_and_forgets_the_action(self):
        self.screen.ask("Delete snapshot 1?", pending="delete")
        self.screen.cancel()
        self.assertIs(self.screen.mode, Mode.BROWSE)
        self.assertEqual(self.screen.pending, "")
        self.assertEqual(self.screen.message, "")

    def test_typing_collects_characters_and_backspace_removes_them(self):
        self.screen.begin_input("description")
        for character in "note":
            self.screen.type_character(character)
        self.screen.backspace()
        self.assertEqual(self.screen.input_buffer, "not")
        self.assertIn("description: not", "\n".join(self.screen.detail_lines()))

    def test_typing_does_nothing_while_browsing(self):
        self.screen.type_character("x")
        self.assertEqual(self.screen.input_buffer, "")

    def test_input_starts_from_what_is_there_when_editing(self):
        self.screen.begin_input("description", initial="before the new kernel")
        self.assertEqual(self.screen.input_buffer, "before the new kernel")

    def test_help_replaces_the_screen_and_lists_every_key(self):
        self.screen.mode = Mode.HELP
        text = "\n".join(self.screen.render())
        self.assertIn("keys", text)
        for key in ("n ", "d ", "k ", "x ", "R ", "/ ", "q "):
            self.assertIn(key, text)

    def test_help_lists_compare_and_rollback_too(self):
        self.screen.mode = Mode.HELP
        text = "\n".join(self.screen.render())
        self.assertIn("roll back to this snapshot", text)
        self.assertIn("compare", text)


class MessageTests(unittest.TestCase):
    def setUp(self):
        self.screen = screen([snap(1, description="a description")])

    def test_a_message_takes_the_detail_line(self):
        self.screen.report("snapshot 2 taken")
        self.assertIn("snapshot 2 taken", "\n".join(self.screen.detail_lines()))

    def test_an_error_is_marked_as_one(self):
        self.screen.fail("could not delete snapshot 1")
        self.assertTrue(self.screen.message_is_error)

    def test_clearing_brings_the_snapshot_back(self):
        self.screen.report("something happened")
        self.screen.clear_message()
        self.assertIn("a description", "\n".join(self.screen.detail_lines()))


class RenderingTests(unittest.TestCase):
    def setUp(self):
        self.snapshots = [
            snap(1, description="before the new kernel", day=15),
            snap(2, kind="dnf-pre", description="upgrade kernel-core, mesa and 12 more", day=16),
            snap(3, kind="rollback", keep=True, description="state before rollback to 2", day=17),
        ]

    def test_nothing_is_wider_than_the_screen(self):
        for width in (46, 60, 72, 80, 100, 132):
            with self.subTest(width=width):
                view = screen(self.snapshots, width=width)
                for line in view.render():
                    self.assertLessEqual(display_width(line), width, line)

    def test_the_cursor_marks_exactly_one_row(self):
        view = screen(self.snapshots)
        marked = [line for line in view.table_lines() if line.startswith(view.glyphs.cursor)]
        self.assertEqual(len(marked), 1)
        self.assertIn("before the new kernel", marked[0])

    def test_kept_snapshots_are_starred(self):
        view = screen(self.snapshots)
        rows = "\n".join(view.table_lines())
        self.assertIn("*3", rows)

    def test_a_narrow_screen_drops_the_kernel_before_the_date(self):
        view = screen(self.snapshots, width=60)
        header = view.table_lines()[0]
        self.assertNotIn("KERNEL", header)
        self.assertIn("DATE", header)
        self.assertIn("DESCRIPTION", header)

    def test_the_detail_keeps_the_kernel_the_table_dropped(self):
        # The two must not both hide it; that is when it would be missed.
        view = screen(self.snapshots, width=78)
        self.assertNotIn("KERNEL", view.table_lines()[0])
        self.assertIn(KERNEL, view.detail_lines()[0])

    def test_the_key_bar_drops_keys_rather_than_being_cut_off(self):
        # "/ filte…" names a key without saying what it does, which is worse
        # than not offering it.
        for width in (46, 60, 80, 120):
            with self.subTest(width=width):
                line = screen(self.snapshots, width=width).key_line()
                self.assertLessEqual(display_width(line), width)
                self.assertNotIn("…", line)

    def test_help_and_quit_are_never_dropped(self):
        line = screen(self.snapshots, width=40).key_line()
        self.assertIn("? help", line)
        self.assertIn("q quit", line)

    def test_keys_that_need_a_snapshot_disappear_when_there_is_none(self):
        line = screen([], width=140).key_line()
        for key in ("d describe", "k keep", "x delete", "R roll back"):
            self.assertNotIn(key, line)
        self.assertIn("n new", line)

    def test_an_empty_list_says_so(self):
        self.assertIn("no snapshots yet", "\n".join(screen([]).table_lines()))

    def test_the_detail_is_always_two_lines_so_the_table_does_not_jump(self):
        for snapshots in ([], self.snapshots, [snap(9, description="")]):
            with self.subTest(n=len(snapshots)):
                self.assertEqual(len(screen(snapshots).detail_lines()), 2)

    def test_the_description_is_shown_in_full_below_the_table(self):
        long = "a description far too long to fit in the column it is given here"
        view = screen([snap(1, description=long)], width=80)
        self.assertIn("…", view.table_lines()[1])
        self.assertEqual(view.detail_lines()[1], long)


class GlyphTests(unittest.TestCase):
    def test_utf8_gets_the_decorated_ones(self):
        self.assertEqual(Glyphs.for_encoding("utf-8").cursor, "▸")

    def test_ascii_falls_back(self):
        plain = Glyphs.for_encoding("ascii")
        self.assertEqual(plain.cursor, ">")
        self.assertEqual(plain.rule, "-")

    def test_no_encoding_at_all_falls_back(self):
        self.assertEqual(Glyphs.for_encoding(None).cursor, ">")

    def test_an_unknown_encoding_falls_back_rather_than_raising(self):
        self.assertEqual(Glyphs.for_encoding("not-a-real-encoding").cursor, ">")

    def test_a_plain_screen_renders_without_any_of_them(self):
        view = screen([snap(1, description="x")])
        view.glyphs = Glyphs.plain()
        text = "\n".join(view.render())
        for fancy in ("▸", "·", "⇥", "─"):
            self.assertNotIn(fancy, text)


class ResizeTests(unittest.TestCase):
    def test_resizing_keeps_the_cursor_visible(self):
        view = screen([snap(i, day=1 + i % 28) for i in range(1, 40)], height=30)
        view.move(35)
        view.resize(80, CHROME_HEIGHT + 3)
        self.assertLessEqual(view.top, view.cursor)
        self.assertLess(view.cursor, view.top + view.rows_available)

    def test_a_tiny_terminal_still_renders(self):
        view = screen([snap(1, description="x")])
        view.resize(20, 1)
        lines = view.render()
        self.assertTrue(lines)
        for line in lines:
            self.assertLessEqual(display_width(line), view.width)


class StyleTests(unittest.TestCase):
    """What the styled renderer marks up, and that it cannot drift from the text.

    Still no curses: these assert on Ink, which is a meaning, not a colour. How
    a meaning is painted is tui.Palette's business and is covered against a real
    terminal in test_tui_pty.py.
    """

    @staticmethod
    def inks(line, text):
        """The inks of every span whose text contains `text`."""
        return [span.ink for span in line.spans if text in span.text]

    def test_the_plain_render_is_exactly_the_spans_joined(self):
        view = screen([snap(1, keep=True, description="first"), snap(2, kind="rollback")])
        self.assertEqual(
            view.render(),
            [line.text for line in view.render_styled()],
        )

    def test_every_mode_renders_both_ways_the_same(self):
        view = screen([snap(1, description="a")])
        for mode in (Mode.BROWSE, Mode.HELP, Mode.DETAIL, Mode.CONFIRM, Mode.INPUT):
            with self.subTest(mode=mode):
                view.mode = mode
                self.assertEqual(view.render(), [l.text for l in view.render_styled()])

    def test_the_cursor_row_is_the_highlighted_one(self):
        view = screen([snap(1), snap(2), snap(3)])
        view.move(1)
        rows = [line for line in view.render_styled() if line.highlight]
        self.assertEqual(len(rows), 1)
        self.assertIn("2", rows[0].text)

    def test_the_bands_are_the_title_and_the_keys(self):
        view = screen([snap(1)])
        bars = [line.text for line in view.render_styled() if line.bar]
        self.assertEqual(len(bars), 2)
        self.assertIn("btrfs-patrol", bars[0])
        self.assertIn("quit", bars[-1])

    def test_a_kept_snapshot_marks_its_asterisk(self):
        view = screen([snap(1, keep=True)])
        row = view.render_styled()[3]
        self.assertIn(Ink.OK, self.inks(row, "*"))
        # And the asterisk is still there for a terminal with no colour at all.
        self.assertIn("*1", row.text)

    def test_a_rollback_kind_is_warned_about_and_a_timer_is_not(self):
        view = screen([snap(1, kind="rollback"), snap(2, kind="timer")])
        rows = view.render_styled()[3:5]
        self.assertIn(Ink.WARNING, self.inks(rows[0], "rollback"))
        self.assertIn(Ink.DIM, self.inks(rows[1], "timer"))

    def test_an_error_message_is_inked_differently_from_a_result(self):
        view = screen([snap(1)])
        view.report("took snapshot 4")
        self.assertIn(Ink.OK, self.inks(view.detail_styled()[0], "took"))
        view.fail("no such snapshot")
        self.assertIn(Ink.ERROR, self.inks(view.detail_styled()[0], "no such"))

    def test_keys_are_accented_and_their_meanings_are_not(self):
        line = screen([snap(1)]).key_styled()
        accented = [span.text for span in line.spans if span.ink is Ink.ACCENT]
        self.assertIn("q", accented)
        self.assertNotIn(" quit", accented)

    def test_clipping_a_styled_line_keeps_it_within_the_width(self):
        view = screen([snap(1, description="x" * 200)], width=40)
        for line in view.render_styled():
            self.assertLessEqual(display_width(line.text), 40)


if __name__ == "__main__":
    unittest.main()
