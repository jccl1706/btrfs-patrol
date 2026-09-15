# SPDX-License-Identifier: GPL-3.0-or-later
"""The manual page documents everything the program offers, and names its version."""

import argparse
import unittest
from pathlib import Path

from btrfs_patrol import __version__
from btrfs_patrol.cli import build_parser
from btrfs_patrol.config import _SCHEMA

MAN_PAGE = Path(__file__).resolve().parent.parent / "man" / "btrfs-patrol.8"


def roff(text):
    """How text appears in the page source: hyphens are escaped as \\-."""
    return text.replace("-", "\\-")


def documented(page, text):
    return text in page or roff(text) in page


def subcommands(parser):
    [action] = [a for a in parser._actions if isinstance(a, argparse._SubParsersAction)]
    return action.choices


def option_strings(parser):
    return [
        option
        for action in parser._actions
        if action.option_strings and action.help != argparse.SUPPRESS
        for option in action.option_strings
    ]


class ManPageTests(unittest.TestCase):
    def setUp(self):
        self.page = MAN_PAGE.read_text()

    def test_version_matches_the_program(self):
        self.assertIn(f'"btrfs-patrol {__version__}"', self.page)

    def test_every_command_and_option_is_documented(self):
        parser = build_parser()
        for option in option_strings(parser):
            with self.subTest(option=option):
                self.assertTrue(documented(self.page, option), option)
        for name, command in subcommands(parser).items():
            with self.subTest(command=name):
                self.assertTrue(documented(self.page, f".B {name}"), name)
            for option in option_strings(command):
                with self.subTest(command=name, option=option):
                    self.assertTrue(documented(self.page, option), f"{name} {option}")

    def test_every_configuration_option_is_documented(self):
        for section, options in _SCHEMA.items():
            with self.subTest(section=section):
                self.assertIn(f"[{section}]", self.page)
            for option in options:
                with self.subTest(option=f"{section}.{option}"):
                    self.assertTrue(documented(self.page, f".BR {option} "), f"{section}.{option}")


if __name__ == "__main__":
    unittest.main()
