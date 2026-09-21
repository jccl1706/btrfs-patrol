<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
# TUI design

A terminal interface for browsing and managing snapshots, on Python's own
`curses` so btrfs-patrol still needs nothing beyond Python, `btrfs-progs` and
`util-linux`.

Status: design agreed, not yet built.

## What the first version does

Browse and manage:

- navigate the snapshots of every configured subvolume, not one at a time;
- see the detail of the highlighted snapshot without leaving the list;
- take, keep, unkeep, describe and delete snapshots.

**Rollback and diff are deliberately not in the first version.** Rollback is the
operation where a bug in a new interface costs the most — a mis-hit key that
replaces the root subvolume is a different class of mistake from one that
deletes a snapshot — and it deserves to land on a UI that has already been used
for a while. Diff is the largest single piece of work here and is worth having
on its own terms rather than rushed alongside everything else. Both remain
available on the command line throughout, which is where they already work.

When they do arrive:

- **Rollback** reuses `rollback.prepare()` exactly as `cmd_rollback` does — the
  checks, the warnings and the plan are already a data structure with a
  `describe()`, so the TUI shows those lines and calls `execute()`. No second
  implementation of the rules.
- **Diff** uses `btrfs send --no-data -p A B | btrfs receive --dump`, which asks
  btrfs itself what changed between two snapshots. It is exact, it reads no file
  contents, and its cost is in metadata rather than tree size. It needs the
  snapshots to be read-only, which they are: `SnapshotStore` creates them with
  `readonly=True`. `diff -rq` was the alternative and was rejected because it
  stats every file in both trees, so it gets slower as the system grows, on
  exactly the subvolume most worth diffing.

## Entry point

```
btrfs-patrol tui
```

A subcommand rather than the behaviour of a bare `btrfs-patrol`, which today
exits with usage and should keep doing so — a program that opens a full-screen
interface when run with no arguments is a surprise in a script.

**It requires root**, like every command that changes anything. The snapshot
store is root-owned, and the first thing anyone opens this to do is act on
something. `btrfs-patrol list` stays the unprivileged way to look.

## Layout

Four regions, top to bottom. Shown at 80 columns, the narrowest supported:

```
btrfs-patrol                                    subvolume: root
───────────────────────────────────────────────────────────────
  ID  DATE        TIME      KIND      DESCRIPTION
   1  2026-09-15  11:10:18  manual    before the new kernel
   2  2026-09-16  11:02:10  dnf-pre   upgrade kernel-core +12
▸  3  2026-09-16  11:10:14  rollback  state before rollback to 2
───────────────────────────────────────────────────────────────
#3 root · keep · 1.2 GiB exclusive · boot entry present
state before rollback to 2
───────────────────────────────────────────────────────────────
↑↓ move  ⇥ subvolume  ⏎ open  k keep  x delete  q quit
```

- **Title**, one line: the program, and which subvolume is being shown.
- **Table**, taking whatever height is left. The same columns `list` prints, so
  the two never disagree about what a snapshot looks like.
- **Detail**, two lines for the highlighted snapshot: the facts that do not fit
  a column, then the description in full — the column truncates it, this does
  not.
- **Key bar**, one line, showing only the keys that apply to the current state.

Stacked rather than two panes because at 80 columns a side-by-side split makes
the table drop columns, and the table is the thing people came to read. The
detail strip is fixed at two lines so the table's height does not jump as the
cursor moves between snapshots with longer or shorter descriptions.

### Narrow and plain terminals

`format_table` already takes a width, but it only ever narrows the description;
below a certain width that is not enough. Dropping columns is new behaviour:
KERNEL goes first, then TIME, because a date and a description identify a
snapshot and a kernel version rarely does. It belongs in the shared layout code
above, so `list` gains it too rather than the two diverging.

`▸`, `·` and `⇥` are decoration. On a terminal that cannot encode them the
fallbacks are `>`, `-` and `tab`; the check is whether the locale's encoding can
represent the string, not a guess from `TERM`.

## Keys

