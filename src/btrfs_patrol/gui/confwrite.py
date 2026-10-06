"""Write settings back into the configuration file, as root.

RUN THROUGH pkexec BY THE FRONT END, never in the GUI process - the file is
root's. It is deliberately small and does one thing, because it is the one piece
of the front end that runs with privileges.

IT EDITS LINES RATHER THAN REWRITING THE FILE. Python reads TOML and cannot
write it - tomllib has no dump - and even with a writer, re-emitting the document
would throw away every comment in it. The configuration is meant to be read and
edited by hand, so the comments are the better half of it. setup.py takes the
same approach for the same reason.

EVERY VALUE IS CHECKED AGAINST THE PACKAGE'S OWN SCHEMA before it is written, so
the front end cannot invent an option or change a type. That matters because the
GUI, not the command line tool, is what writes this file: the schema being
shared is what keeps the two from drifting apart.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import tempfile
from pathlib import Path

from btrfs_patrol import config as config_module

_SECTION_RE = re.compile(r"^\s*\[([^\]]+)\]\s*$")


def _parse_value(section: str, option: str, text: str) -> object:
    """The value as the schema says it must be, or an error naming what was wrong."""
    schema = config_module._SCHEMA.get(section)
    if schema is None or option not in schema:
        raise ValueError(f"no such option: {section}.{option}")
    wanted, _default = schema[option]
    if wanted is bool:
        if text not in ("true", "false"):
            raise ValueError(f"{section}.{option} takes true or false, not {text!r}")
        return text == "true"
    if wanted is int:
        try:
            value = int(text)
        except ValueError:
            raise ValueError(f"{section}.{option} takes a number, not {text!r}") from None
        if value < 0:
            raise ValueError(f"{section}.{option} cannot be negative")
        return value
    if option == "color" and text not in config_module.COLOR_CHOICES:
        raise ValueError(f"color takes one of {', '.join(config_module.COLOR_CHOICES)}")
    return text


def _format(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    return f'"{value}"'


def apply(text: str, changes: dict[tuple[str, str], object]) -> str:
    """The file with those options changed, everything else - comments included - intact."""
    remaining = dict(changes)
    lines = text.splitlines(keepends=True)
    out: list[str] = []
    section = ""
    for line in lines:
        match = _SECTION_RE.match(line)
        if match:
            section = match.group(1)
        else:
            key = line.split("=", 1)[0].strip() if "=" in line else ""
            if key and (section, key) in remaining:
                value = remaining.pop((section, key))
                indent = line[: len(line) - len(line.lstrip())]
                newline = "\n" if line.endswith("\n") else ""
                out.append(f"{indent}{key} = {_format(value)}{newline}")
                continue
        out.append(line)

    # An option the file never mentioned - it was living on the default. Add it
    # under its section, creating the section if the file has no such table.
    for (sect, key), value in remaining.items():
        header = f"[{sect}]"
        if any(_SECTION_RE.match(line) and line.strip() == header for line in out):
            for i, line in enumerate(out):
                if _SECTION_RE.match(line) and line.strip() == header:
                    out.insert(i + 1, f"{key} = {_format(value)}\n")
                    break
        else:
            if out and not out[-1].endswith("\n"):
                out.append("\n")
            out.append(f"\n{header}\n{key} = {_format(value)}\n")
    return "".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="btrfs-patrol-gui-write-config",
        description="Change options in btrfs-patrol's configuration file.",
    )
    parser.add_argument("-c", "--config", type=Path, metavar="PATH")
    parser.add_argument(
        "setting", nargs="+", metavar="SECTION.OPTION=VALUE",
        help="for example retention.max_snapshots=50",
    )
    args = parser.parse_args(argv)

    path = config_module.resolve_path(args.config)
    changes: dict[tuple[str, str], object] = {}
    for setting in args.setting:
        name, sep, raw = setting.partition("=")
        section, dot, option = name.partition(".")
        if not sep or not dot:
            print(f"error: not a SECTION.OPTION=VALUE setting: {setting}", file=sys.stderr)
            return 2
        try:
            changes[(section, option)] = _parse_value(section, option, raw)
        except ValueError as e:
            print(f"error: {e}", file=sys.stderr)
            return 2

    try:
        text = path.read_text()
    except OSError as e:
        print(f"error: cannot read {path}: {e}", file=sys.stderr)
        return 1

    updated = apply(text, changes)

    # ATOMIC, AND ONLY AFTER IT PARSES. A configuration file half-written by a
    # settings page is a system that will not take a snapshot, so the new text
    # is checked by the package's own loader before it replaces the old one.
    directory = path.parent
    try:
        with tempfile.NamedTemporaryFile(
            "w", dir=directory, prefix=f".{path.name}.", delete=False
        ) as f:
            temporary = Path(f.name)
            f.write(updated)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(temporary, 0o644)
        try:
            config_module.load(temporary)
        except Exception as e:  # noqa: BLE001 - any rejection means do not install it
            temporary.unlink(missing_ok=True)
            print(f"error: the result would not load: {e}", file=sys.stderr)
            return 1
        os.replace(temporary, path)
    except OSError as e:
        print(f"error: cannot write {path}: {e}", file=sys.stderr)
        return 1

    for (section, option), value in changes.items():
        print(f"{section}.{option} = {_format(value)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
