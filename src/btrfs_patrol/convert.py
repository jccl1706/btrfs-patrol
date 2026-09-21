# SPDX-License-Identifier: GPL-3.0-or-later
"""Turn an existing directory into a btrfs subvolume, in place.

A directory that is part of the root subvolume cannot be snapshotted or rolled
back on its own - it goes wherever root goes. Converting it makes it a subvolume
nested inside root, which btrfs-patrol can then manage like any other.

NESTED, NOT A SEPARATE MOUNT, and that is what makes this a small change rather
than a large one. A subvolume created inside root appears at its own path with
no /etc/fstab entry and no mount unit: it is simply there, the way /var/lib
/portables already is on a stock Fedora system. Nothing has to be edited that a
failed boot would make hard to undo.

Two consequences, both wanted here:

  - snapshots of root stop including it, which is the point of converting it;
  - rolling back root moves it across into the restored root untouched, which
    rollback.py already does for every nested subvolume, so the logs you would
    want to read after a rollback survive it.

THE COPY IS REFLINKED, so converting a large directory costs almost no space and
very little time: btrfs shares the extents between the old directory and the new
subvolume, and only the metadata is duplicated. Reflinks work across subvolumes
of one filesystem, which is exactly the case here.

WHAT THIS CANNOT DO is make processes that already have files open under the
directory notice. After the swap their descriptors still point at the old inodes,
so they keep writing where the directory used to be - journald is the obvious
one. Nothing here restarts them: this is not the tool to decide that rsyslog can
be bounced. It reports exactly who they are and leaves the choice to the user,
and the old contents are kept rather than deleted so that whatever they wrote in
the meantime can still be recovered.
"""

from __future__ import annotations

import contextlib
import os
import re
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from . import btrfs, selinux, system
from .config import Config, ROOT, SUBVOLUMES_SECTION
from .errors import PatrolError

#: Where the directory's previous contents are kept after the swap. Left behind
#: deliberately: processes holding it open are still writing into it, and
#: deleting it would throw away whatever they wrote between the copy and the
#: swap.
KEPT_SUFFIX = ".pre-subvolume"

#: Scratch name for the new subvolume while it is being filled.
NEW_SUFFIX = ".new-subvolume"

#: Subvolume names btrfs-patrol uses for itself, or that would be ambiguous.
_NAME_RE = re.compile(r"\A[a-z0-9][a-z0-9_-]*\Z")

#: Directories that must not be converted, whatever is asked. Converting any of
#: these either cannot work or breaks the system in a way rollback cannot fix.
_REFUSED = {
    Path("/"): "the root of the filesystem is already a subvolume",
    Path("/boot"): "/boot is a separate partition, not part of the btrfs filesystem",
    Path("/etc"): "the system cannot boot if /etc is missing for even a moment",
    Path("/usr"): "the system cannot boot if /usr is missing for even a moment",
    Path("/dev"): "/dev is a devtmpfs, not part of the btrfs filesystem",
    Path("/proc"): "/proc is a procfs, not part of the btrfs filesystem",
    Path("/sys"): "/sys is a sysfs, not part of the btrfs filesystem",
    Path("/run"): "/run is a tmpfs, not part of the btrfs filesystem",
}


def default_name(path: Path) -> str:
    """A subvolume name derived from the path: /var/log -> "var-log"."""
    parts = [part for part in path.parts if part != "/"]
    name = "-".join(parts).lower()
    name = re.sub(r"[^a-z0-9_-]+", "-", name).strip("-")
    return name or "subvolume"


def check_name(name: str, config: Config) -> None:
    """Raise if name cannot be used for a new managed subvolume."""
    if name == ROOT:
        raise PatrolError(f"{name!r} is the name of the root subvolume; choose another with --name")
    if not _NAME_RE.match(name):
        raise PatrolError(
            f"{name!r} is not a usable subvolume name: use lowercase letters, digits, "
            "'-' and '_', starting with a letter or digit"
        )
    if config.find(name) is not None:
        raise PatrolError(
            f"[{SUBVOLUMES_SECTION}.{name}] is already in the configuration; "
            "choose another name with --name"
        )


@dataclass(frozen=True)
class Holder:
    """A process with a file open under the directory being converted."""

    pid: int
    comm: str
    unit: str | None

    def describe(self) -> str:
        # THE SERVICE NAME WHEN THERE IS ONE, the process name otherwise, and
        # the difference matters. For systemd-journald.service the unit is the
        # useful handle - it is what you would restart. For anything in a user
        # session the unit is "session-c4.scope", which names nothing you can
        # act on and hides the fact that it was, say, tail.
        if self.unit and self.unit.endswith(".service"):
            return f"{self.unit:<28} pid {self.pid}"
        suffix = f"  ({self.unit})" if self.unit else ""
        return f"{self.comm:<28} pid {self.pid}{suffix}"


