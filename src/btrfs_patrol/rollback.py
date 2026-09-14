# SPDX-License-Identifier: GPL-3.0-or-later
"""Rolling the root subvolume back to a snapshot.

Not implemented yet. This is the most dangerous operation in btrfs-patrol, so
it will be built one step at a time, with every step checked and a way back if
a step fails. The planned procedure:

1. Refuse unless the snapshot's kernel is the running kernel and still has a
   boot entry in /boot (see boot.installed_kernel_versions).
2. Mount the top-level subvolume (subvolid=5) on a private directory under
   /run, never on a fixed path in /tmp.
3. Check that the configured root and snapshots subvolumes exist at the top
   level, and that the root subvolume is the one mounted at /.
4. Move the current root subvolume into the snapshots subvolume as a new
   snapshot of kind "rollback", so the state being replaced is kept.
5. Create a writable snapshot of the chosen snapshot as the new root
   subvolume. If that fails, move the old root back before reporting the error.
6. If the system boots from the default subvolume (no subvol= in /etc/fstab
   and no rootflags= on the kernel command line), point the default subvolume
   at the new root and read it back to confirm.
7. Unmount, sync, and tell the user to reboot. Never reboot automatically.

Nested subvolumes need care. systemd creates some inside the root subvolume
(a stock Fedora 44 install has var/lib/portables), and btrfs snapshots don't
include them: a snapshot holds an empty directory in their place. So:

- Before step 4, list the subvolumes nested in the root subvolume. After step
  5, move each one from the old root (now the "rollback" snapshot) to the same
  path in the new root, replacing the empty directory, so they aren't lost.
- Deleting a snapshot fails while it still contains nested subvolumes, so
  SnapshotStore.delete must delete those first, deepest first.
"""

from __future__ import annotations

from btrfs_patrol.config import Config
from btrfs_patrol.errors import PatrolError
from btrfs_patrol.snapshots import Snapshot, SnapshotStore


def rollback(config: Config, store: SnapshotStore, snapshot: Snapshot) -> None:
    raise PatrolError("rollback is not implemented yet")
