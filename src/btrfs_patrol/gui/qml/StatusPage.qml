// The tool's own self-check: does the configuration match what is mounted, and
// is anything broken. It reads more when run as root, but it is useful without,
// so it runs unprivileged and says what it could see.

import QtQuick
import QtQuick.Layouts
import QtQuick.Controls as QQC2
import org.kde.kirigami as Kirigami

Kirigami.Page {
    id: page

    title: "Status"
    footer: StatusFooter {}
    padding: Kirigami.Units.largeSpacing

    actions: [
        Kirigami.Action {
            text: "Run check"
            icon.name: "checkmark"
            enabled: !backend.busy
            onTriggered: backend.runCheck()
        }
    ]

    ColumnLayout {
        anchors.fill: parent
        spacing: Kirigami.Units.largeSpacing

        ErrorBanner { Layout.fillWidth: true }

        OutputView {
            Layout.fillWidth: true
            Layout.fillHeight: true
            text: backend.output
            placeholder: backend.busy ? "Checking…" : "Press Run check"
        }
    }

    Component.onCompleted: backend.runCheck()
}
