# SPDX-License-Identifier: GPL-3.0-or-later
"""The terminal interface: keys in, snapshots changed, screen painted.

Two halves, and the split is deliberate. `Controller` decides what a key does
and performs it against the store — it touches no curses and is covered by
ordinary tests. `run()` is the curses half: set the terminal up, read key codes,
paint the strings `Screen.render()` returns, put the terminal back. It is kept
small because it is the part no test can drive.

THE RULES ABOUT WHAT MAY HAPPEN ARE NOT REIMPLEMENTED HERE. Taking a snapshot
still refuses when a rollback is waiting for its reboot and still checks the
store is mounted; deleting still refuses the snapshot the system is running
from. Those checks live in cli.py beside the commands that already use them, and
this calls them. A second, quietly diverging set of safety rules in a nicer
interface is exactly the bug that would matter most.

See docs/tui-design.md.
"""

from __future__ import annotations

import contextlib
import curses
import locale
from collections.abc import Sequence
from dataclasses import dataclass

from .cli import (
    App,
    mounted_snapshots,
    pending_rollbacks,
    selected_subvolumes,
    sync_boot_entries,
)
from .config import ROOT
from .errors import PatrolError
from .output import display_width
from .screen import ALL, Glyphs, Ink, Mode, Screen, StyledLine
from . import diff as diff_mod
from . import rollback as rollback_mod
from . import system

#: What a key press means, independent of how the terminal spelled it.
UP, DOWN, PAGE_UP, PAGE_DOWN, HOME, END = "up", "down", "page_up", "page_down", "home", "end"
NEXT_SUBVOLUME, PREV_SUBVOLUME = "next_subvolume", "prev_subvolume"
OPEN, NEW, DESCRIBE, KEEP, DELETE = "open", "new", "describe", "keep", "delete"
FILTER, RELOAD, HELP, QUIT = "filter", "reload", "help", "quit"
ROLLBACK = "rollback"
COMPARE = "compare"
ACCEPT, CANCEL, BACKSPACE, YES, NO = "accept", "cancel", "backspace", "yes", "no"


def action_for(key: int, mode: Mode = Mode.BROWSE) -> str | None:
    """The action a key code means in `mode`, or None if it means nothing there.

    THE MODE IS PART OF IT, and leaving it out was a bug rather than a
    simplification: an answer of "y" means yes on a confirmation and on a
    rollback plan, and means nothing at all while browsing. Deciding that in the
    input loop instead - where no test can reach it - is how the rollback plan
    came to answer "nothing rolled back" to a pressed y, while every controller
    test passed because they hand the controller YES directly.
    """
    if mode in (Mode.CONFIRM, Mode.PAGE):
        if key in (curses.KEY_ENTER, 10, 13):
            return ACCEPT
        if 0 <= key < 0x110000:
            return YES if chr(key).lower() == "y" else NO
        return NO
    if mode is Mode.INPUT:
        if key in (curses.KEY_ENTER, 10, 13):
            return ACCEPT
        if key == 27:
            return CANCEL
        if key in (curses.KEY_BACKSPACE, 127, 8):
            return BACKSPACE
        return None
    if key == curses.KEY_UP:
        return UP
    if key == curses.KEY_DOWN:
        return DOWN
    if key == curses.KEY_PPAGE:
        return PAGE_UP
    if key == curses.KEY_NPAGE:
        return PAGE_DOWN
    if key == curses.KEY_HOME:
        return HOME
    if key == curses.KEY_END:
        return END
    if key == curses.KEY_BTAB:
        return PREV_SUBVOLUME
    if key in (curses.KEY_ENTER, 10, 13):
        return OPEN
    if key == 9:
        return NEXT_SUBVOLUME
    if key == 27:
        return CANCEL
    if key in (curses.KEY_BACKSPACE, 127, 8):
        return BACKSPACE
    if 0 <= key < 0x110000:
        return {
            "n": NEW,
            "d": DESCRIBE,
            "k": KEEP,
            "x": DELETE,
            # SHIFT, DELIBERATELY. This is the one key here that replaces a
            # subvolume, and it should not be reachable by a slipped finger on
            # the row below 'e'. Lower-case r stays reload.
            "R": ROLLBACK,
            "c": COMPARE,
            "/": FILTER,
            "r": RELOAD,
            "?": HELP,
            "q": QUIT,
        }.get(chr(key))
    return None


