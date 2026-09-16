# SPDX-License-Identifier: GPL-3.0-or-later
"""Kernels that can actually be booted.

On Fedora, /boot is not part of the root subvolume. A rollback restores
/usr/lib/modules and the RPM database, but not the kernels and boot entries, so
before rolling back we need to know which kernel versions can still be booted.
Both kinds of Boot Loader Specification entries are read:

- Type #1: text files in /boot/loader/entries that name their kernel's version.
  Fedora's GRUB and systemd-boot use these by default.
- Type #2: unified kernel images (UKIs) in EFI/Linux on the EFI system
  partition, each one PE file holding the kernel, its initrd and command line.
  The version comes from the image's .uname section. Images built before that
  section existed fall back to the version string in the Linux kernel's own
  setup header (inside the .linux section), and last to the file name
  kernel-install gives them, <entry-token>-<version>[+tries].efi.

A version is only accepted if it looks like what 'uname -r' prints, so a file
that can't be read can't vouch for a kernel by accident. That also leaves out
GRUB's rescue entry, which Fedora's installer creates with the version
"0-rescue-<machine-id>": it names no kernel whose modules a snapshot could have.
"""

from __future__ import annotations

import re
import struct
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import BinaryIO

from btrfs_patrol import system
from btrfs_patrol.snapshots import SUBVOLUME_NAME

BOOT_ENTRIES = Path("/boot/loader/entries")
# Where the EFI system partition may be mounted; UKIs live in EFI/Linux below it.
ESP_MOUNTS = (Path("/boot"), Path("/efi"), Path("/boot/efi"))
UKI_DIR = "EFI/Linux"
# Where a kernel's modules live, and the tool that owns its boot entry.
MODULES = Path("/usr/lib/modules")
KERNEL_INSTALL = "kernel-install"
# Our own Type #1 entries. The prefix is how we recognise them again:
# nothing else may be touched in a directory the boot loader owns.
ENTRY_PREFIX = "btrfs-patrol-"
MACHINE_ID = Path("/etc/machine-id")
LOCATIONS = f"{BOOT_ENTRIES} or {UKI_DIR} (unified kernel images)"

# 'uname -r': starts with a digit, has a dot, no spaces.
_VERSION = re.compile(r"[0-9]+\.[0-9A-Za-z._+~-]+")
_BOOT_COUNTER = re.compile(r"\+[0-9]+(-[0-9]+)?$")
_MAX_PE_SECTIONS = 96  # the PE format's own limit


def parse_bls_entry(text: str) -> dict[str, str]:
    """Parse a BLS Type #1 entry: one 'key value' pair per line, '#' starts a comment."""
    entry = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        key, _, value = line.partition(" ")
        entry.setdefault(key, value.strip())
    return entry


def _valid(version: str) -> str | None:
    return version if _VERSION.fullmatch(version) else None


