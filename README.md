# btrfs-patrol

A btrfs snapshot manager and rollback tool for Fedora.

> **Status: 0.3.0.** Every command has been tested on a Fedora
> 44 VM and on a ThinkPad T480 (LUKS, LVM and btrfs), from source and from the
> RPM: setup, snapshots from dnf and the timer, rollback to an older state and
> forward again, and hibernating with a rollback waiting for its reboot - and
> again, from the RPM, on a VM with SELinux enforcing, with no denials. 0.2.0
> was tested again on the T480 (systemd-boot entries, SELinux enforcing),
> upgraded from 0.1.1, and its unified kernel image support on a VM booting
> one. A fresh Fedora 44 Workstation install with its default layout and GRUB
> was tested from COPR: setup, dnf and timer snapshots, and a rollback and
> roll forward with reboots. It is
> young software on the part of your system you most need to work, so keep
> backups.

btrfs-patrol is inspired by [timepatrol](https://github.com/abdeoliveira/timepatrol)
and reimplemented in Python for Fedora. See [NOTICE](NOTICE) for credits.

## Goals

- **One-command rollback** that works with Fedora's default subvolume layout
  and checks every step.
- **Handles dnf5**: snapshots before (and optionally after) each transaction.
- **Knows about `/boot`**: Fedora keeps kernels outside the root subvolume, so
  rollback refuses snapshots whose kernel can't be booted. Both kinds of boot
  entry count: `/boot/loader/entries` files and unified kernel images.
- **Other subvolumes too**: `/home`, or any other subvolume, can get snapshots
  of its own and be rolled back on its own.
- **No dependencies** beyond Python 3.11+, `btrfs-progs` and `util-linux`.

## Install

From the [COPR repository](https://copr.fedorainfracloud.org/coprs/jccl1706/btrfs-patrol/),
for Fedora 43 and newer on x86_64 and aarch64:

```sh
sudo dnf copr enable jccl1706/btrfs-patrol
sudo dnf install btrfs-patrol
sudo btrfs-patrol setup --dry-run   # see what setup will do
sudo btrfs-patrol setup
```

`dnf upgrade` then keeps it up to date. Each
[release](https://github.com/jccl1706/btrfs-patrol/releases) also has the RPM
and source RPM, with their SHA-256 sums, for installing without the repository.

## Commands

```
btrfs-patrol setup [--dry-run]            set the system up (run once)
btrfs-patrol list [SELECTOR] [-v]         list snapshots
btrfs-patrol snapshot [-d TEXT] [--keep]  take snapshots of / and other subvolumes
btrfs-patrol describe ID TEXT             change a snapshot's description
btrfs-patrol keep SELECTOR                protect snapshots from pruning
btrfs-patrol unkeep SELECTOR              let snapshots be pruned again
btrfs-patrol delete SELECTOR [--yes]      delete snapshots
btrfs-patrol prune                        delete snapshots beyond the limit
btrfs-patrol check                        check configuration, mounts and snapshots
btrfs-patrol rollback ID [--dry-run]      roll a subvolume back to a snapshot, then reboot
```

Selectors pick snapshots by ID (`3`, `1,10,20-23`) or by field
(`date=2026-09`, `time=16:`, `kernel=6.17`, `kind=dnf-pre`,
`description=gnome`, `subvolume=home`, `keep=yes`).

Snapshot IDs are never reused: deleting the newest snapshot doesn't free its
number, so an ID you noted down always means the same snapshot.

The manual page, `man btrfs-patrol` (or `man -l man/btrfs-patrol.8` in a
checkout), is the full reference: every command and option, the
configuration, the files btrfs-patrol uses, and recovery examples.

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
5. **SELinux:** adds `/.snapshots` to `/etc/selinux/fixfiles_exclude_dirs`
   whenever a policy is installed, and gives the snapshots directory and its
   entries their labels when SELinux is on. A full relabel would otherwise
   change the labels inside writable `rollback` snapshots, and rolling back to
   one would boot a mislabeled system. For the same reason, a manual
   `restorecon -R /` needs `-e /.snapshots`.

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
entry - neither a file in `/boot/loader/entries` nor a unified kernel image in
`EFI/Linux` on the EFI system partition, whose version is read from the image
itself. It warns when other boot entries have kernels the snapshot lacks; pick
the running kernel in the boot menu in that case.

Rollback works whether the root is found at boot through the btrfs default
subvolume (Fedora's installer) or by name with `subvol=`. It refuses setups
that mount the root by subvolume ID (`subvolid=`), which can't follow a
rollback. If any step fails, the steps already done are undone.

## Other subvolumes

Besides the root subvolume, btrfs-patrol can take snapshots of other
subvolumes on the same filesystem, such as `/home`: add a table for each to
`/etc/btrfs-patrol/config.toml`, then run `check`.

```toml
[subvolumes.home]
path = "/home"        # its mount point, or its path inside another subvolume
max_snapshots = 20    # default: retention.max_snapshots
timer = true          # in the daily snapshot (default: true)
dnf = false           # in dnf's snapshots too (default: false)
```

- **Snapshots:** `snapshot` and the daily timer take one snapshot of each
  subvolume, with an ID each; `snapshot --subvolume home` takes just that one.
  dnf only snapshots root, unless a table says `dnf = true`.
- **Listing and pruning:** `list` gets a SUBVOLUME column, `subvolume=home`
  selects, and each subvolume is pruned against its own `max_snapshots`.
- **Rollback:** each subvolume is rolled back on its own. `rollback 42` restores
  the subvolume snapshot 42 is of and leaves the others alone, so undoing a bad
  update never throws away your documents, and restoring `/home` never touches
  the system. A subvolume mounted on its own must be mounted by `subvol=` in
  `/etc/fstab`, and is used from the next reboot; one reached through its path
  inside another subvolume, such as `/var/log`, is replaced at once.
- **Subvolumes inside root** aren't part of root's snapshots, so a rollback of
  root leaves them as they are, and `check` warns about it. That is the point
  for `/var/log`, but think twice before making `/var/lib` one.

The path has to be a subvolume already: btrfs-patrol doesn't turn a directory
into one.

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
  boot.py                       boot entries and unified kernel images
  dnf.py                        dnf5 transaction hook
  selinux.py                    relabel exclusion and labels for the snapshot store
  output.py                     colors and the snapshot table
data/
  dnf5/btrfs-patrol.actions     libdnf5-plugin-actions hook
  systemd/                      daily snapshot service and timer
man/btrfs-patrol.8              manual page
packaging/btrfs-patrol.spec     RPM spec for Fedora / COPR
tests/                          unittest suite (test_man.py keeps the manual in step)
```

## Roadmap

1. A command that turns an existing directory, such as `/var/log`, into a
   subvolume safely, so it can be snapshotted and rolled back on its own.
2. Boot menu entries for snapshots, for systemd-boot and GRUB, so a system
   too broken to log in to (after `rm -rf /etc`, say) can boot a snapshot and
   be rolled back from there, without a live USB. The entries would boot a
   snapshot read-only, with a kernel it has modules for, and be kept in step
   as snapshots are taken, pruned and deleted.

## License

GPL-3.0-or-later. See [LICENSE](LICENSE) and [NOTICE](NOTICE).
