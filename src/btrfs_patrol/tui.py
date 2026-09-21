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
from .screen import ALL, Glyphs, Mode, Screen
from . import rollback as rollback_mod
from . import system

#: What a key press means, independent of how the terminal spelled it.
UP, DOWN, PAGE_UP, PAGE_DOWN, HOME, END = "up", "down", "page_up", "page_down", "home", "end"
NEXT_SUBVOLUME, PREV_SUBVOLUME = "next_subvolume", "prev_subvolume"
OPEN, NEW, DESCRIBE, KEEP, DELETE = "open", "new", "describe", "keep", "delete"
FILTER, RELOAD, HELP, QUIT = "filter", "reload", "help", "quit"
ROLLBACK = "rollback"
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
    screen = controller.screen
    running = True
    while running:
        height, width = window.getmaxyx()
        screen.resize(width, height)
        _paint(window, screen)
        key = window.getch()
        if key == curses.KEY_RESIZE:
            continue
        character = ""
        if screen.mode is Mode.INPUT and 32 <= key < 0x110000 and key != 127:
            character = chr(key)
        running = controller.dispatch(action_for(key, screen.mode), character)


def _paint(window: "curses._CursesWindow", screen: Screen) -> None:
    window.erase()
    height, width = window.getmaxyx()
    lines = screen.render()[:height]
    for row, line in enumerate(lines):
        # ONLY THE LAST ROW GIVES UP ITS LAST COLUMN. Writing the very bottom
        # right cell advances the cursor off the window, which curses reports as
        # an error; every other row can use the full width. Doing it to all of
        # them - which this did at first - takes a column off every line, and it
        # shows: the title read "subvolume: all subvolume" and the rules came up
        # one short of the screen.
        limit = width - 1 if row == height - 1 else width
        window.addnstr(row, 0, line, max(limit, 0))
    window.refresh()
