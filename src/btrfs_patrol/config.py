# SPDX-License-Identifier: GPL-3.0-or-later
"""Loading and validating the btrfs-patrol configuration file."""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass
from importlib import resources
from pathlib import Path, PurePosixPath

from btrfs_patrol.errors import PatrolError

DEFAULT_PATH = Path("/etc/btrfs-patrol/config.toml")
ENV_VAR = "BTRFS_PATROL_CONFIG"

COLOR_CHOICES = ("auto", "always", "never")

ROOT = "root"
"""The name the root subvolume goes by in snapshot metadata, listings and selectors."""

# section -> option -> (type, default). The defaults match Fedora's installer layout.
_SCHEMA: dict[str, dict[str, tuple[type, object]]] = {
    "filesystem": {
        "device": (str, "auto"),
        "root_subvolume": (str, "root"),
        "snapshots_subvolume": (str, "snapshots"),
        "snapshots_dir": (str, "/.snapshots"),
    },
    "retention": {
        "max_snapshots": (int, 50),
    },
    "dnf": {
        "pre_snapshot": (bool, True),
        "post_snapshot": (bool, False),
    },
    "output": {
        "color": (str, "auto"),
    },
}

# Other subvolumes to snapshot, one [subvolumes.<name>] table each:
# option -> (type, default). path has no default; max_snapshots defaults to
# retention.max_snapshots.
SUBVOLUMES_SECTION = "subvolumes"
_SUBVOLUME_SCHEMA: dict[str, tuple[type, object]] = {
    "path": (str, None),
    "max_snapshots": (int, None),
    "timer": (bool, True),
    "dnf": (bool, False),
}
_SUBVOLUME_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")


@dataclass(frozen=True)
class ManagedSubvolume:
    """A subvolume btrfs-patrol takes snapshots of."""

    name: str
    path: Path
    """Where it is: its mount point, or its path inside the subvolume that contains it."""
    max_snapshots: int
    timer: bool
    """Whether the daily timer snapshots it."""
    dnf: bool
    """Whether dnf transactions snapshot it."""


@dataclass(frozen=True)
class Config:
    device: str | None
    """Block device of the btrfs filesystem, or None to use the one mounted at /."""
    root_subvolume: str
    snapshots_subvolume: str
    snapshots_dir: Path
    max_snapshots: int
    dnf_pre_snapshot: bool
    dnf_post_snapshot: bool
    color: str
    subvolumes: tuple[ManagedSubvolume, ...] = ()
    """Subvolumes to snapshot besides root, from the [subvolumes.<name>] tables."""

    @property
    def root(self) -> ManagedSubvolume:
        # Always in the timer's and dnf's snapshots; [dnf] decides whether dnf takes any.
        return ManagedSubvolume(ROOT, Path("/"), self.max_snapshots, timer=True, dnf=True)

    def managed(self) -> tuple[ManagedSubvolume, ...]:
        """Every subvolume snapshots are taken of, root first."""
        return (self.root, *self.subvolumes)

    def find(self, name: str) -> ManagedSubvolume | None:
        return next((s for s in self.managed() if s.name == name), None)


def resolve_path(path: Path | None = None) -> Path:
    """The configuration file to use: path if given, else $BTRFS_PATROL_CONFIG, else the default."""
    if path is not None:
        return path
    return Path(os.environ.get(ENV_VAR) or DEFAULT_PATH)


def example_text() -> str:
    """The commented example configuration shipped inside the package."""
    return resources.files("btrfs_patrol").joinpath("config.toml.example").read_text()


def load(path: Path | None = None) -> Config:
    path = resolve_path(path)
    try:
        with path.open("rb") as f:
            data = tomllib.load(f)
    except FileNotFoundError:
        raise PatrolError(f"configuration file not found: {path}") from None
    except tomllib.TOMLDecodeError as e:
        raise PatrolError(f"{path}: {e}") from None
    return parse(data)


def _checked(value: object, kind: type, where: str) -> object:
    # bool is a subclass of int, so reject it explicitly for integer options.
    if not isinstance(value, kind) or (kind is int and isinstance(value, bool)):
        raise PatrolError(f"{where} must be a {kind.__name__}")
    return value


def parse(data: dict[str, object]) -> Config:
    unknown = sorted(set(data) - set(_SCHEMA) - {SUBVOLUMES_SECTION})
    if unknown:
        raise PatrolError(f"unknown configuration section(s): {', '.join(unknown)}")

    values: dict[str, dict[str, object]] = {}
    for section, options in _SCHEMA.items():
        table = data.get(section, {})
        if not isinstance(table, dict):
            raise PatrolError(f"[{section}] must be a table")
        unknown = sorted(set(table) - set(options))
        if unknown:
            raise PatrolError(f"unknown option(s) in [{section}]: {', '.join(unknown)}")
        values[section] = {
            option: _checked(table.get(option, default), kind, f"{section}.{option}")
            for option, (kind, default) in options.items()
        }

    filesystem = values["filesystem"]
    max_snapshots = values["retention"]["max_snapshots"]
    config = Config(
        device=None if filesystem["device"] == "auto" else filesystem["device"],
        root_subvolume=filesystem["root_subvolume"],
        snapshots_subvolume=filesystem["snapshots_subvolume"],
        snapshots_dir=Path(filesystem["snapshots_dir"]),
        max_snapshots=max_snapshots,
        dnf_pre_snapshot=values["dnf"]["pre_snapshot"],
        dnf_post_snapshot=values["dnf"]["post_snapshot"],
        color=values["output"]["color"],
        subvolumes=_parse_subvolumes(data.get(SUBVOLUMES_SECTION, {}), max_snapshots),
    )
    _validate(config)
    return config


