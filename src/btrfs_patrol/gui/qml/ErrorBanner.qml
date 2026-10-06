// The last failure, kept on screen until it is dismissed.
//
// Not the status line: a status line is rewritten by the next refresh, which is
// exactly when someone is looking for the thing that did not happen.

import QtQuick
import org.kde.kirigami as Kirigami

Kirigami.InlineMessage {
    type: Kirigami.MessageType.Error
    text: backend.error
    visible: backend.error !== ""
    showCloseButton: true
    position: Kirigami.InlineMessage.Position.Header
    onVisibleChanged: if (!visible && backend.error !== "") backend.clearError()
}
