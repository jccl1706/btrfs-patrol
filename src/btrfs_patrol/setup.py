# SPDX-License-Identifier: GPL-3.0-or-later
"""Setting a system up for btrfs-patrol.

prepare() works out what is missing and yields a plan, changing nothing except
a temporary mount of the top-level subvolume. SetupPlan.execute() then does
only the steps that are missing:

1. Write the configuration, if there is none, with the root subvolume
   detected from the mount at /.
2. Create the snapshots subvolume next to the root subvolume, readable only by
   root: old snapshots can contain setuid programs with known security holes.
3. Create the mount point, add it to /etc/fstab (after saving a backup), and
   mount it.
4. Enable and start the daily snapshot timer, when its unit is installed and
   disabled. The RPM installs it off, as Fedora's presets leave every
   package's units, so without this step nothing would take scheduled
   snapshots.
5. SELinux, before the timer (see selinux.py): exclude the snapshots
   directory from full relabels whenever a policy is installed, and give the
   store's own files their labels when SELinux is enabled.

A step that is already done is skipped, so running setup again is safe, also
after a step failed.
"""

from __future__ import annotations

import contextlib
import json
import shutil
import stat
import tomllib
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from btrfs_patrol import btrfs, selinux, system
from btrfs_patrol import config as config_mod
from btrfs_patrol.config import Config
from btrfs_patrol.errors import PatrolError
from btrfs_patrol.snapshots import ROOT_MOUNT

FSTAB = Path("/etc/fstab")
FSTAB_BACKUP_SUFFIX = ".btrfs-patrol-backup"
# The top directory of every btrfs subvolume has this inode number.
SUBVOLUME_ROOT_INODE = 256
TIMER = "btrfs-patrol-snapshot.timer"


def timer_needs_enabling(state: str | None) -> bool:
    """Whether setup should enable the timer, given its 'systemctl is-enabled' state.

    Only "disabled", the state a fresh package install leaves it in. Not
    installed (None, as from a source checkout), already enabled, masked or
    static timers are left alone: a masked timer is a choice someone made.
    """
    return state == "disabled"


def fstab_entry(fstab: str, mount_point: Path) -> list[str] | None:
    """The fields of the fstab line that mounts mount_point, if there is one."""
    for line in fstab.splitlines():
        fields = line.split()
        if len(fields) >= 4 and not fields[0].startswith("#") and fields[1] == str(mount_point):
            return fields
    return None


def snapshot_mount_options(root_fields: list[str] | None, snapshots_subvolume: str) -> str:
    """Mount options for the snapshots subvolume: the root's options, with its own subvol=."""
    options = [
        option
        for option in (root_fields[3].split(",") if root_fields else [])
        if option.partition("=")[0] not in ("subvol", "subvolid") and option != "defaults"
    ]
    return ",".join([f"subvol={snapshots_subvolume}", *(options or ["noatime"])])


def config_text(root_subvolume: str) -> str:
    """The example configuration with root_subvolume filled in."""
    text = config_mod.example_text()
    default = 'root_subvolume = "root"'
    if text.count(default) != 1:
        raise PatrolError("the example configuration has no root_subvolume line to fill in")
    return text.replace(default, f"root_subvolume = {json.dumps(root_subvolume)}")