def _parse_subvolumes(table: object, default_max_snapshots: int) -> tuple[ManagedSubvolume, ...]:
    if not isinstance(table, dict):
        raise PatrolError(
            f"[{SUBVOLUMES_SECTION}] must hold one [{SUBVOLUMES_SECTION}.<name>] table per subvolume"
        )
    subvolumes = []
    for name, options in table.items():
        where = f"{SUBVOLUMES_SECTION}.{name}"
        if not isinstance(options, dict):
            raise PatrolError(f"[{where}] must be a table")
        unknown = sorted(set(options) - set(_SUBVOLUME_SCHEMA))
        if unknown:
            raise PatrolError(f"unknown option(s) in [{where}]: {', '.join(unknown)}")
        if "path" not in options:
            raise PatrolError(f"[{where}] needs a path")
        defaults = {"max_snapshots": default_max_snapshots}
        values = {
            option: _checked(options.get(option, defaults.get(option, default)), kind, f"{where}.{option}")
            for option, (kind, default) in _SUBVOLUME_SCHEMA.items()
        }
        subvolumes.append(
            ManagedSubvolume(
                name=name,
                path=Path(values["path"]),
                max_snapshots=values["max_snapshots"],
                timer=values["timer"],
                dnf=values["dnf"],
            )
        )
    return tuple(subvolumes)


def _validate(config: Config) -> None:
    for option in ("root_subvolume", "snapshots_subvolume"):
        name = getattr(config, option)
        if not name or name.startswith("/") or ".." in PurePosixPath(name).parts:
            raise PatrolError(
                f"filesystem.{option} must be a path relative to the top-level subvolume, "
                f"got {name!r}"
            )
    root = PurePosixPath(config.root_subvolume)
    if PurePosixPath(config.snapshots_subvolume).is_relative_to(root):
        # A rollback moves the root subvolume away, which would take the snapshots with it.
        raise PatrolError("filesystem.snapshots_subvolume must not be inside the root subvolume")
    if not config.snapshots_dir.is_absolute():
        raise PatrolError("filesystem.snapshots_dir must be an absolute path")
    if any(character.isspace() for character in str(config.snapshots_dir)):
        # /etc/fstab separates its fields with whitespace, and setup writes the
        # mount point there unescaped. A path with a space in it produces a line
        # that means something else entirely, and that setup can never match
        # again, so every run appends another copy.
        raise PatrolError("filesystem.snapshots_dir must not contain whitespace")
    if config.max_snapshots < 1:
        raise PatrolError("retention.max_snapshots must be at least 1")
    if config.color not in COLOR_CHOICES:
        raise PatrolError(f"output.color must be one of: {', '.join(COLOR_CHOICES)}")

    paths: dict[Path, str] = {}
    names: dict[str, str] = {}
    for subvolume in config.subvolumes:
        where = f"{SUBVOLUMES_SECTION}.{subvolume.name}"
        # Case-insensitively, because 'subvolume=' matching is case-insensitive:
        # [subvolumes.ROOT] used to be accepted, and then 'delete subvolume=root'
        # selected its snapshots along with the real root's.
        if subvolume.name.casefold() == ROOT:
            raise PatrolError(f"[{where}]: the name {ROOT!r} is the root subvolume's")
        folded = subvolume.name.casefold()
        if folded in names:
            raise PatrolError(
                f"[{where}]: {names[folded]!r} differs only in case, and a selector "
                "could not tell them apart"
            )
        names[folded] = subvolume.name
        if not _SUBVOLUME_NAME.fullmatch(subvolume.name):
            raise PatrolError(
                f"[{where}]: a subvolume's name is letters, digits, '_', '.' and '-', "
                "starting with a letter or digit"
            )
        path = subvolume.path
        if not path.is_absolute() or ".." in path.parts:
            raise PatrolError(f"{where}.path must be an absolute path without '..', got {str(path)!r}")
        if path == Path("/"):
            raise PatrolError(f"{where}.path is /, the root subvolume, which is always snapshotted")
        if path.is_relative_to(config.snapshots_dir):
            raise PatrolError(f"{where}.path is inside the snapshots directory {config.snapshots_dir}")
        if subvolume.max_snapshots < 1:
            raise PatrolError(f"{where}.max_snapshots must be at least 1")
        if path in paths:
            raise PatrolError(
                f"{where}.path {path} is already [{SUBVOLUMES_SECTION}.{paths[path]}]"
            )
        paths[path] = subvolume.name
