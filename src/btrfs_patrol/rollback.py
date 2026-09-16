# SPDX-License-Identifier: GPL-3.0-or-later
"""Rolling a managed subvolume back to one of its snapshots.

prepare() checks everything it can without changing anything, and yields a
plan while the top-level subvolume (ID 5) is mounted. RollbackPlan.execute()
then rolls back:

1. Create a snapshot entry, kind "rollback" and kept, for the current state.
2. Move the current subvolume into that entry.
3. Create a writable snapshot of the chosen snapshot in its place.
4. Move nested subvolumes (systemd creates var/lib/portables in root, for
   example) from the old subvolume into the new one. Snapshots don't contain
   them, so without this their data would stay behind in the old one, and the
   old one couldn't be deleted later.
5. For root, if the system finds it through the btrfs default subvolume, point
   the default at the new root and read it back to confirm.

Every step records how to undo it. If a step fails, the steps already done are
undone newest first. Nothing reboots automatically.

Root is checked more than any other subvolume. On Fedora /boot is not part of
the root subvolume, so kernels and boot entries are not rolled back: prepare()
refuses unless the restored system has modules for the running kernel and that
kernel has a boot entry, and warns about boot entries whose kernel modules the
snapshot lacks. Another subvolume only has to be found by its name at boot:
with subvol= in /etc/fstab when it is mounted on its own, or by its path
inside the subvolume that contains it.

When the restored subvolume is used differs. Root, and a subvolume mounted on
its own such as /home, stay mounted as they are until the reboot: a mount
holds on to the subvolume, not to its name. A subvolume reached through its
path inside another, such as /var/log inside root, is replaced at once: files
opened from then on are the restored ones, while programs holding files open
keep the previous ones until they restart.
"""

from __future__ import annotations

import contextlib
import os
import platform
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from btrfs_patrol import boot, btrfs, system
from btrfs_patrol.btrfs import Subvolume
from btrfs_patrol.config import ROOT, Config, ManagedSubvolume
from btrfs_patrol.errors import PatrolError
from btrfs_patrol.snapshots import ROOT_MOUNT, SUBVOLUME_NAME, Snapshot, SnapshotStore

DEFAULT_SUBVOLUME = "the btrfs default subvolume"
PROC_CMDLINE = Path("/proc/cmdline")
FSTAB = Path("/etc/fstab")

UndoSteps = list[tuple[str, Callable[[], None]]]


def boot_method(root_subvolume: str, fstab: str, cmdline: str) -> str:
    """How the root subvolume is found at boot: by name, or as the default subvolume.

    fstab is the restored system's /etc/fstab and cmdline the kernel command
    line. Mounting by subvolume ID, or by another name, is refused: the
    restored root gets a new ID and keeps the configured name.
    """
    by_name = False
    for option in [*(_fstab_options(fstab, "/") or []), *_rootflags(cmdline)]:
        key, _, value = option.partition("=")
        if key == "subvolid":
            raise PatrolError(
                "the root filesystem is mounted by subvolume ID (subvolid=), which can't follow "
                "a rollback; mount it with subvol= or through the default subvolume instead"
            )
        if key == "subvol":
            if value.strip("/") != root_subvolume.strip("/"):
                raise PatrolError(
                    f"the root filesystem is mounted as subvol={value}, "
                    f"not the configured root subvolume {root_subvolume!r}"
                )
            by_name = True
    return f"subvol={root_subvolume}" if by_name else DEFAULT_SUBVOLUME


def fstab_method(mount_point: Path, subvolume_path: str, fstab: str) -> str:
    """How a subvolume mounted on its own, other than root, is found at boot: by subvol=.

    The restored subvolume keeps its name and gets a new ID, so fstab has to
    mount it by that name; a missing line, subvolid= or another name is refused.
    """
    options = _fstab_options(fstab, str(mount_point))
    if options is None:
        raise PatrolError(
            f"{FSTAB} has no line for {mount_point}, so nothing would mount the restored "
            "subvolume there at boot"
        )
    names = []
    for option in options:
        key, _, value = option.partition("=")
        if key == "subvolid":
            raise PatrolError(
                f"{FSTAB} mounts {mount_point} by subvolume ID (subvolid=), which can't follow "
                "a rollback; mount it with subvol= instead"
            )
        if key == "subvol":
            names.append(value.strip("/"))
    if names != [subvolume_path]:
        found = f"subvol={names[0]}" if names else "no subvol= option"
        raise PatrolError(
            f"{FSTAB} mounts {mount_point} with {found}, not by the name of the subvolume "
            f"mounted there, subvol={subvolume_path}"
        )
    return f"subvol={subvolume_path} in {FSTAB}"


