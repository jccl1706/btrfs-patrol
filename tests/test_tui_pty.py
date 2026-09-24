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

import contextlib
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

#: THE CONSOLE TAKES THE CONFIGURATION'S COLOUR, exactly as cli.main does, and
#: an optional argv[2] stands in for the --color flag - which in the real
#: program overrides the Console and leaves config.color alone. The TUI reads
#: its colours off the console for that reason, so a harness that hardcoded one
#: could not see the flag being honoured or ignored.
CHILD = (
    "import sys, pathlib;"
    "from btrfs_patrol import config as c, tui;"
    "from btrfs_patrol.snapshots import SnapshotStore;"
    "from btrfs_patrol.output import Console;"
    "from btrfs_patrol.cli import App;"
    "cfg = c.load(pathlib.Path(sys.argv[1]));"
    "chosen = sys.argv[2] if len(sys.argv) > 2 else cfg.color;"
    "app = App(config=cfg, store=SnapshotStore(cfg.snapshots_dir), console=Console(chosen));"
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


#: The colours this program CHOOSES: red, green, yellow, blue and cyan in the
#: foreground, blue in the background, and the 256-colour band behind the
#: cursor's row.
#:
#: NOT "any colour escape at all", which is what this looked for first and
#: which fails for a reason worth writing down: curses.wrapper calls
#: start_color() itself, so ncurses sets pair 0 to white-on-black and emits
#: "\x1b[37m\x1b[40m" before this program paints a single character. There is
#: no way to have a curses screen with no colour sequence on the wire, and a
#: test that demanded one was testing ncurses' startup, not `color = never`.
#:
#: Bold, dim and reverse are not colour and are expected either way - with
#: colour off they are what still tells the bands and the header apart.
CHOSEN_COLOR = re.compile(r"\x1b\[[0-9;]*(?:3[1-6]|44|38;5;|48;5;)[0-9;]*m")


@unittest.skipIf(pty is None, "needs a Unix pseudo-terminal")
class ColorTests(unittest.TestCase):
    """Does colour reach the terminal, and does `never` really mean never?

    Not merged into TuiSmokeTests, which strips escapes on the way in: the
    escapes ARE the assertion here, and each case needs its own configuration
    before the child starts.
    """

    def run_tui(self, color: str, flag: str | None = None) -> bytes:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        store = SnapshotStore(root)
        for number in (1, 2):
            path = store.path(number)
            path.mkdir(parents=True)
            (path / "snapshot").mkdir()
            store.save(
                Snapshot(
                    id=number,
                    created=datetime(2026, 9, 14 + number, 11, 10, number),
                    kernel=KERNEL,
                    kind="rollback" if number == 1 else "manual",
                    description=f"number {number}",
                    keep=number == 1,
                )
            )
        config_path = root / "config.toml"
        config_path.write_text(
            f'[filesystem]\nsnapshots_dir = "{root}"\n[output]\ncolor = "{color}"\n'
        )

        pid, fd = pty.fork()
        if pid == 0:  # pragma: no cover - the child execs immediately
            os.environ["TERM"] = "xterm-256color"
            os.environ["ESCDELAY"] = "25"
            os.environ["PYTHONPATH"] = os.pathsep.join(sys.path)
            argv = [sys.executable, "-c", CHILD, str(config_path)]
            if flag is not None:
                argv.append(flag)
            os.execvp(sys.executable, argv)
        os.set_blocking(fd, False)
        collected = b""
        end = time.monotonic() + 3.0
        try:
            while time.monotonic() < end:
                ready, _, _ = select.select([fd], [], [], 0.1)
                if ready:
                    try:
                        collected += os.read(fd, 65536)
                    except OSError:
                        break
        finally:
            with contextlib.suppress(OSError):
                os.close(fd)
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)
            with contextlib.suppress(ChildProcessError, OSError):
                os.waitpid(pid, 0)
        self.assertTrue(collected, "the interface painted nothing")
        return collected

    def test_the_screen_is_painted_in_colour_by_default(self):
        self.assertTrue(
            CHOSEN_COLOR.search(self.run_tui("auto").decode("utf-8", "replace")),
            "no colour reached a terminal that has 256 of them",
        )

    def test_the_color_flag_wins_over_the_configuration(self):
        """--color never with `color = "always"` in the file paints no colour.

        The regression this exists for: the palette first read config.color,
        which the flag does not touch - it overrides the Console - so the flag
        was accepted and then ignored on this screen alone.
        """
        raw = self.run_tui("always", flag="never").decode("utf-8", "replace")
        self.assertIsNone(CHOSEN_COLOR.search(raw), "--color never was ignored")

    def test_never_chooses_no_colour(self):
        raw = self.run_tui("never").decode("utf-8", "replace")
        self.assertIsNone(CHOSEN_COLOR.search(raw), "a colour was chosen with color = never")
        # The screen is still there, and the bands are still bands: reverse
        # video (SGR 7) is how they are drawn when there is no colour to use.
        self.assertIn("rollback", strip_escapes(raw.encode("utf-8", "replace")))
        self.assertRegex(raw, r"\x1b\[[0-9;]*7[0-9;]*m")


if __name__ == "__main__":
    unittest.main()
