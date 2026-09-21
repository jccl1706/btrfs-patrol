# SPDX-License-Identifier: GPL-3.0-or-later
"""Terminal output: status messages, optional colors and the snapshot table."""

from __future__ import annotations

import os
import sys
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TextIO

from btrfs_patrol.snapshots import Snapshot

GAP = "  "
MIN_DESCRIPTION_WIDTH = 20


def color_enabled(setting: str, stream: TextIO) -> bool:
    if setting == "always":
        return True
    if setting == "never":
        return False
    return stream.isatty() and not os.environ.get("NO_COLOR") and os.environ.get("TERM") != "dumb"


class Style:
    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled

    def _sgr(self, code: str, text: str) -> str:
        return f"\033[{code}m{text}\033[0m" if self.enabled else text

    def bold(self, text: str) -> str:
        return self._sgr("1", text)

    def red(self, text: str) -> str:
        return self._sgr("1;31", text)

    def green(self, text: str) -> str:
        return self._sgr("1;32", text)

    def yellow(self, text: str) -> str:
        return self._sgr("1;33", text)

    def blue(self, text: str) -> str:
        return self._sgr("1;34", text)


class Console:
    """Status messages. Warnings, errors and prompts go to stderr so stdout stays clean.

    EVERY MESSAGE FLUSHES stdout first. stdout is block-buffered when it is a
    pipe or a file while stderr is not, so without this a command that prints a
    plan and then warns about it has the warning arrive FIRST once the output is
    piped anywhere - which is exactly when someone is reading it later and least
    able to tell that the order is an artefact.
    """

    def __init__(self, color: str = "auto", out: TextIO | None = None, err: TextIO | None = None):
        self.out = out or sys.stdout
        self.err = err or sys.stderr
        self.style = Style(color_enabled(color, self.out))
        self.err_style = Style(color_enabled(color, self.err))

    def _sync(self) -> None:
        try:
            self.out.flush()
        except (OSError, ValueError):
            pass

    def info(self, message: str) -> None:
        self._sync()
        print(f"{self.style.blue('::')} {message}", file=self.out)

    def warn(self, message: str) -> None:
        self._sync()
        print(f"{self.err_style.yellow('warning:')} {message}", file=self.err)

    def error(self, message: str) -> None:
        self._sync()
        print(f"{self.err_style.red('error:')} {message}", file=self.err)



def printable(text: str) -> str:
    """Text safe to put in a table.

    Descriptions are written by people and by dnf's package lists, and are
    printed straight to a terminal. A newline splits one snapshot across two
    lines so the listing stops being one row per snapshot; an escape sequence is
    obeyed, not shown, so a description can clear the screen or move the cursor;
    and a bidirectional override reorders what is displayed, which matters when
    the text says what a rollback is about to restore. Stored text is left
    alone - this is only how it is shown.
    """
    out = []
    for character in text:
        category = unicodedata.category(character)
        if category == "Cc":
            out.append(" ")  # newline, tab, escape: keep the words apart
        elif category in ("Cf", "Co", "Cs", "Cn"):
            continue  # zero width, bidi overrides, private use, unassigned
        else:
            out.append(character)
    return "".join(out)


def char_width(character: str) -> int:
    """Terminal columns one character occupies: 0 combining, 2 wide, else 1."""
    if unicodedata.combining(character):
        return 0
    return 2 if unicodedata.east_asian_width(character) in ("W", "F") else 1


def display_width(text: str) -> int:
    """Terminal columns text occupies, which is not len() for CJK or emoji."""
    return sum(char_width(character) for character in text)


def pad(text: str, width: int, right: bool = False) -> str:
    """Pad to width DISPLAY columns; str.ljust counts code points instead."""
    filler = " " * max(width - display_width(text), 0)
    return filler + text if right else text + filler


def truncate(text: str, limit: int) -> str:
    """Cut to limit display columns, ending with '…', without splitting a character."""
    if display_width(text) <= limit:
        return text
    kept, used = [], 0
    for character in text:
        width = char_width(character)
        if used + width > limit - 1:  # room for the ellipsis
            break
        kept.append(character)
        used += width
    return "".join(kept) + "…"


def wrap_text(text: str, limit: int) -> list[str]:
    """Wrap on whitespace to limit display columns, breaking a long word if it must."""
    lines: list[str] = []
    current, used = "", 0
    for word in text.split():
        word_width = display_width(word)
        if current and used + 1 + word_width > limit:
            lines.append(current)
            current, used = "", 0
        while word_width > limit:  # a single word wider than the column
            head = truncate(word, limit + 1)[:-1] or word[:1]
            lines.append(head)
            word = word[len(head):]
            word_width = display_width(word)
        if current:
            current += " " + word
            used += 1 + word_width
        else:
            current, used = word, word_width
    if current:
        lines.append(current)
    return lines