| Key | Does |
|---|---|
| `↑` `↓` `PgUp` `PgDn` `Home` `End` | move the cursor |
| `Tab` / `Shift+Tab` | next / previous subvolume, and "all" |
| `Enter` | full detail for the highlighted snapshot |
| `n` | take a snapshot of the current subvolume, prompting for a description |
| `d` | describe — edit the highlighted snapshot's description |
| `k` | keep / unkeep the highlighted snapshot |
| `x` | delete the highlighted snapshot, after a confirmation |
| `/` | filter with a selector, the same syntax `list` takes |
| `r` | reload from disk |
| `?` | help |
| `q` | quit |

`d` is describe rather than diff, and diff will take `c` (compare) when it
lands. Giving diff the obvious letter now and moving describe later would be
worse than choosing once.

Destructive keys are `x` and nothing adjacent to the navigation keys. Delete
always confirms, in the key bar rather than a popup, and the confirmation names
what it will delete.

## Structure

Two layers, and the split is what makes it testable:

```
src/btrfs_patrol/tui.py       the curses front end: input loop, drawing, colour
src/btrfs_patrol/screen.py    the view model: state, and the text of each region
```

`screen.py` holds the snapshots, the cursor, the subvolume filter and any
pending confirmation, and renders each region to a list of plain strings for a
given width and height. **It imports no curses.** Tests drive it directly:
press a key, assert on the lines that come back. That is how a terminal
interface gets the same kind of test coverage as the rest of this project
instead of being the one part nobody can check.

`tui.py` is deliberately thin: initialise curses, translate key codes into the
view model's vocabulary, paint the strings it returns, restore the terminal.
Bugs concentrate in the layer that has tests.

### Reuse, and one thing to fix while doing it

- `SnapshotStore` for loading, and its lock for every mutation, exactly as the
  commands use it.
- `select()` for the `/` filter, so the TUI and `list` accept the same
  selectors.
- `display_width`, `pad`, `truncate` from `output.py` — these already handle
  wide characters, which curses does not do for you.
- The actions call the same store methods `cmd_keep`, `cmd_describe`,
  `cmd_delete` and `cmd_snapshot` call. No second set of rules about what may be
  deleted.

`format_table` builds the whole table as one string. Inside it already are the
four things a per-row renderer needs — `headers`, the `rows` tuples, the
computed `widths`, and the `cells()` closure that pads and joins them — but they
are local to the function. Lift those into something both callers use, leaving
`format_table` as the thin "join the rows with newlines" case. Writing a second
layout instead is how the CLI table and the TUI table come to disagree about
what a snapshot looks like the first time either changes.

### Colour

`Style` emits ANSI escapes; curses wants attributes, so the TUI cannot use it
directly. It defines colour pairs with the same *meaning* — dim, accent,
warning, error — and honours the same `--color`/`output.color` setting, with
`never` giving a monochrome screen that still distinguishes the cursor by the
`▸` marker rather than by colour alone.

## State that changes underneath

Snapshots appear without the TUI doing anything: the daily timer, and every dnf
transaction. The store is reloaded after every action, on `r`, and when the
store's directory mtime changes while idle — checked on the input loop's
timeout, not by a watcher, because a half-second of staleness costs nothing here
and a thread does not pay for itself.

A snapshot that vanishes under the cursor is not an error: the list reloads, the
cursor stays at the same position in the new list, and the key bar says what
happened.

## Failure

The terminal is restored before anything is printed, always — a `PatrolError`
shown over a half-torn-down curses screen is unreadable, and one that leaves the
terminal without an echo is worse. `run()` wraps the loop so the exit path is
the same whether it ended in `q`, an exception, or `SIGINT`, and errors then
print through the ordinary `Console` and return the same exit status the CLI
would.

## Testing

- `screen.py` gets ordinary unit tests: construct it over a fixed set of
  snapshots, send keys, assert on the rendered lines. Cursor movement, filtering,
  subvolume switching, confirmation state, narrow widths and the ASCII fallbacks
  are all reachable this way.
- Actions are tested against a real `SnapshotStore` in a temporary directory, as
  the existing snapshot tests already do.
- `tui.py` gets a smoke test only, and stays small enough for that to be
  honest.
