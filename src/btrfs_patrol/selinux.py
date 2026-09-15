# SPDX-License-Identifier: GPL-3.0-or-later
"""SELinux: keeping full relabels out of the snapshots, and labeling the store.

Two problems, both found on a Fedora 44 VM with SELinux enforcing:

1. A full relabel (fixfiles, as run for /.autorelabel) descends into the
   mounted snapshots directory and gives everything there the policy's label
   for it, snapperd_data_t. Read-only snapshots can't be changed, but a
   "rollback" snapshot is the previous root subvolume, and it is writable:
   a dry run found 73,951 files a relabel would change. Rolling back to it
   afterwards would boot a system whose every file is snapperd_data_t. So
   the snapshots directory goes into /etc/selinux/fixfiles_exclude_dirs,
   which fixfiles honours - whenever a policy is installed, even if SELinux is
   off now, because turning it on is exactly when a relabel runs.

2. A new btrfs subvolume has no label, so the store and every entry created
   in it were unlabeled_t. The store's own files - the directory, the entry
   directories and their metadata - get the policy's label with restorecon,
   never recursively: what is inside a snapshot must keep the labels of the
   system it was taken from. Entries created later inherit the store's label.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from btrfs_patrol import system
from btrfs_patrol.snapshots import COUNTER_FILE, INFO_FILE, LOCK_FILE

SELINUX_DIR = Path("/etc/selinux")
EXCLUDE_FILE = SELINUX_DIR / "fixfiles_exclude_dirs"
SELINUXFS_ENFORCE = Path("/sys/fs/selinux/enforce")


def policy_installed() -> bool:
    return (SELINUX_DIR / "config").is_file()


def enabled() -> bool:
    """SELinux is on, enforcing or permissive."""
    return SELINUXFS_ENFORCE.exists()


def excluded(snapshots_dir: Path, exclude_text: str) -> bool:
    """Whether fixfiles_exclude_dirs text lists snapshots_dir."""
    wanted = str(snapshots_dir).rstrip("/")
    return any(line.strip().rstrip("/") == wanted for line in exclude_text.splitlines())


def _exclude_text() -> str:
    try:
        return EXCLUDE_FILE.read_text()
    except FileNotFoundError:
        return ""


def exclusion_missing(snapshots_dir: Path) -> bool:
    """A policy is installed, and a full relabel would reach into the snapshots."""
    return policy_installed() and not excluded(snapshots_dir, _exclude_text())


def add_exclusion(snapshots_dir: Path) -> None:
    current = _exclude_text()
    separator = "\n" if current and not current.endswith("\n") else ""
    system.write_atomic(EXCLUDE_FILE, f"{current}{separator}{snapshots_dir}\n", mode=0o644)


def store_paths(snapshots_dir: Path) -> list[Path]:
    """The store's own files, which get the store's label - never a snapshot's contents."""
    paths = [snapshots_dir]
    for entry in sorted(snapshots_dir.iterdir()):
        if entry.name in (LOCK_FILE, COUNTER_FILE):
            paths.append(entry)
        elif entry.name.isascii() and entry.name.isdigit() and entry.is_dir():
            paths.append(entry)
            info = entry / INFO_FILE
            if info.exists():
                paths.append(info)
    return paths


def _restorecon(*args: str | Path) -> str:
    return system.run(
        "restorecon", *args, missing="restorecon not found; install policycoreutils"
    )


def mislabeled(paths: Sequence[Path]) -> list[str]:
    """What restorecon would relabel among paths, without changing anything."""
    output = _restorecon("-n", "-v", *paths)
    return [line for line in output.splitlines() if line.startswith("Would relabel")]


def label(paths: Sequence[Path]) -> None:
    """Give paths the policy's labels. restorecon without -R: no directory is descended."""
    _restorecon(*paths)