@dataclass(frozen=True)
class TableLayout:
    """The column widths for a set of snapshots, and how to render one row.

    SPLIT OUT OF format_table SO THE TUI CAN DRAW ONE ROW AT A TIME. It needs a
    cursor and it scrolls, so it cannot take the whole table as a single string
    - and a second copy of this layout is how the two come to disagree about
    what a snapshot looks like the first time either changes.
    """

    headers: tuple[str, ...]
    widths: tuple[int, ...]
    show_subvolume: bool
    description_width: int

    @property
    def indent(self) -> int:
        """Where the DESCRIPTION column starts."""
        return sum(self.widths) + len(GAP) * len(self.widths)

    def header(self) -> str:
        return self.cells(self.headers) + "DESCRIPTION"

    def cells(self, values: Sequence[str]) -> str:
        # IDs are right-aligned so the '*' marker sits next to the number.
        first = pad(values[0], self.widths[0], right=True)
        rest = (pad(value, w) for value, w in zip(values[1:], self.widths[1:]))
        return GAP.join([first, *rest]) + GAP

    def row(self, snapshot: Snapshot) -> str:
        """The row's fixed columns, without the description."""
        return self.cells(columns(snapshot, self.headers))

    def description(self, snapshot: Snapshot) -> str:
        """The description as it appears in the table: printable, truncated, never empty."""
        return truncate(printable(snapshot.description) or "-", self.description_width)


#: Columns the table gives up when the width is tight, in the order they go.
#: KERNEL first and TIME second: a date and a description identify a snapshot,
#: and a kernel version rarely does. ID, DATE, KIND and DESCRIPTION always stay,
#: and SUBVOLUME is already only asked for when it distinguishes anything.
DROPPABLE = ("KERNEL", "TIME")


def _value(snapshot: Snapshot, header: str) -> str:
    if header == "ID":
        return f"{'*' if snapshot.keep else ''}{snapshot.id}"
    if header == "DATE":
        return snapshot.created.strftime("%Y-%m-%d")
    if header == "TIME":
        return snapshot.created.strftime("%H:%M:%S")
    if header == "SUBVOLUME":
        return snapshot.subvolume
    if header == "KERNEL":
        return snapshot.kernel
    return snapshot.kind


def columns(snapshot: Snapshot, headers: Sequence[str]) -> tuple[str, ...]:
    """A snapshot's fixed columns, in the order headers gives them."""
    return tuple(_value(snapshot, header) for header in headers)


def layout(snapshots: Sequence[Snapshot], width: int, show_subvolume: bool = False) -> TableLayout:
    """Measure the columns needed for snapshots within width.

    COLUMNS ARE DROPPED WHEN THEY DO NOT FIT, rather than letting the row run
    past the width it was given. A long kernel version - and Fedora's are long -
    could otherwise leave the description four columns wide on an 80-column
    terminal, or push the row off the edge entirely, which is the one thing a
    width argument is supposed to prevent.
    """
    headers = ["ID", "DATE", "TIME", *(["SUBVOLUME"] if show_subvolume else []), "KERNEL", "KIND"]
    for droppable in (None, *DROPPABLE):
        if droppable is not None:
            headers.remove(droppable)
        widths = tuple(
            max([display_width(header), *(display_width(_value(s, header)) for s in snapshots)])
            for header in headers
        )
        indent = sum(widths) + len(GAP) * len(widths)
        if width - indent >= MIN_DESCRIPTION_WIDTH or droppable == DROPPABLE[-1]:
            return TableLayout(
                headers=tuple(headers),
                widths=widths,
                show_subvolume=show_subvolume,
                description_width=max(width - indent, MIN_DESCRIPTION_WIDTH),
            )
    raise AssertionError("unreachable")


def format_table(
    snapshots: Sequence[Snapshot],
    style: Style,
    width: int,
    wrap: bool = False,
    show_subvolume: bool = False,
) -> str:
    """Render snapshots as a table that fits in width columns.

    Long descriptions are truncated with '\u2026', or wrapped onto indented lines
    when wrap is true. Kept snapshots are marked with '*' before their ID. The
    SUBVOLUME column is only shown when asked for, so a system that snapshots
    root alone keeps the narrower table.
    """
    table = layout(snapshots, width, show_subvolume)
    lines = [style.bold(table.header())]
    for snapshot in snapshots:
        prefix = table.row(snapshot)
        if snapshot.keep:
            prefix = prefix.replace("*", style.green("*"), 1)
        description = printable(snapshot.description) or "-"
        if wrap:
            parts = wrap_text(description, table.description_width) or [description]
        else:
            parts = [truncate(description, table.description_width)]
        lines.append(prefix + parts[0])
        lines.extend(" " * table.indent + part for part in parts[1:])
    return "\n".join(lines)
