# btrfs-patrol

A btrfs snapshot manager and rollback tool for Fedora.

> **Status: early development.** Snapshots, listing, pruning and `check` work
> but have not been tested much on real systems. `rollback` and `setup` are
> not implemented yet. Try it in a virtual machine first.

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
btrfs-patrol rollback ID                  (not implemented yet)
btrfs-patrol setup                        (not implemented yet)
```

Selectors pick snapshots by ID (`3`, `1,10,20-23`) or by field
(`date=2026-09`, `time=16:`, `kernel=6.17`, `kind=dnf-pre`,
`description=gnome`, `keep=yes`).

Snapshot IDs are never reused: deleting the newest snapshot doesn't free its
number, so an ID you noted down always means the same snapshot.

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
  btrfs.py                      wrapper around the btrfs command
  system.py                     mount table parsing
  boot.py                       boot entries in /boot
  dnf.py                        dnf5 transaction hook
  rollback.py                   rollback (planned steps, not implemented)
  output.py                     colors and the snapshot table
data/
  config.toml.example           default configuration
  dnf5/btrfs-patrol.actions     libdnf5-plugin-actions hook
  systemd/                      daily snapshot service and timer
packaging/btrfs-patrol.spec     RPM spec for Fedora / COPR
tests/                          unittest suite
```

## Roadmap

1. Test the core commands on a Fedora VM.
2. Rollback, following the plan in `src/btrfs_patrol/rollback.py`.
3. `setup` to create the snapshots subvolume, fstab entry and configuration.
4. Package lists from dnf5 transactions in snapshot descriptions.
5. COPR repository.

## License

GPL-3.0-or-later. See [LICENSE](LICENSE) and [NOTICE](NOTICE).
