<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
# Cutting a release

Three places have to end up agreeing: the git tag, the COPR repository people
install from, and the GitHub release that carries the RPMs for anyone not using
COPR. None of them follows from the others automatically, which is why this file
exists — the sequence had to be reconstructed once already, and by then four
versions had tags but no GitHub release.

Worked example below is 0.6.0. Substitute the version throughout.

## 1. The version lives in five files

`test_man.py` compares the manual page against `__version__` and will fail if
they disagree, which catches one of the five. The other four it cannot see, so
change all of them together:

| File | What to change |
|---|---|
| `src/btrfs_patrol/__init__.py` | `__version__ = "0.6.0"` — the one the tests check against |
| `pyproject.toml` | `version = "0.6.0"` |
| `packaging/btrfs-patrol.spec` | `Version:        0.6.0` |
| `man/btrfs-patrol.8` | the `.TH` line: date and `"btrfs-patrol 0.6.0"` |
| `README.md` | the `> **Status: 0.6.0.**` line |

`Release:` in the spec stays `%autorelease`, and `%changelog` stays
`%autochangelog`. Both are resolved at build time from git, so neither is edited
by hand.

## 2. Check it before tagging

```sh
PYTHONPATH=src python3 -m unittest discover -s tests
sudo tests/check-convert.sh          # needs root and a btrfs /
```

The unit suite runs anywhere. The second one exercises what unit tests cannot —
creating a subvolume, reflinking a directory into it, swapping the two — and
should be run on a real btrfs root. For anything touching `convert`, also run it
against a live `/var/log`, where the holders are real services; see
**Testing in a VM** below.

## 3. Tag, and push the tag

```sh
git tag -a v0.6.0 -m "btrfs-patrol 0.6.0"
git push origin v0.6.0
```

Annotated, and the message is the same form as the release title. The tag has to
be pushed before the COPR build, because the spec's `Source` is the GitHub
archive of that tag.

## 4. Build it in COPR

The COPR package is **upload type**, so nothing rebuilds on a push. Submit the
build by hand, from the tag:

```sh
copr-cli buildscm \
  --clone-url https://github.com/jccl1706/btrfs-patrol \
  --commit v0.6.0 \
  --spec packaging/btrfs-patrol.spec \
  --type git --method rpkg \
  jccl1706/btrfs-patrol
```

COPR clones the tag and builds the SRPM itself, with `rpkg` resolving
`%autorelease` and `%autochangelog` from git history. **Nothing needs to be
installed locally** — which matters, because building the SRPM here instead
wants `rpm-build`, `rpmdevtools` and `rpmautospec`, and `dnf` pulls in about 60
packages for them. The older releases were built that way; the result is
identical either way.

Watch it, and confirm every chroot:

```sh
copr-cli status <build-id>
copr-cli monitor jccl1706/btrfs-patrol
```

Eight chroots are enabled: Fedora 43, 44, 45 and rawhide, on x86_64 and aarch64.
All eight should say `succeeded` before going further.

## 5. The GitHub release

The RPMs come from the COPR build that actually shipped, rather than being
rebuilt, so what people download is what the repository served.

**The results directories no longer hold the RPMs** — COPR has moved results to
Pulp, so browsing
`download.copr.fedorainfracloud.org/results/.../<build>-btrfs-patrol/` finds only
logs. Use the downloader:

```sh
copr-cli download-build <build-id> --dest /tmp/rel --chroot fedora-44-x86_64
# the RPMs land in /tmp/rel/fedora-44-x86_64/, not directly in /tmp/rel
```

The package is `BuildArch: noarch`, so one Fedora release's build covers every
architecture; fc44 is what previous releases attached. Stage the two RPMs, add
the checksums in the same format the earlier releases use, and publish:

```sh
cd /tmp/rel/fedora-44-x86_64
sha256sum btrfs-patrol-0.6.0-1.fc44.noarch.rpm \
          btrfs-patrol-0.6.0-1.fc44.src.rpm > SHA256SUMS

gh release create v0.6.0 \
   btrfs-patrol-0.6.0-1.fc44.noarch.rpm \
   btrfs-patrol-0.6.0-1.fc44.src.rpm \
   SHA256SUMS \
   --title "btrfs-patrol 0.6.0" --notes-file notes.md --latest
```

Title is `btrfs-patrol <version>`, with no `v`. The notes open with one sentence
saying what the release is for, then a `## New` section — see the existing
releases for the shape.

## 6. Check what was published

Download it back rather than trusting the upload:

```sh
gh release download v0.6.0 -D /tmp/check && cd /tmp/check
sha256sum -c SHA256SUMS
rpm -qp --qf '%{name} %{version}-%{release} %{arch}\n' btrfs-patrol-*.noarch.rpm
rpm -qlp btrfs-patrol-*.noarch.rpm | grep -E 'bin/|man8'
```

Then, on a machine with the repository enabled, `sudo dnf upgrade btrfs-patrol`
should offer the new version.

## Testing in a VM

There is no local VM any more. A throwaway Fedora VM on the Proxmox cluster is
both faster and closer to a real system, and it can be driven entirely over ssh
with no console.

A Fedora Cloud Base qcow2 is cached on the `woody` node under
`/var/lib/vz/template/iso/`. It is a good test bed for this project: it boots
with a btrfs root on a `root` subvolume, plus separate `home`, `boot` and `var`
subvolumes, and cloud-init takes an ssh key. Create the VM with a disk on the
`fast` pool — the `local` storage has no `images` content type and cannot hold
one.

Converting that VM's live `/var/log` is the test worth running before any
release that touches `convert`: it is the only place the holders are real
services, and it is what found that `auditd` refuses a manual restart.
