// The snapshots, and everything that can be done to one.
//
// Per-row actions live in a menu rather than on the row: there are six of them,
// one reboots the machine, and a row of six buttons repeated down a list is how
// the destructive one gets clicked by accident.

import QtQuick
import QtQuick.Layouts
import QtQuick.Controls as QQC2
import org.kde.kirigami as Kirigami

Kirigami.ScrollablePage {
    id: page

    title: "Snapshots"

    // The colours the terminal interface prints each kind in, so the two read
    // the same. Anything unknown stays ordinary text rather than being given a
    // colour that means nothing.
    function colourForKind(kind) {
        switch (kind) {
        case "manual":   return Kirigami.Theme.positiveTextColor
        case "dnf-pre":
        case "dnf-post": return Kirigami.Theme.linkColor
        case "rollback": return Kirigami.Theme.negativeTextColor
        case "timer":    return Kirigami.Theme.neutralTextColor
        default:         return Kirigami.Theme.textColor
        }
    }

    actions: [
        Kirigami.Action {
            text: "Take Snapshot…"
            icon.name: "list-add"
            enabled: !backend.busy
            onTriggered: takeDialog.open()
        },
        Kirigami.Action {
            text: "Prune"
            icon.name: "edit-clear-history"
            enabled: !backend.busy
            tooltip: "Delete each subvolume's oldest snapshots beyond its limit"
            onTriggered: pruneDialog.open()
        },
        Kirigami.Action {
            text: "Refresh"
            icon.name: "view-refresh"
            enabled: !backend.busy
            onTriggered: backend.refresh()
        }
    ]

    // ONE CHILD, AND IT MUST BE THE FLICKABLE. A ScrollablePage puts its single
    // child in a scroll view; given two it renders an empty page and says
    // nothing at all about why.
    ListView {
        id: listView
        model: backend.snapshots
        currentIndex: -1

        header: ErrorBanner { width: listView.width }

        Kirigami.PlaceholderMessage {
            anchors.centerIn: parent
            width: parent.width - Kirigami.Units.gridUnit * 4
            visible: listView.count === 0
            // AN EMPTY LIST HAS TWO MEANINGS and they must not look alike:
            // there are no snapshots, or there are and this user may not read
            // them. The second is the normal state on a real system, where the
            // store is root-only by design.
            icon.name: backend.locked ? "lock" : "drive-harddisk"
            text: backend.locked ? "Snapshots are readable only by root" : "No snapshots"
            explanation: backend.status
            helpfulAction: backend.locked ? unlockAction : null
        }

        Kirigami.Action {
            id: unlockAction
            text: "Show snapshots…"
            icon.name: "unlock"
            enabled: !backend.busy
            onTriggered: backend.unlock()
        }

        delegate: QQC2.ItemDelegate {
            id: row
            width: ListView.view.width
            highlighted: ListView.isCurrentItem
            onClicked: listView.currentIndex = index

            contentItem: RowLayout {
                spacing: Kirigami.Units.largeSpacing

                QQC2.Label {
                    text: model.snapshotId
                    font.family: "monospace"
                    horizontalAlignment: Text.AlignRight
                    Layout.minimumWidth: Kirigami.Units.gridUnit * 2
                }

                ColumnLayout {
                    spacing: 0
                    Layout.fillWidth: true

                    QQC2.Label {
                        text: model.description !== "" ? model.description : "(no description)"
                        opacity: model.description !== "" ? 1 : 0.6
                        elide: Text.ElideRight
                        Layout.fillWidth: true
                    }
                    QQC2.Label {
                        text: model.date + "  " + model.time + "   " + model.kernel
                        font: Kirigami.Theme.smallFont
                        opacity: 0.7
                        elide: Text.ElideRight
                        Layout.fillWidth: true
                    }
                }

                // Which subvolume, since a configuration with [subvolumes.*]
                // tables lists snapshots of several of them together.
                QQC2.Label {
                    text: model.subvolume
                    font: Kirigami.Theme.smallFont
                    opacity: 0.7
                    horizontalAlignment: Text.AlignRight
                    Layout.minimumWidth: Kirigami.Units.gridUnit * 4
                }

                // Kept snapshots are never pruned, which is worth seeing at a
                // glance rather than in a detail view.
                Kirigami.Icon {
                    source: "lock"
                    visible: model.keep
                    implicitWidth: Kirigami.Units.iconSizes.small
                    implicitHeight: Kirigami.Units.iconSizes.small
                    opacity: 0.7
                }

                QQC2.Label {
                    text: model.kind
                    color: page.colourForKind(model.kind)
                    font.family: "monospace"
                    horizontalAlignment: Text.AlignRight
                    Layout.minimumWidth: Kirigami.Units.gridUnit * 5
                }

                QQC2.ToolButton {
                    icon.name: "overflow-menu"
                    enabled: !backend.busy
                    onClicked: {
                        listView.currentIndex = index
                        rowMenu.snapshotId = model.snapshotId
                        rowMenu.description = model.description
                        rowMenu.kept = model.keep
                        rowMenu.popup()
                    }
                }
            }
        }

        QQC2.Menu {
            id: rowMenu
            property int snapshotId: -1
            property string description: ""
            property bool kept: false

            QQC2.MenuItem {
                text: "Restore this snapshot…"
                icon.name: "edit-undo"
                onTriggered: restoreDialog.start(rowMenu.snapshotId)
            }
            QQC2.MenuItem {
                text: "Browse files"
                icon.name: "folder-open"
                onTriggered: backend.browse(rowMenu.snapshotId)
            }
            QQC2.MenuItem {
                text: "Compare with…"
                icon.name: "document-multiple"
                onTriggered: pageStack.layers.push(comparePageComponent,
                                                   { preselected: rowMenu.snapshotId })
            }
            QQC2.MenuSeparator {}
            QQC2.MenuItem {
                text: "Rename…"
                icon.name: "edit-rename"
                onTriggered: renameDialog.start(rowMenu.snapshotId, rowMenu.description)
            }
            QQC2.MenuItem {
                text: rowMenu.kept ? "Allow pruning" : "Keep forever"
                icon.name: rowMenu.kept ? "unlock" : "lock"
                onTriggered: backend.setKeep(rowMenu.snapshotId, !rowMenu.kept)
            }
            QQC2.MenuItem {
                text: "Delete…"
                icon.name: "edit-delete"
                onTriggered: deleteDialog.start(rowMenu.snapshotId, rowMenu.description)
            }
        }
    }

    footer: StatusFooter {}

    // --- dialogs -------------------------------------------------------------

    Kirigami.PromptDialog {
        id: takeDialog
        title: "Take a snapshot"
        standardButtons: Kirigami.Dialog.Ok | Kirigami.Dialog.Cancel

        ColumnLayout {
            QQC2.Label { text: "What is this snapshot for?"; Layout.fillWidth: true }
            QQC2.TextField {
                id: descriptionField
                placeholderText: "before the new kernel"
                Layout.fillWidth: true
                onAccepted: takeDialog.accept()
            }
            QQC2.Label {
                text: "Of which subvolume?"
                Layout.fillWidth: true
                Layout.topMargin: Kirigami.Units.smallSpacing
            }
            QQC2.ComboBox {
                id: subvolumeBox
                model: ["All subvolumes"].concat(backend.subvolumes)
                currentIndex: 0
                Layout.fillWidth: true
            }
        }
        onAccepted: {
            backend.takeSnapshot(descriptionField.text,
                                 subvolumeBox.currentIndex === 0 ? "" : subvolumeBox.currentText)
            descriptionField.text = ""
        }
        onRejected: descriptionField.text = ""
    }

    Kirigami.PromptDialog {
        id: renameDialog
        property int snapshotId: -1
        title: "Rename snapshot"
        standardButtons: Kirigami.Dialog.Ok | Kirigami.Dialog.Cancel

        function start(id, text) {
            snapshotId = id
            renameField.text = text
            open()
        }

        QQC2.TextField {
            id: renameField
            onAccepted: renameDialog.accept()
        }
        onAccepted: backend.setDescription(renameDialog.snapshotId, renameField.text)
    }

    Kirigami.PromptDialog {
        id: deleteDialog
        property int snapshotId: -1
        title: "Delete snapshot"
        subtitle: "Snapshot " + snapshotId + " will be removed. This cannot be undone."
        standardButtons: Kirigami.Dialog.Cancel
        customFooterActions: [
            Kirigami.Action {
                text: "Delete"
                icon.name: "edit-delete"
                onTriggered: {
                    backend.deleteSnapshot(deleteDialog.snapshotId)
                    deleteDialog.close()
                }
            }
        ]
        function start(id, description) {
            snapshotId = id
            open()
        }
    }

    Kirigami.PromptDialog {
        id: pruneDialog
        title: "Prune snapshots"
        subtitle: "Each subvolume's oldest snapshots beyond its limit will be deleted. " +
                  "Snapshots marked to keep are never pruned."
        standardButtons: Kirigami.Dialog.Cancel
        customFooterActions: [
            Kirigami.Action {
                text: "Prune"
                icon.name: "edit-clear-history"
                onTriggered: { backend.prune(); pruneDialog.close() }
            }
        ]
    }

    // RESTORE ASKS TWICE, AND SHOWS THE PLAN IN BETWEEN. The first step runs
    // --dry-run and prints what would happen; only then is there a button that
    // does it. This is the one operation that decides what the machine boots
    // into next, so it is worth two deliberate clicks and a read.
    Kirigami.Dialog {
        id: restoreDialog
        property int snapshotId: -1
        title: "Restore snapshot " + snapshotId
        preferredWidth: Kirigami.Units.gridUnit * 36
        preferredHeight: Kirigami.Units.gridUnit * 24
        standardButtons: Kirigami.Dialog.Cancel
        customFooterActions: [
            Kirigami.Action {
                text: "Restore and reboot later"
                icon.name: "edit-undo"
                enabled: !backend.busy
                onTriggered: {
                    backend.rollback(restoreDialog.snapshotId, false)
                    restoreDialog.close()
                }
            }
        ]

        function start(id) {
            snapshotId = id
            open()
            backend.rollback(id, true)   // the plan, not the deed
        }

        ColumnLayout {
            spacing: Kirigami.Units.largeSpacing

            Kirigami.InlineMessage {
                Layout.fillWidth: true
                type: Kirigami.MessageType.Warning
                visible: true
                text: "This changes what the system boots into. Nothing moves until you " +
                      "reboot, and the snapshot you are on now is kept."
            }
            OutputView {
                Layout.fillWidth: true
                Layout.fillHeight: true
                Layout.minimumHeight: Kirigami.Units.gridUnit * 12
                text: backend.output
                placeholder: backend.busy ? "Working out what this would do…"
                                          : "No plan to show"
            }
        }
    }

    Component {
        id: comparePageComponent
        ComparePage {}
    }
}
