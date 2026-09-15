# SPDX-License-Identifier: GPL-3.0-or-later
"""Snapshots around dnf5 transactions.

libdnf5-plugin-actions runs 'btrfs-patrol dnf-hook pre' before and
'btrfs-patrol dnf-hook post' after each transaction (see
data/dnf5/btrfs-patrol.actions), in the plugin's "json" communication mode
(libdnf5-actions(8)): the hook writes requests to stdout and reads the replies
from stdin, one JSON object per line.

It asks the plugin for the packages in the transaction, so the snapshot says
what changed - "upgrade kernel-core, mesa-dri-drivers +12 more; install tree" -
instead of only "dnf transaction". It logs through the plugin too, which writes
to /var/log/dnf5.log, and repeats warnings on stderr.

A snapshot problem must never make a dnf transaction fail, so the exit status
is always 0. A plugin that doesn't answer costs only the package list: each
reply is waited for at most REPLY_TIMEOUT seconds, and after the first one that
doesn't come, no more requests are sent.
"""

from __future__ import annotations

import io
import json
import select
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TextIO

from btrfs_patrol import config as config_mod
from btrfs_patrol import rollback, system
from btrfs_patrol.config import ROOT
from btrfs_patrol.errors import PatrolError
from btrfs_patrol.output import Console
from btrfs_patrol.snapshots import ROOT_MOUNT, Snapshot, SnapshotStore

REPLY_TIMEOUT = 10.0

# The plugin's transaction actions, in the order the description lists them.
# "O" (a package replaced by an upgrade, downgrade or obsoletion) is left out:
# the package that replaced it is already listed. "?" (a changed install
# reason) doesn't change the system.
ACTIONS = {"U": "upgrade", "I": "install", "D": "downgrade", "R": "reinstall", "E": "remove"}
NAMES_PER_ACTION = 3
FALLBACK_DESCRIPTION = "dnf transaction"


def describe_transaction(packages: Sequence[Mapping[str, object]]) -> str:
    """A short summary such as "upgrade kernel-core, mesa +2 more; remove foo"."""
    names: dict[str, set[str]] = {}
    for package in packages:
        action, name = package.get("action"), package.get("name")
        if action in ACTIONS and isinstance(name, str) and name:
            names.setdefault(action, set()).add(name)
    parts = []
    for action, verb in ACTIONS.items():
        # Ignoring case: sorted by code point, "R-srpm-macros" came before "add-determinism".
        found = sorted(names.get(action, ()), key=str.casefold)
        if not found:
            continue
        part = f"{verb} {', '.join(found[:NAMES_PER_ACTION])}"
        if len(found) > NAMES_PER_ACTION:
            part += f" +{len(found) - NAMES_PER_ACTION} more"
        parts.append(part)
    return "; ".join(parts)


class Plugin:
    """The actions plugin's json communication channel."""

    def __init__(self, stdin: TextIO, stdout: TextIO) -> None:
        self.stdin = stdin
        self.stdout = stdout
        self.alive = True

    def _reply_ready(self) -> bool:
        try:
            fd = self.stdin.fileno()
        except (OSError, ValueError, io.UnsupportedOperation):
            return True  # not a real file (tests): readline() won't block
        return bool(select.select([fd], [], [], REPLY_TIMEOUT)[0])

    def request(self, message: Mapping[str, object]) -> dict | None:
        """Send a request and return its reply, or None when there is no usable reply."""
        if not self.alive:
            return None
        try:
            print(json.dumps(message, separators=(",", ":")), file=self.stdout, flush=True)
            line = self.stdin.readline() if self._reply_ready() else ""
        except OSError:
            line = ""
        if not line:
            self.alive = False
            return None
        try:
            reply = json.loads(line)
        except json.JSONDecodeError:
            self.alive = False
            return None
        if not isinstance(reply, dict) or reply.get("status") != "OK":
            return None
        return reply

    def transaction_packages(self) -> list[Mapping[str, object]]:
        reply = self.request(
            {"op": "get", "domain": "trans_packages", "args": {"output": ["name", "action"]}}
        )
        packages = ((reply or {}).get("return") or {}).get("trans_packages")
        if not isinstance(packages, list):
            return []
        return [p for p in packages if isinstance(p, dict)]

    def log(self, level: str, message: str) -> None:
        # One line per message in dnf5.log.
        text = " ".join(f"btrfs-patrol: {message}".splitlines())
        self.request({"op": "log", "args": {"level": level, "message": text}})


def run_hook(
    phase: str,
    config_path: Path | None,
    console: Console,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
) -> int:
    plugin = Plugin(stdin or sys.stdin, stdout or sys.stdout)
    path = config_mod.resolve_path(config_path)
    if not path.exists():
        # Installed but not set up yet: stay quiet rather than warn on every transaction.
        return 0

    def warn(message: str) -> None:
        plugin.log("WARNING", message)
        console.warn(f"btrfs-patrol: {message}")

    kind = f"dnf-{phase}"
    created: list[Snapshot] = []
    pruned: list[Snapshot] = []
    try:
        config = config_mod.load(path)
        if not (config.dnf_pre_snapshot if phase == "pre" else config.dnf_post_snapshot):
            return 0
        mounts = system.read_mounts()
        pending = rollback.pending_rollback(config, system.find_mount(ROOT_MOUNT, mounts))
        if pending is not None:
            # This transaction changes the system being left, so what it installs is gone
            # after the reboot - worth saying while the user can still stop and reboot first.
            warn(
                f"{rollback.pending_rollback_message(pending)}; no snapshot until the reboot, "
                "and this transaction's changes are lost at the reboot"
            )
            return 0
        # Root, and the other subvolumes that asked for dnf snapshots - except one whose
        # own rollback is waiting for a reboot, which would only snapshot the state being left.
        subvolumes = [
            subvolume
            for subvolume in config.managed()
            if subvolume.dnf
            and (
                subvolume.name == ROOT
                or rollback.pending_rollback(config, system.find_mount(subvolume.path, mounts)) is None
            )
        ]
        description = describe_transaction(plugin.transaction_packages()) or FALLBACK_DESCRIPTION
        store = SnapshotStore(config.snapshots_dir)
        with store.lock():
            for subvolume in subvolumes:
                created.append(
                    store.create(
                        subvolume.path, kind=kind, description=description, subvolume=subvolume.name
                    )
                )
                pruned.extend(store.prune(subvolume.max_snapshots, subvolume.name))
    except (PatrolError, OSError) as e:
        after = f" (after creating {_listed(created)})" if created else ""
        warn(f"dnf {phase}-transaction snapshot failed{after}: {e}")
        return 0
    if len(created) == 1 and created[0].subvolume == ROOT:
        message = f"created snapshot {created[0].id} ({kind}): {description}"
    else:
        message = f"created snapshots {_listed(created)} ({kind}): {description}"
    if pruned:
        message += f"; pruned {', '.join(str(s.id) for s in pruned)}"
    plugin.log("INFO", message)
    return 0


def _listed(snapshots: Sequence[Snapshot]) -> str:
    return ", ".join(f"{s.id} ({s.subvolume})" for s in snapshots)
