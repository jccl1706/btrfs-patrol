# SPDX-License-Identifier: GPL-3.0-or-later
"""Terminal output: status messages, optional colors and the snapshot table."""

from __future__ import annotations

import os
import sys
import unicodedata
from collections.abc import Sequence
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

def format_table(
    snapshots: Sequence[Snapshot],
    style: Style,
    width: int,
    wrap: bool = False,
    show_subvolume: bool = False,
) -> str:
    """Render snapshots as a table that fits in width columns.

    Long descriptions are truncated with '…', or wrapped onto indented lines
    when wrap is true. Kept snapshots are marked with '*' before their ID. The
    SUBVOLUME column is only shown when asked for, so a system that snapshots
    root alone keeps the narrower table.
    """
    headers = ["ID", "DATE", "TIME", *(["SUBVOLUME"] if show_subvolume else []), "KERNEL", "KIND"]
    rows = [
        (
            f"{'*' if s.keep else ''}{s.id}",
            s.created.strftime("%Y-%m-%d"),
            s.created.strftime("%H:%M:%S"),
            *([s.subvolume] if show_subvolume else []),
            s.kernel,
            s.kind,
        )
        for s in snapshots
    ]
    widths = [
        max([display_width(header), *(display_width(row[i]) for row in rows)])
        for i, header in enumerate(headers)
    ]
    indent = sum(widths) + len(GAP) * len(widths)
    description_width = max(width - indent, MIN_DESCRIPTION_WIDTH)

    def cells(values: Sequence[str]) -> str:
        # IDs are right-aligned so the '*' marker sits next to the number.
        first = pad(values[0], widths[0], right=True)
        rest = (pad(value, w) for value, w in zip(values[1:], widths[1:]))
        return GAP.join([first, *rest]) + GAP

    lines = [style.bold(cells(headers) + "DESCRIPTION")]
    for snapshot, row in zip(snapshots, rows):
        prefix = cells(row)
        if snapshot.keep:
            prefix = prefix.replace("*", style.green("*"), 1)
        description = printable(snapshot.description) or "-"
        if wrap:
            parts = wrap_text(description, description_width) or [description]
        else:
            parts = [truncate(description, description_width)]
        lines.append(prefix + parts[0])
        lines.extend(" " * indent + part for part in parts[1:])
    return "\n".join(lines)
