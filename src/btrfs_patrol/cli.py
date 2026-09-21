# SPDX-License-Identifier: GPL-3.0-or-later
"""The btrfs-patrol command-line interface."""

from __future__ import annotations

import argparse
import os
import platform
import shutil
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from btrfs_patrol import __version__, boot, btrfs, convert, dnf, rollback, selinux, setup, system
from btrfs_patrol import config as config_mod
from btrfs_patrol.config import ROOT, Config, ManagedSubvolume
from btrfs_patrol.errors import PatrolError
from btrfs_patrol.output import Console, format_table
from btrfs_patrol.selectors import select
from btrfs_patrol.snapshots import ROOT_MOUNT, SUBVOLUME_NAME, Snapshot, SnapshotStore

MACHINE_ID = Path("/etc/machine-id")

SELECTOR_HELP = """\
selectors:
  3, 1,10,20-23       snapshot IDs and ID ranges
  date=2026-09        text in a field: date, time, kernel, kind or description
  description=gnome   (matching ignores case)
  subvolume=home      snapshots of one subvolume (its whole name)
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
        # Off a terminal - a pipe, a file, ssh without a tty - there is no width to fit,
        # and a description cut short is lost to grep, so print it whole.
        if self.console.out.isatty():
            width = shutil.get_terminal_size().columns
        else:
            width = sys.maxsize
        # Only a system that snapshots more than root needs to be told which one.
        show_subvolume = bool(self.config.subvolumes) or any(s.subvolume != ROOT for s in snapshots)
        return format_table(snapshots, self.console.style, width, wrap, show_subvolume)


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


def selected_subvolumes(
    config: Config, names: Sequence[str] | None, kind: str
) -> list[ManagedSubvolume]:
    """The subvolumes a snapshot command takes: the named ones, or every one it is for."""
    if not names:
        return [s for s in config.managed() if kind != "timer" or s.timer]
    chosen: list[ManagedSubvolume] = []
    for name in names:
        subvolume = config.find(name)
        if subvolume is None:
            known = ", ".join(s.name for s in config.managed())
            raise PatrolError(f"no subvolume named {name!r} in the configuration (there are: {known})")
        if subvolume not in chosen:
            chosen.append(subvolume)
    return chosen


def mounted_snapshots(config: Config, subvolumes: Sequence[ManagedSubvolume]) -> dict[str, int]:
    """Name -> snapshot ID, for the subvolumes currently mounted from a snapshot."""
    mounts = system.read_mounts()
    found = {}
    for subvolume in subvolumes:
        snapshot_id = rollback.mounted_snapshot(config, system.find_mount(subvolume.path, mounts))
        if snapshot_id is not None:
            found[subvolume.name] = snapshot_id
    return found


def pending_rollbacks(config: Config, subvolumes: Sequence[ManagedSubvolume]) -> dict[str, int]:
    """Name -> snapshot ID, for the subvolumes whose rollback is waiting for a reboot."""
    mounts = system.read_mounts()
    pending = {}
    for subvolume in subvolumes:
        snapshot_id = rollback.pending_rollback(config, system.find_mount(subvolume.path, mounts))
        if snapshot_id is not None:
            pending[subvolume.name] = snapshot_id
    return pending


def sync_boot_entries(app: App) -> None:
    """Make the boot menu match the snapshots, and never fail the caller.

    Called after anything that adds or removes snapshots. Writing to the boot
    partition can fail in ways that have nothing to do with the snapshot that
    was just taken - a full ESP, a read-only mount, no /boot at all - and none
    of them are a reason to fail the snapshot, so they are warnings.
    """
    config = app.config
    try:
        machine_id = boot.MACHINE_ID.read_text().strip()
        boot_dir = boot.boot_partition(machine_id)
        if boot_dir is None:
            if config.boot_entries:
                app.console.warn("no boot partition found, so no snapshot boot entries")
            return
        entries_dir = boot_dir / "loader/entries"
        # Even with the feature off, ours are cleaned up rather than left behind.
        wanted: list[tuple[int, str, str]] = []
        if config.boot_entries:
            roots = sorted(
                (s for s in app.store.load_all() if s.subvolume == ROOT),
                key=lambda s: s.id,
                reverse=True,
            )
            for snapshot in roots[: config.boot_entries]:
                # Only a snapshot that can actually load its drivers is offered.
                if not (app.store.subvolume(snapshot.id) / "usr/lib/modules" / snapshot.kernel).is_dir():
                    continue
                when = snapshot.created.strftime("%Y-%m-%d %H:%M")
                description = snapshot.description or snapshot.kind
                wanted.append((snapshot.id, snapshot.kernel, f"Snapshot {snapshot.id} - {when} - {description}"))
        written, removed, skipped = boot.sync_snapshot_entries(
            entries_dir, boot_dir, wanted, machine_id,
            rollback.PROC_CMDLINE.read_text(), config.snapshots_subvolume,
        )
        for snapshot_id in written:
            app.console.info(f"boot entry for snapshot {snapshot_id}")
        for snapshot_id in removed:
            app.console.info(f"removed the boot entry for snapshot {snapshot_id}")
        if skipped:
            app.console.warn(
                f"no boot entry for snapshot(s) {', '.join(map(str, skipped))}: "
                "their kernel is no longer on the boot partition"
            )
    except (PatrolError, OSError) as e:
        app.console.warn(f"could not update the snapshot boot entries: {e}")


def cmd_list(app: App, args: argparse.Namespace) -> int:
    snapshots = app.store.load_all()
    if args.selector:
        snapshots = select(args.selector, snapshots)
    app.print(app.table(snapshots, wrap=args.verbose))
    kept = sum(s.keep for s in snapshots)
    app.print(f"{len(snapshots)} snapshot(s), {kept} kept")
    return 0


def cmd_snapshot(app: App, args: argparse.Namespace) -> int:
    config = app.config
    subvolumes = selected_subvolumes(config, args.subvolume, args.kind)
    named = bool(config.subvolumes)
    pending = pending_rollbacks(config, subvolumes)
    for subvolume in subvolumes:
        if subvolume.name not in pending:
            continue
        message = rollback.pending_rollback_message(pending[subvolume.name], subvolume.path)
        if args.kind != "timer":
            raise PatrolError(f"{message}; reboot first")
        # A snapshot of the state being left would only be clutter, and a failed
        # unit until the reboot would be noise: the next scheduled run takes it.
        of = f" of {subvolume.name}" if named else ""
        app.console.info(f"{message}; skipping the scheduled snapshot{of}")
    subvolumes = [s for s in subvolumes if s.name not in pending]
    if not subvolumes:
        return 0
    # Before writing anything: an unmounted store would take the snapshot into
    # the parent subvolume, where nothing will ever see it again.
    system.require_mounted(config.snapshots_dir, config.snapshots_subvolume)
    with app.store.lock():
        for subvolume in subvolumes:
            snapshot = app.store.create(
                subvolume.path,
                kind=args.kind,
                description=args.description,
                keep=args.keep,
                subvolume=subvolume.name,
            )
            app.console.info(f"created snapshot {snapshot.id}" + (f" of {subvolume.name}" if named else ""))
            for pruned in app.store.prune(subvolume.max_snapshots, subvolume.name):
                app.console.info(f"pruned snapshot {pruned.id}")
    sync_boot_entries(app)
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
    # A subvolume can be mounted from a snapshot for two reasons: a rollback is
    # waiting for its reboot, or the snapshot's own boot entry was booted to
    # repair a broken system. Deleting it is an easy thing to try in both cases
    # - the rollback message names the snapshot, and the boot menu names it -
    # and in both it aims 'btrfs subvolume delete' at the running system. Asking
    # "is it mounted" rather than "is a rollback pending" covers both.
    in_use = set(mounted_snapshots(app.config, list(app.config.managed())).values())
    blocked = sorted(s.id for s in snapshots if s.id in in_use)
    if blocked:
        raise PatrolError(
            f"snapshot {', '.join(map(str, blocked))} is what the system is running from; "
            "reboot into the normal system first, then delete it"
        )
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
    sync_boot_entries(app)
    return 0


def cmd_prune(app: App, args: argparse.Namespace) -> int:
    with app.store.lock():
        pruned = [
            snapshot
            for subvolume in app.config.managed()
            for snapshot in app.store.prune(subvolume.max_snapshots, subvolume.name)
        ]
    for snapshot in pruned:
        app.console.info(f"pruned snapshot {snapshot.id}")
    if not pruned:
        app.console.info("nothing to prune")
    sync_boot_entries(app)
    return 0


def cmd_prune_kernels(app: App, args: argparse.Namespace) -> int:
    running = platform.release()
    # The running kernel always has modules, so it is never stale; guard anyway,
    # because removing its entry would leave nothing to boot.
    stale = sorted(version for version in boot.stale_kernel_versions() if version != running)
    if not stale:
        app.console.info("every boot entry has modules; nothing to remove")
        return 0
    require_confirmation_possible(args.yes or args.dry_run)
    app.print(f"boot entries without modules in {boot.MODULES}:")
    for version in stale:
        app.print(f"  {version}")
    app.console.warn(
        "a snapshot taken while these kernels were installed will need "
        "'kernel-install add-all' after rolling forward to it"
    )
    if args.dry_run:
        app.console.info("dry run: nothing changed")
        return 0
    question = f"Remove {len(stale)} boot {'entry' if len(stale) == 1 else 'entries'}?"
    if not confirm(app.console, question, args.yes):
        app.console.info("nothing changed")
        return 1
    for version in stale:
        boot.remove_boot_entry(version)
    # kernel-install only removes entries it owns, and exits 0 either way, so
    # check rather than report success on its behalf.
    remaining = boot.stale_kernel_versions()
    for version in stale:
        if version not in remaining:
            app.console.info(f"removed the boot entry for {version}")
    left = [version for version in stale if version in remaining]
    if left:
        app.console.warn(
            f"still has a boot entry after kernel-install remove: {', '.join(left)}; "
            "an entry kernel-install does not manage has to be removed by hand"
        )
        return 1
    return 0


def cmd_rollback(app: App, args: argparse.Namespace) -> int:
    [target] = app.matching(str(args.id))
    require_confirmation_possible(args.yes or args.dry_run)
    with rollback.prepare(app.config, app.store, target) as plan:
        app.print(app.table([target]))
        for line in plan.describe():
            app.print(line)
        for warning in plan.warnings:
            app.console.warn(warning)
        if args.dry_run:
            app.console.info("dry run: nothing changed")
            return 0
        if not confirm(app.console, f"Roll back to snapshot {target.id}?", args.yes):
            app.console.info("nothing changed")
            return 1
        saved = plan.execute()
    if plan.name != ROOT:
        app.console.info(
            f"rolled back {plan.path} to snapshot {target.id}; "
            f"the previous state is kept as snapshot {saved.id}"
        )
        if plan.mounted:
            app.console.info(f"reboot to use the restored {plan.path}")
        else:
            app.console.info(
                f"the restored {plan.path} is in place; restart the programs that use it, or reboot"
            )
        return 0
    app.console.info(
        f"rolled back to snapshot {target.id}; the previous state is kept as snapshot {saved.id}"
    )
    app.console.info("reboot to start the restored system")
    journal = app.config.snapshots_dir / str(saved.id) / SUBVOLUME_NAME / "var/log/journal"
    try:
        journal /= MACHINE_ID.read_text().strip()
    except OSError:
        journal /= "<machine-id>"
    app.console.info(f"logs from before the rollback stay in snapshot {saved.id}: journalctl -D {journal}")
    return 0


def check_subvolume(
    config: Config, subvolume: ManagedSubvolume, root: system.Mount, mounts: Sequence[system.Mount]
) -> tuple[list[str], list[str]]:
    """Problems and warnings about a [subvolumes.<name>] table, given the btrfs mount at /."""
    path = subvolume.path
    label = f"{path} (subvolume {subvolume.name!r})"
    own = system.find_mount(path, mounts)
    if own is not None:
        if own.fstype != "btrfs" or own.source != root.source:
            return [f"{label} is not on the btrfs filesystem mounted at /"], []
        pending = rollback.pending_rollback(config, own)
        if pending is not None:
            message = rollback.pending_rollback_message(pending, path)
            return [], [f"{message}; reboot to use the restored {path}"]
        location = own.root.strip("/")
    else:
        if not path.is_dir():
            return [f"{label} doesn't exist"], []
        outer = system.containing_mount(path, mounts)
        if outer is None or outer.fstype != "btrfs" or outer.source != root.source:
            return [f"{label} is not on the btrfs filesystem mounted at /"], []
        if not btrfs.is_subvolume(path):
            return [f"{label} is a directory, not a btrfs subvolume"], []
        inside = path.relative_to(outer.mount_point).as_posix()
        location = f"{outer.root.strip('/')}/{inside}".strip("/")
    if location.startswith(f"{config.root_subvolume.strip('/')}/"):
        return [], [
            f"{path} is a subvolume inside the root subvolume, so snapshots and rollbacks "
            "of root don't include it"
        ]
    return [], []


def cmd_convert(app: App, args: argparse.Namespace) -> int:
    path = Path(args.path)
    name = args.name or convert.default_name(path)
    require_confirmation_possible(args.yes or args.dry_run)
    manage = not args.no_config
    with convert.prepare(
        app.config, config_mod.resolve_path(args.config), path, name, manage
    ) as plan:
        for line in plan.describe():
            app.print(line)
        for warning in plan.warnings:
            app.console.warn(warning)
        if args.dry_run:
            app.console.info("dry run: nothing changed")
            return 0
        if not confirm(app.console, f"Convert {plan.path} to subvolume {name!r}?", args.yes):
            app.console.info("nothing changed")
            return 1
        plan.execute()

    app.console.info(f"{plan.path} is now the subvolume {name!r}")
    if manage:
        app.console.info(
            f"added [{config_mod.SUBVOLUMES_SECTION}.{name}] to "
            f"{config_mod.resolve_path(args.config)}; "
            f"'btrfs-patrol snapshot -s {name}' takes its first snapshot"
        )
    else:
        app.console.info("to snapshot it, add this to the configuration:")
        for line in convert.table_text(name, plan.path).strip("\n").split("\n"):
            app.print(f"  {line}")
    # Reported AFTER the swap rather than before: the point is what to do now,
    # and a process that opened the directory while the copy ran would have been
    # missed by a list printed earlier.
    still = convert.holders(plan.kept)
    if still:
        app.console.warn(
            f"these still have the old {plan.path} open and keep writing to it:"
        )
        for holder in still:
            app.print(f"  {holder.describe()}")
        hint = convert.restart_hint(still)
        if hint:
            app.console.info(f"reopen them with: {hint}")
        app.console.info("or reboot, which reopens everything")
    app.console.info(
        f"the previous contents are kept at {plan.kept}; "
        "remove them once you are satisfied nothing is missing"
    )
    return 0


def cmd_check(app: App, args: argparse.Namespace) -> int:
    """Compare the configuration with what is mounted, and look for broken snapshots."""
    config = app.config
    problems = []
    mounts = system.read_mounts()

    root = system.find_mount(ROOT_MOUNT, mounts)
    booted = rollback.mounted_snapshot(config, root)
    if root is None or root.fstype != "btrfs":
        problems.append("/ is not a btrfs filesystem")
    elif booted is not None:
        # / comes from a snapshot for one of two reasons, and telling them apart
        # needs to read the subvolume's read-only property, which needs root.
        # Without it, say so rather than pick one: reporting a rollback that is
        # not happening sends the user to reboot for nothing.
        try:
            read_only = btrfs.is_read_only(config.snapshots_dir / str(booted) / SUBVOLUME_NAME)
        except (PatrolError, OSError):
            app.console.warn(
                f"/ is mounted from snapshot {booted}; run as root to tell whether a rollback "
                "is waiting for a reboot or this is the snapshot's own boot entry"
            )
        else:
            if read_only:
                # A snapshot's own boot entry, booted to repair a system that
                # would not start. Not a problem, and not a pending rollback.
                app.console.warn(
                    f"booted from snapshot {booted}, read-only: the system's own root subvolume "
                    f"is not running. 'btrfs-patrol rollback <ID>' restores it, then reboot"
                )
            else:
                app.console.warn(
                    f"{rollback.pending_rollback_message(booted)}; reboot to start the restored system"
                )
    elif root.root != f"/{config.root_subvolume}":
        problems.append(
            f"/ is subvolume {root.root!r}, "
            f"but filesystem.root_subvolume is {config.root_subvolume!r}"
        )

    snapshots_mount = system.find_mount(config.snapshots_dir, mounts)
    if snapshots_mount is None:
        problems.append(
            f"{config.snapshots_dir} is not a mount point; run 'btrfs-patrol setup' to create "
            f"and mount the top-level {config.snapshots_subvolume!r} subvolume there"
        )
    elif snapshots_mount.root != f"/{config.snapshots_subvolume}":
        problems.append(
            f"{config.snapshots_dir} is subvolume {snapshots_mount.root!r}, "
            f"but filesystem.snapshots_subvolume is {config.snapshots_subvolume!r}"
        )
    elif root is not None and snapshots_mount.source != root.source:
        problems.append(f"{config.snapshots_dir} is not on the same filesystem as /")
    else:
        managed = {subvolume.name for subvolume in config.managed()}
        unmanaged: dict[str, list[int]] = {}
        for snapshot_id in app.store.ids():
            try:
                snapshot = app.store.load(snapshot_id)
            except PatrolError as e:
                problems.append(str(e))
                continue
            if not app.store.subvolume(snapshot_id).is_dir():
                problems.append(f"snapshot {snapshot_id} has metadata but no subvolume")
            if snapshot.subvolume not in managed:
                unmanaged.setdefault(snapshot.subvolume, []).append(snapshot_id)
        for name, ids in sorted(unmanaged.items()):
            app.console.warn(
                f"snapshot(s) {', '.join(str(i) for i in ids)} are of the subvolume {name!r}, "
                "which isn't in the configuration: they are never pruned and can't be rolled back"
            )
        if selinux.enabled():
            try:
                if selinux.mislabeled(selinux.store_paths(config.snapshots_dir)):
                    app.console.warn(
                        f"{config.snapshots_dir} or its snapshot entries don't have their "
                        "SELinux labels; run 'btrfs-patrol setup'"
                    )
            except PatrolError as e:
                app.console.warn(f"could not check SELinux labels: {e}")

    if root is not None and root.fstype == "btrfs":
        for subvolume in config.subvolumes:
            found, warnings = check_subvolume(config, subvolume, root, mounts)
            problems.extend(found)
            for warning in warnings:
                app.console.warn(warning)

    if selinux.exclusion_missing(config.snapshots_dir):
        app.console.warn(
            f"{config.snapshots_dir} isn't excluded from full SELinux relabels: one would change "
            "the labels inside writable rollback snapshots, and rolling back to such a snapshot "
            "would boot a mislabeled system; run 'btrfs-patrol setup'"
        )

    for problem in problems:
        app.console.error(problem)
    if problems:
        return 1
    app.console.info("configuration and snapshots look good")
    return 0


def cmd_setup(args: argparse.Namespace, console: Console) -> int:
    """Set the system up, then check it. Unlike the other commands, needs no configuration."""
    require_confirmation_possible(args.yes or args.dry_run)
    with setup.prepare(config_mod.resolve_path(args.config)) as plan:
        actions = plan.actions()
        if not actions:
            console.info("already set up; nothing to do")
        else:
            print("setup will:", file=console.out)
            for action in actions:
                print(f"  - {action}", file=console.out)
            if args.dry_run:
                console.info("dry run: nothing changed")
                return 0
            if not confirm(console, "Continue?", args.yes):
                console.info("nothing changed")
                return 1
            plan.execute()
            console.info("setup done")
    if args.color is None:
        console = Console(plan.config.color)
    return cmd_check(App(plan.config, SnapshotStore(plan.config.snapshots_dir), console), args)


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

    command = add("snapshot", cmd_snapshot, "take snapshots of the managed subvolumes", True)
    command.add_argument("-d", "--description", default="")
    command.add_argument(
        "-k", "--keep", action="store_true", help="never prune these snapshots automatically"
    )
    command.add_argument(
        "-s", "--subvolume", action="append", metavar="NAME",
        help="only this subvolume, root or a [subvolumes.NAME] table; repeat for more "
             "(default: all of them)",
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

    add("prune", cmd_prune, "delete each subvolume's oldest snapshots beyond its max_snapshots", True)

    command = add(
        "prune-kernels", cmd_prune_kernels,
        "remove boot entries for kernels this system has no modules for", True,
    )
    command.add_argument("-y", "--yes", action="store_true", help="don't ask for confirmation")
    command.add_argument(
        "-n", "--dry-run", action="store_true",
        help="run every check and show what would be removed, without changing anything",
    )

    command = add("rollback", cmd_rollback,
                  "roll a subvolume back to one of its snapshots, then reboot to use it", True)
    command.add_argument("id", type=int)
    command.add_argument("-y", "--yes", action="store_true", help="don't ask for confirmation")
    command.add_argument("-n", "--dry-run", action="store_true",
                         help="run every check and show the plan, without changing anything")

    command = add("convert", cmd_convert,
                  "turn an existing directory into a btrfs subvolume it can snapshot", True)
    command.add_argument("path", help="the directory to convert, such as /var/log")
    command.add_argument("--name", metavar="NAME",
                         help="name for the subvolume (default: from the path, /var/log -> var-log)")
    command.add_argument("--no-config", action="store_true",
                         help="don't add a [subvolumes.NAME] table to the configuration")
    command.add_argument("-y", "--yes", action="store_true", help="don't ask for confirmation")
    command.add_argument("-n", "--dry-run", action="store_true",
                         help="run every check and show the plan, without changing anything")

    add("check", cmd_check, "check the configuration, mounts and snapshots")

    command = add("setup", None,
                  "create the configuration and the snapshots subvolume, and mount it", True)
    command.add_argument("-y", "--yes", action="store_true", help="don't ask for confirmation")
    command.add_argument("-n", "--dry-run", action="store_true",
                         help="show what setup would do, without changing anything")

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
            if sys.stdin.isatty():
                # It would wait on the terminal for the plugin's replies.
                raise PatrolError("'btrfs-patrol dnf-hook' is run by dnf's actions plugin, not by hand")
            return dnf.run_hook(args.phase, args.config, console)
        if args.command == "setup":
            return cmd_setup(args, console)
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
