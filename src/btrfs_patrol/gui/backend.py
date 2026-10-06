"""The model the interface binds to, and every operation it can run.

READS HAPPEN HERE, WRITES HAPPEN THROUGH pkexec. Listing and inspecting need no
privileges, so they import btrfs_patrol and work with Snapshot objects. Anything
that changes the system is handed to the command line tool as root, which keeps
one implementation of every rule about what may be deleted, pruned or rolled
back - the front end is not the place to decide that a second time.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import (
    Property, QAbstractListModel, QByteArray, QModelIndex, QObject, Qt, Signal, Slot,
)

from btrfs_patrol import config as config_module
from btrfs_patrol.errors import PatrolError
from btrfs_patrol.snapshots import SnapshotStore

# Where the configuration writer lives once installed. The environment variable
# is for running the front end from a source tree, where it is not installed at
# all - pkexec wipes the environment, so the override has to be resolved on this
# side, before the privileged process exists.
HELPER_ENV = "BTRFS_PATROL_GUI_HELPER"
HELPER_PATH = "/usr/bin/btrfs-patrol-gui-write-config"

# The read-only reader, used when the snapshot store cannot be read directly.
READER_ENV = "BTRFS_PATROL_GUI_READER"
READER_PATH = "/usr/bin/btrfs-patrol-gui-read"

# polkit's own exit code for "the person dismissed the password prompt".
PKEXEC_DISMISSED = 126


class SnapshotModel(QAbstractListModel):
    """The snapshot store as a list model.

    One role per column the command line prints, so the delegate shows the same
    fields the table does and the two cannot drift apart.
    """

    _ROLES = {
        Qt.ItemDataRole.UserRole + 1: b"snapshotId",
        Qt.ItemDataRole.UserRole + 2: b"date",
        Qt.ItemDataRole.UserRole + 3: b"time",
        Qt.ItemDataRole.UserRole + 4: b"kernel",
        Qt.ItemDataRole.UserRole + 5: b"kind",
        Qt.ItemDataRole.UserRole + 6: b"description",
        Qt.ItemDataRole.UserRole + 7: b"keep",
        Qt.ItemDataRole.UserRole + 8: b"subvolume",
        # One line naming a snapshot, for the places that offer a choice of one.
        Qt.ItemDataRole.UserRole + 9: b"label",
    }

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._rows: list[dict] = []

    def roleNames(self) -> dict[int, QByteArray]:  # noqa: N802 - Qt's name
        return {role: QByteArray(name) for role, name in self._ROLES.items()}

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self._rows)

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or not 0 <= index.row() < len(self._rows):
            return None
        name = self._ROLES.get(role)
        return None if name is None else self._rows[index.row()][name.decode()]

    def replace(self, entries) -> None:
        """Newest first, which is the order the eye wants and the reverse of the table's.

        Entries are plain dictionaries, because they arrive two ways: read here
        when the store is readable, and parsed from the privileged reader's JSON
        when it is not. One shape means one delegate and no second formatting.
        """
        self.beginResetModel()
        self._rows = []
        for entry in sorted(entries, key=lambda e: e["id"], reverse=True):
            created = entry["created"]
            if isinstance(created, str):
                created = datetime.fromisoformat(created)
            self._rows.append({
                "snapshotId": entry["id"],
                "date": created.strftime("%Y-%m-%d"),
                "time": created.strftime("%H:%M:%S"),
                "kernel": entry["kernel"],
                "kind": entry["kind"],
                "description": entry["description"],
                "keep": entry["keep"],
                "subvolume": entry["subvolume"],
                "label": f"{entry['id']} - {entry['description'] or entry['kind']}"
                         f" ({entry['subvolume']}, {created.strftime('%Y-%m-%d %H:%M')})",
            })
        self.endResetModel()


class Backend(QObject):
    """Everything QML talks to."""

    statusChanged = Signal()
    busyChanged = Signal()
    errorChanged = Signal()
    configPathChanged = Signal()
    lockedChanged = Signal()
    subvolumesChanged = Signal()
    settingsChanged = Signal()
    outputChanged = Signal()
    rebootNeeded = Signal()

    def __init__(self, config_path: Path | None = None, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._config_path = config_path
        self._model = SnapshotModel(self)
        self._status = ""
        self._error = ""
        self._busy = False
        self._subvolumes: list[str] = []
        self._settings: dict[str, object] = {}
        self._output = ""
        self._locked = False
        self.refresh()

    # --- things QML reads ----------------------------------------------------

    @Property(QObject, constant=True)
    def snapshots(self) -> SnapshotModel:
        return self._model

    @Property(str, notify=statusChanged)
    def status(self) -> str:
        return self._status

    @Property(bool, notify=busyChanged)
    def busy(self) -> bool:
        return self._busy

    @Property(str, notify=errorChanged)
    def error(self) -> str:
        """The last failure, which STAYS until it is dismissed.

        It used to share the status line with the snapshot count, and a count is
        rewritten by every refresh - so the sentence explaining why nothing
        happened was wiped by the next thing the window did, usually the refresh
        pressed while looking for the snapshot that was never taken.
        """
        return self._error

    @Property(str, notify=configPathChanged)
    def configPath(self) -> str:  # noqa: N802 - read from QML
        return str(config_module.resolve_path(self._config_path))

    @Property(list, notify=subvolumesChanged)
    def subvolumes(self) -> list[str]:
        """The managed subvolumes: root first, then the [subvolumes.*] tables."""
        return self._subvolumes

    @Property("QVariantMap", notify=settingsChanged)
    def settings(self) -> dict:
        """The configuration as the settings page shows it."""
        return self._settings

    @Property(bool, notify=lockedChanged)
    def locked(self) -> bool:
        """The snapshot store is there but this user may not read it.

        Which is the normal state on a real system: setup makes the store
        root-only on purpose. The window says so and offers to unlock rather
        than showing an empty list that looks like "no snapshots".
        """
        return self._locked

    @Property(str, notify=outputChanged)
    def output(self) -> str:
        """Whatever the last long-running command printed: a diff, a check, a plan."""
        return self._output

    # --- setters -------------------------------------------------------------

    def _set_status(self, text: str) -> None:
        if text != self._status:
            self._status = text
            self.statusChanged.emit()

    def _set_error(self, text: str) -> None:
        if text != self._error:
            self._error = text
            self.errorChanged.emit()

    def _set_busy(self, value: bool) -> None:
        if value != self._busy:
            self._busy = value
            self.busyChanged.emit()

    def _set_output(self, text: str) -> None:
        self._output = text
        self.outputChanged.emit()

    @Slot()
    def clearError(self) -> None:  # noqa: N802 - called from QML
        self._set_error("")

    # --- running the command line tool ---------------------------------------

    def _argv(self, args: list[str], privileged: bool) -> list[str]:
        argv = ["pkexec"] if privileged else []
        argv.append("btrfs-patrol")
        if self._config_path is not None:
            argv += ["-c", str(self._config_path)]
        return argv + args

    def _run(self, args: list[str], privileged: bool = True, timeout: int = 300):
        """Run the tool and hand back what happened, or None if it could not run at all."""
        argv = self._argv(args, privileged)
        self._set_busy(True)
        try:
            done = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        except FileNotFoundError:
            self._set_error(
                "pkexec is not installed, so this cannot ask for permission"
                if privileged else "btrfs-patrol is not installed"
            )
            return None
        except subprocess.TimeoutExpired:
            self._set_error(f"timed out after {timeout}s: {' '.join(args)}")
            return None
        finally:
            self._set_busy(False)

        # To the log as well as the window: when something does not happen, the
        # first question is what was actually run, and a status line cannot
        # answer it after the fact.
        print(f"btrfs-patrol-gui: {' '.join(argv)} -> {done.returncode}", file=sys.stderr)
        if done.stderr.strip():
            print(f"btrfs-patrol-gui: stderr: {done.stderr.strip()}", file=sys.stderr)
        return done

    def _failed(self, done) -> bool:
        """True if it did not work, having put the reason somewhere readable."""
        if done.returncode == 0:
            return False
        text = (done.stderr or done.stdout or "").strip()
        if done.returncode == PKEXEC_DISMISSED:
            self._set_status("cancelled")
            return True
        # NO AGENT MEANS THE WRONG SESSION, NOT A BROKEN INSTALL. pkexec asks
        # polkit for the agent registered to this process's login session;
        # started outside the graphical one - over ssh, say - it finds none,
        # falls back to a text agent and dies on /dev/tty. The raw message sends
        # people looking for a missing package when the agent is running
        # perfectly well a session away.
        if "authentication agent" in text or "/dev/tty" in text:
            self._set_error("no authentication agent answered - start this from the desktop")
            return True
        lines = text.splitlines()
        self._set_error(lines[-1] if lines else f"failed ({done.returncode})")
        return True

    # --- reading -------------------------------------------------------------

    def _set_locked(self, value: bool) -> None:
        if value != self._locked:
            self._locked = value
            self.lockedChanged.emit()

    @Slot()
    def unlock(self) -> None:
        """Read the store through the privileged reader, once, when asked.

        NOT AUTOMATICALLY. Running this on every refresh would put a password
        prompt in front of someone for the act of looking at a list, so it
        happens when they ask for it.
        """
        reader = os.environ.get(READER_ENV, READER_PATH)
        if not Path(reader).exists():
            self._set_error(f"the snapshot reader is missing: {reader}")
            return
        self._set_busy(True)
        try:
            done = subprocess.run(["pkexec", reader], capture_output=True, text=True, timeout=120)
        except (FileNotFoundError, subprocess.TimeoutExpired) as e:
            self._set_error(f"could not read the snapshots: {e}")
            return
        finally:
            self._set_busy(False)
        if self._failed(done):
            return
        try:
            data = json.loads(done.stdout)
        except ValueError:
            self._set_error("the snapshot reader returned something unreadable")
            return
        if "error" in data:
            self._set_error(data["error"])
            return
        self._apply(data["snapshots"], data["subvolumes"], data["settings"])
        self._set_locked(False)
        self._set_error("")

    def _apply(self, entries, names, settings) -> None:
        """Put a reading - from either side of the privilege boundary - on screen."""
        if names != self._subvolumes:
            self._subvolumes = list(names)
            self.subvolumesChanged.emit()
        self._settings = dict(settings)
        self.settingsChanged.emit()
        self._model.replace(entries)
        kept = sum(1 for e in entries if e["keep"])
        self._set_status(f"{len(entries)} snapshot(s), {kept} kept")

    @Slot()
    def refresh(self) -> None:
        """Re-read the store and the configuration.

        A PatrolError here is the ordinary way to learn that this machine has no
        btrfs, or no configuration yet - it belongs on screen rather than in a
        traceback, because it is a sentence meant for a person.
        """
        try:
            cfg = config_module.load(self._config_path)
        except PatrolError as e:
            self._model.replace([])
            self._set_status(str(e))
            return

        settings = {
            "max_snapshots": cfg.max_snapshots,
            "boot_entries": cfg.boot_entries,
            "dnf_pre_snapshot": cfg.dnf_pre_snapshot,
            "dnf_post_snapshot": cfg.dnf_post_snapshot,
            "color": cfg.color,
            # Shown, never edited: changing where the filesystem is from a
            # settings page is how you point the tool at the wrong disk.
            "device": cfg.device or "auto",
            "root_subvolume": cfg.root_subvolume,
            "snapshots_subvolume": cfg.snapshots_subvolume,
            "snapshots_dir": str(cfg.snapshots_dir),
        }

        try:
            snapshots = SnapshotStore(cfg.snapshots_dir).load_all()
        except PermissionError:
            # THE NORMAL STATE ON A REAL SYSTEM, not a failure. setup creates
            # the store readable only by root, so an unprivileged read of it is
            # expected to be refused - say so and offer to unlock, rather than
            # showing an empty list that reads as "no snapshots".
            self._model.replace([])
            self._settings = settings
            self.settingsChanged.emit()
            self._set_locked(True)
            self._set_status(
                f"The store is at {cfg.snapshots_dir}, which belongs to root."
                " Reading it needs permission."
            )
            return
        except PatrolError as e:
            self._model.replace([])
            self._set_status(str(e))
            return
        except Exception as e:  # noqa: BLE001 - last resort, still a sentence
            self._model.replace([])
            self._set_status(f"could not read snapshots: {e}")
            return

        self._set_locked(False)
        self._apply(
            [
                {
                    "id": s.id, "created": s.created, "kernel": s.kernel, "kind": s.kind,
                    "description": s.description, "keep": s.keep, "subvolume": s.subvolume,
                }
                for s in snapshots
            ],
            [sub.name for sub in cfg.managed()],
            settings,
        )

    # --- taking, keeping, naming, deleting -----------------------------------

    @Slot(str, str)
    def takeSnapshot(self, description: str, subvolume: str = "") -> None:  # noqa: N802
        args = ["snapshot"]
        # Empty means every managed subvolume, as the command line does with no -s.
        if subvolume:
            args += ["-s", subvolume]
        if description.strip():
            args += ["-d", description.strip()]
        self._do(args, "snapshot taken")

    @Slot(int, bool)
    def setKeep(self, snapshot_id: int, keep: bool) -> None:  # noqa: N802
        self._do(["keep" if keep else "unkeep", str(snapshot_id)],
                 f"snapshot {snapshot_id} {'kept' if keep else 'prunable again'}")

    @Slot(int, str)
    def setDescription(self, snapshot_id: int, description: str) -> None:  # noqa: N802
        self._do(["describe", str(snapshot_id), description], "description changed")

    @Slot(int)
    def deleteSnapshot(self, snapshot_id: int) -> None:  # noqa: N802
        # -y because the window asked already; a second question in a terminal
        # nobody is looking at would simply hang.
        self._do(["delete", "-y", str(snapshot_id)], f"snapshot {snapshot_id} deleted")

    @Slot()
    def prune(self) -> None:
        self._do(["prune"], "pruned")

    def _do(self, args: list[str], done_message: str) -> None:
        done = self._run(args)
        if done is None or self._failed(done):
            return
        self._set_error("")
        self.refresh()
        self._set_status(done_message)

    # --- looking at one ------------------------------------------------------

    @Slot(int)
    def browse(self, snapshot_id: int) -> None:
        """Open the snapshot in the file manager.

        NO PRIVILEGES AND NO FILE BROWSER OF OUR OWN. A snapshot is a directory;
        the desktop already has something good at showing directories, and a
        read-only subvolume is traversable by anyone. Dolphin opens it in a tab
        and everything in it is the system as it was.
        """
        try:
            cfg = config_module.load(self._config_path)
            path = SnapshotStore(cfg.snapshots_dir).subvolume(snapshot_id)
        except PatrolError as e:
            self._set_error(str(e))
            return
        if not path.exists():
            self._set_error(f"snapshot {snapshot_id} is not on disk at {path}")
            return
        opener = shutil.which("dolphin") or shutil.which("xdg-open")
        if opener is None:
            self._set_error(f"no file manager found; the snapshot is at {path}")
            return
        subprocess.Popen(
            [opener, str(path)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
        )
        self._set_status(f"opened snapshot {snapshot_id} in the file manager")

    @Slot(int, int)
    def compare(self, older: int, newer: int) -> None:
        """What changed between two snapshots, which needs root to read both."""
        self._set_output("")
        done = self._run(["diff", str(older), str(newer)])
        if done is None or self._failed(done):
            return
        self._set_error("")
        text = (done.stdout or "").rstrip()
        self._set_output(text or "nothing changed between these two snapshots")
        self._set_status(f"compared {older} to {newer}")

    @Slot()
    def runCheck(self) -> None:  # noqa: N802
        """The tool's own self-check. It reads more when run as root, but works without."""
        self._set_output("")
        done = self._run(["check"], privileged=False, timeout=120)
        if done is None:
            return
        text = ((done.stdout or "") + (done.stderr or "")).rstrip()
        self._set_output(text or "nothing to report")
        self._set_status("configuration checked" if done.returncode == 0
                         else "the check found problems")

    # --- rolling back --------------------------------------------------------

    @Slot(int, bool)
    def rollback(self, snapshot_id: int, dry_run: bool) -> None:
        """Roll a subvolume back to a snapshot.

        THE PLAN FIRST, ALWAYS. The window runs --dry-run and shows what would
        happen before it offers to do it, because this is the one operation that
        decides what the machine boots into next.
        """
        args = ["rollback", "-y", str(snapshot_id)]
        if dry_run:
            args.insert(1, "-n")
        self._set_output("")
        done = self._run(args, timeout=600)
        if done is None or self._failed(done):
            return
        self._set_error("")
        self._set_output((done.stdout or "").rstrip())
        if dry_run:
            self._set_status(f"this is what rolling back to {snapshot_id} would do")
            return
        self.refresh()
        self._set_status(f"rolled back to snapshot {snapshot_id} - reboot to use it")
        self.rebootNeeded.emit()

    # --- settings ------------------------------------------------------------

    @Slot("QVariantMap")
    def saveSettings(self, values: dict) -> None:  # noqa: N802
        """Write the changed options, through the privileged helper.

        Only the options the settings page owns, and only the ones that actually
        changed - so a page that shows ten values does not rewrite ten lines of
        somebody's hand-edited file to say exactly what it said before.
        """
        mapping = {
            "max_snapshots": ("retention", "max_snapshots"),
            "boot_entries": ("boot", "entries"),
            "dnf_pre_snapshot": ("dnf", "pre_snapshot"),
            "dnf_post_snapshot": ("dnf", "post_snapshot"),
            "color": ("output", "color"),
        }
        changes = []
        for key, (section, option) in mapping.items():
            if key not in values:
                continue
            new = values[key]
            if key in self._settings and self._settings[key] == new:
                continue
            text = ("true" if new else "false") if isinstance(new, bool) else str(new)
            changes.append(f"{section}.{option}={text}")
        if not changes:
            self._set_status("nothing to save")
            return

        helper = os.environ.get(HELPER_ENV, HELPER_PATH)
        if not Path(helper).exists():
            self._set_error(f"the configuration writer is missing: {helper}")
            return
        argv = ["pkexec", helper]
        if self._config_path is not None:
            argv += ["-c", str(self._config_path)]
        argv += changes

        self._set_busy(True)
        try:
            done = subprocess.run(argv, capture_output=True, text=True, timeout=120)
        except (FileNotFoundError, subprocess.TimeoutExpired) as e:
            self._set_error(f"could not write the configuration: {e}")
            return
        finally:
            self._set_busy(False)

        print(f"btrfs-patrol-gui: {' '.join(argv)} -> {done.returncode}", file=sys.stderr)
        if self._failed(done):
            return
        self._set_error("")
        self.refresh()
        self._set_status(f"saved {len(changes)} setting(s)")
