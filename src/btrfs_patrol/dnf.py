# SPDX-License-Identifier: GPL-3.0-or-later
"""Snapshots around dnf5 transactions.

libdnf5-plugin-actions runs 'btrfs-patrol dnf-hook pre' before and
'btrfs-patrol dnf-hook post' after each transaction (see
data/dnf5/btrfs-patrol.actions).

The plugin runs actions in its "plain" mode, where every line the command
writes to stdout is an instruction to the plugin (libdnf5-actions(8)). So the
hook writes only "log.<LEVEL>=<message>" lines there, which end up in
/var/log/dnf5.log, and repeats warnings on stderr. A snapshot problem must
never make a dnf transaction fail, so the exit status is always 0.

TODO: put the transaction's packages in the snapshot description, as
timepatrol does for pacman.
"""

from __future__ import annotations

from pathlib import Path

from btrfs_patrol import config as config_mod
from btrfs_patrol.errors import PatrolError
from btrfs_patrol.output import Console
from btrfs_patrol.snapshots import ROOT_MOUNT, SnapshotStore


def plugin_log(level: str, message: str) -> None:
    """Write message to dnf5's log through the actions plugin's stdout protocol."""
    # One instruction per line: a newline in the message would start an invalid one.
    print(f"log.{level}=btrfs-patrol: {' '.join(message.splitlines())}", flush=True)


def run_hook(phase: str, config_path: Path | None, console: Console) -> int:
    path = config_mod.resolve_path(config_path)
    if not path.exists():
        # Installed but not set up yet: stay quiet rather than warn on every transaction.
        return 0
    try:
        config = config_mod.load(path)
        if not (config.dnf_pre_snapshot if phase == "pre" else config.dnf_post_snapshot):
            return 0
        store = SnapshotStore(config.snapshots_dir)
        with store.lock():
            snapshot = store.create(ROOT_MOUNT, kind=f"dnf-{phase}", description="dnf transaction")
            pruned = store.prune(config.max_snapshots)
    except (PatrolError, OSError) as e:
        message = f"dnf {phase}-transaction snapshot failed: {e}"
        plugin_log("WARNING", message)
        console.warn(f"btrfs-patrol: {message}")
        return 0
    message = f"created snapshot {snapshot.id} ({snapshot.kind})"
    if pruned:
        message += f", pruned {', '.join(str(s.id) for s in pruned)}"
    plugin_log("INFO", message)
    return 0