def _pe_sections(f: BinaryIO) -> dict[str, tuple[int, int]]:
    """Section name -> (file offset, size) for a PE file, or {} if it isn't one."""
    head = f.read(0x40)
    if len(head) < 0x40 or head[:2] != b"MZ":
        return {}
    (pe_offset,) = struct.unpack_from("<I", head, 0x3C)
    f.seek(pe_offset)
    coff = f.read(24)  # "PE\0\0" and the COFF file header
    if len(coff) < 24 or coff[:4] != b"PE\0\0":
        return {}
    (count,) = struct.unpack_from("<H", coff, 6)
    (optional_header_size,) = struct.unpack_from("<H", coff, 20)
    if count > _MAX_PE_SECTIONS:
        return {}
    f.seek(pe_offset + 24 + optional_header_size)
    table = f.read(40 * count)
    sections: dict[str, tuple[int, int]] = {}
    for i in range(len(table) // 40):
        header = table[40 * i : 40 * i + 40]
        name = header[:8].rstrip(b"\0").decode("ascii", "replace")
        virtual_size, _address, raw_size, raw_offset = struct.unpack_from("<IIII", header, 8)
        size = min(virtual_size, raw_size) if virtual_size else raw_size
        sections.setdefault(name, (raw_offset, size))
    return sections


def _read_cstring(f: BinaryIO, offset: int, limit: int = 256) -> str:
    f.seek(offset)
    return f.read(limit).split(b"\0", 1)[0].decode("ascii", "replace").strip()


def _version_from_image(f: BinaryIO) -> str | None:
    sections = _pe_sections(f)
    if ".uname" in sections:
        offset, size = sections[".uname"]
        version = _valid(_read_cstring(f, offset, min(size, 256)))
        if version:
            return version
    if ".linux" in sections:
        # The Linux boot protocol's setup header: "HdrS" at 0x202, and at 0x20E a
        # pointer to the version string, relative to 0x200.
        offset, _size = sections[".linux"]
        f.seek(offset + 0x202)
        if f.read(4) == b"HdrS":
            f.seek(offset + 0x20E)
            raw = f.read(2)
            if len(raw) == 2:
                (pointer,) = struct.unpack("<H", raw)
                if pointer:
                    text = _read_cstring(f, offset + 0x200 + pointer)
                    version = _valid(text.split()[0]) if text else None
                    if version:
                        return version
    return None


def _version_from_file_name(name: str) -> str | None:
    stem = _BOOT_COUNTER.sub("", name.removesuffix(".efi"))
    _token, dash, version = stem.partition("-")
    return _valid(version) if dash else None


def uki_version(path: Path) -> str | None:
    """The kernel version a unified kernel image boots, or None if it can't be told."""
    try:
        with path.open("rb") as f:
            version = _version_from_image(f)
    except (OSError, struct.error):
        version = None
    return version or _version_from_file_name(path.name)


def installed_kernel_versions(
    entries_dir: Path = BOOT_ENTRIES, esp_mounts: Iterable[Path] = ESP_MOUNTS
) -> set[str]:
    """Kernel versions (as in 'uname -r') that have a boot entry of either type."""
    versions = set()
    # Type #1 entries live in $BOOT/loader/entries, and $BOOT is the XBOOTLDR
    # partition when there is one, otherwise the ESP. With the ESP at /efi and no
    # XBOOTLDR, kernel-install writes to /efi/loader/entries and nothing is under
    # /boot at all, which used to refuse every rollback on a healthy machine.
    entry_dirs, seen_dirs = [], set()
    for candidate in [entries_dir, *(mount / "loader/entries" for mount in esp_mounts)]:
        try:
            if not candidate.is_dir():
                continue
            key = candidate.resolve()
        except OSError:
            continue
        if key not in seen_dirs:
            seen_dirs.add(key)
            entry_dirs.append(candidate)
    for path in sorted(path for directory in entry_dirs for path in directory.glob("*.conf")):
        # Unreadable or non-UTF-8 entries must not escape as a traceback: this
        # runs inside the rollback checks. errors="replace" keeps a usable
        # version line in a file with a few bad bytes; a dangling symlink or an
        # unreadable mode is skipped, the same way a bad UKI is.
        try:
            text = path.read_text(errors="replace")
        except OSError:
            continue
        version = _valid(parse_bls_entry(text).get("version", ""))
        if version:
            versions.add(version)
    seen = set()
    for mount in esp_mounts:
        directory = mount / UKI_DIR
        try:
            if not directory.is_dir():
                continue
            key = directory.resolve()
        except OSError:
            continue
        if key in seen:  # /boot and /boot/efi can be the same partition
            continue
        seen.add(key)
        for path in sorted(directory.glob("*.efi")):
            version = uki_version(path)
            if version:
                versions.add(version)
    return versions


def stale_kernel_versions(
    modules: Path = MODULES,
    entries_dir: Path = BOOT_ENTRIES,
    esp_mounts: Iterable[Path] = ESP_MOUNTS,
) -> set[str]:
    """Kernel versions that have a boot entry but no modules on this system.

    A rollback restores /usr/lib/modules but not /boot, so a snapshot taken
    before a kernel was installed leaves that kernel's entry behind, offering a
    boot with no drivers. These are the entries worth removing.
    """
    return {
        version
        for version in installed_kernel_versions(entries_dir, esp_mounts)
        if not (modules / version).is_dir()
    }


def remove_boot_entry(version: str) -> str:
    """Remove a kernel's boot entry and images with kernel-install(8)."""
    return system.run(
        KERNEL_INSTALL,
        "remove",
        version,
        missing=f"{KERNEL_INSTALL} not found; install systemd-udev",
    )


def snapshot_options(cmdline: str, subvolume_path: str) -> str:
    """The kernel command line that boots a snapshot, from the running one.

    Derived rather than built, so that everything the system needs to reach its
    disk at all - rd.luks.uuid, rd.lvm.lv, root=UUID - is carried over untouched.
    Four things change:

    - subvol=/subvolid= in rootflags is replaced with the snapshot's path, while
      the rest of rootflags (compress=, noatime) is kept.
    - ro, never rw: a snapshot is read-only and btrfs refuses the writes anyway.
    - resume= is dropped. Resuming a hibernation image taken by the normal
      system on top of a snapshot would restore memory that does not match the
      filesystem underneath it.
    - systemd.volatile= is dropped. An overlay needs support in the initrd that
      Fedora's does not build, and the boot hangs after loading SELinux policy.
    - systemd.machine_id= is dropped. A unified kernel image bakes it into its
      command line, and it tells systemd the machine-id is transient and must be
      committed to disk at boot, which a read-only root cannot do:
      systemd-machine-id-commit.service then fails and the system comes up
      degraded. The snapshot's own /etc/machine-id holds the same value.
    """
    words, flags = [], []
    for word in cmdline.split():
        key, _, value = word.partition("=")
        if key == "rootflags":
            flags.extend(
                option
                for option in value.split(",")
                if option and option.partition("=")[0] not in ("subvol", "subvolid")
            )
        elif key in (
            "resume",
            "resume_offset",
            "systemd.volatile",
            "systemd.machine_id",
        ) or word in ("ro", "rw"):
            continue
        else:
            words.append(word)
    flags.append(f"subvol={subvolume_path}")
    words.append("ro")
    words.append("rootflags=" + ",".join(flags))
    return " ".join(words)


def entry_text(
    title: str, version: str, linux: str, initrd: str, options: str, sort_key: str = "zz-btrfs-patrol"
) -> str:
    """A Boot Loader Specification Type #1 entry.

    Read by systemd-boot and, through blscfg, by Fedora's GRUB, so one file
    serves both. The sort-key puts snapshots below the real entries rather than
    between them.
    """
    return (
        f"title      {title}\n"
        f"version    {version}\n"
        f"linux      {linux}\n"
        f"initrd     {initrd}\n"
        f"options    {options}\n"
        f"sort-key   {sort_key}\n"
    )


def kernel_files(boot_dir: Path, machine_id: str, version: str) -> tuple[str, str] | None:
    """Paths to a kernel and its initrd, relative to $BOOT, or None if they are missing.

    Both layouts kernel-install can be configured with are looked for. With
    layout=uki the images still land in <machine-id>/<version>/ on the way to
    being packed into the unified image, which is what makes a Type #1 entry
    possible on a system that otherwise has none.
    """
    candidates = (
        (f"{machine_id}/{version}/linux", f"{machine_id}/{version}/initrd"),
        (f"vmlinuz-{version}", f"initramfs-{version}.img"),
    )
    for linux, initrd in candidates:
        if (boot_dir / linux).is_file() and (boot_dir / initrd).is_file():
            return f"/{linux}", f"/{initrd}"
    return None


def snapshot_entries(entries_dir: Path) -> dict[int, Path]:
    """Our own entries, by snapshot ID. Entries we did not write are never touched."""
    found = {}
    for path in sorted(entries_dir.glob(f"{ENTRY_PREFIX}*.conf")):
        stem = path.name[len(ENTRY_PREFIX) : -len(".conf")]
        if stem.isascii() and stem.isdigit():
            found[int(stem)] = path
    return found


def sync_snapshot_entries(
    entries_dir: Path,
    boot_dir: Path,
    wanted: Sequence[tuple[int, str, str]],
    machine_id: str,
    cmdline: str,
    snapshots_subvolume: str,
) -> tuple[list[int], list[int], list[int]]:
    """Make the boot menu match the snapshots that should have an entry.

    wanted is (snapshot id, kernel version, title), newest first. Returns the
    IDs written, removed, and skipped because their kernel is no longer on the
    boot partition - a snapshot whose kernel has been removed cannot be booted,
    and an entry promising otherwise is worse than no entry.

    Only files this program wrote are ever removed: the directory belongs to the
    boot loader, and kernel-install writes its own entries into it.
    """
    entries_dir.mkdir(parents=True, exist_ok=True)
    existing = snapshot_entries(entries_dir)
    written, skipped = [], []
    for snapshot_id, version, title in wanted:
        files = kernel_files(boot_dir, machine_id, version)
        if files is None:
            skipped.append(snapshot_id)
            continue
        linux, initrd = files
        subvolume = f"{snapshots_subvolume.strip('/')}/{snapshot_id}/{SUBVOLUME_NAME}"
        text = entry_text(title, version, linux, initrd, snapshot_options(cmdline, subvolume))
        path = entries_dir / f"{ENTRY_PREFIX}{snapshot_id}.conf"
        if not path.is_file() or path.read_text() != text:
            system.write_atomic(path, text, mode=0o600)
        written.append(snapshot_id)
    keep = set(written)
    removed = []
    for snapshot_id, path in sorted(existing.items()):
        if snapshot_id not in keep:
            path.unlink(missing_ok=True)
            removed.append(snapshot_id)
    return written, removed, skipped


def boot_partition(machine_id: str, esp_mounts: Iterable[Path] = ESP_MOUNTS) -> Path | None:
    """Where Type #1 entries and kernel images live, or None if it can't be found.

    $BOOT is the XBOOTLDR partition when there is one and the ESP otherwise, and
    Fedora mounts either at /boot. The machine-id directory or an existing
    loader/entries is what identifies it.
    """
    for mount in (Path("/boot"), *esp_mounts):
        try:
            if (mount / machine_id).is_dir() or (mount / "loader/entries").is_dir():
                return mount
        except OSError:
            continue
    return None
