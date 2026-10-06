// Whatever a command printed: a diff, a check, a rollback plan.
//
// Monospace and selectable, because this is the tool's own output and what
// people do with it is copy a line into a terminal or a bug report.
//
// AN ITEM WRAPPING THE SCROLL VIEW, NOT THE SCROLL VIEW ITSELF. A ScrollView
// takes one content child, exactly as ScrollablePage does; a placeholder put
// beside the text area became part of the scrollable content instead of
// floating over it, and rendered at the top of the pane overlapping the
// controls above. The placeholder has to be a sibling OF the scroll view.

import QtQuick
import QtQuick.Controls as QQC2
import org.kde.kirigami as Kirigami

Item {
    id: view

    property alias text: output.text
    property string placeholder: ""

    implicitHeight: Kirigami.Units.gridUnit * 10

    QQC2.ScrollView {
        anchors.fill: parent
        clip: true

        QQC2.TextArea {
            id: output
            readOnly: true
            selectByMouse: true
            wrapMode: TextEdit.NoWrap
            font.family: "monospace"
            font.pointSize: Kirigami.Theme.smallFont.pointSize
        }
    }

    Kirigami.PlaceholderMessage {
        anchors.centerIn: parent
        width: parent.width - Kirigami.Units.gridUnit * 4
        visible: output.text === "" && view.placeholder !== ""
        text: view.placeholder
    }
}