def holders(path: Path) -> list[Holder]:
    """Processes with a file open under path, or the directory as their cwd.

    Best effort: /proc entries come and go while this walks them, and a
    descriptor that is gone by the time it is read simply is not reported.
    """
    found: dict[int, Holder] = {}
    prefix = f"{path}/"
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        if pid == os.getpid():
            continue
        targets = []
        with contextlib.suppress(OSError):
            targets.append(os.readlink(entry / "cwd"))
        with contextlib.suppress(OSError):
            for fd in (entry / "fd").iterdir():
                with contextlib.suppress(OSError):
                    targets.append(os.readlink(fd))
        if not any(t == str(path) or t.startswith(prefix) for t in targets):
            continue
        comm = "?"
        with contextlib.suppress(OSError):
            comm = (entry / "comm").read_text().strip()
        found[pid] = Holder(pid=pid, comm=comm, unit=_unit_of(entry))
    return sorted(found.values(), key=lambda h: h.pid)


def _unit_of(proc: Path) -> str | None:
    """The systemd unit a process belongs to, from its cgroup, or None."""
    try:
        text = (proc / "cgroup").read_text()
    except OSError:
        return None
    # "0::/system.slice/systemd-journald.service" - the last .service, .scope or
    # .mount component is the unit; user slices nest deeper than that.
    for part in reversed(text.strip().split("\n")[-1].split("/")):
        if part.endswith((".service", ".scope", ".mount", ".socket")):
            return part
    return None


def refuses_manual_restart(unit: str) -> bool:
    """Whether systemd would decline 'systemctl restart unit'.

    AUDITD IS WHY THIS EXISTS, and it is not an exotic case: it holds /var/log
    open, so it appears in the report for the very example this command was
    written for, and it sets RefuseManualStart=yes. Naming it in the restart
    line made the WHOLE command fail - exit status 4, "Operation refused, unit
    auditd.service may be requested by dependency only" - and left it holding
    the old directory while the user believed they had been told what to do.
    Advice that does not work is worse than no advice.
    """
    try:
        out = system.run(
            "systemctl", "show", "-p", "RefuseManualStart", "-p", "RefuseManualStop",
            "--value", unit,
        )
    except (PatrolError, OSError):
        # No systemd, or the unit went away between the scan and here. Offering
        # the restart and being wrong beats silently dropping it from the list.
        return False
    return "yes" in out.split()


def restart_plan(found: Sequence[Holder]) -> tuple[str | None, list[str]]:
    """How to make the holders reopen: a restart line, and the units that refuse one.

    Returns (command, reboot_only). Either can be empty - nothing here is a
    service, or every service that is refuses to be restarted by hand.
    """
    services = sorted({h.unit for h in found if h.unit and h.unit.endswith(".service")})
    reboot_only = [unit for unit in services if refuses_manual_restart(unit)]
    restartable = [unit for unit in services if unit not in reboot_only]
    command = (
        "systemctl restart " + " ".join(u.removesuffix(".service") for u in restartable)
        if restartable
        else None
    )
    return command, reboot_only


@contextlib.contextmanager
def prepare(
    config: Config, config_path: Path, path: Path, name: str, manage: bool
) -> Iterator["ConvertPlan"]:
    """Check that path can be converted, and yield the plan.

    The top-level subvolume stays mounted while the plan is in use.
    """
    path = Path(os.path.abspath(path))
    for refused, why in _REFUSED.items():
        if path == refused:
            raise PatrolError(f"refusing to convert {path}: {why}")
    if not path.exists():
        raise PatrolError(f"{path} doesn't exist")
    if path.is_symlink():
        raise PatrolError(f"{path} is a symlink; convert what it points at instead")
    if not path.is_dir():
        raise PatrolError(f"{path} is not a directory")
    if btrfs.is_subvolume(path):
        managed = next((s for s in config.subvolumes if s.path == path), None)
        already = f", already managed as [{SUBVOLUMES_SECTION}.{managed.name}]" if managed else (
            f"; add it to the configuration as [{SUBVOLUMES_SECTION}.{name}] to snapshot it"
        )
        raise PatrolError(f"{path} is already a btrfs subvolume{already}")
    check_name(name, config)

    mounts = system.read_mounts()
    root = system.require_mounted(Path("/"), config.root_subvolume, mounts)
    own = system.find_mount(path, mounts)
    if own is not None:
        raise PatrolError(
            f"{path} is a mount point ({own.fstype} from {own.source}), not a plain directory"
        )
    outer = system.containing_mount(path, mounts)
    if outer is None or outer.fstype != "btrfs" or outer.source != root.source:
        raise PatrolError(f"{path} is not on the btrfs filesystem mounted at /")

    # Where the directory sits when seen from the top-level subvolume, which is
    # what a nested subvolume's path has to be measured against.
    inside = path.relative_to(outer.mount_point).as_posix().strip("/")
    location = f"{outer.root.strip('/')}/{inside}".strip("/")

    parent = path.parent
    kept = parent / f".{path.name}{KEPT_SUFFIX}"
    scratch = parent / f".{path.name}{NEW_SUFFIX}"
    for existing in (kept, scratch):
        if existing.exists():
            raise PatrolError(
                f"{existing} is in the way, left by an earlier run; "
                "move or remove it and try again"
            )

    size, files = _measure(path)
    warnings: list[str] = []
    if not location.startswith(f"{config.root_subvolume.strip('/')}/"):
        warnings.append(
            f"{path} is not inside the root subvolume {config.root_subvolume!r}, "
            "so rolling back root was never going to affect it anyway"
        )

    with system.mounted_top_level(root.source) as top:
        plan = ConvertPlan(
            config=config,
            config_path=config_path,
            path=path,
            name=name,
            device=root.source,
            top=top,
            location=location,
            size=size,
            files=files,
            kept=kept,
            scratch=scratch,
            manage=manage,
            found=holders(path),
            warnings=warnings,
        )
        yield plan


