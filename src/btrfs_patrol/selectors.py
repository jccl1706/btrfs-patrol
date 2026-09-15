# SPDX-License-Identifier: GPL-3.0-or-later
"""Snapshot selectors.

A selector is either a list of IDs and ID ranges, such as "3" or "1,10,20-23",
or a single field match:

    date=2026-09        date (YYYY-MM-DD) contains the text
    time=16:            time (HH:MM:SS) contains the text
    kernel=6.17         kernel version contains the text
    kind=dnf-pre        kind contains the text
    description=gnome   description contains the text
    keep=yes            kept snapshots (keep=no for the others)

Text matches ignore case. Field matches can't be combined with commas, so the
text may contain commas.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence

from btrfs_patrol.errors import PatrolError
from btrfs_patrol.snapshots import Snapshot

_FIELDS: dict[str, Callable[[Snapshot], str]] = {
    "date": lambda s: s.created.strftime("%Y-%m-%d"),
    "time": lambda s: s.created.strftime("%H:%M:%S"),
    "kernel": lambda s: s.kernel,
    "kind": lambda s: s.kind,
    "description": lambda s: s.description,
}
_ID = re.compile(r"[0-9]+")
_RANGE = re.compile(r"([0-9]+)-([0-9]+)")


def select(selector: str, snapshots: Sequence[Snapshot]) -> list[Snapshot]:
    """The snapshots matching selector, ordered by ID.

    An explicit ID that doesn't exist is an error; ranges and field matches
    select whatever exists.
    """
    if "=" in selector:
        return _select_by_field(selector, snapshots)

    by_id = {s.id: s for s in snapshots}
    chosen: set[int] = set()
    for term in (t.strip() for t in selector.split(",")):
        if _ID.fullmatch(term):
            snapshot_id = int(term)
            if snapshot_id not in by_id:
                raise PatrolError(f"no snapshot with ID {snapshot_id}")
            chosen.add(snapshot_id)
        elif match := _RANGE.fullmatch(term):
            low, high = sorted((int(match[1]), int(match[2])))
            chosen.update(i for i in by_id if low <= i <= high)
        else:
            raise PatrolError(f"invalid selector {selector!r}")
    return [by_id[i] for i in sorted(chosen)]


def _select_by_field(selector: str, snapshots: Sequence[Snapshot]) -> list[Snapshot]:
    field, _, value = selector.partition("=")
    field = field.strip()
    if field == "keep":
        if value not in ("yes", "no"):
            raise PatrolError("keep= must be followed by 'yes' or 'no'")
        matches = (s for s in snapshots if s.keep == (value == "yes"))
    else:
        getter = _FIELDS.get(field)
        if getter is None:
            choices = ", ".join([*_FIELDS, "keep"])
            raise PatrolError(f"unknown selector field {field!r} (expected one of: {choices})")
        if not value:
            raise PatrolError(f"{field}= must be followed by some text")
        needle = value.casefold()
        matches = (s for s in snapshots if needle in getter(s).casefold())
    return sorted(matches, key=lambda s: s.id)
