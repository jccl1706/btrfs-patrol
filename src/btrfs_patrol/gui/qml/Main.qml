// The window: a page stack with a drawer to choose the page.
//
// NO i18n() HERE OR IN ANY PAGE. It is not a QML built-in: KLocalizedContext
// puts it on the engine, and that is a KDE Frameworks thing a PySide6
// application does not get for free. Calling it anyway is not an error you can
// see - the call is undefined, the binding yields nothing, and every translated
// string renders EMPTY. That was an empty page header, unlabelled actions and a
// placeholder with no title. If this is ever translated, install the context
// from app.py first.

import QtQuick
import QtQuick.Controls as QQC2
import org.kde.kirigami as Kirigami

Kirigami.ApplicationWindow {
    id: root

    title: "Btrfs Patrol"
    minimumWidth: Kirigami.Units.gridUnit * 32
    minimumHeight: Kirigami.Units.gridUnit * 22
    width: Kirigami.Units.gridUnit * 52
    height: Kirigami.Units.gridUnit * 34

    // The page header, with each page's actions in it. Without this the window
    // has a toolbar strip and nothing in it, because with a single page Kirigami
    // leaves the global toolbar alone unless asked.
    pageStack.globalToolBar.style: Kirigami.ApplicationHeaderStyle.ToolBar

    function show(component) {
        // The output pane is shared, and a diff left on screen while the
        // settings page opens is just confusing.
        pageStack.clear()
        pageStack.push(component)
    }

    globalDrawer: Kirigami.GlobalDrawer {
        title: "Btrfs Patrol"
        titleIcon: "drive-harddisk"
        isMenu: false
        modal: false
        collapsible: true
        collapsed: true

        actions: [
            Kirigami.Action {
                text: "Snapshots"
                icon.name: "view-list-details"
                onTriggered: root.show(snapshotsComponent)
            },
            Kirigami.Action {
                text: "Compare"
                icon.name: "document-multiple"
                onTriggered: root.show(compareComponent)
            },
            Kirigami.Action {
                text: "Settings"
                icon.name: "configure"
                onTriggered: root.show(settingsComponent)
            },
            Kirigami.Action {
                text: "Status"
                icon.name: "checkmark"
                onTriggered: root.show(statusComponent)
            }
        ]
    }

    Component { id: snapshotsComponent; SnapshotsPage {} }
    Component { id: compareComponent;   ComparePage {} }
    Component { id: settingsComponent;  SettingsPage {} }
    Component { id: statusComponent;    StatusPage {} }

    pageStack.initialPage: snapshotsComponent

    // A rollback has happened and the machine is still running the old state.
    // Saying so once, in a sheet that has to be dismissed, is the honest way to
    // report a change that has not taken effect yet.
    Kirigami.PromptDialog {
        id: rebootDialog
        title: "Restored"
        subtitle: "The system will use the restored snapshot after a reboot. " +
                  "Nothing has changed in the running system."
        standardButtons: Kirigami.Dialog.Ok
    }

    Connections {
        target: backend
        function onRebootNeeded() { rebootDialog.open() }
    }
}
