# SPDX-License-Identifier: GPL-3.0-or-later
"""Rolling the root subvolume back to a snapshot.

prepare() checks everything it can without changing anything, and yields a
plan while the top-level subvolume (ID 5) is mounted. RollbackPlan.execute()
then rolls back:

1. Create a snapshot entry, kind "rollback" and kept, for the current state.
2. Move the current root subvolume into that entry. The running system keeps
   using it until the reboot.
3. Create a writable snapshot of the chosen snapshot as the new root subvolume.
4. Move nested subvolumes (systemd creates var/lib/portables, for example)
   from the old root into the new one. Snapshots don't contain them, so
   without this their data would stay behind in the old root, and the old
   root couldn't be deleted later.
5. If the system finds its root through the btrfs default subvolume, point
   the default at the new root and read it back to confirm.

Every step records how to undo it. If a step fails, the steps already done are
undone newest first. Nothing reboots automatically.

On Fedora /boot is not part of the root subvolume, so kernels and boot entries
are not rolled back. prepare() refuses unless the restored system has modules
for the running kernel and that kernel has a boot entry, and warns about boot
entries whose kernel modules the snapshot lacks.
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
from btrfs_patrol.config import Config
from btrfs_patrol.errors import PatrolError
from btrfs_patrol.snapshots import ROOT_MOUNT, SUBVOLUME_NAME, Snapshot, SnapshotStore

DEFAULT_SUBVOLUME = "the btrfs default subvolume"
PROC_CMDLINE = Path("/proc/cmdline")

UndoSteps = list[tuple[str, Callable[[], None]]]


def boot_method(root_subvolume: str, fstab: str, cmdline: str) -> str:
    """How the root subvolume is found at boot: by name, or as the default subvolume.

    fstab is the restored system's /etc/fstab and cmdline the kernel command
    line. Mounting by subvolume ID, or by another name, is refused: the
    restored root gets a new ID and keeps the configured name.
    """
    by_name = False
    for option in [*_fstab_root_options(fstab), *_rootflags(cmdline)]:
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


def pending_rollback(config: Config, root: system.Mount | None) -> int | None:
    """The ID of the snapshot that / still is while a rollback waits for a reboot, else None.

    A rollback moves the running root subvolume into the snapshot store, and the
    mount table follows it: until the reboot, / is mounted from
    "/<snapshots_subvolume>/<id>/snapshot" instead of "/<root_subvolume>".
    """
    if root is None or root.fstype != "btrfs":
        return None
    prefix = f"/{config.snapshots_subvolume.strip('/')}/"
    suffix = f"/{SUBVOLUME_NAME}"
    if not (root.root.startswith(prefix) and root.root.endswith(suffix)):
        return None
    middle = root.root[len(prefix) : -len(suffix)]
    return int(middle) if middle.isascii() and middle.isdigit() else None


def pending_rollback_message(snapshot_id: int) -> str:
    return (
        "a rollback is waiting for a reboot: / is still the previous system, "
        f"kept as snapshot {snapshot_id}"
    )


def _fstab_root_options(fstab: str) -> list[str]:
    for line in fstab.splitlines():
        fields = line.split()
        if len(fields) >= 4 and not fields[0].startswith("#") and fields[1] == "/":
            return fields[3].split(",")
    return []


def _rootflags(cmdline: str) -> list[str]:
    return [
        option
        for token in cmdline.split()
        if token.startswith("rootflags=")
        for option in token.removeprefix("rootflags=").split(",")
    ]


def nested_subvolumes(subvolumes: Sequence[Subvolume], root_subvolume: str) -> list[str]:
    """Outermost subvolumes inside the root subvolume, as paths relative to it.

    subvolumes must be listed from the top-level subvolume, so that their paths
    start at the top level. Subvolumes nested deeper move with their parent.
    """
    prefix = root_subvolume.strip("/") + "/"
    inside = sorted(s.path.removeprefix(prefix) for s in subvolumes if s.path.startswith(prefix))
    outermost: list[str] = []
    for path in inside:
        if not any(path.startswith(parent + "/") for parent in outermost):
            outermost.append(path)
    return outermost


@contextlib.contextmanager
def prepare(config: Config, store: SnapshotStore, target: Snapshot) -> Iterator[RollbackPlan]:
    """Check that rolling back to target can work, and yield the plan.

    The top-level subvolume stays mounted while the plan is in use.
    """
    kernel = platform.release()
    snapshot_root = store.subvolume(target.id)
    if not snapshot_root.is_dir():
        raise PatrolError(f"snapshot {target.id} has no subvolume")
    modules = snapshot_root / "usr/lib/modules"
    if not (modules / kernel).is_dir():
        raise PatrolError(
            f"snapshot {target.id} has no kernel modules for the running kernel {kernel}, "
            "so the restored system couldn't load its drivers"
        )
    kernels = boot.installed_kernel_versions()
    if kernel not in kernels:
        raise PatrolError(f"the running kernel {kernel} has no boot entry in {boot.BOOT_ENTRIES}")
    warnings = []
    lacking = sorted(k for k in kernels if not (modules / k).is_dir())
    if lacking:
        warnings.append(
            f"snapshot {target.id} has no modules for {', '.join(lacking)}; "
            f"choose {kernel} in the boot menu when you reboot"
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
        snapshots = top / config.snapshots_subvolume
        if not snapshots.is_dir() or btrfs.subvolume_id(snapshots) != btrfs.subvolume_id(
            config.snapshots_dir
        ):
            raise PatrolError(
                f"{config.snapshots_subvolume!r} on {device} is not the subvolume "
                f"mounted at {config.snapshots_dir}"
            )
        if boots_by == DEFAULT_SUBVOLUME and btrfs.get_default_subvolume_id(top) != root_id:
            raise PatrolError(
                "the system finds its root through the default subvolume, "
                "but the default subvolume isn't the root subvolume"
            )
        nested = nested_subvolumes(btrfs.list_subvolumes(top), config.root_subvolume)
        yield RollbackPlan(
            config, store, target, top, device, root_id, boots_by, nested, kernel, warnings
        )


@dataclass
class RollbackPlan:
    config: Config
    store: SnapshotStore
    target: Snapshot
    top: Path
    """Where the top-level subvolume is mounted."""
    device: str
    root_id: int
    boots_by: str
    nested: list[str]
    kernel: str
    warnings: list[str] = field(default_factory=list)

    def describe(self) -> list[str]:
        return [
            f"root subvolume:     {self.config.root_subvolume} (ID {self.root_id}) on {self.device}",
            f"found at boot by:   {self.boots_by}",
            f"running kernel:     {self.kernel} (modules in the snapshot, boot entry present)",
            f"nested subvolumes:  {', '.join(self.nested) or 'none'} (kept, moved into the restored root)",
            "current state:      kept as a new snapshot of kind 'rollback'",
            "logs:               /var/log is part of the root subvolume, so logs written "
            f"since snapshot {self.target.id} stay with the current state",
        ]

    def execute(self) -> Snapshot:
        """Roll back, and return the snapshot that keeps the previous state."""
        snapshots = self.top / self.config.snapshots_subvolume
        root = self.top / self.config.root_subvolume
        undo: UndoSteps = []
        with self.store.lock():
            try:
                saved = self.store.new_entry(
                    kind="rollback",
                    description=f"state before rollback to {self.target.id}",
                    keep=True,
                )
                undo.append(("remove the new snapshot entry", lambda: self.store.remove_entry(saved.id)))

                old_root = snapshots / str(saved.id) / SUBVOLUME_NAME
                os.rename(root, old_root)
                undo.append(("move the old root subvolume back", lambda: os.rename(old_root, root)))

                btrfs.create_snapshot(snapshots / str(self.target.id) / SUBVOLUME_NAME, root)
                undo.append(("delete the new root subvolume", lambda: btrfs.delete_subvolume(root)))

                for path in self.nested:
                    _move_nested(old_root / path, root / path, undo)

                if self.boots_by == DEFAULT_SUBVOLUME:
                    new_id = btrfs.subvolume_id(root)
                    btrfs.set_default_subvolume(new_id, self.top)
                    undo.append(
                        (
                            "restore the default subvolume",
                            lambda: btrfs.set_default_subvolume(self.root_id, self.top),
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
    # The new root is a snapshot, which has an empty directory where the nested subvolume was.
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
        except Exception as e:
            failed.append(f"could not {description}: {e}")
    return failed
