// What just happened, on every page.
//
// It used to be on the snapshots page alone, so a settings page that decided
// there was nothing to save said so into a status line that page did not show -
// and pressing Save appeared to do nothing at all, which is indistinguishable
// from saving.

import QtQuick
import QtQuick.Layouts
import QtQuick.Controls as QQC2
import org.kde.kirigami as Kirigami

QQC2.ToolBar {
    contentItem: RowLayout {
        QQC2.Label {
            text: backend.status
            opacity: 0.7
            elide: Text.ElideRight
            Layout.fillWidth: true
        }
        QQC2.BusyIndicator {
            running: backend.busy
            visible: backend.busy
            implicitWidth: Kirigami.Units.iconSizes.small
            implicitHeight: Kirigami.Units.iconSizes.small
        }
    }
}
