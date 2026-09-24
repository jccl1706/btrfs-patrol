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
from .output import (
    MIN_DESCRIPTION_WIDTH,
    columns,
    display_width,
    layout,
    printable,
    truncate,
    wrap_text,
)
from .selectors import select
from .snapshots import Snapshot

#: Rows the table needs before it is worth drawing at all: a header and one row.
MIN_ROWS = 2

#: Everything on the screen that is not a table row: the title band, the
#: table's own header, the two detail lines and the key band.
#:
#: IT MUST MATCH WHAT render() ACTUALLY PRODUCES, and it did not. The value was
#: 6 while the render emitted 8 - a title, three rules, a header, two detail
#: lines and the key bar - so a list long enough to fill the screen pushed the
#: last two lines past the window, and tui._paint truncated them. The key bar
#: was simply absent on any terminal with more snapshots than rows, which is
#: every machine that has been running this for a while, and it looked like a
#: deliberately bare screen rather than a miscount.
#:
#: Found by rendering at a known height and counting; there is now a test that
#: does exactly that for a range of sizes, because this number cannot be
#: checked by reading it.
CHROME_HEIGHT = 5

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

    DETAIL = "detail"
    """One snapshot's full detail is covering the screen."""

    PAGE = "page"
    """A full-screen page - a rollback plan - waiting for an answer."""


class Ink(Enum):
    """What a piece of text MEANS, not what colour it is.

    The curses layer decides how each of these is painted, and on a terminal
    with no colour they all come out plain - which is why nothing here is
    distinguished by colour ALONE. The cursor keeps its marker, kept snapshots
    keep their asterisk, errors keep the word.
    """

    PLAIN = "plain"
    DIM = "dim"
    """Present but not the point: rules, the header row, a kernel version."""
    ACCENT = "accent"
    """Where the eye should go: the cursor, a key you can press, a filter."""
    HEADING = "heading"
    WARNING = "warning"
    ERROR = "error"
    OK = "ok"


#: How each snapshot kind is inked in the KIND column. A rollback is the one
#: worth spotting in a list - it is the state a machine was in before someone
#: undid it - and the dnf kinds are the ones there are most of.
KIND_INK = {
    "rollback": Ink.WARNING,
    "dnf-pre": Ink.ACCENT,
    "dnf-post": Ink.ACCENT,
    "timer": Ink.DIM,
}


@dataclass(frozen=True)
class Span:
    """A run of text with one meaning."""

    text: str
    ink: Ink = Ink.PLAIN
    bold: bool = False


