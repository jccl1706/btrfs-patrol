# SPDX-License-Identifier: GPL-3.0-or-later
"""The terminal interface's view model: state, keys and the text of each region.

THIS MODULE IMPORTS NO CURSES, and that is the whole point of it existing
separately. Everything that decides what appears on screen - which snapshot the
cursor is on, which subvolume is shown, whether a delete is waiting to be
confirmed, how a row is laid out at this width - lives here and renders to plain
strings. So it can be driven from a test: press a key, assert on the lines that
come back. tui.py then does nothing but read key codes and paint what this
returns, which keeps the untestable layer small enough to read in one sitting.

The screen is four regions, top to bottom:

    title       one line: the program, and which subvolume is shown
    table       the snapshot list, taking whatever height is left
    detail      two lines about the highlighted snapshot
    keys        one line, showing only the keys that apply right now

See docs/tui-design.md for why it is stacked rather than two panes, and for what
the first version deliberately leaves out.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import Enum

from .config import Config, ROOT
from .errors import PatrolError
from .output import MIN_DESCRIPTION_WIDTH, display_width, layout, printable, truncate
from .selectors import select
from .snapshots import Snapshot

#: Rows the table needs before it is worth drawing at all: a header and one row.
MIN_ROWS = 2

#: The title, the two rules around the detail, the detail, and the key bar.
CHROME_HEIGHT = 6

#: Shown in the subvolume cycle to mean "no filter".
ALL = "all"


class Mode(Enum):
    """What the interface is waiting for."""

    BROWSE = "browse"
    """The ordinary state: the cursor moves, keys act."""

    CONFIRM = "confirm"
    """A destructive action is waiting for y or n."""

    INPUT = "input"
    """Text is being typed - a description, or a filter."""

    HELP = "help"
    """The key list is covering the screen."""


@dataclass(frozen=True)
class Glyphs:
    """The decoration, and the ASCII it falls back to.

    A terminal whose encoding cannot represent these is not exotic - a serial
    console, a minimal initramfs shell, LANG=C over ssh - and it is exactly the
    situation someone is in when they need to roll a system back. The check is
    whether the locale's encoding can encode the string, not a guess from TERM.
    """

    cursor: str = "▸"      # ▸
    separator: str = "·"   # ·
    tab: str = "⇥"         # ⇥
    rule: str = "─"        # ─

    @classmethod
    def for_encoding(cls, encoding: str | None) -> Glyphs:
        fancy = cls()
        if not encoding:
            return cls.plain()
        try:
            "".join([fancy.cursor, fancy.separator, fancy.tab, fancy.rule]).encode(encoding)
        except (UnicodeEncodeError, LookupError):
            return cls.plain()
        return fancy

    @classmethod
    def plain(cls) -> Glyphs:
        return cls(cursor=">", separator="-", tab="tab", rule="-")


@dataclass
class Screen:
    """What is on screen, and what the keys do to it.

    Construct it with the snapshots and the configuration, then feed it keys.
    `render()` returns the lines to paint.
    """

    config: Config
    snapshots: list[Snapshot]
    width: int = 80
    height: int = 24
    glyphs: Glyphs = field(default_factory=Glyphs)

    #: Index into `visible`, not into `snapshots`.
    cursor: int = 0
    #: First visible row, for scrolling.
    top: int = 0
    #: Subvolume name, or ALL.
    subvolume: str = ALL
    #: Selector text from '/', or "".
    query: str = ""

    mode: Mode = Mode.BROWSE
    #: One line under the table: what just happened, or what is being asked.
    message: str = ""
    #: True when `message` reports a failure rather than a result.
    message_is_error: bool = False
    #: While mode is INPUT: what is being typed, and what it is for.
    input_buffer: str = ""
    input_purpose: str = ""
    #: While mode is CONFIRM: what will happen if the answer is yes.
    pending: str = ""

    # --- the list ---------------------------------------------------------

    @property
    def subvolume_names(self) -> list[str]:
        """The subvolume filter's cycle: every configured subvolume, and ALL.

        From the CONFIGURATION rather than from the snapshots, so a subvolume
        that has no snapshots yet can still be selected - otherwise the one
        place you would look to find out why it has none cannot be reached.
        """
        return [ALL, *(s.name for s in self.config.managed())]

    @property
    def visible(self) -> list[Snapshot]:
        """The snapshots the filters leave, newest last, as the table shows them."""
        rows = self.snapshots
        if self.subvolume != ALL:
            rows = [s for s in rows if s.subvolume == self.subvolume]
        if self.query:
            try:
                matching = {s.id for s in select(self.query, rows)}
            except PatrolError:
                # An unfinished or wrong selector shows nothing rather than
                # everything: "no rows" reads as a filter that matched nothing,
                # while the whole list reads as a filter that is not applied.
                return []
            rows = [s for s in rows if s.id in matching]
        return rows

    @property
    def current(self) -> Snapshot | None:
        rows = self.visible
        if not rows:
            return None
        return rows[min(self.cursor, len(rows) - 1)]

    @property
    def rows_available(self) -> int:
        """How many table rows fit, header excluded."""
        return max(self.height - CHROME_HEIGHT, 1)

    @property
    def show_subvolume(self) -> bool:
        """Whether the table carries a SUBVOLUME column.

        Only when more than one could appear. Showing it while a single
        subvolume is selected spends a column repeating the title.
        """
        return self.subvolume == ALL and (
            bool(self.config.subvolumes) or any(s.subvolume != ROOT for s in self.snapshots)
        )

    # --- movement ---------------------------------------------------------

    def _clamp(self) -> None:
        rows = self.visible
        self.cursor = max(0, min(self.cursor, len(rows) - 1)) if rows else 0
        span = self.rows_available
        if self.cursor < self.top:
            self.top = self.cursor
        elif self.cursor >= self.top + span:
            self.top = self.cursor - span + 1
        self.top = max(0, min(self.top, max(len(rows) - span, 0)))

    def move(self, delta: int) -> None:
        self.cursor += delta
        self._clamp()

    def move_to(self, index: int) -> None:
        self.cursor = index
        self._clamp()

    def cycle_subvolume(self, delta: int = 1) -> None:
        names = self.subvolume_names
        try:
            index = names.index(self.subvolume)
        except ValueError:
            index = 0
        self.subvolume = names[(index + delta) % len(names)]
        # Back to the top: the old position means nothing in a different list.
        self.cursor = 0
        self.top = 0
        self._clamp()

    def resize(self, width: int, height: int) -> None:
        self.width = max(width, MIN_DESCRIPTION_WIDTH)
        self.height = max(height, CHROME_HEIGHT + MIN_ROWS)
        self._clamp()

    # --- messages ---------------------------------------------------------

    def report(self, text: str) -> None:
        self.message = text
        self.message_is_error = False

    def fail(self, text: str) -> None:
        self.message = text
        self.message_is_error = True

    def clear_message(self) -> None:
        self.message = ""
        self.message_is_error = False

    # --- modes ------------------------------------------------------------

    def ask(self, question: str, pending: str) -> None:
        """Wait for y/n. `pending` names the action for whoever runs it."""
        self.mode = Mode.CONFIRM
        self.pending = pending
        self.report(question)

    def begin_input(self, purpose: str, initial: str = "") -> None:
        self.mode = Mode.INPUT
        self.input_purpose = purpose
        self.input_buffer = initial
        self.clear_message()

    def cancel(self) -> None:
        """Back to browsing, whatever was in progress."""
        self.mode = Mode.BROWSE
        self.pending = ""
        self.input_buffer = ""
        self.input_purpose = ""
        self.clear_message()

    def type_character(self, character: str) -> None:
        if self.mode is Mode.INPUT:
            self.input_buffer += character

    def backspace(self) -> None:
        if self.mode is Mode.INPUT:
            self.input_buffer = self.input_buffer[:-1]

    # --- what the list looks like after something changed -------------------

    def replace(self, snapshots: Sequence[Snapshot], keep_id: int | None = None) -> None:
        """Take a freshly loaded list, staying where the user was.

        BY ID, NOT BY POSITION. Snapshots appear on their own here - the daily
        timer, every dnf transaction - and one arriving above the cursor would
        otherwise slide the selection onto a different snapshot than the one the
        user was looking at, which matters when the next key deletes it.
        """
        want = keep_id if keep_id is not None else (self.current.id if self.current else None)
        self.snapshots = list(snapshots)
        if want is not None:
            for index, snapshot in enumerate(self.visible):
                if snapshot.id == want:
                    self.move_to(index)
                    return
        self._clamp()

    # --- rendering --------------------------------------------------------

    def render(self) -> list[str]:
        """Every line of the screen, in order, padded to nothing wider than `width`."""
        if self.mode is Mode.HELP:
            return self._fit(self.help_lines())
        lines = [self.title_line(), self.rule()]
        lines.extend(self.table_lines())
        lines.append(self.rule())
        lines.extend(self.detail_lines())
        lines.append(self.rule())
        lines.append(self.key_line())
        return self._fit(lines)

    def _fit(self, lines: Sequence[str]) -> list[str]:
        return [truncate(line, self.width) for line in lines]

    def rule(self) -> str:
        return self.glyphs.rule * self.width

    def title_line(self) -> str:
        shown = "all subvolumes" if self.subvolume == ALL else self.subvolume
        left = "btrfs-patrol"
        right = f"subvolume: {shown}"
        if self.query:
            right = f"/{self.query}  {right}"
        gap = self.width - len(left) - len(right)
        return left + " " * gap + right if gap > 0 else f"{left}  {right}"

    def table_lines(self) -> list[str]:
        rows = self.visible
        table = layout(rows, self.width - 2, self.show_subvolume)
        lines = ["  " + table.header()]
        if not rows:
            lines.append("  " + (self._empty_reason()))
            return lines
        span = self.rows_available
        for index in range(self.top, min(self.top + span, len(rows))):
            snapshot = rows[index]
            marker = f"{self.glyphs.cursor} " if index == self.cursor else "  "
            lines.append(marker + table.row(snapshot) + table.description(snapshot))
        return lines

    def _empty_reason(self) -> str:
        if self.query:
            return f"no snapshots match /{self.query}"
        if self.subvolume != ALL:
            return f"no snapshots of {self.subvolume} yet"
        return "no snapshots yet - press n to take one"

    def detail_lines(self) -> list[str]:
        """Two lines: the facts that do not fit a column, then the description in full."""
        if self.message:
            first = self.message
        elif self.mode is Mode.INPUT:
            first = f"{self.input_purpose}: {self.input_buffer}"
        else:
            first = self._facts()
        snapshot = self.current
        if self.mode is Mode.INPUT or self.message:
            second = ""
        else:
            second = printable(snapshot.description) if snapshot else ""
        return [first, second or ""]

    def _facts(self) -> str:
        """The highlighted snapshot's facts, as many as fit.

        DROPPED FROM THE END, and the order is chosen for that. KIND is last
        because the table always shows it, while KERNEL sits above it because
        the table is the first thing to drop KERNEL when the terminal is narrow
        - so the two never both hide it, which is when it would be missed.
        'kept' is high up because it changes what the other keys will do.
        """
        snapshot = self.current
        if snapshot is None:
            return ""
        parts = [
            f"#{snapshot.id}",
            snapshot.subvolume,
            *(["kept"] if snapshot.keep else []),
            snapshot.created.strftime("%Y-%m-%d %H:%M:%S"),
            snapshot.kernel,
            snapshot.kind,
        ]
        sep = f" {self.glyphs.separator} "
        while len(parts) > 1 and display_width(sep.join(parts)) > self.width:
            parts.pop()
        return sep.join(parts)

    def key_line(self) -> str:
        """The keys, as many as fit.

        DROPPED FROM THE RIGHT WHEN THE WIDTH RUNS OUT, rather than truncated.
        A bar ending in "/ filte\u2026" tells you there is a key without telling you
        what it does, which is worse than not offering it: the ones kept are
        the ones you cannot work without, and ? still lists them all.
        """
        if self.mode is Mode.CONFIRM:
            return "y confirm   n cancel"
        if self.mode is Mode.INPUT:
            return "enter accept   esc cancel"
        keys = [
            ("up/down", "move"),
            (self.glyphs.tab, "subvolume"),
            ("n", "new"),
            ("d", "describe"),
            ("k", "keep"),
            ("x", "delete"),
            ("/", "filter"),
            ("r", "reload"),
            ("?", "help"),
            ("q", "quit"),
        ]
        if self.current is None:
            # Nothing to act on, so offer only what still means something.
            acts_on_a_snapshot = ("d", "k", "x")
            keys = [k for k in keys if k[0] not in acts_on_a_snapshot]
        if self.query:
            keys.insert(-2, ("esc", "clear filter"))
        # ? and q go last and are never dropped: one explains the rest and the
        # other is the way out.
        keep_last = keys[-2:]
        rest = keys[:-2]
        while rest:
            line = self._join_keys([*rest, *keep_last])
            if display_width(line) <= self.width:
                return line
            rest.pop()
        return self._join_keys(keep_last)

    @staticmethod
    def _join_keys(keys: Sequence[tuple[str, str]]) -> str:
        return "   ".join(f"{key} {what}" for key, what in keys)

    def help_lines(self) -> list[str]:
        sep = self.glyphs.separator
        return [
            "btrfs-patrol " + sep + " keys",
            "",
            "  up down pgup pgdn home end   move the cursor",
            "  tab / shift-tab              next / previous subvolume",
            "  enter                        full detail for this snapshot",
            "  n                            take a snapshot of this subvolume",
            "  d                            change this snapshot's description",
            "  k                            keep / unkeep this snapshot",
            "  x                            delete this snapshot, after confirming",
            "  /                            filter, using a 'list' selector",
            "  r                            reload from disk",
            "  ?                            this help",
            "  q                            quit",
            "",
            "  Rollback and diff are not here yet: use 'btrfs-patrol rollback'.",
            "",
            "  any key to go back",
        ]
