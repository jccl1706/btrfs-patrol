"""A Plasma front end for btrfs-patrol.

Kirigami for the interface and PySide6 to drive it, which keeps the whole
project in one language and uses the same components Plasma's own applications
do, so it is themed and laid out like them rather than merely running next to
them.

READS GO THROUGH THE PACKAGE, WRITES GO THROUGH pkexec. Listing snapshots needs
no privileges, so the backend imports btrfs_patrol and gets Snapshot objects -
no parsing of the CLI's text, and no second output format to keep in step.
Taking one does need root, so that is handed to `pkexec btrfs-patrol snapshot`
and this process stays unprivileged. A GUI that runs as root to avoid a password
prompt is a GUI that runs every QML file it loads as root.
"""