@dataclass
class Controller:
    """What each action does. No curses, so this can be driven from a test."""

    app: App
    screen: Screen

    #: While a rollback plan is on screen: the open plan, and the context that
    #: keeps the top-level subvolume mounted for as long as the plan is in use.
    plan: object | None = None
    plan_context: contextlib.ExitStack | None = None

    # --- helpers ----------------------------------------------------------

    def reload(self, keep_id: int | None = None) -> None:
        self.screen.replace(self.app.store.load_all(), keep_id=keep_id)

    def _subvolume_for_new(self) -> str:
        """Which subvolume a new snapshot is of.

        The one being shown, or root when showing everything - guessing at "all
        of them" from a single keypress would take several snapshots at once,
        which is not what one key should do.
        """
        return ROOT if self.screen.subvolume == ALL else self.screen.subvolume

    # --- actions ----------------------------------------------------------

    def dispatch(self, action: str | None, character: str = "") -> bool:
        """Perform `action`. Returns False when the interface should exit."""
        screen = self.screen
        if screen.mode is Mode.HELP or screen.mode is Mode.DETAIL:
            # Any key returns; the page says so.
            screen.mode = Mode.BROWSE
            return True
        if screen.mode is Mode.PAGE:
            self._page(action)
            return True
        if screen.mode is Mode.CONFIRM:
            self._confirm(action)
            return True
        if screen.mode is Mode.INPUT:
            self._input(action, character)
            return True
        return self._browse(action)

    def _browse(self, action: str | None) -> bool:
        screen = self.screen
        if action == QUIT:
            return False
        if action in (UP, DOWN, PAGE_UP, PAGE_DOWN, HOME, END):
            self._move(action)
            return True
        screen.clear_message()
        if action == NEXT_SUBVOLUME:
            screen.cycle_subvolume(1)
        elif action == PREV_SUBVOLUME:
            screen.cycle_subvolume(-1)
        elif action == HELP:
            screen.mode = Mode.HELP
        elif action == OPEN:
            if screen.current is not None:
                screen.mode = Mode.DETAIL
        elif action == RELOAD:
            self._guard(self.reload)
            screen.report("reloaded")
        elif action == FILTER:
            screen.begin_input("filter", screen.query)
        elif action == NEW:
            screen.begin_input(f"description for the new snapshot of {self._subvolume_for_new()}")
        elif action == DESCRIBE:
            if screen.current is not None:
                screen.begin_input("description", screen.current.description)
        elif action == KEEP:
            self._guard(self.toggle_keep)
        elif action == DELETE:
            self._ask_delete()
        elif action == ROLLBACK:
            self._begin_rollback()
        elif action == COMPARE:
            self._compare()
        elif action == CANCEL and screen.query:
            screen.query = ""
            screen.move_to(0)
        return True

    def _move(self, action: str) -> None:
        screen = self.screen
        page = screen.rows_available
        if action == UP:
            screen.move(-1)
        elif action == DOWN:
            screen.move(1)
        elif action == PAGE_UP:
            screen.move(-page)
        elif action == PAGE_DOWN:
            screen.move(page)
        elif action == HOME:
            screen.move_to(0)
        elif action == END:
            screen.move_to(len(screen.visible) - 1)

    def _input(self, action: str | None, character: str) -> None:
        screen = self.screen
        if action == CANCEL:
            purpose = screen.input_purpose
            screen.cancel()
            if purpose == "filter":
                screen.query = ""
            return
        if action == BACKSPACE:
            screen.backspace()
            return
        if action == ACCEPT:
            text, purpose = screen.input_buffer, screen.input_purpose
            screen.cancel()
            if purpose == "filter":
                screen.query = text.strip()
                screen.move_to(0)
            elif purpose == "description":
                self._guard(lambda: self.set_description(text))
            else:
                self._guard(lambda: self.take_snapshot(text))
            return
        if character:
            screen.type_character(character)
        # The filter applies as it is typed, so the list narrows under the
        # cursor and you can see whether the selector says what you meant.
        if screen.input_purpose == "filter":
            screen.query = screen.input_buffer.strip()

    def _confirm(self, action: str | None) -> None:
        screen = self.screen
        pending = screen.pending
        if action in (YES,) or action == ACCEPT:
            screen.cancel()
            if pending.startswith("delete:"):
                self._guard(lambda: self.delete(int(pending.split(":", 1)[1])))
        else:
            screen.cancel()
            screen.report("nothing changed")

    def _compare(self) -> None:
        """Mark a snapshot, or compare the marked one with this one.

        TWO KEYSTROKES APART, because a comparison needs two snapshots and
        there is only one cursor. The first press marks and says so; the second,
        on a different row, does the work. Pressing it again on the same row
        unmarks, so a mis-hit costs nothing.
        """
        snapshot = self.screen.current
        if snapshot is None:
            return
        if self.screen.marked_id is None:
            self.screen.marked_id = snapshot.id
            self.screen.report(f"snapshot {snapshot.id} marked; press c on another to compare")
            return
        if self.screen.marked_id == snapshot.id:
            self.screen.marked_id = None
            self.screen.report("mark cleared")
            return
        marked = self.screen.marked_id
        self.screen.marked_id = None
        self._guard(lambda: self._show_comparison(marked, snapshot.id))

    def _show_comparison(self, first: int, second: int) -> None:
        store = self.app.store
        one, two = store.load(first), store.load(second)
        if one.subvolume != two.subvolume:
            raise PatrolError(
                f"snapshot {one.id} is of {one.subvolume!r} and {two.id} is of "
                f"{two.subvolume!r}; only snapshots of the same subvolume can be compared"
            )
        # Oldest first, so the changes read as what happened over time rather
        # than backwards - whichever order the two were marked in.
        older, newer = (one, two) if one.id < two.id else (two, one)
        comparison = diff_mod.compare(
            store.subvolume(older.id), store.subvolume(newer.id), older.id, newer.id
        )
        body = ["", f"  {comparison.summary()}", ""]
        body.extend(f"  {line}" for line in comparison.lines())
        self.screen.show_page(
            f"Snapshot {older.id} to {newer.id} ({older.subvolume})",
            body,
            footer="any key to go back",
        )

    # --- rollback ---------------------------------------------------------

    def _begin_rollback(self) -> None:
        """Work out whether the rollback can happen, and show what it would do.

        EVERY CHECK IS rollback.prepare()'S, not a copy of them. It refuses a
        snapshot whose kernel this system has no modules for, disagrees about
        the device, or whose store is wrong, and it works out how root is found
        at boot and which nested subvolumes move with it. Re-deciding any of
        that here would be a second set of rules for the operation where being
        wrong costs most.
        """
        snapshot = self.screen.current
        if snapshot is None:
            return
        stack = contextlib.ExitStack()
        try:
            plan = stack.enter_context(
                rollback_mod.prepare(self.app.config, self.app.store, snapshot)
            )
        except (PatrolError, OSError) as error:
            stack.close()
            self.screen.fail(str(error))
            return
        self.plan = plan
        self.plan_context = stack
        self.screen.show_page(
            f"Roll {plan.name} back to snapshot {snapshot.id}",
            ["", *plan.describe()],
            warnings=plan.warnings,
            footer="y roll back   any other key cancels",
        )

    def _page(self, action: str | None) -> None:
        confirmed = action in (YES, ACCEPT)
        plan, stack = self.plan, self.plan_context
        self.plan = self.plan_context = None
        self.screen.cancel()
        try:
            if confirmed and plan is not None:
                self._guard(lambda: self._execute_rollback(plan))
            elif plan is not None:
                self.screen.report("nothing rolled back")
        finally:
            # The top-level subvolume is unmounted whether it went ahead, was
            # declined, or raised on the way.
            if stack is not None:
                stack.close()

    def _execute_rollback(self, plan) -> None:
        saved = plan.execute()
        self.reload()
        where = "" if plan.name == ROOT else f" {plan.path}"
        self.screen.show_page(
            f"Rolled {plan.name} back to snapshot {plan.target.id}",
            [
                "",
                f"  the state from before is kept as snapshot {saved.id}",
                "",
                f"  reboot to use the restored{where or ' system'}"
                if plan.mounted or plan.name == ROOT
                else f"  the restored{where} is in place; restart what uses it, or reboot",
            ],
            footer="any key to go back",
        )
        # Nothing else is safe to assume about the list until it is read again.
        self.screen.mode = Mode.PAGE

    def _guard(self, work) -> None:
        """Run an action, turning a refusal into a message instead of a crash."""
        try:
            work()
        except (PatrolError, OSError) as error:
            self.screen.fail(str(error))

    # --- the operations themselves ----------------------------------------

    def take_snapshot(self, description: str) -> None:
        name = self._subvolume_for_new()
        config = self.app.config
        [subvolume] = selected_subvolumes(config, [name], "manual")
        pending = pending_rollbacks(config, [subvolume])
        if subvolume.name in pending:
            message = rollback_mod.pending_rollback_message(pending[subvolume.name], subvolume.path)
            raise PatrolError(f"{message}; reboot first")
        # An unmounted store would take the snapshot into the parent subvolume,
        # where nothing will ever see it again.
        system.require_mounted(config.snapshots_dir, config.snapshots_subvolume)
        with self.app.store.lock():
            snapshot = self.app.store.create(
                subvolume.path, kind="manual", description=description.strip(),
                keep=False, subvolume=subvolume.name,
            )
            pruned = [s.id for s in self.app.store.prune(subvolume.max_snapshots, subvolume.name)]
        sync_boot_entries(self.app)
        self.reload(keep_id=snapshot.id)
        note = f", pruned {', '.join(map(str, pruned))}" if pruned else ""
        self.screen.report(f"took snapshot {snapshot.id} of {subvolume.name}{note}")

    def set_description(self, description: str) -> None:
        snapshot = self.screen.current
        if snapshot is None:
            return
        with self.app.store.lock():
            fresh = self.app.store.load(snapshot.id)
            fresh.description = description.strip()
            self.app.store.save(fresh)
        self.reload(keep_id=snapshot.id)
        self.screen.report(f"described snapshot {snapshot.id}")

    def toggle_keep(self) -> None:
        snapshot = self.screen.current
        if snapshot is None:
            return
        with self.app.store.lock():
            fresh = self.app.store.load(snapshot.id)
            fresh.keep = not fresh.keep
            self.app.store.save(fresh)
            kept = fresh.keep
        self.reload(keep_id=snapshot.id)
        state = "will be kept" if kept else "can be pruned again"
        self.screen.report(f"snapshot {snapshot.id} {state}")

    def _ask_delete(self) -> None:
        snapshot = self.screen.current
        if snapshot is None:
            return
        # The same question cmd_delete asks, and for the same reason: a
        # subvolume mounted from a snapshot means either a rollback waiting for
        # its reboot or that snapshot's own boot entry was booted to repair the
        # system, and deleting it aims 'btrfs subvolume delete' at the running
        # system in both cases.
        in_use = set(mounted_snapshots(self.app.config, list(self.app.config.managed())).values())
        if snapshot.id in in_use:
            self.screen.fail(
                f"snapshot {snapshot.id} is what the system is running from; "
                "reboot into the normal system first"
            )
            return
        kept = " (kept)" if snapshot.keep else ""
        self.screen.ask(
            f"Delete snapshot {snapshot.id} of {snapshot.subvolume}{kept}?",
            pending=f"delete:{snapshot.id}",
        )

    def delete(self, snapshot_id: int) -> None:
        with self.app.store.lock():
            if snapshot_id in set(self.app.store.ids()):
                self.app.store.delete(snapshot_id)
        sync_boot_entries(self.app)
        self.reload()
        self.screen.report(f"deleted snapshot {snapshot_id}")


