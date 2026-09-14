# SPDX-License-Identifier: GPL-3.0-or-later
from datetime import datetime

from btrfs_patrol.snapshots import Snapshot


def make_snapshot(
    snapshot_id: int,
    *,
    created: str = "2026-09-14T10:30:00+00:00",
    kernel: str = "6.17.1-300.fc44.x86_64",
    kind: str = "manual",
    description: str = "",
    keep: bool = False,
) -> Snapshot:
    return Snapshot(
        id=snapshot_id,
        created=datetime.fromisoformat(created),
        kernel=kernel,
        kind=kind,
        description=description,
        keep=keep,
    )
