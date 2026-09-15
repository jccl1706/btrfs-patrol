# btrfs-patrol

A btrfs snapshot manager and rollback tool for Fedora.

> **Status: 0.1.0, first release.** Every command has been tested on a Fedora
> 44 VM and on a ThinkPad T480 (LUKS, LVM and btrfs), from source and from the
> RPM: setup, snapshots from dnf and the timer, rollback to an older state and
> forward again, and hibernating with a rollback waiting for its reboot. It is
> young software on the part of your system you most need to work, so keep
> backups.

btrfs-patrol is inspired by [timepatrol](https://github.com/abdeoliveira/timepatrol)
and reimplemented in Python for Fedora. See [NOTICE](NOTICE) for credits.

## Goals

- **One-command rollback** that works with Fedora's default subvolume layout
  and checks every step.
- **Handles dnf5**: snapshots before (and optionally after) each transaction.
- **Knows about `/boot`**: Fedora keeps kernels outside the root subvolume, so
  rollback refuses snapshots whose kernel can't be booted.
- **No dependencies** beyond Python 3.11+, `btrfs-progs` and `util-linux`.

## Commands

```
btrfs-patrol setup [--dry-run]            set the system up (run once)
btrfs-patrol list [SELECTOR] [-v]         list snapshots
btrfs-patrol snapshot [-d TEXT] [--keep]  take a snapshot of /
btrfs-patrol describe ID TEXT             change a snapshot's description
btrfs-patrol keep SELECTOR                protect snapshots from pruning
btrfs-patrol unkeep SELECTOR              let snapshots be pruned again
btrfs-patrol delete SELECTOR [--yes]      delete snapshots
btrfs-patrol prune                        delete snapshots beyond the limit
btrfs-patrol check                        check configuration, mounts and snapshots
btrfs-patrol rollback ID [--dry-run]      roll / back to a snapshot, then reboot
```

Selectors pick snapshots by ID (`3`, `1,10,20-23`) or by field
(`date=2026-09`, `time=16:`, `kernel=6.17`, `kind=dnf-pre`,
`description=gnome`, `keep=yes`).

Snapshot IDs are never reused: deleting the newest snapshot doesn't free its
number, so an ID you noted down always means the same snapshot.

## Setup

```sh
sudo btrfs-patrol setup --dry-run   # show what it would do
sudo btrfs-patrol setup             # do it, after confirmation
```

`setup` does only what is missing, so it is safe to run again:

1. **Configuration:** writes `/etc/btrfs-patrol/config.toml` from the
   commented example, with the root subvolume detected from the mount at `/`.
   An existing configuration is kept.
2. **Snapshots subvolume:** creates a top-level subvolume named `snapshots`
   next to the root subvolume, readable only by root, because old snapshots
   can contain setuid programs with known security holes.
3. **Mount:** creates `/.snapshots`, adds it to `/etc/fstab` with the root's
   mount options (saving the old file as `/etc/fstab.btrfs-patrol-backup`),
   and mounts it.
4. **Timer:** enables and starts the daily snapshot timer. The RPM installs it
   switched off, as Fedora installs every package's services, so this is what
   turns scheduled snapshots on. A masked timer is left alone.

It then runs `check`. It refuses rather than guesses when something is in the
way: a root that isn't btrfs or is the top-level subvolume, a configuration
for a different root subvolume, something else mounted at `/.snapshots`, a
non-empty `/.snapshots` directory, or an fstab line that mounts it differently.

For dnf5 snapshots, install `libdnf5-plugin-actions` and copy
`data/dnf5/btrfs-patrol.actions` to `/etc/dnf/libdnf5-plugins/actions.d/`.
The RPM package installs that file for you. Each snapshot's description says
what the transaction changes, such as
`upgrade kernel-core, mesa-dri-drivers +12 more; install tree`.

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
- **Logs leave with the previous state:** `/var/log` is part of the root
  subvolume, so the journal written since the snapshot stays in the `rollback`
  snapshot. `rollback` prints the `journalctl -D` command that reads it.

Until you reboot, `/` is still the previous system. `check` says so,
`snapshot` refuses to run, and the timer and the dnf hook skip their snapshots.

Because `/boot` isn't rolled back, rollback refuses a snapshot that has no
kernel modules for the running kernel, or when the running kernel has no boot
entry. It warns when other boot entries have kernels the snapshot lacks; pick
the running kernel in the boot menu in that case.

Rollback works whether the root is found at boot through the btrfs default
subvolume (Fedora's installer) or by name with `subvol=`. It refuses setups
that mount the root by subvolume ID (`subvolid=`), which can't follow a
rollback. If any step fails, the steps already done are undone.

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
  config.toml.example           the commented default configuration
  setup.py                      setup's checks and steps
  snapshots.py                  snapshot metadata, store and pruning
  selectors.py                  "1,10,20-23", "kernel=6.17", ...
  rollback.py                   rollback checks, plan and undoable steps
  btrfs.py                      wrapper around the btrfs command
  system.py                     commands, atomic writes, mount table, top-level mount
  boot.py                       boot entries in /boot
  dnf.py                        dnf5 transaction hook
  output.py                     colors and the snapshot table
data/
  dnf5/btrfs-patrol.actions     libdnf5-plugin-actions hook
  systemd/                      daily snapshot service and timer
packaging/btrfs-patrol.spec     RPM spec for Fedora / COPR
tests/                          unittest suite
```

## Roadmap

1. COPR repository.
2. Test with SELinux enforcing: both test machines had it disabled.
3. Unified kernel images (Boot Loader Specification Type #2 entries) in the
   rollback's boot checks.

## License

GPL-3.0-or-later. See [LICENSE](LICENSE) and [NOTICE](NOTICE).
