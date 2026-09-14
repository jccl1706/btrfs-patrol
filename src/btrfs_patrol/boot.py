# SPDX-License-Identifier: GPL-3.0-or-later
"""Kernels that can actually be booted.

On Fedora, /boot is not part of the root subvolume. A rollback restores
/usr/lib/modules and the RPM database, but not the kernels and boot entries in
/boot, so before rolling back we need to know which kernel versions still have
a boot entry. Fedora's GRUB and systemd-boot both use Boot Loader
Specification (BLS) Type #1 entries in /boot/loader/entries.

TODO: also handle Type #2 entries (unified kernel images in /boot/EFI/Linux).
"""

from __future__ import annotations

from pathlib import Path

BOOT_ENTRIES = Path("/boot/loader/entries")


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


def installed_kernel_versions(entries_dir: Path = BOOT_ENTRIES) -> set[str]:
    """Kernel versions (as in 'uname -r') that have a boot entry."""
    versions = set()
    for path in sorted(entries_dir.glob("*.conf")):
        version = parse_bls_entry(path.read_text()).get("version")
        if version:
            versions.add(version)
    return versions
