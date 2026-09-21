# btrfs-patrol

**A btrfs snapshot manager and rollback tool for Fedora's default layout.**

[![COPR build status](https://copr.fedorainfracloud.org/coprs/jccl1706/btrfs-patrol/package/btrfs-patrol/status_image/last_build.png)](https://copr.fedorainfracloud.org/coprs/jccl1706/btrfs-patrol/)
![Fedora 43+](https://img.shields.io/badge/Fedora-43%2B-51A2DA?logo=fedora&logoColor=white)
![License GPL-3.0-or-later](https://img.shields.io/badge/license-GPL--3.0--or--later-blue)

A snapshot before every `dnf` transaction and once a day, and one command to put
the system back:

```console
$ btrfs-patrol list
 ID  DATE        TIME      KERNEL                 KIND      DESCRIPTION
  1  2026-09-15  11:10:18  7.2.4-200.fc44.x86_64  manual    before the new kernel
  2  2026-09-16  11:02:10  7.2.4-200.fc44.x86_64  dnf-pre   upgrade kernel-core, mesa +12 more
 *3  2026-09-16  11:10:14  7.2.4-200.fc44.x86_64  rollback  state before rollback to 2

$ sudo btrfs-patrol rollback 1
 ID  DATE        TIME      KERNEL                 KIND    DESCRIPTION
  1  2026-09-15  11:10:18  7.2.4-200.fc44.x86_64  manual  before the new kernel
root subvolume:     root (ID 256) on /dev/vda2
found at boot by:   the btrfs default subvolume
running kernel:     7.2.4-200.fc44.x86_64 (modules in the snapshot, boot entry present)
nested subvolumes:  var/lib/portables (kept, moved into the restored root)
current state:      kept as a new snapshot of kind 'rollback'
Roll back to snapshot 1? [y/N] y
:: rolled back to snapshot 1; the previous state is kept as snapshot 4
:: reboot to start the restored system
```

It targets the layout `dnf` and Anaconda give you - btrfs with `root` and `home`
subvolumes, `/boot` on its own partition, optionally LUKS - and nothing else, so
it can check each step rather than guess.

> **Status: 0.7.0.** Young software, on the part of your system you most need to
> work. Every release is tested by hand on real hardware and in VMs, including
> rollbacks with reboots - see [Tested on](#tested-on) - but snapshots are not
> backups, so keep backups too.

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
  of its own and be rolled back on its own. `btrfs-patrol convert` turns an
  ordinary directory such as `/var/log` into one.
- **Boots a snapshot** from the boot menu, so a system that no longer starts can
  be rolled back without a live USB.
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

`dnf upgrade` then keeps it up to date. To install without the repository, each
[release](https://github.com/jccl1706/btrfs-patrol/releases) has the RPM and
source RPM with their SHA-256 sums, and every
[COPR build](https://copr.fedorainfracloud.org/coprs/jccl1706/btrfs-patrol/builds/)
keeps a set per Fedora release and architecture.

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
btrfs-patrol prune-kernels [--dry-run]    remove boot entries with no kernel modules
btrfs-patrol check                        check configuration, mounts and snapshots
btrfs-patrol diff OLD NEW                 what changed between two snapshots
btrfs-patrol tui                          browse and manage snapshots full-screen
btrfs-patrol convert PATH [--dry-run]     turn a directory into a subvolume it can snapshot
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

## Booting a snapshot

A rollback needs a system you can still log in to. When there isn't one - after
`rm -rf /etc`, or an update that leaves the machine at an emergency shell -
btrfs-patrol can put snapshots in the boot menu, so you can boot one and roll
back from there without a live USB.

It is off by default, because the boot menu is shared with the rest of the
system:

```toml
[boot]
entries = 3
```

The newest snapshots then get an entry each, written when a snapshot is taken
and removed when one is deleted or pruned. They are Boot Loader Specification
Type #1 entries, which systemd-boot reads directly. Fedora's GRUB reads the same
files through `blscfg`, so one entry should serve both - but only systemd-boot
has been tested so far, on two machines. They work on a system whose
kernels are unified kernel images too: the kernel and initrd that go into the
image are still on the boot partition, which is what an entry needs.

Booting one gives you:

- The snapshot, **read-only**. Nothing done in that session is kept, and the
  snapshot itself cannot be altered by it.
- `/home`, and any other subvolume mounted on its own, writable as usual.
- `btrfs-patrol rollback ID`, which restores the root subvolume - the one that
  is *not* running, so nothing is using it - after which a normal reboot starts
  the restored system.

Units that write to `/` fail in such a session. That is what read-only means,
not a fault.

The kernel command line is taken from the running one rather than built, so
everything needed to reach the disk - `rd.luks.uuid`, `rd.lvm.lv`, `root=UUID` -
is carried over. Four things are dropped, each because it breaks such a boot or
does not belong in a Type #1 entry: `resume=`, `initrd=`, `systemd.machine_id=`
and `systemd.volatile=`.

Only entries btrfs-patrol wrote are ever changed or removed - `kernel-install`
owns that directory too - and a snapshot whose kernel is no longer on the boot
partition is skipped rather than offered as something it is not.

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

## Converting a directory

A directory inside the root subvolume goes wherever root goes: it is part of
root's snapshots, and rolling root back rolls it back too. `convert` makes it a
subvolume of its own, which can then be snapshotted and rolled back separately.

```console
$ sudo btrfs-patrol convert /var/log
directory:          /var/log (412.8 MiB, 1,203 files)
filesystem:         /dev/vda2, as root/var/log from the top level
new subvolume:      nested in place, no /etc/fstab entry needed
SELinux:            labels copied with the contents
plan:
  1. create a subvolume at /var/.log.new-subvolume
  2. copy the contents into it, sharing extents (reflink)
  3. move /var/log aside to /var/.log.pre-subvolume
  4. move the subvolume into place as /var/log
  5. add [subvolumes.var-log] to /etc/btrfs-patrol/config.toml
still open by:
  systemd-journald.service     pid 412
Convert /var/log to subvolume 'var-log'? [y/N] y
:: /var/log is now the subvolume 'var-log'
:: added [subvolumes.var-log] to /etc/btrfs-patrol/config.toml
```

**It is nested, not mounted.** A subvolume created where the directory was
appears at its own path with no `/etc/fstab` entry and no mount unit, the way
`/var/lib/portables` already does on a stock Fedora system. Nothing is edited
that a failed boot would make hard to undo. Snapshots of root stop including it,
which is the point; rolling root back moves it across into the restored root
untouched, so the logs you would want to read *after* a rollback survive it.

**The copy is reflinked**, so converting a large directory costs almost no space
and little time — btrfs shares the extents and only the metadata is duplicated.

**Processes that already have the directory open keep writing to the old copy**
until they reopen it; systemd-journald is the obvious one. They are listed
afterwards, with a `systemctl restart` line for the services that will accept
one. Nothing is restarted for you, and the previous contents are *kept*, not
deleted, so whatever they wrote in the meantime can still be recovered:

```console
warning: these still have the old /var/log open and keep writing to it:
  auditd.service               pid 3651
  systemd-journald.service     pid 14502
:: reopen them with: systemctl restart systemd-journald
:: auditd.service refuses a manual restart; only a reboot reopens it
:: a reboot reopens everything at once
:: the previous contents are kept at /var/.log.pre-subvolume
```

A unit that sets `RefuseManualStart` or `RefuseManualStop` is named separately
rather than put in the restart line. `auditd` does, and it holds `/var/log`, so
including it made the whole command fail with exit status 4 and reopened
nothing — only a reboot moves it onto the new subvolume.

Remove `/var/.log.pre-subvolume` once you are satisfied nothing is missing.

It refuses `/`, `/boot`, `/etc`, `/usr` and the pseudo-filesystems, a path that
is already a subvolume, a mount point, a symlink, and anything not on the btrfs
filesystem mounted at `/`. `--name` sets the subvolume's name (the default comes
from the path: `/var/log` becomes `var-log`) and `--no-config` prints the
`[subvolumes.NAME]` table instead of writing it.

## Tested on

Every release is exercised by hand, not only by the test suite: real rollbacks,
real reboots, on hardware and in VMs.

| Version | What was tested |
| --- | --- |
| 0.1.x - 0.2.0 | Fedora 44 VM and a ThinkPad T480 (LUKS, LVM, btrfs), from source and from the RPM: setup, snapshots from dnf and the timer, rollback and roll forward, and hibernating with a rollback waiting for its reboot. Again from the RPM on a VM with SELinux enforcing, with no denials. |
| 0.2.0 | The T480 again (systemd-boot entries, SELinux enforcing), upgraded from 0.1.1, and unified kernel image support on a VM booting one. |
| 0.3.0 | A fresh Fedora 44 Workstation install with the default layout and GRUB, from COPR: setup, dnf and timer snapshots, rollback and roll forward with reboots. Snapshots of other subvolumes on that VM and the T480: `/home` and root rolled back separately, each leaving the other untouched. |
| 0.4.0 | A Fedora 44 to 45 upgrade with `dnf system-upgrade`, rolled back: the offline transaction takes its own snapshot, the restored system came back on the older release with the packages the upgrade added and removed put back, and `prune-kernels` removed the stranded kernel's boot entry. |
| after 0.4.1 | Snapshot boot entries, on two different layouts: a VM whose kernels are unified kernel images and no encryption, and the T480 with LUKS, LVM and Type #1 entries. Booted a generated entry on both, read-only with the desktop up, and rolled the system back from inside one. |
| 0.4.1 | A review of every module. The tests now cover a snapshot taken into an unmounted store, a rollback acting on checks that had stopped being true while it waited to be confirmed, an interrupted rollback that could leave no root subvolume, and three ways the dnf hook could abort or hang a transaction. 162 tests to 212. |

## Development

No packages need to be installed; the tests use the standard library's
`unittest` and don't need root or a btrfs filesystem.

```sh
PYTHONPATH=src python3 -m unittest discover -s tests
PYTHONPATH=src python3 -m btrfs_patrol --help
```

`tests/check-convert.sh` is the exception, and needs both. `convert` creates a
subvolume, reflinks a directory's contents into it and swaps the two - none of
which a unit test can reach - so that part is checked end to end against a real
btrfs root instead of not at all:

```sh
sudo tests/check-convert.sh              # or TARGET=/var/tmp/x, LOG=/tmp/out.log
```

It creates its own directory, checks the contents, permissions, symlinks, hidden
files, SELinux context, that the copy really was reflinked and that a process
holding the directory open is reported, then removes everything it made whether
the checks passed or not. Its exit status is the number of failures.

To try read-only commands without root, point `BTRFS_PATROL_CONFIG` (or
`--config`) at a configuration whose `snapshots_dir` is a scratch directory.

### Layout

```
pyproject.toml                  package metadata
src/btrfs_patrol/
  cli.py                        argument parsing and commands
  tui.py · screen.py            the full-screen interface, and its view model
  diff.py                       what changed between two snapshots, via btrfs send
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
packaging/RELEASE.md            how a release is cut: version, tag, COPR, GitHub
docs/tui-design.md              the planned terminal interface
tests/                          unittest suite (test_man.py keeps the manual in step)
  check-convert.sh              end-to-end check for convert; needs root and a btrfs /
```

## Roadmap

Nothing outstanding. The last item, a terminal interface, is built — see
[docs/tui-design.md](docs/tui-design.md) — and `btrfs-patrol tui` browses every
subvolume's snapshots, takes, keeps, describes and deletes them, compares two,
and steps through a rollback with its checks and plan on screen before
confirming. It uses Python's own curses, so btrfs-patrol still needs nothing
beyond Python, `btrfs-progs` and `util-linux`.

## License

GPL-3.0-or-later. See [LICENSE](LICENSE) and [NOTICE](NOTICE).
