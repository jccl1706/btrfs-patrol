# SPDX-License-Identifier: GPL-3.0-or-later
"""The btrfs-patrol command-line interface."""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from btrfs_patrol import __version__, dnf, rollback, system
from btrfs_patrol import config as config_mod
from btrfs_patrol.config import Config
from btrfs_patrol.errors import PatrolError
from btrfs_patrol.output import Console, format_table
from btrfs_patrol.selectors import select
from btrfs_patrol.snapshots import ROOT_MOUNT, Snapshot, SnapshotStore

SELECTOR_HELP = """\
selectors:
  3, 1,10,20-23       snapshot IDs and ID ranges
  date=2026-09        text in a field: date, time, kernel, kind or description
  description=gnome   (description matching ignores case)
  keep=yes            kept snapshots (keep=no for the others)
"""


@dataclass
class App:
    config: Config
    store: SnapshotStore
    console: Console

    def print(self, text: str = "") -> None:
        print(text, file=self.console.out)

    def matching(self, selector: str) -> list[Snapshot]:
        snapshots = select(selector, self.store.load_all())
        if not snapshots:
            raise PatrolError(f"no snapshots match {selector!r}")
        return snapshots

    def table(self, snapshots: Sequence[Snapshot], wrap: bool = False) -> str:
        width = shutil.get_terminal_size().columns
        return format_table(snapshots, self.console.style, width, wrap)


def require_confirmation_possible(assume_yes: bool) -> None:
    """Fail early, before showing anything, when a confirmation prompt couldn't be answered."""
    if not assume_yes and not sys.stdin.isatty():
        raise PatrolError("confirmation needed but input is not a terminal; pass --yes")


def confirm(console: Console, question: str, assume_yes: bool) -> bool:
    if assume_yes:
        return True
    require_confirmation_possible(assume_yes)
    print(f"{question} [y/N] ", end="", file=console.err, flush=True)
    return sys.stdin.readline().strip().lower() in ("y", "yes")


def cmd_list(app: App, args: argparse.Namespace) -> int:
    snapshots = app.store.load_all()
    if args.selector:
        snapshots = select(args.selector, snapshots)
    app.print(app.table(snapshots, wrap=args.verbose))
    kept = sum(s.keep for s in snapshots)
    app.print(f"{len(snapshots)} snapshot(s), {kept} kept")
    return 0


def cmd_snapshot(app: App, args: argparse.Namespace) -> int:
    with app.store.lock():
        snapshot = app.store.create(
            ROOT_MOUNT, kind=args.kind, description=args.description, keep=args.keep
        )
        app.console.info(f"created snapshot {snapshot.id}")
        for pruned in app.store.prune(app.config.max_snapshots):
            app.console.info(f"pruned snapshot {pruned.id}")
    return 0


def cmd_describe(app: App, args: argparse.Namespace) -> int:
    with app.store.lock():
        [snapshot] = app.matching(str(args.id))
        snapshot.description = args.description
        app.store.save(snapshot)
    return 0


def cmd_keep(app: App, args: argparse.Namespace) -> int:
    keep = args.command == "keep"
    with app.store.lock():
        for snapshot in app.matching(args.selector):
            if snapshot.keep != keep:
                snapshot.keep = keep
                app.store.save(snapshot)
                state = "will be kept" if keep else "can be pruned again"
                app.console.info(f"snapshot {snapshot.id} {state}")
    return 0


def cmd_delete(app: App, args: argparse.Namespace) -> int:
    snapshots = app.matching(args.selector)
    require_confirmation_possible(args.yes)
    app.print(app.table(snapshots))
    if not confirm(app.console, f"Delete {len(snapshots)} snapshot(s)?", args.yes):
        app.console.info("nothing deleted")
        return 1
    # Lock only after confirming, so a pending prompt can't block a dnf transaction.
    with app.store.lock():
        existing = set(app.store.ids())
        for snapshot in snapshots:
            if snapshot.id in existing:
                app.store.delete(snapshot.id)
                app.console.info(f"deleted snapshot {snapshot.id}")
    return 0


def cmd_prune(app: App, args: argparse.Namespace) -> int:
    with app.store.lock():
        pruned = app.store.prune(app.config.max_snapshots)
    for snapshot in pruned:
        app.console.info(f"pruned snapshot {snapshot.id}")
    if not pruned:
        app.console.info("nothing to prune")
    return 0


def cmd_rollback(app: App, args: argparse.Namespace) -> int:
    [snapshot] = app.matching(str(args.id))
    rollback.rollback(app.config, app.store, snapshot)
    return 0


