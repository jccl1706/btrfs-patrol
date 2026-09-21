#!/bin/bash
# SPDX-License-Identifier: GPL-3.0-or-later
#
# End-to-end check for `btrfs-patrol convert`, on a real btrfs root.
#
# NOT PART OF THE UNITTEST SUITE, and deliberately so: everything in tests/*.py
# runs as any user on any filesystem, while convert's execute() needs root, a
# btrfs /, and the top-level subvolume mounted. That is exactly the part unit
# tests cannot reach - creating the subvolume, reflinking the contents, swapping
# the directory for it - so it gets checked here instead of not at all.
#
# It touches ONE directory, creates it itself, and removes it again whether the
# checks pass or fail. Nothing in /etc is written: the command runs against a
# scratch configuration in a temporary file.
#
#   sudo tests/check-convert.sh                 # default target under /srv
#   sudo TARGET=/var/tmp/x tests/check-convert.sh
#   sudo LOG=/tmp/out.log tests/check-convert.sh
#
# Exit status is the number of failed checks, so it can be used in a pipeline.
set -uo pipefail

REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
TARGET=${TARGET:-/srv/btrfs-patrol-convert-test}
NAME=$(basename "$TARGET")
KEPT="$(dirname "$TARGET")/.$NAME.pre-subvolume"
# Asked of the program rather than reimplemented here: a second copy of
# default_name() in bash would drift from the Python the first time either changed.
SUBVOL=$(PYTHONPATH=$REPO/src python3 -c \
    'import sys; from pathlib import Path; from btrfs_patrol.convert import default_name; print(default_name(Path(sys.argv[1])))' \
    "$TARGET")
CONFIG=$(mktemp /tmp/patrol-convert-config-XXXXXX.toml)
OUTPUT=$(mktemp /tmp/patrol-convert-output-XXXXXX.txt)
HOLDER_PID=""
fails=0

# Everything printed also goes to a log, so a run can be read back afterwards
# instead of having to be copied off the terminal.
LOG=${LOG:-$(mktemp /tmp/btrfs-patrol-convert-check-XXXXXX.log)}
exec > >(tee "$LOG") 2>&1

say() { printf '\n\033[1m== %s\033[0m\n' "$*"; }
ok()  { printf '   \033[32mok\033[0m   %s\n' "$*"; }
bad() { printf '   \033[31mFAIL\033[0m %s\n' "$*"; fails=$((fails+1)); }

cleanup() {
    say "cleaning up"
    [ -n "$HOLDER_PID" ] && kill "$HOLDER_PID" 2>/dev/null
    [ -n "${HOLDER_UNIT:-}" ] && systemctl stop "$HOLDER_UNIT.service" 2>/dev/null
    if btrfs subvolume show "$TARGET" >/dev/null 2>&1; then
        btrfs subvolume delete "$TARGET" >/dev/null 2>&1 && echo "   removed subvolume $TARGET"
    fi
    rm -rf "$TARGET" "$KEPT"
    rm -f "$CONFIG" "$OUTPUT"
    echo "   log kept at $LOG"
}
trap cleanup EXIT

[ "$(id -u)" -eq 0 ] || { echo "run me as root: sudo $0"; exit 1; }
[ "$(findmnt -n -o FSTYPE /)" = btrfs ] || { echo "/ is not btrfs; nothing to check here"; exit 1; }
# Refuse to touch anything that was already there: this script deletes its
# target at the end, and the target is meant to be its own.
[ -e "$TARGET" ] && { echo "$TARGET already exists; remove it or set TARGET="; exit 1; }
[ -e "$KEPT" ] && { echo "$KEPT is in the way; remove it"; exit 1; }

cat > "$CONFIG" <<'TOML'
# scratch configuration for the convert check - the defaults are what we want
[retention]
max_snapshots = 50
TOML

say "setting up a directory to convert"
mkdir -p "$TARGET/sub/deeper"
head -c 3000000 /dev/urandom > "$TARGET/big.bin"
echo "hello" > "$TARGET/sub/note.txt"
echo "deep"  > "$TARGET/sub/deeper/leaf.txt"
ln -s sub/note.txt "$TARGET/link"
touch "$TARGET/.hidden"
chmod 0640 "$TARGET/sub/note.txt"
BEFORE_TREE=$(cd "$TARGET" && find . | sort | sha256sum)
BEFORE_DATA=$(cd "$TARGET" && find . -type f -exec sha256sum {} + | sort | sha256sum)
BEFORE_MODE=$(stat -c '%a' "$TARGET/sub/note.txt")
BEFORE_CTX=$(ls -Zd "$TARGET" | awk '{print $1}')
echo "   $(find "$TARGET" | wc -l) entries, context $BEFORE_CTX"