# --- the curses half -------------------------------------------------------


def run(app: App) -> int:
    """Show the interface. Returns the process's exit status."""
    locale.setlocale(locale.LC_ALL, "")
    screen = Screen(
        config=app.config,
        snapshots=app.store.load_all(),
        glyphs=Glyphs.for_encoding(locale.getpreferredencoding(False)),
    )
    controller = Controller(app=app, screen=screen)
    try:
        curses.wrapper(_loop, controller)
    except PatrolError as error:
        # curses.wrapper has already restored the terminal, so this prints
        # legibly rather than over a half-torn-down screen.
        app.console.error(str(error))
        return 1
    return 0


def _loop(window: "curses._CursesWindow", controller: Controller) -> None:
    curses.curs_set(0)
    window.keypad(True)
    # FROM THE CONSOLE, NOT FROM config.color. The --color flag overrides the
    # Console that cli.main builds and leaves config.color alone, so reading the
    # configuration here would have honoured the file and quietly ignored
    # `btrfs-patrol --color never tui`. The console has already resolved the
    # flag, the file, NO_COLOR and whether stdout is a terminal - which it is,
    # here, or there would be no curses screen to paint.
    palette = Palette(controller.app.console.style.enabled)
    screen = controller.screen
    running = True
    while running:
        height, width = window.getmaxyx()
        screen.resize(width, height)
        _paint(window, screen, palette)
        key = window.getch()
        if key == curses.KEY_RESIZE:
            continue
        character = ""
        if screen.mode is Mode.INPUT and 32 <= key < 0x110000 and key != 127:
            character = chr(key)
        running = controller.dispatch(action_for(key, screen.mode), character)


