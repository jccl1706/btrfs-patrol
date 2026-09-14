# SPDX-License-Identifier: GPL-3.0-or-later
"""A thin wrapper around btrfs(8).

Commands are always passed as argument lists, never through a shell, and a
non-zero exit status always raises PatrolError with btrfs's own message.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from btrfs_patrol.errors import PatrolError

BTRFS = "btrfs"


@dataclass(frozen=True)
class Subvolume:
    id: int
    path: str
    """Path relative to the top-level subvolume, e.g. "root" or "snapshots/3/snapshot"."""


def run(*args: str | Path) -> str:
    command = [BTRFS, *map(str, args)]
    try:
        result = subprocess.run(command, capture_output=True, text=True, check=False)
    except FileNotFoundError:
        raise PatrolError(f"{BTRFS} not found; install btrfs-progs") from None
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit status {result.returncode}"
        raise PatrolError(f"{' '.join(command)}: {detail}")
    return result.stdout


def parse_subvolume_list(output: str) -> list[Subvolume]:
    """Parse 'btrfs subvolume list' lines such as 'ID 256 gen 9123 top level 5 path root'."""
    subvolumes = []
    for line in output.splitlines():
        head, separator, path = line.partition(" path ")
        fields = head.split()
        if not separator or len(fields) < 2 or fields[0] != "ID" or not fields[1].isdigit():
            raise PatrolError(f"unexpected output from btrfs subvolume list: {line!r}")
        subvolumes.append(Subvolume(int(fields[1]), path))
    return subvolumes


def list_subvolumes(mount: Path) -> list[Subvolume]:
    """All subvolumes of the filesystem mounted at mount."""
    return parse_subvolume_list(run("subvolume", "list", mount))


def create_snapshot(source: Path, destination: Path, readonly: bool = False) -> None:
    run("subvolume", "snapshot", *(["-r"] if readonly else []), source, destination)


def delete_subvolume(path: Path) -> None:
    run("subvolume", "delete", path)


def get_default_subvolume_id(mount: Path) -> int:
    # "ID 256 gen 9123 top level 5 path root", or "ID 5 (FS_TREE)" for the top level.
    output = run("subvolume", "get-default", mount)
    fields = output.split()
    if len(fields) < 2 or fields[0] != "ID" or not fields[1].isdigit():
        raise PatrolError(f"unexpected output from btrfs subvolume get-default: {output!r}")
    return int(fields[1])


def set_default_subvolume(subvolume_id: int, mount: Path) -> None:
    run("subvolume", "set-default", str(subvolume_id), mount)
