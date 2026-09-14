# SPDX-License-Identifier: GPL-3.0-or-later
"""Loading and validating the btrfs-patrol configuration file."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from btrfs_patrol.errors import PatrolError

DEFAULT_PATH = Path("/etc/btrfs-patrol/config.toml")
ENV_VAR = "BTRFS_PATROL_CONFIG"

COLOR_CHOICES = ("auto", "always", "never")

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


def resolve_path(path: Path | None = None) -> Path:
    """The configuration file to use: path if given, else $BTRFS_PATROL_CONFIG, else the default."""
    if path is not None:
        return path
    return Path(os.environ.get(ENV_VAR) or DEFAULT_PATH)


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


def parse(data: dict[str, object]) -> Config:
    unknown = sorted(set(data) - set(_SCHEMA))
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
        values[section] = {}
        for option, (kind, default) in options.items():
            value = table.get(option, default)
            # bool is a subclass of int, so reject it explicitly for integer options.
            if not isinstance(value, kind) or (kind is int and isinstance(value, bool)):
                raise PatrolError(f"{section}.{option} must be a {kind.__name__}")
            values[section][option] = value

    filesystem = values["filesystem"]
    config = Config(
        device=None if filesystem["device"] == "auto" else filesystem["device"],
        root_subvolume=filesystem["root_subvolume"],
        snapshots_subvolume=filesystem["snapshots_subvolume"],
        snapshots_dir=Path(filesystem["snapshots_dir"]),
        max_snapshots=values["retention"]["max_snapshots"],
        dnf_pre_snapshot=values["dnf"]["pre_snapshot"],
        dnf_post_snapshot=values["dnf"]["post_snapshot"],
        color=values["output"]["color"],
    )
    _validate(config)
    return config


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
    if config.max_snapshots < 1:
        raise PatrolError("retention.max_snapshots must be at least 1")
    if config.color not in COLOR_CHOICES:
        raise PatrolError(f"output.color must be one of: {', '.join(COLOR_CHOICES)}")
