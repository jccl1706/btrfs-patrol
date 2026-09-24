#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Draw the terminal interface as an SVG, for the README.

    python3 tools/tui-preview.py docs/tui.svg

WHY NOT A SCREENSHOT OF A TERMINAL. One was taken first, with grim against a
real kitty window, and every part of that was a fight: the compositor tiles the
window at whatever width it likes, kitty keeps its own cell grid when the frame
is resized under it, and the capture raced the repaint - three attempts, each
cut off down the right-hand side. A screenshot also fixes one machine's font,
theme and HiDPI scale into the repository, and goes stale the moment the layout
changes with nothing to say it has.

This renders from Screen.render_styled(), which is the same spans tui.py paints,
so the picture cannot drift from the program: change the layout and re-run this.
What it does NOT cover is the painter itself - that a span reaches curses in one
piece is what tests/test_tui_pty.py is for, and a blank row for kept snapshots
got past this kind of rendering once already.

SVG rather than PNG because there is no image library here and none is worth a
dependency for one picture: this is text, GitHub renders it, and it stays sharp.
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from btrfs_patrol import config as config_mod          # noqa: E402
from btrfs_patrol.screen import Ink, Screen            # noqa: E402
from btrfs_patrol.snapshots import Snapshot            # noqa: E402

#: The palette tui.Palette asks curses for, in the colours a terminal gives it.
#: Adwaita's, so the picture looks like the desktop this was written on rather
#: than like whatever a browser thinks "blue" is.
COLORS = {
    Ink.PLAIN: "#deddda",
    Ink.DIM: "#9a9996",
    Ink.ACCENT: "#4fd2fd",
    Ink.HEADING: "#51a1ff",
    Ink.WARNING: "#f8e45c",
    Ink.ERROR: "#ff7b63",
    Ink.OK: "#57e389",
}
BACKGROUND = "#1e1e1e"
BAND_BG = "#1c71d8"
BAND_FG = "#ffffff"
HIGHLIGHT_BG = "#303030"

CELL_W, CELL_H, PAD = 8.4, 19.0, 14.0
KERNEL, OLD = "7.2.7-200.fc44.x86_64", "7.2.4-200.fc44.x86_64"

ROWS = [
    (1, "2026-09-15 11:10:18", OLD, "manual", "before the new kernel", True),
    (2, "2026-09-16 11:02:10", OLD, "dnf-pre", "upgrade kernel-core, mesa-dri-drivers +12 more", False),
    (3, "2026-09-16 11:10:14", OLD, "rollback", "state before rollback to 2", False),
    (4, "2026-09-17 00:10:02", KERNEL, "timer", "daily", False),
    (5, "2026-09-18 09:41:55", KERNEL, "dnf-pre", "install btop, tree; upgrade systemd +3 more", False),
    (6, "2026-09-19 00:10:04", KERNEL, "timer", "daily", False),
    (7, "2026-09-20 16:22:07", KERNEL, "manual", "before converting /var/log to a subvolume", True),
    (8, "2026-09-21 00:10:01", KERNEL, "timer", "daily", False),
    (9, "2026-09-22 13:05:44", KERNEL, "dnf-post", "upgrade kernel-core, kernel-modules +2 more", False),
    (10, "2026-09-23 00:10:03", KERNEL, "timer", "daily", False),
]


def escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def build(width: int = 96, height: int = 15) -> Screen:
    """A screen whose rows exactly fit the demo list.

    height is rows + CHROME_HEIGHT, so the picture has no empty band under the
    last snapshot. If you add a row to ROWS, add one here.
    """
    snapshots = [
        Snapshot(
            id=number,
            created=datetime.strptime(when, "%Y-%m-%d %H:%M:%S"),
            kernel=kernel,
            kind=kind,
            description=description,
            keep=keep,
        )
        for number, when, kernel, kind, description, keep in ROWS
    ]
    return Screen(config=config_mod.parse({}), snapshots=snapshots, width=width, height=height)


def render(screen: Screen) -> str:
    lines = screen.render_styled()
    w = screen.width * CELL_W + PAD * 2
    h = len(lines) * CELL_H + PAD * 2
    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{w:.0f}" height="{h:.0f}" '
        f'viewBox="0 0 {w:.0f} {h:.0f}" font-family="JetBrains Mono, DejaVu Sans Mono, monospace" '
        f'font-size="14">',
        f'<rect width="100%" height="100%" rx="8" fill="{BACKGROUND}"/>',
    ]
    for row, line in enumerate(lines):
        y = PAD + row * CELL_H
        if line.bar or line.highlight:
            fill = BAND_BG if line.bar else HIGHLIGHT_BG
            out.append(f'<rect x="{PAD:.0f}" y="{y:.1f}" width="{screen.width * CELL_W:.0f}" '
                       f'height="{CELL_H:.0f}" fill="{fill}"/>')
        column = 0
        for span in line.spans:
            if not span.text:
                continue
            x = PAD + column * CELL_W
            colour = BAND_FG if line.bar else COLORS[span.ink]
            weight = ' font-weight="bold"' if span.bold else ""
            out.append(f'<text x="{x:.1f}" y="{y + CELL_H - 5:.1f}" fill="{colour}"{weight} '
                       f'xml:space="preserve">{escape(span.text)}</text>')
            column += len(span.text)
    out.append("</svg>")
    return "\n".join(out)


if __name__ == "__main__":
    target = Path(sys.argv[1] if len(sys.argv) > 1 else "docs/tui.svg")
    target.write_text(render(build()) + "\n")
    print(f"wrote {target}")
