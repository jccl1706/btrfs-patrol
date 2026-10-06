// What changed between two snapshots.
//
// Reading both snapshots needs root, so the comparison runs through pkexec like
// everything else that does - a diff of a root filesystem is not readable by the
// person looking at it.

import QtQuick
import QtQuick.Layouts
import QtQuick.Controls as QQC2
import org.kde.kirigami as Kirigami

Kirigami.Page {
    id: page

    // Set when this page is opened from a row's "Compare with…".
    property int preselected: -1

    title: "Compare"
    footer: StatusFooter {}
    padding: Kirigami.Units.largeSpacing

    function indexOfId(id) {
        for (var i = 0; i < backend.snapshots.rowCount(); i++) {
            if (olderBox.valueAt(i) === id)
                return i
        }
        return -1
    }

    ColumnLayout {
        anchors.fill: parent
        spacing: Kirigami.Units.largeSpacing

        ErrorBanner { Layout.fillWidth: true }

        RowLayout {
            Layout.fillWidth: true
            spacing: Kirigami.Units.largeSpacing

            ColumnLayout {
                Layout.fillWidth: true
                QQC2.Label { text: "Older"; font: Kirigami.Theme.smallFont; opacity: 0.7 }
                QQC2.ComboBox {
                    id: olderBox
                    Layout.fillWidth: true
                    model: backend.snapshots
                    textRole: "label"
                    valueRole: "snapshotId"
                }
            }
            ColumnLayout {
                Layout.fillWidth: true
                QQC2.Label { text: "Newer"; font: Kirigami.Theme.smallFont; opacity: 0.7 }
                QQC2.ComboBox {
                    id: newerBox
                    Layout.fillWidth: true
                    model: backend.snapshots
                    textRole: "label"
                    valueRole: "snapshotId"
                }
            }
            QQC2.Button {
                text: "Compare"
                icon.name: "document-multiple"
                Layout.alignment: Qt.AlignBottom
                enabled: !backend.busy && olderBox.currentValue !== newerBox.currentValue
                onClicked: backend.compare(olderBox.currentValue, newerBox.currentValue)
            }
        }

        QQC2.Label {
            Layout.fillWidth: true
            visible: olderBox.currentValue === newerBox.currentValue
            text: "Choose two different snapshots of the same subvolume."
            font: Kirigami.Theme.smallFont
            opacity: 0.7
        }

        OutputView {
            Layout.fillWidth: true
            Layout.fillHeight: true
            text: backend.output
            placeholder: backend.busy ? "Comparing…" : "Pick two snapshots and press Compare"
        }
    }

    Component.onCompleted: {
        // The list is newest first, so the newer snapshot is the one above.
        if (backend.snapshots.rowCount() > 1) {
            newerBox.currentIndex = 0
            olderBox.currentIndex = 1
        }
        if (preselected >= 0) {
            var i = page.indexOfId(preselected)
            if (i >= 0)
                olderBox.currentIndex = i
        }
    }
}