def _measure(path: Path) -> tuple[int, int]:
    """Apparent size in bytes and number of files under path."""
    total = 0
    count = 0
    for root, _dirs, names in os.walk(path, onerror=lambda e: None):
        for name in names:
            count += 1
            with contextlib.suppress(OSError):
                total += os.lstat(Path(root) / name).st_size
    return total, count


def human(size: int) -> str:
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TiB"


@dataclass
class ConvertPlan:
    config: Config
    config_path: Path
    path: Path
    name: str
    device: str
    top: Path
    location: str
    size: int
    files: int
    kept: Path
    scratch: Path
    manage: bool
    found: list[Holder]
    warnings: list[str] = field(default_factory=list)

    def describe(self) -> list[str]:
        lines = [
            f"directory:          {self.path} ({human(self.size)}, {self.files:,} files)",
            f"filesystem:         {self.device}, as {self.location} from the top level",
            f"new subvolume:      nested in place, no /etc/fstab entry needed",
        ]
        if selinux.enabled():
            lines.append("SELinux:            labels copied with the contents")
        lines.append("plan:")
        lines.append(f"  1. create a subvolume at {self.scratch}")
        lines.append("  2. copy the contents into it, sharing extents (reflink)")
        lines.append(f"  3. move {self.path} aside to {self.kept}")
        lines.append(f"  4. move the subvolume into place as {self.path}")
        if self.manage:
            lines.append(
                f"  5. add [{SUBVOLUMES_SECTION}.{self.name}] to {self.config_path}"
            )
        if self.found:
            lines.append("still open by:")
            for holder in self.found:
                lines.append(f"  {holder.describe()}")
        return lines

    def execute(self) -> None:
        undo: list[tuple[str, object]] = []
        try:
            btrfs.create_subvolume(self.scratch)
            undo.append(("delete the new subvolume", lambda: btrfs.delete_subvolume(self.scratch)))

            # "src/." rather than "src" so the CONTENTS are copied into an
            # existing destination instead of a copy of the directory appearing
            # one level down - and --reflink=auto rather than =always so a file
            # that cannot be shared (a small inline extent, say) is still
            # copied rather than aborting the whole thing.
            system.run(
                "cp", "--archive", "--reflink=auto", "--one-file-system",
                f"{self.path}/.", str(self.scratch),
                missing="cp not found; install coreutils",
            )

            os.rename(self.path, self.kept)
            undo.append((f"move {self.path} back", lambda: os.rename(self.kept, self.path)))

            os.rename(self.scratch, self.path)
            undo.append((f"move the subvolume back to {self.scratch}",
                         lambda: os.rename(self.path, self.scratch)))

            # 'btrfs subvolume create' into an existing directory would have
            # failed above, but the rename could still have landed somewhere
            # unexpected. Checking costs nothing and the alternative is a
            # directory that looks converted and is not.
            if not btrfs.is_subvolume(self.path):
                raise PatrolError(f"{self.path} is not a subvolume after the swap")

            if self.manage:
                add_to_config(self.config_path, self.name, self.path)
        except BaseException as error:
            failed = _undo(undo)
            if failed:
                raise PatrolError(
                    f"converting {self.path} failed ({error}), and undoing it failed too: "
                    f"{'; '.join(failed)}. Check {self.path} and {self.kept} before rebooting"
                ) from error
            raise
        os.sync()


def _undo(steps: Sequence[tuple[str, object]]) -> list[str]:
    """Run undo steps newest first, and describe the ones that failed."""
    failed = []
    for description, action in reversed(list(steps)):
        try:
            action()  # type: ignore[operator]
        except (PatrolError, OSError) as error:
            failed.append(f"couldn't {description}: {error}")
    return failed


def table_text(name: str, path: Path) -> str:
    """The [subvolumes.<name>] table for a converted directory."""
    return f'\n[{SUBVOLUMES_SECTION}.{name}]\npath = "{path}"\n'


def add_to_config(config_path: Path, name: str, path: Path) -> None:
    """Append a [subvolumes.<name>] table to the configuration file.

    Appended as text rather than re-serialised: the file is full of comments
    explaining every option, and rewriting it from the parsed values would throw
    all of them away. A new table at the end of a TOML file cannot land inside
    another one.
    """
    try:
        text = config_path.read_text()
    except OSError as error:
        raise PatrolError(f"can't read {config_path}: {error}") from error
    if not text.endswith("\n"):
        text += "\n"
    system.write_atomic(config_path, text + table_text(name, path), mode=0o644)
