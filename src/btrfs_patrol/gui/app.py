"""Start the Plasma front end."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from PySide6.QtCore import QUrl
from PySide6.QtGui import QGuiApplication, QIcon
from PySide6.QtQml import QQmlApplicationEngine

from btrfs_patrol.gui.backend import Backend

QML_DIR = Path(__file__).parent / "qml"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="btrfs-patrol-gui",
        description="Browse btrfs-patrol's snapshots and take new ones.",
    )
    parser.add_argument(
        "-c", "--config", type=Path, metavar="PATH",
        help="configuration to read, as the command line tool takes",
    )
    args = parser.parse_args(argv)

    app = QGuiApplication(sys.argv[:1])
    app.setApplicationName("btrfs-patrol")
    app.setApplicationDisplayName("Btrfs Patrol")
    app.setDesktopFileName("org.fd44.btrfspatrol")
    # BREEZE BY NAME, BECAUSE QT DOES NOT FIND IT ON ITS OWN HERE. Without KDE's
    # platform integration plugin loaded, Qt reports the icon theme as "hicolor"
    # - not empty - so a `themeName() or "breeze"` fallback never fires and every
    # icon.name in the QML silently resolves to nothing. Measured on the Plasma
    # desktop: themeName() == "hicolor", and hasThemeIcon("drive-harddisk") goes
    # False -> True the moment the name is set.
    if QIcon.themeName() in ("", "hicolor"):
        QIcon.setThemeName("breeze")

    backend = Backend(args.config)

    engine = QQmlApplicationEngine()
    engine.rootContext().setContextProperty("backend", backend)
    engine.load(QUrl.fromLocalFile(str(QML_DIR / "Main.qml")))
    if not engine.rootObjects():
        print("btrfs-patrol-gui: the interface failed to load", file=sys.stderr)
        return 1
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
