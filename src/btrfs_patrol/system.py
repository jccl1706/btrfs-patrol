# SPDX-License-Identifier: GPL-3.0-or-later
"""Running commands, writing files safely, reading the mount table and mounting the
top-level subvolume."""

from __future__ import annotations

import contextlib
import os
import re
import subprocess
import tempfile
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path

from btrfs_patrol.errors import PatrolError

MOUNTINFO = Path("/proc/self/mountinfo")
RUN_DIR = Path("/run")


def run(*command: str | Path, missing: str | None = None) -> str:
    """Run command without a shell and return its stdout.

    A non-zero exit status raises PatrolError with the command's own message;
    missing replaces the message when the program isn't installed.
    """
    args = [str(part) for part in command]
    try:
        result = subprocess.run(args, capture_output=True, text=True, check=False)
    except FileNotFoundError:
        raise PatrolError(missing or f"{args[0]} not found") from None
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit status {result.returncode}"
        raise PatrolError(f"{' '.join(args)}: {detail}")
    return result.stdout


def unit_file_state(unit: str) -> str | None:
    """The unit's 'systemctl is-enabled' state, such as "enabled" or "disabled".

    None when systemd doesn't know the unit, or isn't there at all.
    """
    try:
        result = subprocess.run(
            ["systemctl", "is-enabled", unit], capture_output=True, text=True, check=False
        )
    except FileNotFoundError:
        return None
    state = result.stdout.strip()
    return None if state in ("", "not-found") else state


def write_atomic(path: Path, text: str, mode: int | None = None) -> None:
    """Replace path with text, so a crash never leaves a half-written file."""
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    if mode is not None:
        tmp.chmod(mode)
    os.replace(tmp, path)


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


@contextlib.contextmanager
def mounted_top_level(device: str) -> Iterator[Path]:
    """Mount the btrfs top-level subvolume (ID 5) of device on a private directory under /run."""
    directory = Path(tempfile.mkdtemp(prefix="btrfs-patrol-", dir=RUN_DIR))
    try:
        run("mount", "-o", "subvolid=5", device, directory)
        try:
            yield directory
        finally:
            run("umount", directory)
    finally:
        directory.rmdir()