def pending_rollback(config: Config, mount: system.Mount | None) -> int | None:
    """The ID of the snapshot a mount still is while a rollback waits for a reboot, else None.

    A rollback moves the mounted subvolume into the snapshot store, and the
    mount table follows it: until the reboot, the mount is from
    "/<snapshots_subvolume>/<id>/snapshot" instead of its usual subvolume.
    """
    if mount is None or mount.fstype != "btrfs":
        return None
    prefix = f"/{config.snapshots_subvolume.strip('/')}/"
    suffix = f"/{SUBVOLUME_NAME}"
    if not (mount.root.startswith(prefix) and mount.root.endswith(suffix)):
        return None
    middle = mount.root[len(prefix) : -len(suffix)]
    return int(middle) if middle.isascii() and middle.isdigit() else None


def pending_rollback_message(snapshot_id: int, path: Path = ROOT_MOUNT) -> str:
    what = "/ is still the previous system" if path == ROOT_MOUNT else f"{path} is still the previous state"
    return f"a rollback is waiting for a reboot: {what}, kept as snapshot {snapshot_id}"


def _fstab_options(fstab: str, mount_point: str) -> list[str] | None:
    for line in fstab.splitlines():
        fields = line.split()
        if len(fields) >= 4 and not fields[0].startswith("#") and fields[1] == mount_point:
            return fields[3].split(",")
    return None


def _rootflags(cmdline: str) -> list[str]:
    return [
        option
        for token in cmdline.split()
        if token.startswith("rootflags=")
        for option in token.removeprefix("rootflags=").split(",")
    ]


def nested_subvolumes(subvolumes: Sequence[Subvolume], parent: str) -> list[str]:
    """Outermost subvolumes inside parent, as paths relative to it.

    subvolumes must be listed from the top-level subvolume, so that their paths
    start at the top level. Subvolumes nested deeper move with their parent.
    """
    prefix = parent.strip("/") + "/"
    inside = sorted(s.path.removeprefix(prefix) for s in subvolumes if s.path.startswith(prefix))
    outermost: list[str] = []
    for path in inside:
        if not any(path.startswith(outer + "/") for outer in outermost):
            outermost.append(path)
    return outermost


@contextlib.contextmanager
def prepare(config: Config, store: SnapshotStore, target: Snapshot) -> Iterator[RollbackPlan]:
    """Check that rolling back to target can work, and yield the plan.

    The top-level subvolume stays mounted while the plan is in use.
    """
    subvolume = config.find(target.subvolume)
    if subvolume is None:
        raise PatrolError(
            f"snapshot {target.id} is of the subvolume {target.subvolume!r}, "
            "which isn't in the configuration"
        )
    snapshot_root = store.subvolume(target.id)
    if not snapshot_root.is_dir():
        raise PatrolError(f"snapshot {target.id} has no subvolume")
    if subvolume.name == ROOT:
        checks = _prepare_root(config, store, target, snapshot_root)
    else:
        checks = _prepare_other(config, store, target, subvolume)
    with checks as plan:
        yield plan


@contextlib.contextmanager
def _prepare_root(
    config: Config, store: SnapshotStore, target: Snapshot, snapshot_root: Path
) -> Iterator[RollbackPlan]:
    kernel = platform.release()
    modules = snapshot_root / "usr/lib/modules"
    if not (modules / kernel).is_dir():
        raise PatrolError(
            f"snapshot {target.id} has no kernel modules for the running kernel {kernel}, "
            "so the restored system couldn't load its drivers"
        )
    kernels = boot.installed_kernel_versions()
    if kernel not in kernels:
        raise PatrolError(f"the running kernel {kernel} has no boot entry in {boot.LOCATIONS}")
    warnings = []
    lacking = sorted(k for k in kernels if not (modules / k).is_dir())
    if lacking:
        warnings.append(
            f"snapshot {target.id} has no modules for {', '.join(lacking)}; "
            f"choose {kernel} in the boot menu when you reboot, then remove the "
            "other entries with 'btrfs-patrol prune-kernels'"
        )
    fstab = snapshot_root / "etc/fstab"
    boots_by = boot_method(
        config.root_subvolume,
        fstab.read_text() if fstab.is_file() else "",
        PROC_CMDLINE.read_text(),
    )

    root_mount = system.find_mount(ROOT_MOUNT, system.read_mounts())
    if root_mount is None or root_mount.fstype != "btrfs":
        raise PatrolError("/ is not a btrfs filesystem")
    device = config.device or root_mount.source
    root_id = btrfs.subvolume_id(ROOT_MOUNT)

    with system.mounted_top_level(device) as top:
        root = top / config.root_subvolume
        if not root.is_dir() or btrfs.subvolume_id(root) != root_id:
            raise PatrolError(
                f"{config.root_subvolume!r} on {device} is not the subvolume mounted at /; "
                "if you already rolled back, reboot first"
            )
        _check_snapshots_subvolume(config, top, device)
        if boots_by == DEFAULT_SUBVOLUME and btrfs.get_default_subvolume_id(top) != root_id:
            raise PatrolError(
                "the system finds its root through the default subvolume, "
                "but the default subvolume isn't the root subvolume"
            )
        nested = nested_subvolumes(btrfs.list_subvolumes(top), config.root_subvolume)
        yield RollbackPlan(
            config, store, target, top, device, root_id, boots_by, nested, kernel, warnings
        )


