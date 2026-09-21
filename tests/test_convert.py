# SPDX-License-Identifier: GPL-3.0-or-later
"""Turning a directory into a subvolume: names, checks, holders and the config table."""

import os
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

from btrfs_patrol import config as config_mod
from btrfs_patrol import convert
from btrfs_patrol.config import SUBVOLUMES_SECTION
from btrfs_patrol.errors import PatrolError


class DefaultNameTests(unittest.TestCase):
    def test_joins_the_path_components(self):
        self.assertEqual(convert.default_name(Path("/var/log")), "var-log")
        self.assertEqual(convert.default_name(Path("/srv")), "srv")
        self.assertEqual(convert.default_name(Path("/var/lib/machines")), "var-lib-machines")

    def test_lowercases_and_replaces_what_a_name_cannot_hold(self):
        self.assertEqual(convert.default_name(Path("/srv/My Data")), "srv-my-data")
        self.assertEqual(convert.default_name(Path("/srv/a.b+c")), "srv-a-b-c")

    def test_never_returns_an_empty_name(self):
        self.assertEqual(convert.default_name(Path("/")), "subvolume")


class CheckNameTests(unittest.TestCase):
    def setUp(self):
        self.config = config_mod.parse({})

    def test_accepts_a_plain_name(self):
        convert.check_name("var-log", self.config)

    def test_rejects_the_root_name(self):
        with self.assertRaisesRegex(PatrolError, "root subvolume"):
            convert.check_name("root", self.config)

    def test_rejects_characters_a_table_key_would_have_to_quote(self):
        for name in ("Var-Log", "var/log", "var log", "-leading", ""):
            with self.subTest(name=name):
                with self.assertRaisesRegex(PatrolError, "not a usable subvolume name"):
                    convert.check_name(name, self.config)

    def test_rejects_a_name_already_in_the_configuration(self):
        config = config_mod.parse({SUBVOLUMES_SECTION: {"home": {"path": "/home"}}})
        with self.assertRaisesRegex(PatrolError, "already in the configuration"):
            convert.check_name("home", config)


class HumanTests(unittest.TestCase):
    def test_bytes_have_no_decimal_place(self):
        self.assertEqual(convert.human(0), "0 B")
        self.assertEqual(convert.human(512), "512 B")

    def test_larger_units_have_one(self):
        self.assertEqual(convert.human(1024), "1.0 KiB")
        self.assertEqual(convert.human(1536), "1.5 KiB")
        self.assertEqual(convert.human(5 * 1024 ** 3), "5.0 GiB")


class UnitOfTests(unittest.TestCase):
    """The systemd unit a pid belongs to, read from its cgroup line."""

    def cgroup(self, text):
        directory = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(directory, ignore_errors=True))
        (directory / "cgroup").write_text(text)
        return directory

    def test_finds_a_service(self):
        proc = self.cgroup("0::/system.slice/systemd-journald.service\n")
        self.assertEqual(convert._unit_of(proc), "systemd-journald.service")

    def test_finds_the_innermost_unit_of_a_user_session(self):
        proc = self.cgroup(
            "0::/user.slice/user-1000.slice/user@1000.service/app.slice/app-kitty.scope\n"
        )
        self.assertEqual(convert._unit_of(proc), "app-kitty.scope")

    def test_returns_none_outside_any_unit(self):
        self.assertIsNone(convert._unit_of(self.cgroup("0::/\n")))

    def test_returns_none_when_there_is_no_cgroup_file(self):
        self.assertIsNone(convert._unit_of(Path("/nonexistent-pid")))


class RestartPlanTests(unittest.TestCase):
    def plan(self, found, refusing=()):
        with mock.patch("btrfs_patrol.convert.refuses_manual_restart",
                        side_effect=lambda unit: unit in refusing):
            return convert.restart_plan(found)

    def test_names_only_services_and_strips_the_suffix(self):
        found = [
            convert.Holder(pid=1, comm="systemd-journal", unit="systemd-journald.service"),
            convert.Holder(pid=2, comm="kitty", unit="app-kitty.scope"),
            convert.Holder(pid=3, comm="rsyslogd", unit="rsyslog.service"),
        ]
        command, reboot_only = self.plan(found)
        self.assertEqual(command, "systemctl restart rsyslog systemd-journald")
        self.assertEqual(reboot_only, [])

    def test_none_when_nothing_is_a_service(self):
        found = [convert.Holder(pid=2, comm="bash", unit=None)]
        self.assertEqual(self.plan(found), (None, []))
        self.assertEqual(self.plan([]), (None, []))

    def test_a_unit_that_refuses_is_kept_out_of_the_command(self):
        # auditd holds /var/log and sets RefuseManualStart=yes. Naming it made
        # the whole restart fail and reopened nothing.
        found = [
            convert.Holder(pid=1, comm="auditd", unit="auditd.service"),
            convert.Holder(pid=2, comm="systemd-journal", unit="systemd-journald.service"),
        ]
        command, reboot_only = self.plan(found, refusing={"auditd.service"})
        self.assertEqual(command, "systemctl restart systemd-journald")
        self.assertEqual(reboot_only, ["auditd.service"])

    def test_no_command_at_all_when_every_service_refuses(self):
        found = [convert.Holder(pid=1, comm="auditd", unit="auditd.service")]
        command, reboot_only = self.plan(found, refusing={"auditd.service"})
        self.assertIsNone(command)
        self.assertEqual(reboot_only, ["auditd.service"])


