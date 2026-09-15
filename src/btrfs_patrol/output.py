# SPDX-License-Identifier: GPL-3.0-or-later
"""Terminal output: status messages, optional colors and the snapshot table."""

from __future__ import annotations

import os
import sys
import textwrap
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
    """Status messages. Warnings, errors and prompts go to stderr so stdout stays clean."""

    def __init__(self, color: str = "auto", out: TextIO | None = None, err: TextIO | None = None):
        self.out = out or sys.stdout
        self.err = err or sys.stderr
        self.style = Style(color_enabled(color, self.out))
        self.err_style = Style(color_enabled(color, self.err))

    def info(self, message: str) -> None:
        print(f"{self.style.blue('::')} {message}", file=self.out)

    def warn(self, message: str) -> None:
        print(f"{self.err_style.yellow('warning:')} {message}", file=self.err)

    def error(self, message: str) -> None:
        print(f"{self.err_style.red('error:')} {message}", file=self.err)


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
        max([len(header), *(len(row[i]) for row in rows)]) for i, header in enumerate(headers)
    ]
    indent = sum(widths) + len(GAP) * len(widths)
    description_width = max(width - indent, MIN_DESCRIPTION_WIDTH)

    def cells(values: Sequence[str]) -> str:
        # IDs are right-aligned so the '*' marker sits next to the number.
        first = values[0].rjust(widths[0])
        rest = (value.ljust(w) for value, w in zip(values[1:], widths[1:]))
        return GAP.join([first, *rest]) + GAP

    lines = [style.bold(cells(headers) + "DESCRIPTION")]
    for snapshot, row in zip(snapshots, rows):
        prefix = cells(row)
        if snapshot.keep:
            prefix = prefix.replace("*", style.green("*"), 1)
        description = snapshot.description or "-"
        if wrap:
            parts = textwrap.wrap(description, description_width) or [description]
        elif len(description) > description_width:
            parts = [description[: description_width - 1] + "…"]
        else:
            parts = [description]
        lines.append(prefix + parts[0])
        lines.extend(" " * indent + part for part in parts[1:])
    return "\n".join(lines)