@contextlib.contextmanager
def _prepare_other(
    config: Config, store: SnapshotStore, target: Snapshot, subvolume: ManagedSubvolume
) -> Iterator[RollbackPlan]:
    mounts = system.read_mounts()
    root_mount = system.find_mount(ROOT_MOUNT, mounts)
    if root_mount is None or root_mount.fstype != "btrfs":
        raise PatrolError("/ is not a btrfs filesystem")
    pending = pending_rollback(config, root_mount)
    if pending is not None:
        raise PatrolError(f"{pending_rollback_message(pending)}; reboot first")

    path = subvolume.path
    own_mount = system.find_mount(path, mounts)
    if own_mount is not None:
        if own_mount.fstype != "btrfs" or own_mount.source != root_mount.source:
            raise PatrolError(f"{path} is not on the btrfs filesystem mounted at /")
        pending = pending_rollback(config, own_mount)
        if pending is not None:
            raise PatrolError(f"{pending_rollback_message(pending, path)}; reboot first")
        subvolume_path = own_mount.root.strip("/")
        found_by = fstab_method(path, subvolume_path, FSTAB.read_text()) if subvolume_path else ""
    else:
        outer = system.containing_mount(path, mounts)
        if outer is None or outer.fstype != "btrfs" or outer.source != root_mount.source:
            raise PatrolError(f"{path} is not on the btrfs filesystem mounted at /")
        if not path.is_dir() or not btrfs.is_subvolume(path):
            raise PatrolError(f"{path} is not a btrfs subvolume")
        parent = outer.root.strip("/")
        inside = path.relative_to(outer.mount_point).as_posix()
        subvolume_path = f"{parent}/{inside}" if parent else inside
        found_by = f"its path inside the {parent!r} subvolume" if parent else "its path"
    if not subvolume_path:
        raise PatrolError(f"{path} is the top-level subvolume, which can't be rolled back")

    current_id = btrfs.subvolume_id(path)
    device = config.device or root_mount.source
    with system.mounted_top_level(device) as top:
        location = top / subvolume_path
        if not location.is_dir() or btrfs.subvolume_id(location) != current_id:
            raise PatrolError(f"{subvolume_path!r} on {device} is not the subvolume at {path}")
        _check_snapshots_subvolume(config, top, device)
        nested = nested_subvolumes(btrfs.list_subvolumes(top), subvolume_path)
        yield RollbackPlan(
            config, store, target, top, device, current_id, found_by, nested, None,
            subvolume=subvolume, subvolume_path=subvolume_path, mounted=own_mount is not None,
        )


def _check_snapshots_subvolume(config: Config, top: Path, device: str) -> None:
    snapshots = top / config.snapshots_subvolume
    if not snapshots.is_dir() or btrfs.subvolume_id(snapshots) != btrfs.subvolume_id(
        config.snapshots_dir
    ):
        raise PatrolError(
            f"{config.snapshots_subvolume!r} on {device} is not the subvolume "
            f"mounted at {config.snapshots_dir}"
        )


