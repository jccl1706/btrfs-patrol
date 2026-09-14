# SPDX-License-Identifier: GPL-3.0-or-later
"""Reading the mount table."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

MOUNTINFO = Path("/proc/self/mountinfo")


@dataclass(frozen=True)
class Mount:
    root: str
    """Directory of the filesystem that is mounted; for btrfs, the subvolume, e.g. "/root"."""
    mount_point: str
    fstype: str
    source: str


def parse_mountinfo(text: str) -> list[Mount]:
    """Parse /proc/<pid>/mountinfo; see proc_pid_mountinfo(5)."""
    mounts = []
    for line in text.splitlines():
        fields = line.split()
        # Optional fields vary in number and end with a lone "-".
        separator = fields.index("-")
        mounts.append(
            Mount(
                root=_unescape(fields[3]),
                mount_point=_unescape(fields[4]),
                fstype=fields[separator + 1],
                source=_unescape(fields[separator + 2]),
            )
        )
    return mounts


def _unescape(field: str) -> str:
    """Undo mountinfo's octal escapes, such as \\040 for a space."""
    return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m[1], 8)), field)


def read_mounts(path: Path = MOUNTINFO) -> list[Mount]:
    return parse_mountinfo(path.read_text())


def find_mount(mount_point: Path, mounts: Sequence[Mount]) -> Mount | None:
    """The mount visible at mount_point: the last one listed there, since later mounts stack on top."""
    matches = [m for m in mounts if m.mount_point == str(mount_point)]
    return matches[-1] if matches else None