@contextlib.contextmanager
def prepare(config_path: Path) -> Iterator[SetupPlan]:
    """Work out what setup has to do, and yield the plan while the top-level subvolume is mounted."""
    mounts = system.read_mounts()
    root = system.find_mount(ROOT_MOUNT, mounts)
    if root is None or root.fstype != "btrfs":
        raise PatrolError("/ is not a btrfs filesystem")
    root_subvolume = root.root.strip("/")
    if not root_subvolume:
        raise PatrolError(
            "/ is the top-level subvolume; btrfs-patrol needs the root in a subvolume "
            "of its own, as Fedora's installer sets it up"
        )

    if config_path.exists():
        config = config_mod.load(config_path)
        new_config_text = None
        if config.root_subvolume != root_subvolume:
            raise PatrolError(
                f"{config_path} says the root subvolume is {config.root_subvolume!r}, "
                f"but / is {root_subvolume!r}"
            )
    else:
        new_config_text = config_text(root_subvolume)
        config = config_mod.parse(tomllib.loads(new_config_text))

    snapshots_dir = config.snapshots_dir
    snapshots_mount = system.find_mount(snapshots_dir, mounts)
    if snapshots_mount is not None and snapshots_mount.root != f"/{config.snapshots_subvolume}":
        raise PatrolError(
            f"{snapshots_dir} already has {snapshots_mount.root!r} mounted, "
            f"not the subvolume {config.snapshots_subvolume!r}"
        )
    if snapshots_mount is None and snapshots_dir.exists():
        if not snapshots_dir.is_dir() or any(snapshots_dir.iterdir()):
            raise PatrolError(f"{snapshots_dir} exists and isn't an empty directory")

    fstab = FSTAB.read_text()
    entry = fstab_entry(fstab, snapshots_dir)
    if entry is not None:
        subvols = [
            value.strip("/")
            for key, _, value in (option.partition("=") for option in entry[3].split(","))
            if key == "subvol"
        ]
        if entry[2] != "btrfs" or subvols != [config.snapshots_subvolume]:
            raise PatrolError(
                f"{FSTAB} already mounts {snapshots_dir}, but not as btrfs "
                f"subvol={config.snapshots_subvolume}: {' '.join(entry)}"
            )
        fstab_line = None
    else:
        uuid = system.run("findmnt", "-no", "UUID", ROOT_MOUNT).strip()
        if not uuid:
            raise PatrolError("could not find the UUID of the filesystem mounted at /")
        options = snapshot_mount_options(fstab_entry(fstab, ROOT_MOUNT), config.snapshots_subvolume)
        fstab_line = f"UUID={uuid}  {snapshots_dir}  btrfs  {options}  0 0"

    enable_timer = timer_needs_enabling(system.unit_file_state(TIMER))
    add_exclusion = selinux.exclusion_missing(snapshots_dir)
    # A store that isn't mounted yet gets labeled once it is; a mounted one only when
    # restorecon says its labels differ from the policy's.
    label_store = selinux.enabled() and (
        snapshots_mount is None or bool(selinux.mislabeled(selinux.store_paths(snapshots_dir)))
    )

    device = config.device or root.source
    with system.mounted_top_level(device) as top:
        subvolume = top / config.snapshots_subvolume
        if subvolume.exists() and subvolume.stat().st_ino != SUBVOLUME_ROOT_INODE:
            raise PatrolError(
                f"{config.snapshots_subvolume!r} on {device} exists but isn't a subvolume"
            )
        if not subvolume.parent.is_dir():
            raise PatrolError(f"{subvolume.parent.relative_to(top)!r} doesn't exist on {device}")
        yield SetupPlan(
            config_path=config_path,
            config=config,
            new_config_text=new_config_text,
            top=top,
            create_subvolume=not subvolume.exists(),
            create_mount_point=snapshots_mount is None and not snapshots_dir.exists(),
            fstab_line=fstab_line,
            mount=snapshots_mount is None,
            enable_timer=enable_timer,
            add_exclusion=add_exclusion,
            label_store=label_store,
        )


@dataclass
class SetupPlan:
    config_path: Path
    config: Config
    new_config_text: str | None
    """The configuration to write, or None when the file already exists."""
    top: Path
    """Where the top-level subvolume is mounted."""
    create_subvolume: bool
    create_mount_point: bool
    fstab_line: str | None
    """The line to add to /etc/fstab, or None when it already has one."""
    mount: bool
    enable_timer: bool = False
    add_exclusion: bool = False
    """Add the snapshots directory to SELinux's fixfiles_exclude_dirs."""
    label_store: bool = False
    """Give the store's own files their SELinux labels."""

    def actions(self) -> list[str]:
        config = self.config
        actions = []
        if self.new_config_text is not None:
            actions.append(f"write {self.config_path} (root subvolume {config.root_subvolume!r})")
        if self.create_subvolume:
            actions.append(
                f"create the top-level subvolume {config.snapshots_subvolume!r}, "
                "readable only by root"
            )
        if self.create_mount_point:
            actions.append(f"create the directory {config.snapshots_dir}")
        if self.fstab_line is not None:
            actions.append(
                f"add to {FSTAB}, saving a copy as {FSTAB}{FSTAB_BACKUP_SUFFIX}: {self.fstab_line}"
            )
        if self.mount:
            actions.append(f"mount {config.snapshots_dir}")
        if self.add_exclusion:
            actions.append(
                f"exclude {config.snapshots_dir} from full SELinux relabels ({selinux.EXCLUDE_FILE})"
            )
        if self.label_store:
            actions.append(
                f"set the SELinux labels of {config.snapshots_dir} and its snapshot entries "
                "(not of what is inside the snapshots)"
            )
        if self.enable_timer:
            actions.append(f"enable and start the daily snapshot timer ({TIMER})")
        return actions

    def execute(self) -> None:
        config = self.config
        if self.new_config_text is not None:
            self.config_path.parent.mkdir(parents=True, exist_ok=True)
            system.write_atomic(self.config_path, self.new_config_text, mode=0o644)
        if self.create_subvolume:
            subvolume = self.top / config.snapshots_subvolume
            btrfs.create_subvolume(subvolume)
            subvolume.chmod(0o700)
        if self.create_mount_point:
            config.snapshots_dir.mkdir(mode=0o755)
        if self.fstab_line is not None:
            current = FSTAB.read_text()
            shutil.copy2(FSTAB, FSTAB.with_name(FSTAB.name + FSTAB_BACKUP_SUFFIX))
            separator = "\n" if current and not current.endswith("\n") else ""
            system.write_atomic(
                FSTAB,
                f"{current}{separator}{self.fstab_line}\n",
                mode=stat.S_IMODE(FSTAB.stat().st_mode),
            )
            # systemd turns fstab into mount units; reload so they match the file.
            system.run("systemctl", "daemon-reload")
        if self.mount:
            system.run("mount", config.snapshots_dir)
        if self.add_exclusion:
            selinux.add_exclusion(config.snapshots_dir)
        if self.label_store:
            selinux.label(selinux.store_paths(config.snapshots_dir))
        # Last, so the first scheduled run finds the snapshots directory mounted.
        if self.enable_timer:
            system.run("systemctl", "enable", "--now", TIMER)