@dataclass
class RollbackPlan:
    config: Config
    store: SnapshotStore
    target: Snapshot
    top: Path
    """Where the top-level subvolume is mounted."""
    device: str
    current_id: int
    """ID of the subvolume being rolled back, as it is now."""
    found_by: str
    """How the subvolume is found at boot."""
    nested: list[str]
    kernel: str | None
    """The running kernel, checked for root; None for other subvolumes."""
    warnings: list[str] = field(default_factory=list)
    subvolume: ManagedSubvolume | None = None
    """What is rolled back; None for root."""
    subvolume_path: str | None = None
    """Its path from the top-level subvolume; None for the configured root subvolume."""
    mounted: bool = True
    """Whether it stays mounted as it is until the reboot, as root does; False when it
    is reached through its path inside another subvolume and is replaced at once."""

    @property
    def name(self) -> str:
        return self.subvolume.name if self.subvolume else ROOT

    @property
    def path(self) -> Path:
        return self.subvolume.path if self.subvolume else ROOT_MOUNT

    @property
    def location(self) -> str:
        return self.subvolume_path or self.config.root_subvolume

    def describe(self) -> list[str]:
        if self.name == ROOT:
            return [
                f"root subvolume:     {self.location} (ID {self.current_id}) on {self.device}",
                f"found at boot by:   {self.found_by}",
                f"running kernel:     {self.kernel} (modules in the snapshot, boot entry present)",
                f"nested subvolumes:  {', '.join(self.nested) or 'none'} (kept, moved into the restored root)",
                "current state:      kept as a new snapshot of kind 'rollback'",
                "logs:               /var/log is part of the root subvolume, so logs written "
                f"since snapshot {self.target.id} stay with the current state",
            ]
        if self.mounted:
            effect = f"at the next reboot; until then {self.path} is still the current state"
        else:
            effect = f"at once; programs with files open in {self.path} keep the current ones until they restart"
        return [
            f"subvolume:          {self.name}, {self.location} (ID {self.current_id}) on {self.device}",
            f"found at boot by:   {self.found_by}",
            f"nested subvolumes:  {', '.join(self.nested) or 'none'} (kept, moved into the restored subvolume)",
            "current state:      kept as a new snapshot of kind 'rollback'",
            f"takes effect:       {effect}",
        ]

    def execute(self) -> Snapshot:
        """Roll back, and return the snapshot that keeps the previous state."""
        snapshots = self.top / self.config.snapshots_subvolume
        current = self.top / self.location
        undo: UndoSteps = []
        with self.store.lock():
            try:
                saved = self.store.new_entry(
                    kind="rollback",
                    description=f"state before rollback to {self.target.id}",
                    keep=True,
                    subvolume=self.name,
                )
                undo.append(("remove the new snapshot entry", lambda: self.store.remove_entry(saved.id)))

                old = snapshots / str(saved.id) / SUBVOLUME_NAME
                os.rename(current, old)
                undo.append((f"move the old {self.name} subvolume back", lambda: os.rename(old, current)))

                btrfs.create_snapshot(snapshots / str(self.target.id) / SUBVOLUME_NAME, current)
                undo.append((f"delete the new {self.name} subvolume", lambda: btrfs.delete_subvolume(current)))
                # 'btrfs subvolume snapshot' into a path that already exists as a
                # DIRECTORY puts the snapshot one level down and still exits 0.
                # Nothing below would notice: the by-name path checks nothing, and
                # the default-subvolume path asks for the id of the subvolume
                # CONTAINING current, then compares that id against itself, so it
                # passes while the default points somewhere else entirely.
                if not btrfs.is_subvolume(current):
                    raise PatrolError(
                        f"{current} is not a subvolume after restoring snapshot {self.target.id}; "
                        "the snapshot was placed inside it instead of becoming it"
                    )

                for path in self.nested:
                    _move_nested(old / path, current / path, undo)

                if self.name == ROOT and self.found_by == DEFAULT_SUBVOLUME:
                    new_id = btrfs.subvolume_id(current)
                    btrfs.set_default_subvolume(new_id, self.top)
                    undo.append(
                        (
                            "restore the default subvolume",
                            lambda: btrfs.set_default_subvolume(self.current_id, self.top),
                        )
                    )
                    if btrfs.get_default_subvolume_id(self.top) != new_id:
                        raise PatrolError("the default subvolume did not change")
            except BaseException as error:
                failed = _undo(undo)
                if failed:
                    raise PatrolError(
                        f"rollback failed ({error}), and undoing it failed too: "
                        f"{'; '.join(failed)}. Check 'btrfs subvolume list /' before rebooting"
                    ) from error
                raise
        os.sync()
        return saved


def _move_nested(source: Path, destination: Path, undo: UndoSteps) -> None:
    # The new subvolume is a snapshot, which has an empty directory where the nested one was.
    if destination.is_dir() and not any(destination.iterdir()):
        destination.rmdir()
    destination.parent.mkdir(parents=True, exist_ok=True)
    os.rename(source, destination)
    undo.append((f"move {source.name} back", lambda: os.rename(destination, source)))


def _undo(steps: UndoSteps) -> list[str]:
    """Run undo steps newest first, and describe the ones that failed."""
    failed = []
    for description, action in reversed(steps):
        try:
            action()
        # BaseException, like the handler that calls this: the unwind is what
        # puts the root subvolume back under its name, and a second Ctrl-C
        # while a slow step runs must not abandon it half done. A machine with
        # no 'root' subvolume boots to the dracut emergency shell.
        except BaseException as e:  # noqa: BLE001
            failed.append(f"could not {description}: {e}")
    return failed