# Two processes holding a file open, because the report treats them
# differently and only one of the two paths is otherwise ever taken here:
#
#   - a plain background job lands in the session's scope, so it is named by
#     the process ("tail"), with the scope in brackets;
#   - a transient systemd unit is a .service, so it is named by the unit AND
#     gets a "systemctl restart" line offered for it.
#
# The second is what holds /var/log on a real system - systemd-journald - so
# without it the service branch would only ever be exercised in anger.
tail -f "$TARGET/sub/note.txt" >/dev/null 2>&1 &
HOLDER_PID=$!
HOLDER_UNIT=patrol-convert-check-holder
systemd-run --quiet --collect --unit="$HOLDER_UNIT" \
    tail -f "$TARGET/sub/deeper/leaf.txt" 2>/dev/null \
    && echo "   holding it open: pid $HOLDER_PID (session) and $HOLDER_UNIT.service" \
    || { HOLDER_UNIT=""; echo "   holding it open: pid $HOLDER_PID (systemd-run unavailable)"; }
sleep 0.5

say "1. --dry-run changes nothing"
PYTHONPATH=$REPO/src python3 -m btrfs_patrol --config "$CONFIG" convert "$TARGET" --dry-run
if btrfs subvolume show "$TARGET" >/dev/null 2>&1; then bad "dry run created a subvolume"
else ok "still a plain directory"; fi
[ -e "$KEPT" ] && bad "dry run left $KEPT" || ok "nothing moved aside"

say "2. the real conversion"
PYTHONPATH=$REPO/src python3 -m btrfs_patrol --config "$CONFIG" convert "$TARGET" -y 2>&1 \
    | tee "$OUTPUT"
rc=${PIPESTATUS[0]}
[ "$rc" -eq 0 ] && ok "exit status 0" || bad "exit status $rc"

say "3. checking the result"
btrfs subvolume show "$TARGET" >/dev/null 2>&1 \
    && ok "$TARGET is now a subvolume" || bad "$TARGET is not a subvolume"
[ -d "$KEPT" ] && ok "previous contents kept at $KEPT" || bad "$KEPT is missing"

AFTER_TREE=$(cd "$TARGET" && find . | sort | sha256sum)
AFTER_DATA=$(cd "$TARGET" && find . -type f -exec sha256sum {} + | sort | sha256sum)
[ "$BEFORE_TREE" = "$AFTER_TREE" ] && ok "every entry is present" || bad "the file list changed"
[ "$BEFORE_DATA" = "$AFTER_DATA" ] && ok "every file's contents match" || bad "contents differ"
[ -L "$TARGET/link" ] && ok "the symlink is still a symlink" || bad "symlink was dereferenced"
[ -e "$TARGET/.hidden" ] && ok "hidden files came across" || bad "hidden file missing"
AFTER_MODE=$(stat -c '%a' "$TARGET/sub/note.txt")
[ "$BEFORE_MODE" = "$AFTER_MODE" ] && ok "permissions preserved ($AFTER_MODE)" \
    || bad "mode $BEFORE_MODE -> $AFTER_MODE"
AFTER_CTX=$(ls -Zd "$TARGET" | awk '{print $1}')
[ "$BEFORE_CTX" = "$AFTER_CTX" ] && ok "SELinux context preserved ($AFTER_CTX)" \
    || bad "context $BEFORE_CTX -> $AFTER_CTX"

say "4. reflinks - the copy shares extents rather than duplicating them"
if command -v filefrag >/dev/null; then
    a=$(filefrag -v "$TARGET/big.bin" 2>/dev/null | awk '/^ *0:/{print $4}')
    b=$(filefrag -v "$KEPT/big.bin"   2>/dev/null | awk '/^ *0:/{print $4}')
    if [ -n "$a" ] && [ "$a" = "$b" ]; then ok "big.bin shares its first extent ($a)"
    else bad "first extents differ ($a vs $b) - the copy was not reflinked"; fi
else
    echo "   (filefrag not installed - dnf install e2fsprogs - skipping)"
fi

say "5. the configuration gained the table"
grep -q "\[subvolumes.$SUBVOL\]" "$CONFIG" \
    && ok "[subvolumes.$SUBVOL] appended" || bad "no [subvolumes.$SUBVOL] in $CONFIG"
python3 -c "import tomllib,sys; tomllib.load(open(sys.argv[1],'rb'))" "$CONFIG" \
    && ok "the configuration still parses" || bad "the configuration no longer parses"

say "6. the processes holding it open were reported"
if grep -q "pid $HOLDER_PID" "$OUTPUT"; then ok "the session process (pid $HOLDER_PID) was named"
else bad "the session process was not reported"; fi
if grep -q "still open by" "$OUTPUT"; then ok "the plan listed them before converting"
else bad "the plan did not list them"; fi
if [ -n "${HOLDER_UNIT:-}" ]; then
    if grep -q "$HOLDER_UNIT.service" "$OUTPUT"; then ok "the service was named by its unit"
    else bad "the service was not named by its unit"; fi
    # The branch that matters on a real system: journald holds /var/log, and
    # what you want told is which unit to restart.
    if grep -q "systemctl restart .*$HOLDER_UNIT" "$OUTPUT"; then
        ok "a 'systemctl restart' line was offered for it"
    else bad "no 'systemctl restart' line for the service"; fi
else
    echo "   (systemd-run unavailable, service branch skipped)"
fi

say "RESULT"
[ "$fails" -eq 0 ] && printf '   \033[32mall checks passed\033[0m\n' \
                   || printf '   \033[31m%s check(s) failed\033[0m\n' "$fails"
exit "$fails"