class Palette:
    """Turns an Ink into a curses attribute, or into nothing.

    WHY NOT output.Style: it emits ANSI escapes, which curses would print
    literally. This is the same vocabulary - dim, accent, warning, error - and
    honours the same --color/output.color setting, so a screen and a piped
    `list` agree about what colour is for even though they cannot share code.

    NOTHING IS DISTINGUISHED BY COLOUR ALONE, which is what makes `never` a
    usable setting rather than a broken one: the cursor keeps its marker, kept
    snapshots keep their asterisk, errors keep the word, and the bars keep
    reverse video.
    """

    #: Inks that get a colour of their own. PLAIN is the terminal's own.
    COLORS = {
        Ink.ACCENT: curses.COLOR_CYAN,
        Ink.HEADING: curses.COLOR_BLUE,
        Ink.WARNING: curses.COLOR_YELLOW,
        Ink.ERROR: curses.COLOR_RED,
        Ink.OK: curses.COLOR_GREEN,
    }

    #: 256-colour terminals get a band a couple of shades off the background for
    #: the cursor's row, which leaves every colour in the row still readable.
    #: With 8 colours there is no such shade, so the row is reversed instead and
    #: the span colours are dropped - white on cyan on a reversed row is worse
    #: than plain.
    HIGHLIGHT_BG = 237

    def __init__(self, enabled: bool) -> None:
        self.enabled = False
        self.shaded = False
        if not enabled or not curses.has_colors():
            return
        curses.start_color()
        try:
            # -1 means "whatever the terminal already uses", so a light theme
            # stays light instead of being forced onto black.
            curses.use_default_colors()
        except curses.error:
            return
        self.enabled = True
        self.shaded = curses.COLORS >= 256 and curses.COLOR_PAIRS > len(self.COLORS) * 2 + 4
        self._pairs: dict[tuple[Ink, bool], int] = {}
        index = 1
        for ink, color in self.COLORS.items():
            curses.init_pair(index, color, -1)
            self._pairs[(ink, False)] = index
            index += 1
            if self.shaded:
                curses.init_pair(index, color, self.HIGHLIGHT_BG)
                self._pairs[(ink, True)] = index
                index += 1
        if self.shaded:
            curses.init_pair(index, -1, self.HIGHLIGHT_BG)
            self._plain_shaded = index
            index += 1
        # The bands. White on blue is what a terminal program's title bar has
        # looked like since before any of this, and it reads on every theme.
        curses.init_pair(index, curses.COLOR_WHITE, curses.COLOR_BLUE)
        self._bar = index

    def background(self, line: StyledLine) -> int:
        """The attribute the rest of the row is filled with."""
        if line.bar:
            return curses.color_pair(self._bar) if self.enabled else curses.A_REVERSE
        if line.highlight:
            if self.enabled and self.shaded:
                return curses.color_pair(self._plain_shaded)
            return curses.A_REVERSE
        return curses.A_NORMAL

    def attr(self, span_ink: Ink, bold: bool, line: StyledLine) -> int:
        attr = curses.A_BOLD if bold else curses.A_NORMAL
        if line.bar:
            # On the band, emphasis is bold: a second colour on a blue
            # background is where this stops being readable.
            return self.background(line) | attr
        if line.highlight and not (self.enabled and self.shaded):
            return curses.A_REVERSE | attr
        if not self.enabled:
            # A_DIM is the one attribute that survives with no colour at all,
            # and it is what tells the header row from the rows below it.
            return (curses.A_DIM if span_ink is Ink.DIM else curses.A_NORMAL) | attr
        shaded = bool(line.highlight and self.shaded)
        if span_ink is Ink.DIM:
            base = curses.color_pair(self._plain_shaded) if shaded else curses.A_NORMAL
            return base | curses.A_DIM | attr
        pair = self._pairs.get((span_ink, shaded))
        if pair is None:
            pair = self._pairs.get((span_ink, False))
        if pair is None:
            base = curses.color_pair(self._plain_shaded) if shaded else curses.A_NORMAL
            return base | attr
        return curses.color_pair(pair) | attr


def _paint(window: "curses._CursesWindow", screen: Screen, palette: Palette) -> None:
    window.erase()
    height, width = window.getmaxyx()
    lines = screen.render_styled()[:height]
    for row, line in enumerate(lines):
        # ONLY THE LAST ROW GIVES UP ITS LAST COLUMN. Writing the very bottom
        # right cell advances the cursor off the window, which curses reports as
        # an error; every other row can use the full width. Doing it to all of
        # them - which this did at first - takes a column off every line, and it
        # shows: the title read "subvolume: all subvolume" and the rules came up
        # one short of the screen.
        limit = max(width - 1 if row == height - 1 else width, 0)
        column = 0
        for span in line.spans:
            if column >= limit or not span.text:
                break
            window.addnstr(row, column, span.text, limit - column,
                           palette.attr(span.ink, span.bold, line))
            column += display_width(span.text)
        # THE BANDS AND THE CURSOR'S ROW RUN TO THE EDGE. Without this the
        # title's colour would stop where its text does and the bar would look
        # like a coloured word rather than a band.
        if (line.bar or line.highlight) and column < limit:
            window.addnstr(row, column, " " * (limit - column), limit - column,
                           palette.background(line))
    window.refresh()
