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
from collections.abc import Iterable
from pathlib import Path
from typing import BinaryIO

from btrfs_patrol import system

BOOT_ENTRIES = Path("/boot/loader/entries")
# Where the EFI system partition may be mounted; UKIs live in EFI/Linux below it.
ESP_MOUNTS = (Path("/boot"), Path("/efi"), Path("/boot/efi"))
UKI_DIR = "EFI/Linux"
# Where a kernel's modules live, and the tool that owns its boot entry.
MODULES = Path("/usr/lib/modules")
KERNEL_INSTALL = "kernel-install"
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
    for path in sorted(entries_dir.glob("*.conf")):
        version = _valid(parse_bls_entry(path.read_text()).get("version", ""))
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