@dataclass(frozen=True)
class StyledLine:
    """One line of the screen: its spans, and what the whole row does.

    `bar` and `highlight` are row-wide because they are BACKGROUNDS - the title
    and the key line are bands across the terminal, and the cursor's row is a
    band the width of the window. The painter fills the rest of the row; the
    text here stays exactly what the plain renderer produces, so a test can
    still assert on strings.
    """

    spans: tuple[Span, ...] = ()
    bar: bool = False
    highlight: bool = False

    @property
    def text(self) -> str:
        return "".join(span.text for span in self.spans)


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
    arrows: str = "↑↓"        # ↑↓
    tab: str = "⇥"         # ⇥
    rule: str = "─"        # ─

    @classmethod
    def for_encoding(cls, encoding: str | None) -> Glyphs:
        fancy = cls()
        if not encoding:
            return cls.plain()
        try:
            "".join(
                [fancy.cursor, fancy.separator, fancy.arrows, fancy.tab, fancy.rule]
            ).encode(encoding)
        except (UnicodeEncodeError, LookupError):
            return cls.plain()
        return fancy

    @classmethod
    def plain(cls) -> Glyphs:
        return cls(cursor=">", separator="-", arrows="up/dn", tab="tab", rule="-")


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
    #: The snapshot marked with 'c', waiting for a second one to compare against.
    marked_id: int | None = None
    #: While mode is PAGE: the heading, the body, and the keys under it.
    page_title: str = ""
    page_lines: list[str] = field(default_factory=list)
    page_warnings: list[str] = field(default_factory=list)
    page_footer: str = ""

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

    def show_page(
        self, title: str, lines: Sequence[str], warnings: Sequence[str] = (), footer: str = ""
    ) -> None:
        """Cover the screen with something that needs reading before answering.

        The rollback plan is the reason this exists: its checks, its warnings
        and what it is about to do are far more than the two-line detail strip
        can hold, and it is the one action here that should not be answered
        without reading.
        """
        self.mode = Mode.PAGE
        self.page_title = title
        self.page_lines = list(lines)
        self.page_warnings = list(warnings)
        self.page_footer = footer
        self.clear_message()

    def cancel(self) -> None:
        """Back to browsing, whatever was in progress."""
        self.mode = Mode.BROWSE
        self.pending = ""
        self.input_buffer = ""
        self.input_purpose = ""
        self.page_title = ""
        self.page_lines = []
        self.page_warnings = []
        self.page_footer = ""
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
        """Every line of the screen, in order, padded to nothing wider than `width`.

        THE STYLED RENDERER IS THE SOURCE OF TRUTH and this joins its spans. A
        second pass that built the strings separately is how the plain and the
        coloured screen come to disagree the first time either changes - the
        same reason output.TableLayout exists rather than two layouts.
        """
        return [line.text for line in self.render_styled()]

    def render_styled(self) -> list[StyledLine]:
        """Every line of the screen, with what each run of text means."""
        if self.mode is Mode.HELP:
            return self._clip_all(self._page(self.help_lines()))
        if self.mode is Mode.DETAIL:
            return self._clip_all(self._page(self.detail_page()))
        if self.mode is Mode.PAGE:
            return self._clip_all(self._page(self.page_body()))
        # NO RULES BETWEEN THE REGIONS. The title and the keys are bands now,
        # which is a stronger edge than a line of ─ ever was, and the detail is
        # told from the table by being under it and differently inked. Three
        # rules cost three of the twenty-odd rows a terminal has, to repeat
        # what the colour already says.
        #
        # rule() stays: the full-screen pages still use one under their
        # heading, where there is no band to do the same job.
        lines = [self.title_styled()]
        lines.extend(self.table_styled())
        lines.extend(self.detail_styled())
        lines.append(self.key_styled())
        return self._clip_all(lines)

    def _page(self, lines: Sequence[str]) -> list[StyledLine]:
        """A full-screen page, inked by shape: a heading, warnings, the rest.

        These pages are written as plain text - a help list, a rollback plan -
        and reading their shape back is enough. Marking up every line of them by
        hand would be a second copy of the page to keep in step.
        """
        out: list[StyledLine] = []
        for index, line in enumerate(lines):
            if index == 0:
                out.append(StyledLine((Span(line, Ink.HEADING, bold=True),)))
            elif line and set(line) == {self.glyphs.rule}:
                out.append(StyledLine((Span(line, Ink.DIM),)))
            elif line.startswith("warning: "):
                out.append(StyledLine((Span("warning: ", Ink.WARNING, bold=True),
                                       Span(line[len("warning: "):], Ink.WARNING))))
            elif line.strip() in ("any key to go back",) or line.endswith("to go back"):
                out.append(StyledLine((Span(line, Ink.DIM),)))
            else:
                out.append(StyledLine((Span(line),)))
        return out

    def _clip_all(self, lines: Sequence[StyledLine]) -> list[StyledLine]:
        return [self._clip(line) for line in lines]

    def _clip(self, line: StyledLine) -> StyledLine:
        """Truncate a styled line to the width, span by span."""
        if display_width(line.text) <= self.width:
            return line
        spans: list[Span] = []
        used = 0
        for span in line.spans:
            if used >= self.width:
                break
            room = self.width - used
            span_width = display_width(span.text)
            if span_width <= room:
                spans.append(span)
                used += span_width
            else:
                spans.append(Span(truncate(span.text, room), span.ink, span.bold))
                used = self.width
        return StyledLine(tuple(spans), bar=line.bar, highlight=line.highlight)

    def _fit(self, lines: Sequence[str]) -> list[str]:
        return [truncate(line, self.width) for line in lines]

    def rule(self) -> str:
        return self.glyphs.rule * self.width

    def rule_styled(self) -> StyledLine:
        return StyledLine((Span(self.rule(), Ink.DIM),))

    def title_line(self) -> str:
        return self.title_styled().text

    def title_styled(self) -> StyledLine:
        """The top band: the program on the left, what is shown on the right."""
        shown = "all subvolumes" if self.subvolume == ALL else self.subvolume
        left = "btrfs-patrol"
        right = f"subvolume: {shown}"
        filter_text = f"/{self.query}  " if self.query else ""
        gap = self.width - len(left) - len(filter_text) - len(right)
        spans = [Span(left, Ink.HEADING, bold=True),
                 Span(" " * gap if gap > 0 else "  ")]
        # The filter is the one thing on this line that changes what the table
        # below is showing, so it is the one thing accented.
        if filter_text:
            spans.append(Span(filter_text, Ink.ACCENT, bold=True))
        spans.append(Span(right))
        return StyledLine(tuple(spans), bar=True)

    def table_lines(self) -> list[str]:
        return [line.text for line in self.table_styled()]

    def table_styled(self) -> list[StyledLine]:
        rows = self.visible
        table = layout(rows, self.width - 2, self.show_subvolume)
        lines = [StyledLine((Span("  " + table.header(), Ink.DIM, bold=True),))]
        if not rows:
            lines.append(StyledLine((Span("  " + self._empty_reason(), Ink.DIM),)))
            return lines
        span = self.rows_available
        for index in range(self.top, min(self.top + span, len(rows))):
            snapshot = rows[index]
            on_cursor = index == self.cursor
            if snapshot.id == self.marked_id:
                # The marked snapshot stays visible while the cursor moves away
                # to find the other one, which is the whole point of marking.
                marker = f"{self.glyphs.cursor}c" if on_cursor else " c"
            else:
                marker = f"{self.glyphs.cursor} " if on_cursor else "  "
            spans = [Span(marker, Ink.ACCENT, bold=True)]
            spans.extend(self._cell_spans(table, snapshot))
            spans.append(Span(table.description(snapshot), Ink.PLAIN if on_cursor else Ink.DIM))
            lines.append(StyledLine(tuple(spans), highlight=on_cursor))
        return lines

    def _cell_spans(self, table: object, snapshot: Snapshot) -> list[Span]:
        """One span per column, inked by what the column is.

        Through TableLayout.cell_parts rather than by counting characters, so
        the colours cannot drift out of step with the widths the table chose.
        """
        parts = table.cell_parts(columns(snapshot, table.headers))  # type: ignore[attr-defined]
        spans: list[Span] = []
        for header, text in zip(table.headers, parts):  # type: ignore[attr-defined]
            if header == "ID" and snapshot.keep:
                # The '*' is what says "kept" on a screen with no colour, so it
                # is coloured as well rather than instead.
                star = text.index("*")
                spans.append(Span(text[:star], Ink.PLAIN))
                spans.append(Span("*", Ink.OK, bold=True))
                spans.append(Span(text[star + 1:], Ink.PLAIN))
            elif header == "KIND":
                spans.append(Span(text, KIND_INK.get(snapshot.kind, Ink.PLAIN)))
            elif header in ("TIME", "KERNEL"):
                spans.append(Span(text, Ink.DIM))
            else:
                spans.append(Span(text))
        return spans

    def _empty_reason(self) -> str:
        if self.query:
            return f"no snapshots match /{self.query}"
        if self.subvolume != ALL:
            return f"no snapshots of {self.subvolume} yet"
        return "no snapshots yet - press n to take one"

    def detail_lines(self) -> list[str]:
        return [line.text for line in self.detail_styled()]

    def detail_styled(self) -> list[StyledLine]:
        """Two lines: what happened or what is highlighted, then its description."""
        first, second = self._detail_text()
        if self.message:
            ink = Ink.ERROR if self.message_is_error else Ink.OK
            head = StyledLine((Span(first, ink, bold=True),))
        elif self.mode is Mode.INPUT:
            # The typed text is the only live thing on the screen; the prompt
            # in front of it is not.
            prompt = f"{self.input_purpose}: "
            head = StyledLine((Span(prompt, Ink.DIM),
                               Span(self.input_buffer, Ink.ACCENT, bold=True)))
        else:
            head = StyledLine((Span(first, Ink.DIM),))
        return [head, StyledLine((Span(second),))]

    def _detail_text(self) -> tuple[str, str]:
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
        return first, second or ""

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

        ORDERED BY USEFULNESS, NOT BY DANGER, because the order is the order
        they are dropped in. Compare sits above delete and roll back: it is the
        one people reach for most and the only one of the three that changes
        nothing, while the two that do are exactly the ones worth looking up in
        ? before pressing.

        DROPPED FROM THE RIGHT WHEN THE WIDTH RUNS OUT, rather than truncated.
        A bar ending in "/ filte\u2026" tells you there is a key without telling you
        what it does, which is worse than not offering it: the ones kept are
        the ones you cannot work without, and ? still lists them all.
        """
        return self._join_keys(self._key_pairs())

    def key_styled(self) -> StyledLine:
        """The bottom band: each key accented, what it does beside it."""
        spans: list[Span] = []
        for index, (key, what) in enumerate(self._key_pairs()):
            if index:
                spans.append(Span("   "))
            spans.append(Span(key, Ink.ACCENT, bold=True))
            spans.append(Span(" " + what, Ink.DIM))
        return StyledLine(tuple(spans), bar=True)

    def _key_pairs(self) -> list[tuple[str, str]]:
        """The keys that fit, in the order they are dropped from the right."""
        if self.mode is Mode.CONFIRM:
            return [("y", "confirm"), ("n", "cancel")]
        if self.mode is Mode.INPUT:
            return [("enter", "accept"), ("esc", "cancel")]
        keys = [
            (self.glyphs.arrows, "move"),
            (self.glyphs.tab, "subvol"),
            ("n", "new"),
            ("d", "describe"),
            ("k", "keep"),
            ("c", "compare" if self.marked_id is None else f"compare with #{self.marked_id}"),
            ("x", "delete"),
            ("R", "rollback"),
            ("/", "filter"),
            ("r", "reload"),
            ("?", "help"),
            ("q", "quit"),
        ]
        if self.current is None:
            # Nothing to act on, so offer only what still means something.
            acts_on_a_snapshot = ("d", "k", "x", "R", "c")
            keys = [k for k in keys if k[0] not in acts_on_a_snapshot]
        if self.query:
            keys.insert(-2, ("esc", "clear filter"))
        # ? and q go last and are never dropped: one explains the rest and the
        # other is the way out.
        keep_last = keys[-2:]
        rest = keys[:-2]
        while rest:
            pairs = [*rest, *keep_last]
            if display_width(self._join_keys(pairs)) <= self.width:
                return pairs
            rest.pop()
        return keep_last

    @staticmethod
    def _join_keys(keys: Sequence[tuple[str, str]]) -> str:
        return "   ".join(f"{key} {what}" for key, what in keys)

    def page_body(self) -> list[str]:
        """A full-screen page: heading, body, warnings, then the keys."""
        lines = [self.page_title, self.rule()]
        lines.extend(self.page_lines)
        if self.page_warnings:
            lines.append("")
            lines.extend(f"warning: {text}" for text in self.page_warnings)
        lines.append("")
        lines.append(self.rule())
        lines.append(self.page_footer)
        return lines

    def detail_page(self) -> list[str]:
        """Everything known about the highlighted snapshot, nothing abbreviated.

        The place where the description is not truncated and the path is spelled
        out - the table cannot afford either, and both are what you want before
        deciding to delete something.
        """
        snapshot = self.current
        if snapshot is None:
            return ["no snapshot selected", "", "  any key to go back"]
        rows = [
            ("id", str(snapshot.id)),
            ("subvolume", snapshot.subvolume),
            ("taken", snapshot.created.strftime("%Y-%m-%d %H:%M:%S")),
            ("kernel", snapshot.kernel),
            ("kind", snapshot.kind),
            ("kept", "yes, never pruned automatically" if snapshot.keep else "no"),
        ]
        width = max(len(name) for name, _ in rows)
        lines = [f"snapshot {snapshot.id}", ""]
        lines.extend(f"  {name.ljust(width)}  {value}" for name, value in rows)
        lines.append("")
        lines.append("  description")
        description = printable(snapshot.description)
        if description:
            lines.extend(f"    {part}" for part in wrap_text(description, max(self.width - 4, 20)))
        else:
            lines.append("    (none)")
        lines.append("")
        lines.append("  any key to go back")
        return lines

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
            "  R                            roll back to this snapshot (shift, deliberately)",
            "  c                            compare: mark one snapshot, then press c on another",
            "  /                            filter, using a 'list' selector",
            "  r                            reload from disk",
            "  ?                            this help",
            "  q                            quit",
            "",
            "  Everything the roadmap asked for is here.",
            "",
            "  any key to go back",
        ]
