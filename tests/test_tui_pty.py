# SPDX-License-Identifier: GPL-3.0-or-later
"""A smoke test for the curses half: does it actually run in a terminal?

test_tui.py covers the controller, which is where the decisions are. This covers
the part that has no decisions and cannot be faked - curses starting up, the
keys arriving, the screen being painted, the terminal being handed back - by
giving it a real pseudo-terminal and typing at it.

ARROW KEYS ARE SENT IN APPLICATION MODE ("\\x1bOB", not "\\x1b[B"), and getting
that wrong is a trap worth naming: curses emits the terminfo `smkx` string on
startup, which puts the terminal into application cursor-key mode, so
xterm-256color's Down becomes ESC O B. Sending the normal-mode sequence gets
back three separate key presses - 27, 91, 66 - the cursor does not move, and it
looks exactly like a bug in the program.

Everything here is bounded and the child is always killed, because this runs in
the RPM's %check and a hang there would stop a release.
"""

import os
import re
import select
import signal
import sys
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path

from btrfs_patrol.snapshots import Snapshot, SnapshotStore

try:
    import pty
except ImportError:  # pragma: no cover - not Unix
    pty = None

#: Nothing here should take anywhere near this long.
DEADLINE = 20.0

KERNEL = "7.2.4-200.fc44.x86_64"

#: Application-mode cursor keys - see the module docstring.
DOWN = b"\x1bOB"

CHILD = (
    "import sys, pathlib;"
    "from btrfs_patrol import config as c, tui;"
    "from btrfs_patrol.snapshots import SnapshotStore;"
    "from btrfs_patrol.output import Console;"
    "from btrfs_patrol.cli import App;"
    "cfg = c.load(pathlib.Path(sys.argv[1]));"
    "app = App(config=cfg, store=SnapshotStore(cfg.snapshots_dir), console=Console('never'));"
    "sys.exit(tui.run(app))"
)


def strip_escapes(raw: bytes) -> str:
    text = raw.decode("utf-8", "replace")
    text = re.sub(r"\x1b\[[0-9;?]*[a-zA-Z]", "", text)
    text = re.sub(r"\x1b[()=><][A-Z0-9]?", "", text)
    return text.replace("\x0f", "")


@unittest.skipIf(pty is None, "needs a Unix pseudo-terminal")
class TuiSmokeTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.dir = Path(directory.name)
        self.store = SnapshotStore(self.dir)
        for number in (1, 2, 3):
            path = self.store.path(number)
            path.mkdir(parents=True)
            (path / "snapshot").mkdir()
            self.store.save(
                Snapshot(
                    id=number,
                    created=datetime(2026, 9, 14 + number, 11, 10, number),
                    kernel=KERNEL,
                    kind="manual",
                    description=f"number {number}",
                )
            )
        self.config_path = self.dir / "config.toml"
        self.config_path.write_text(f'[filesystem]\nsnapshots_dir = "{self.dir}"\n')
        self.started = time.monotonic()
        self.start()

    def start(self):
        self.pid, self.fd = pty.fork()
        if self.pid == 0:  # pragma: no cover - the child execs immediately
            os.environ["TERM"] = "xterm-256color"
            os.environ["ESCDELAY"] = "25"
            os.environ["PYTHONPATH"] = os.pathsep.join(sys.path)
            os.execvp(sys.executable, [sys.executable, "-c", CHILD, str(self.config_path)])
        os.set_blocking(self.fd, False)
        self.addCleanup(self.stop)

    def stop(self):
        try:
            os.close(self.fd)
        except OSError:
            pass
        try:
            os.kill(self.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            os.waitpid(self.pid, 0)
        except (ChildProcessError, OSError):
            pass

    def read(self, seconds=1.0):
        self.assertLess(time.monotonic() - self.started, DEADLINE, "the interface took too long")
        collected = b""
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            ready, _, _ = select.select([self.fd], [], [], 0.1)
            if ready:
                try:
                    collected += os.read(self.fd, 65536)
                except OSError:
                    break
        return strip_escapes(collected)

    def send(self, data: bytes, seconds=0.8):
        os.write(self.fd, data)
        return self.read(seconds)

    def wait_for(self, text, seconds=6.0):
        """Read until `text` has been painted, or fail saying what was seen."""
        seen = ""
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            seen += self.read(0.4)
            if text in seen:
                return seen
        self.fail(f"never saw {text!r}; the screen showed:\n{seen[-500:]}")

    # --- the tests --------------------------------------------------------

    def test_it_opens_and_paints_the_snapshots(self):
        seen = self.wait_for("btrfs-patrol")
        self.assertIn("DESCRIPTION", seen)

    def test_arrows_move_and_enter_opens_the_detail_page(self):
        self.wait_for("btrfs-patrol")
        self.send(DOWN)
        self.send(DOWN)
        detail = self.send(b"\r", seconds=1.2)
        self.assertIn("snapshot 3", detail)

    def test_a_key_changes_the_store_underneath(self):
        self.wait_for("btrfs-patrol")
        self.send(DOWN)
        self.send(DOWN)
        self.send(b"k", seconds=1.2)
        self.assertTrue(self.store.load(3).keep)

    def test_q_exits_cleanly(self):
        self.wait_for("btrfs-patrol")
        os.write(self.fd, b"q")
        end = time.monotonic() + 6.0
        status = None
        while time.monotonic() < end:
            done, status = os.waitpid(self.pid, os.WNOHANG)
            if done:
                break
            time.sleep(0.1)
        self.assertIsNotNone(status, "it did not exit")
        self.assertEqual(os.waitstatus_to_exitcode(status), 0)


if __name__ == "__main__":
    unittest.main()
