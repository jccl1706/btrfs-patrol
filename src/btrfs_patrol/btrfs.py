# SPDX-License-Identifier: GPL-3.0-or-later
"""A thin wrapper around btrfs(8).

Commands are always passed as argument lists, never through a shell, and a
non-zero exit status always raises PatrolError with btrfs's own message.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from btrfs_patrol import system
from btrfs_patrol.errors import PatrolError

BTRFS = "btrfs"
# The top directory of every btrfs subvolume has this inode number.
SUBVOLUME_ROOT_INODE = 256


@dataclass(frozen=True)
class Subvolume:
    id: int
    path: str
    """Path as btrfs lists it. Listed from the top-level subvolume, this is relative to
    the top level, e.g. "root", "root/var/lib/portables" or "snapshots/3/snapshot"."""


def run(*args: str | Path) -> str:
    return system.run(BTRFS, *args, missing=f"{BTRFS} not found; install btrfs-progs")


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


def create_subvolume(path: Path) -> None:
    run("subvolume", "create", path)


def delete_subvolume(path: Path) -> None:
    run("subvolume", "delete", path)


def is_subvolume(path: Path) -> bool:
    """Whether path, on a btrfs filesystem, is the top directory of a subvolume."""
    return path.stat().st_ino == SUBVOLUME_ROOT_INODE


def subvolume_id(path: Path) -> int:
    """ID of the subvolume that contains path."""
    output = run("inspect-internal", "rootid", path).strip()
    if not output.isdigit():
        raise PatrolError(f"unexpected output from btrfs inspect-internal rootid: {output!r}")
    return int(output)


def get_default_subvolume_id(mount: Path) -> int:
    # "ID 256 gen 9123 top level 5 path root", or "ID 5 (FS_TREE)" for the top level.
    output = run("subvolume", "get-default", mount)
    fields = output.split()
    if len(fields) < 2 or fields[0] != "ID" or not fields[1].isdigit():
        raise PatrolError(f"unexpected output from btrfs subvolume get-default: {output!r}")
    return int(fields[1])


def set_default_subvolume(subvolume_id: int, mount: Path) -> None:
    run("subvolume", "set-default", str(subvolume_id), mount)
