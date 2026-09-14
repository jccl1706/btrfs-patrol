# btrfs-patrol

A btrfs snapshot manager and rollback tool for Fedora.

> **Status: early development.** Snapshots, listing, pruning, `check` and
> `rollback` work and have been tested on a Fedora 44 VM, but not yet on real
> hardware. `setup` is not implemented yet. Try rollback in a virtual machine
> first.

btrfs-patrol is inspired by [timepatrol](https://github.com/abdeoliveira/timepatrol)
and reimplemented in Python for Fedora. See [NOTICE](NOTICE) for credits.

## Goals

- **One-command rollback** that works with Fedora's default subvolume layout
  and checks every step.
- **Handles dnf5**: snapshots before (and optionally after) each transaction.
- **Knows about `/boot`**: Fedora keeps kernels outside the root subvolume, so
  rollback refuses snapshots whose kernel can't be booted.
- **No dependencies** beyond Python 3.11+ and `btrfs-progs`.

## Commands

```
btrfs-patrol list [SELECTOR] [-v]         list snapshots
btrfs-patrol snapshot [-d TEXT] [--keep]  take a snapshot of /
btrfs-patrol describe ID TEXT             change a snapshot's description
btrfs-patrol keep SELECTOR                protect snapshots from pruning
btrfs-patrol unkeep SELECTOR              let snapshots be pruned again
btrfs-patrol delete SELECTOR [--yes]      delete snapshots
btrfs-patrol prune                        delete snapshots beyond the limit
btrfs-patrol check                        check configuration, mounts and snapshots
btrfs-patrol rollback ID [--dry-run]      roll / back to a snapshot, then reboot
btrfs-patrol setup                        (not implemented yet)
```

Selectors pick snapshots by ID (`3`, `1,10,20-23`) or by field
(`date=2026-09`, `time=16:`, `kernel=6.17`, `kind=dnf-pre`,
`description=gnome`, `keep=yes`).

Snapshot IDs are never reused: deleting the newest snapshot doesn't free its
number, so an ID you noted down always means the same snapshot.

## Rolling back

```sh
sudo btrfs-patrol rollback 12 --dry-run   # run every check, change nothing
sudo btrfs-patrol rollback 12             # roll back, after confirmation
sudo reboot
```

A rollback replaces the root subvolume with a writable copy of the snapshot.
The system you are leaving isn't lost: it is kept as a new snapshot of kind
`rollback`, marked kept, so you can roll forward to it again or delete it once
you're happy. The running system keeps working until you reboot; nothing
reboots automatically.

What is and isn't rolled back:

- **Rolled back:** everything in the root subvolume, including `/etc`, `/usr`
  and the RPM database.
- **Not rolled back:** `/home` and any other separate subvolume, and `/boot`,
  which on Fedora is a separate partition holding the kernels.
- **Kept, not rolled back:** subvolumes nested inside the root subvolume, such
  as `/var/lib/portables`. They are moved into the restored root.

Because `/boot` isn't rolled back, rollback refuses a snapshot that has no
kernel modules for the running kernel, or when the running kernel has no boot
entry. It warns when other boot entries have kernels the snapshot lacks; pick
the running kernel in the boot menu in that case.

Rollback works whether the root is found at boot through the btrfs default
subvolume (Fedora's installer) or by name with `subvol=`. It refuses setups
that mount the root by subvolume ID (`subvolid=`), which can't follow a
rollback. If any step fails, the steps already done are undone.

## Manual setup

`btrfs-patrol setup` will automate this. Until then, on a default Fedora
install:

```sh
# 1. Create a top-level "snapshots" subvolume next to "root".
UUID=$(findmnt -no UUID /)
sudo mount -o subvolid=5 UUID=$UUID /mnt
sudo btrfs subvolume create /mnt/snapshots
sudo chmod 700 /mnt/snapshots   # old snapshots may contain vulnerable setuid binaries
sudo umount /mnt

# 2. Mount it at /.snapshots on every boot.
sudo mkdir /.snapshots
echo "UUID=$UUID  /.snapshots  btrfs  subvol=snapshots,noatime  0 0" | sudo tee -a /etc/fstab
sudo systemctl daemon-reload
sudo mount /.snapshots

# 3. Install the configuration and check it.
sudo install -Dm644 data/config.toml.example /etc/btrfs-patrol/config.toml
sudo PYTHONPATH=src python3 -m btrfs_patrol check
```

For dnf5 snapshots, install `libdnf5-plugin-actions` and copy
`data/dnf5/btrfs-patrol.actions` to `/etc/dnf/libdnf5-plugins/actions.d/`.

## Development

No packages need to be installed; the tests use the standard library's
`unittest` and don't need root or a btrfs filesystem.

```sh
PYTHONPATH=src python3 -m unittest discover -s tests
PYTHONPATH=src python3 -m btrfs_patrol --help
```

To try read-only commands without root, point `BTRFS_PATROL_CONFIG` (or
`--config`) at a configuration whose `snapshots_dir` is a scratch directory.

### Layout

```
pyproject.toml                  package metadata
src/btrfs_patrol/
  cli.py                        argument parsing and commands
  config.py                     configuration loading and validation
  snapshots.py                  snapshot metadata, store and pruning
  selectors.py                  "1,10,20-23", "kernel=6.17", ...
  rollback.py                   rollback checks, plan and undoable steps
  btrfs.py                      wrapper around the btrfs command
  system.py                     commands, mount table, top-level mount
  boot.py                       boot entries in /boot
  dnf.py                        dnf5 transaction hook
  output.py                     colors and the snapshot table
data/
  config.toml.example           default configuration
  dnf5/btrfs-patrol.actions     libdnf5-plugin-actions hook
  systemd/                      daily snapshot service and timer
packaging/btrfs-patrol.spec     RPM spec for Fedora / COPR
tests/                          unittest suite
```

## Roadmap

1. Test on real hardware.
2. `setup` to create the snapshots subvolume, fstab entry and configuration.
3. Package lists from dnf5 transactions in snapshot descriptions.
4. COPR repository.

## License

GPL-3.0-or-later. See [LICENSE](LICENSE) and [NOTICE](NOTICE).
