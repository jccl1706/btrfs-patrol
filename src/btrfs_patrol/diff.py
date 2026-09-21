# SPDX-License-Identifier: GPL-3.0-or-later
"""What changed between two snapshots, asked of btrfs rather than worked out.

    btrfs send --no-data -p <older> <newer> | btrfs receive --dump

btrfs already knows which extents and metadata differ between two snapshots that
share a parent, so it can say what changed without reading a single file. The
alternative was walking both trees with something like `diff -rq`, which stats
every file in both and therefore gets slower as the system grows - on exactly
the subvolume most worth comparing. `--no-data` keeps file contents out of the
stream: the sizes are reported, the bytes are never read.

It needs both snapshots to be READ-ONLY, which they are - SnapshotStore creates
them that way - and it needs root, like everything else that touches btrfs here.

THE STREAM IS NOT A DIFF, and most of this file is the distance between the two.
It is a recipe for turning the older snapshot into the newer one, so it says
things like "make a file called o50578-55-0, set its mode, then rename it to
difftest/added.txt". Three things in particular have to be undone:

  - Paths are prefixed with the subvolume's own directory name, so everything
    arrives as "./snapshot/difftest/added.txt".

  - A NEW FILE IS CREATED UNDER A TEMPORARY NAME - o<inode>-<transid>-<n> - and
    only given its real name by a later rename. Reading the stream literally
    reports a file nobody ever created called o50578-55-0.

  - A RENAME COMES THROUGH AS link + unlink, not as a rename, and the link's
    dest= is the only path in the whole stream WITHOUT the "./snapshot/" prefix.
    Treating those two lines separately reports a deletion and an addition of
    the same file.

Every one of those was found by running it against snapshots whose differences
were known in advance, and tests/data/ keeps that stream.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from .errors import PatrolError

#: The temporary name btrfs gives a file before renaming it into place.
TEMP_NAME = re.compile(r"\Ao\d+-\d+-\d+\Z")

#: Commands that bring something into existence, under a temporary name.
CREATES = {"mkfile", "mkdir", "mknod", "mkfifo", "mksock", "symlink"}

#: Commands that change a file's contents. With --no-data it is update_extent
#: rather than write, and clone appears when a file was reflinked.
CONTENT = {"write", "update_extent", "truncate", "clone"}

#: Commands that change metadata in a way someone meant.
METADATA = {"chmod", "chown", "set_xattr", "remove_xattr"}

#: Timestamps, which are reported for paths nobody touched.
#:
#: ON ITS OWN THIS IS NOT A CHANGE. Running a program updates its atime, so a
#: stream taken across any real work is full of lines like
#:
#:     utimes  ./snapshot/usr/bin/rmdir  atime=... mtime=2026-08-03 ctime=...
#:
#: for a binary that was executed and nothing more - that one is in the test
#: data because running rmdir was how a directory got deleted. A directory's
#: mtime moves for the same sort of reason, because a child changed, and the
#: child is already in the list. So utimes counts only alongside a real change
#: to the same path, where it is the timestamp that went with it.
TIMES = {"utimes"}


class Change(Enum):
    ADDED = "added"
    REMOVED = "removed"
    MODIFIED = "modified"
    RENAMED = "renamed"
    ATTRS = "attrs"

    @property
    def marker(self) -> str:
        return {"added": "+", "removed": "-", "modified": "~", "renamed": ">", "attrs": "a"}[
            self.value
        ]


@dataclass(frozen=True)
class Entry:
    change: Change
    path: str
    #: Where it came from, for a rename.
    old_path: str = ""

    def describe(self) -> str:
        if self.change is Change.RENAMED:
            return f"{self.change.marker} {self.old_path} -> {self.path}"
        return f"{self.change.marker} {self.path}"


@dataclass
class Comparison:
    """What changed, grouped."""

    older_id: int
    newer_id: int
    entries: list[Entry] = field(default_factory=list)

    def of(self, change: Change) -> list[Entry]:
        return [e for e in self.entries if e.change is change]

    @property
    def empty(self) -> bool:
        return not self.entries

    def summary(self) -> str:
        counts = [
            (change, len(self.of(change)))
            for change in (Change.ADDED, Change.REMOVED, Change.MODIFIED, Change.RENAMED, Change.ATTRS)
        ]
        parts = [f"{count} {change.value}" for change, count in counts if count]
        return ", ".join(parts) if parts else "no differences"

    def lines(self) -> list[str]:
        """Every change, grouped and in a stable order."""
        out: list[str] = []
        for change in (Change.ADDED, Change.REMOVED, Change.MODIFIED, Change.RENAMED, Change.ATTRS):
            found = sorted(self.of(change), key=lambda e: e.path)
            if not found:
                continue
            out.extend(entry.describe() for entry in found)
        return out


def parse_fields(rest: str) -> dict[str, str]:
    """The key=value tail of a dump line.

    Values can hold spaces - an SELinux xattr does - so this splits on the NEXT
    key rather than on whitespace.
    """
    fields: dict[str, str] = {}
    matches = list(re.finditer(r"(\w+)=", rest))
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(rest)
        fields[match.group(1)] = rest[match.end() : end].strip()
    return fields


def strip_subvolume(path: str) -> str:
    """"./snapshot/difftest/added.txt" -> "difftest/added.txt".

    The stream names everything relative to the stream itself, whose first
    component is the subvolume's directory. The subvolume's own line is "."
    once stripped.
    """
    cleaned = path.removeprefix("./")
    head, _, tail = cleaned.partition("/")
    return tail if tail else ""


def parse(dump: str) -> list[tuple[str, str, dict[str, str]]]:
    """Dump text to (command, path, fields), with paths still as written."""
    parsed = []
    for line in dump.splitlines():
        if not line.strip():
            continue
        parts = line.split(None, 1)
        command = parts[0]
        rest = parts[1] if len(parts) > 1 else ""
        # The path runs to the first key=value, or to the end.
        match = re.search(r"\s+\w+=", rest)
        path = (rest[: match.start()] if match else rest).strip()
        fields = parse_fields(rest[match.start() :]) if match else {}
        parsed.append((command, path, fields))
    return parsed


def interpret(dump: str) -> list[Entry]:
    """Turn a receive --dump stream into what actually changed."""
    commands = parse(dump)

    # Pass one: what each temporary name ends up being called. Without this a
    # new file is reported as "o50578-55-0", which is nobody's filename.
    final_name: dict[str, str] = {}
    for command, path, fields in commands:
        if command != "rename":
            continue
        source = strip_subvolume(path)
        if TEMP_NAME.match(source):
            final_name[source] = strip_subvolume(fields.get("dest", ""))

    def resolve(path: str) -> str:
        return final_name.get(path, path)

    created: set[str] = set()
    removed: set[str] = set()
    modified: set[str] = set()
    attrs: set[str] = set()
    renamed: dict[str, str] = {}

    for command, raw_path, fields in commands:
        path = resolve(strip_subvolume(raw_path))
        if command == "snapshot" or not path:
            continue
        if command in CREATES:
            created.add(path)
        elif command == "link":
            # The rename half that names the new path. Its dest is the OLD
            # path, and it is the one path in the stream with no prefix.
            renamed[fields.get("dest", "").removeprefix("./")] = path
        elif command in ("unlink", "rmdir"):
            removed.add(path)
        elif command in CONTENT:
            modified.add(path)
        elif command in METADATA:
            attrs.add(path)
        elif command in TIMES:
            pass  # see TIMES - never a change by itself
        elif command == "rename":
            source = strip_subvolume(raw_path)
            if not TEMP_NAME.match(source):
                renamed[source] = strip_subvolume(fields.get("dest", ""))

    # A renamed file arrives as a link to the new name and an unlink of the old
    # one; neither half is an addition or a deletion on its own.
    for old, new in renamed.items():
        removed.discard(old)
        created.discard(new)
        modified.discard(new)
        attrs.discard(new)

    # Something created in this window was not modified in it, it was written.
    modified -= created
    attrs -= created | modified | removed
    # A directory's mtime changes because a child changed; saying so twice is noise.
    removed -= created

    entries = [Entry(Change.ADDED, path) for path in created]
    entries += [Entry(Change.REMOVED, path) for path in removed]
    entries += [Entry(Change.MODIFIED, path) for path in modified]
    entries += [Entry(Change.RENAMED, new, old_path=old) for old, new in renamed.items()]
    entries += [Entry(Change.ATTRS, path) for path in attrs]
    return entries


def compare(older: Path, newer: Path, older_id: int = 0, newer_id: int = 0) -> Comparison:
    """Run btrfs and interpret what it says about the two snapshot subvolumes."""
    for path in (older, newer):
        if not path.is_dir():
            raise PatrolError(f"{path} is not there; the snapshot has no subvolume")
    send = subprocess.Popen(
        ["btrfs", "send", "--no-data", "-p", str(older), str(newer)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        receive = subprocess.run(
            ["btrfs", "receive", "--dump"],
            stdin=send.stdout,
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        send.kill()
        raise PatrolError("btrfs not found; install btrfs-progs") from None
    finally:
        if send.stdout is not None:
            send.stdout.close()
    send_error = send.stderr.read().decode(errors="replace").strip() if send.stderr else ""
    send.wait()
    if send.returncode != 0:
        raise PatrolError(f"btrfs send: {send_error or f'exit status {send.returncode}'}")
    if receive.returncode != 0:
        detail = receive.stderr.strip() or f"exit status {receive.returncode}"
        raise PatrolError(f"btrfs receive: {detail}")
    return Comparison(older_id=older_id, newer_id=newer_id, entries=interpret(receive.stdout))