class RefusesManualRestartTests(unittest.TestCase):
    def answer(self, output):
        with mock.patch("btrfs_patrol.convert.system.run", return_value=output):
            return convert.refuses_manual_restart("x.service")

    def test_yes_for_either_property(self):
        self.assertTrue(self.answer("yes\nno\n"))    # RefuseManualStart
        self.assertTrue(self.answer("no\nyes\n"))    # RefuseManualStop

    def test_no_when_both_are_no(self):
        self.assertFalse(self.answer("no\nno\n"))

    def test_assumes_restartable_when_systemctl_cannot_be_asked(self):
        with mock.patch("btrfs_patrol.convert.system.run", side_effect=PatrolError("no systemd")):
            self.assertFalse(convert.refuses_manual_restart("x.service"))


class HolderDescribeTests(unittest.TestCase):
    def test_a_service_is_named_by_its_unit(self):
        holder = convert.Holder(pid=412, comm="systemd-journal", unit="systemd-journald.service")
        self.assertIn("systemd-journald.service", holder.describe())
        self.assertIn("pid 412", holder.describe())

    def test_a_session_process_is_named_by_the_process(self):
        # "session-c4.scope" names nothing anyone can act on; "tail" does.
        holder = convert.Holder(pid=98302, comm="tail", unit="session-c4.scope")
        text = holder.describe()
        self.assertTrue(text.startswith("tail"), text)
        self.assertIn("session-c4.scope", text)

    def test_no_unit_at_all_still_names_the_process(self):
        holder = convert.Holder(pid=7, comm="dd", unit=None)
        self.assertTrue(holder.describe().startswith("dd"))


class HoldersTests(unittest.TestCase):
    """Reads the real /proc, using this test process as the known holder."""

    def test_finds_a_process_with_a_file_open_under_the_directory(self):
        with tempfile.TemporaryDirectory() as name:
            directory = Path(name)
            with (directory / "held").open("w") as handle:
                handle.write("x")
                handle.flush()
                # holders() skips this very process, so ask about the child-free
                # case by patching getpid to something that is not us.
                with mock.patch("btrfs_patrol.convert.os.getpid", return_value=-1):
                    found = convert.holders(directory)
        pids = [holder.pid for holder in found]
        self.assertIn(os.getpid(), pids)

    def test_finds_nothing_for_an_untouched_directory(self):
        with tempfile.TemporaryDirectory() as name:
            self.assertEqual(convert.holders(Path(name)), [])


class TableTextTests(unittest.TestCase):
    def test_is_a_table_the_loader_accepts(self):
        text = convert.table_text("var-log", Path("/var/log"))
        self.assertIn(f"[{SUBVOLUMES_SECTION}.var-log]", text)
        self.assertIn('path = "/var/log"', text)

    def test_round_trips_through_the_configuration_loader(self):
        import tomllib

        data = tomllib.loads(convert.table_text("var-log", Path("/var/log")))
        self.assertEqual(data[SUBVOLUMES_SECTION]["var-log"]["path"], "/var/log")


class AddToConfigTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.directory, ignore_errors=True))
        self.path = self.directory / "config.toml"

    def test_appends_and_keeps_what_was_there(self):
        original = textwrap.dedent(
            """\
            # a comment worth keeping
            [retention]
            max_snapshots = 50
            """
        )
        self.path.write_text(original)
        convert.add_to_config(self.path, "var-log", Path("/var/log"))
        text = self.path.read_text()
        self.assertIn("# a comment worth keeping", text)
        self.assertIn(f"[{SUBVOLUMES_SECTION}.var-log]", text)

    def test_the_result_still_parses(self):
        import tomllib

        self.path.write_text("[retention]\nmax_snapshots = 50\n")
        convert.add_to_config(self.path, "var-log", Path("/var/log"))
        data = tomllib.loads(self.path.read_text())
        self.assertEqual(data["retention"]["max_snapshots"], 50)
        self.assertEqual(data[SUBVOLUMES_SECTION]["var-log"]["path"], "/var/log")

    def test_separates_the_table_from_a_file_with_no_trailing_newline(self):
        import tomllib

        self.path.write_text("[retention]\nmax_snapshots = 50")
        convert.add_to_config(self.path, "var-log", Path("/var/log"))
        tomllib.loads(self.path.read_text())

    def test_reports_a_missing_file_rather_than_raising_oserror(self):
        with self.assertRaisesRegex(PatrolError, "can't read"):
            convert.add_to_config(self.directory / "absent.toml", "x", Path("/srv"))


class RefusedPathTests(unittest.TestCase):
    """prepare() refuses the paths that cannot work, before touching anything."""

    def refuse(self, path):
        config = config_mod.parse({})
        with self.assertRaises(PatrolError) as caught:
            with convert.prepare(config, Path("/etc/btrfs-patrol/config.toml"),
                                 Path(path), "x", True):
                pass
        return str(caught.exception)

    def test_refuses_the_filesystem_root(self):
        self.assertIn("already a subvolume", self.refuse("/"))

    def test_refuses_directories_the_system_cannot_boot_without(self):
        for path in ("/etc", "/usr", "/boot"):
            with self.subTest(path=path):
                self.assertIn("refusing to convert", self.refuse(path))

    def test_refuses_pseudo_filesystems(self):
        for path in ("/proc", "/sys", "/dev", "/run"):
            with self.subTest(path=path):
                self.assertIn("refusing to convert", self.refuse(path))

    def test_reports_a_directory_that_is_not_there(self):
        self.assertIn("doesn't exist", self.refuse("/nonexistent-directory-for-tests"))


if __name__ == "__main__":
    unittest.main()