def cmd_check(app: App, args: argparse.Namespace) -> int:
    """Compare the configuration with what is mounted, and look for broken snapshots.

    TODO: add a rollback dry run once rollback is implemented.
    """
    config = app.config
    problems = []
    mounts = system.read_mounts()

    root = system.find_mount(ROOT_MOUNT, mounts)
    if root is None or root.fstype != "btrfs":
        problems.append("/ is not a btrfs filesystem")
    elif root.root != f"/{config.root_subvolume}":
        problems.append(
            f"/ is subvolume {root.root!r}, "
            f"but filesystem.root_subvolume is {config.root_subvolume!r}"
        )

    snapshots_mount = system.find_mount(config.snapshots_dir, mounts)
    if snapshots_mount is None:
        problems.append(
            f"{config.snapshots_dir} is not a mount point; mount the top-level "
            f"{config.snapshots_subvolume!r} subvolume there so snapshots stay outside "
            "the root subvolume"
        )
    elif snapshots_mount.root != f"/{config.snapshots_subvolume}":
        problems.append(
            f"{config.snapshots_dir} is subvolume {snapshots_mount.root!r}, "
            f"but filesystem.snapshots_subvolume is {config.snapshots_subvolume!r}"
        )
    elif root is not None and snapshots_mount.source != root.source:
        problems.append(f"{config.snapshots_dir} is not on the same filesystem as /")
    else:
        for snapshot_id in app.store.ids():
            try:
                app.store.load(snapshot_id)
            except PatrolError as e:
                problems.append(str(e))
                continue
            if not app.store.subvolume(snapshot_id).is_dir():
                problems.append(f"snapshot {snapshot_id} has metadata but no subvolume")

    for problem in problems:
        app.console.error(problem)
    if problems:
        return 1
    app.console.info("configuration and snapshots look good")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="btrfs-patrol", description="Btrfs snapshot manager and rollback tool."
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "-c", "--config", type=Path, metavar="PATH",
        help=f"configuration file (default: {config_mod.DEFAULT_PATH})",
    )
    parser.add_argument(
        "--color", choices=config_mod.COLOR_CHOICES,
        help="override output.color from the configuration",
    )
    commands = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    def add(
        name: str,
        handler: Callable[[App, argparse.Namespace], int] | None,
        summary: str,
        needs_root: bool = False,
        selectors: bool = False,
    ) -> argparse.ArgumentParser:
        command = commands.add_parser(
            name, help=summary, description=summary,
            epilog=SELECTOR_HELP if selectors else None,
            formatter_class=argparse.RawDescriptionHelpFormatter,
        )
        command.set_defaults(handler=handler, needs_root=needs_root)
        return command

    command = add("list", cmd_list, "list snapshots", selectors=True)
    command.add_argument("selector", nargs="?", help="only list matching snapshots")
    command.add_argument(
        "-v", "--verbose", action="store_true",
        help="wrap long descriptions instead of truncating them",
    )

    command = add("snapshot", cmd_snapshot, "take a snapshot of the root subvolume", True)
    command.add_argument("-d", "--description", default="")
    command.add_argument(
        "-k", "--keep", action="store_true", help="never prune this snapshot automatically"
    )
    command.add_argument("--kind", choices=("manual", "timer"), default="manual",
                         help=argparse.SUPPRESS)

    command = add("describe", cmd_describe, "change a snapshot's description", True)
    command.add_argument("id", type=int)
    command.add_argument("description")

    command = add("keep", cmd_keep, "protect snapshots from pruning", True, selectors=True)
    command.add_argument("selector")
    command = add("unkeep", cmd_keep, "let snapshots be pruned again", True, selectors=True)
    command.add_argument("selector")

    command = add("delete", cmd_delete, "delete snapshots", True, selectors=True)
    command.add_argument("selector")
    command.add_argument("-y", "--yes", action="store_true", help="don't ask for confirmation")

    add("prune", cmd_prune, "delete the oldest snapshots beyond retention.max_snapshots", True)

    command = add("rollback", cmd_rollback,
                  "roll the root subvolume back to a snapshot (not implemented yet)", True)
    command.add_argument("id", type=int)

    add("check", cmd_check, "check the configuration, mounts and snapshots")
    add("setup", None, "create the snapshots subvolume and configuration (not implemented yet)",
        True)

    command = add("dnf-hook", None, "take a snapshot for a dnf5 transaction (used by dnf)", True)
    command.add_argument("phase", choices=("pre", "post"))

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    console = Console(args.color or "auto")
    try:
        if args.needs_root and os.geteuid() != 0:
            raise PatrolError(f"'btrfs-patrol {args.command}' must be run as root")
        if args.command == "dnf-hook":
            return dnf.run_hook(args.phase, args.config, console)
        if args.command == "setup":
            raise PatrolError("setup is not implemented yet; see README.md for the manual steps")
        config = config_mod.load(args.config)
        if args.color is None:
            console = Console(config.color)
        return args.handler(App(config, SnapshotStore(config.snapshots_dir), console), args)
    except PermissionError as e:
        where = f": {e.filename}" if e.filename else ""
        console.error(f"permission denied{where} (run as root?)")
        return 1
    except (PatrolError, OSError) as e:
        console.error(str(e))
        return 1
    except KeyboardInterrupt:
        print(file=console.err)
        return 130
