# SPDX-License-Identifier: GPL-3.0-or-later
"""Snapshot metadata and the on-disk snapshot store.

Each snapshot lives in its own numbered directory under the snapshots
directory (by default /.snapshots, where a top-level subvolume is mounted):

    <id>/info.json   metadata written by btrfs-patrol
    <id>/snapshot    read-only btrfs snapshot of the root subvolume
    .next-id         the ID the next snapshot gets, so IDs are never reused
    .lock            held while the store is being changed
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import platform
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from btrfs_patrol import btrfs
from btrfs_patrol.errors import PatrolError

ROOT_MOUNT = Path("/")
INFO_FILE = "info.json"
SUBVOLUME_NAME = "snapshot"
COUNTER_FILE = ".next-id"
LOCK_FILE = ".lock"
FORMAT_VERSION = 1


@dataclass
class Snapshot:
    id: int
    created: datetime
    kernel: str
    kind: str = "manual"
    """How the snapshot was taken: manual, timer, dnf-pre, dnf-post or rollback."""
    description: str = ""
    keep: bool = False
    """Kept snapshots are never pruned and don't count toward retention.max_snapshots."""

    def to_json(self) -> dict[str, object]:
        return {
            "format": FORMAT_VERSION,
            "created": self.created.isoformat(timespec="seconds"),
            "kernel": self.kernel,
            "kind": self.kind,
            "description": self.description,
            "keep": self.keep,
        }

    @classmethod
    def from_json(cls, snapshot_id: int, data: dict[str, object]) -> Snapshot:
        try:
            if data["format"] != FORMAT_VERSION:
                raise PatrolError(
                    f"snapshot {snapshot_id}: unsupported metadata format {data['format']!r}"
                )
            return cls(
                id=snapshot_id,
                created=datetime.fromisoformat(data["created"]),
                kernel=str(data["kernel"]),
                kind=str(data["kind"]),
                description=str(data["description"]),
                keep=data["keep"] is True,
            )
        except (KeyError, TypeError, ValueError) as e:
            raise PatrolError(f"snapshot {snapshot_id}: invalid metadata ({e!r})") from None


def select_for_pruning(snapshots: Sequence[Snapshot], max_snapshots: int) -> list[Snapshot]:
    """The oldest snapshots that aren't kept, beyond the newest max_snapshots of them."""
    candidates = sorted((s for s in snapshots if not s.keep), key=lambda s: s.id)
    return candidates[: max(len(candidates) - max_snapshots, 0)]


def _write_atomic(path: Path, text: str) -> None:
    """Replace path with text, so a crash never leaves a half-written file."""
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


class SnapshotStore:
    def __init__(self, directory: Path) -> None:
        self.directory = directory

    def path(self, snapshot_id: int) -> Path:
        return self.directory / str(snapshot_id)

    def subvolume(self, snapshot_id: int) -> Path:
        return self.path(snapshot_id) / SUBVOLUME_NAME

    def ids(self) -> list[int]:
        try:
            entries = list(self.directory.iterdir())
        except FileNotFoundError:
            raise PatrolError(f"snapshots directory not found: {self.directory}") from None
        return sorted(
            int(entry.name)
            for entry in entries
            if entry.name.isascii() and entry.name.isdigit() and entry.is_dir()
        )

    def load(self, snapshot_id: int) -> Snapshot:
        info = self.path(snapshot_id) / INFO_FILE
        try:
            data = json.loads(info.read_text())
        except FileNotFoundError:
            raise PatrolError(f"snapshot {snapshot_id} has no {INFO_FILE}") from None
        except json.JSONDecodeError as e:
            raise PatrolError(f"{info}: invalid JSON ({e})") from None
        return Snapshot.from_json(snapshot_id, data)

    def load_all(self) -> list[Snapshot]:
        return [self.load(snapshot_id) for snapshot_id in self.ids()]

    def save(self, snapshot: Snapshot) -> None:
        _write_atomic(
            self.path(snapshot.id) / INFO_FILE, json.dumps(snapshot.to_json(), indent=2) + "\n"
        )

    @contextlib.contextmanager
    def lock(self) -> Iterator[None]:
        """Hold an exclusive lock, so concurrent runs (say, a dnf hook and the timer)
        can't pick the same ID or prune each other's snapshots."""
        if not self.directory.is_dir():
            raise PatrolError(f"snapshots directory not found: {self.directory}")
        with open(self.directory / LOCK_FILE, "a") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            yield

    def _reserve_id(self) -> int:
        """Take the next snapshot ID and advance the counter past it.

        The counter only moves forward, so an ID is never reused, even after
        the newest snapshot is deleted. It also never falls behind the
        snapshots on disk, such as in a store created before the counter existed.
        """
        counter = self.directory / COUNTER_FILE
        try:
            stored = int(counter.read_text())
        except FileNotFoundError:
            stored = 1
        except ValueError:
            raise PatrolError(f"{counter}: expected a snapshot ID") from None
        snapshot_id = max(stored, max(self.ids(), default=0) + 1)
        # Advance before creating anything, so a failed create can't hand the ID out twice.
        _write_atomic(counter, f"{snapshot_id + 1}\n")
        return snapshot_id

    def new_entry(self, kind: str, description: str = "", keep: bool = False) -> Snapshot:
        """Reserve an ID and write the metadata for a snapshot whose subvolume the caller
        then puts in place. Call this while holding lock()."""
        snapshot = Snapshot(
            id=self._reserve_id(),
            # Metadata stores whole seconds; match it so the returned snapshot equals the saved one.
            created=datetime.now().astimezone().replace(microsecond=0),
            kernel=platform.release(),
            kind=kind,
            description=description,
            keep=keep,
        )
        self.path(snapshot.id).mkdir()
        try:
            self.save(snapshot)
        except Exception:
            self.remove_entry(snapshot.id)
            raise
        return snapshot

    def remove_entry(self, snapshot_id: int) -> None:
        """Remove a snapshot's metadata and its directory, which must hold nothing else."""
        directory = self.path(snapshot_id)
        (directory / INFO_FILE).unlink(missing_ok=True)
        (directory / (INFO_FILE + ".tmp")).unlink(missing_ok=True)
        directory.rmdir()

    def create(
        self, source: Path, kind: str, description: str = "", keep: bool = False
    ) -> Snapshot:
        """Take a read-only snapshot of source. Call this while holding lock()."""
        # Metadata first, so a snapshot subvolume never exists without it.
        snapshot = self.new_entry(kind, description, keep)
        try:
            btrfs.create_snapshot(source, self.subvolume(snapshot.id), readonly=True)
        except Exception:
            self.remove_entry(snapshot.id)
            raise
        return snapshot

    def delete(self, snapshot_id: int) -> None:
        """Delete a snapshot's subvolume, then its metadata. Call this while holding lock()."""
        subvolume = self.subvolume(snapshot_id)
        if subvolume.exists():
            btrfs.delete_subvolume(subvolume)
        self.remove_entry(snapshot_id)

    def prune(self, max_snapshots: int) -> list[Snapshot]:
        """Delete snapshots beyond max_snapshots and return them. Call this while holding lock()."""
        pruned = select_for_pruning(self.load_all(), max_snapshots)
        for snapshot in pruned:
            self.delete(snapshot.id)
        return pruned
