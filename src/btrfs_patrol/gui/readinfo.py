"""Read the snapshot store and print it as JSON, for the front end.

WHY THIS EXISTS. The store is deliberately root-only - setup creates it 0700 and
fixes the mode if something loosens it - so the GUI, running as the person using
it, cannot read the list it is meant to show. This is run through pkexec when
that happens, and prints metadata: ids, dates, kinds, descriptions. Never file
contents.

IT TAKES NO ARGUMENTS, DELIBERATELY. Anything run with privileges on behalf of
an unprivileged caller should not be steerable - no --config, no path, nothing
that could aim a root-privileged reader at a file of someone's choosing. It
reads the configuration from its one fixed location and reports what it finds.
"""

from __future__ import annotations

import json
import sys

from btrfs_patrol import config as config_module
from btrfs_patrol.errors import PatrolError
from btrfs_patrol.snapshots import SnapshotStore


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    if argv:
        print("error: this takes no arguments", file=sys.stderr)
        return 2

    try:
        cfg = config_module.load(config_module.DEFAULT_PATH)
        snapshots = SnapshotStore(cfg.snapshots_dir).load_all()
    except PatrolError as e:
        print(json.dumps({"error": str(e)}))
        return 1
    except OSError as e:
        # Including the one this exists for: the store is root-only, so an
        # unprivileged run lands here. A sentence, not a traceback.
        print(json.dumps({"error": f"cannot read the snapshot store: {e}"}))
        return 1

    print(json.dumps({
        "snapshots": [
            {
                "id": s.id,
                "created": s.created.isoformat(),
                "kernel": s.kernel,
                "kind": s.kind,
                "description": s.description,
                "keep": s.keep,
                "subvolume": s.subvolume,
            }
            for s in snapshots
        ],
        "subvolumes": [sub.name for sub in cfg.managed()],
        "settings": {
            "max_snapshots": cfg.max_snapshots,
            "boot_entries": cfg.boot_entries,
            "dnf_pre_snapshot": cfg.dnf_pre_snapshot,
            "dnf_post_snapshot": cfg.dnf_post_snapshot,
            "color": cfg.color,
            "device": cfg.device or "auto",
            "root_subvolume": cfg.root_subvolume,
            "snapshots_subvolume": cfg.snapshots_subvolume,
            "snapshots_dir": str(cfg.snapshots_dir),
        },
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
