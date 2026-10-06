// The configuration, as a form.
//
// Only the options that are safe to change from a window. Where the filesystem
// is - the device, the subvolume names, the snapshots directory - is shown and
// not editable: pointing the tool at a different disk from a settings page is a
// way to lose the snapshots it was looking after, and those values belong in the
// file where setup put them.

import QtQuick
import QtQuick.Layouts
import QtQuick.Controls as QQC2
import org.kde.kirigami as Kirigami

Kirigami.ScrollablePage {
    id: page

    title: "Settings"

    property bool loading: false

    function load() {
        loading = true
        maxSnapshots.value = backend.settings.max_snapshots
        bootEntries.value = backend.settings.boot_entries
        dnfPre.checked = backend.settings.dnf_pre_snapshot
        dnfPost.checked = backend.settings.dnf_post_snapshot
        colourBox.currentIndex = colourBox.indexOfValue(backend.settings.color)
        loading = false
    }

    // A PROPERTY, NOT A FUNCTION. A function is called once and never
    // re-evaluated, so a Save button bound to one would never notice a changed
    // value. As a property it depends on the controls, and Qt re-evaluates it
    // whenever any of them moves.
    readonly property bool dirty: !loading
        && backend.settings.max_snapshots !== undefined
        && (maxSnapshots.value !== backend.settings.max_snapshots
            || bootEntries.value !== backend.settings.boot_entries
            || dnfPre.checked !== backend.settings.dnf_pre_snapshot
            || dnfPost.checked !== backend.settings.dnf_post_snapshot
            || colourBox.currentValue !== backend.settings.color)

    actions: [
        Kirigami.Action {
            // Offered only when something would actually be written, so pressing
            // it always does something and a disabled button is the honest way
            // to say "this already matches the file".
            text: page.dirty ? "Save" : "Saved"
            icon.name: "document-save"
            enabled: !backend.busy && page.dirty
            onTriggered: backend.saveSettings({
                "max_snapshots": maxSnapshots.value,
                "boot_entries": bootEntries.value,
                "dnf_pre_snapshot": dnfPre.checked,
                "dnf_post_snapshot": dnfPost.checked,
                "color": colourBox.currentValue
            })
        },
        Kirigami.Action {
            text: "Discard changes"
            icon.name: "edit-undo"
            enabled: !backend.busy && page.dirty
            onTriggered: page.load()
        }
    ]

    footer: StatusFooter {}

    Kirigami.FormLayout {
        id: form

        ErrorBanner { Kirigami.FormData.isSection: true }

        Kirigami.Separator { Kirigami.FormData.label: "Retention"; Kirigami.FormData.isSection: true }

        QQC2.SpinBox {
            id: maxSnapshots
            Kirigami.FormData.label: "Keep at most:"
            from: 1
            to: 1000
            editable: true
        }
        QQC2.Label {
            text: "Snapshots per subvolume. Pruning removes the oldest beyond this;\n" +
                  "snapshots marked to keep do not count."
            font: Kirigami.Theme.smallFont
            opacity: 0.7
        }

        Kirigami.Separator { Kirigami.FormData.label: "Boot menu"; Kirigami.FormData.isSection: true }

        QQC2.SpinBox {
            id: bootEntries
            Kirigami.FormData.label: "Boot entries:"
            from: 0
            to: 20
            editable: true
        }
        QQC2.Label {
            text: "How many snapshots get an entry in the boot menu. Zero writes none,\n" +
                  "which is the default: the boot menu is shared with the rest of the system."
            font: Kirigami.Theme.smallFont
            opacity: 0.7
        }

        Kirigami.Separator { Kirigami.FormData.label: "Software updates"; Kirigami.FormData.isSection: true }

        QQC2.CheckBox {
            id: dnfPre
            Kirigami.FormData.label: "Snapshot before dnf:"
            text: "Take a snapshot before each transaction"
        }
        QQC2.CheckBox {
            id: dnfPost
            Kirigami.FormData.label: "Snapshot after dnf:"
            text: "Take a second one when it finishes"
        }

        Kirigami.Separator { Kirigami.FormData.label: "Output"; Kirigami.FormData.isSection: true }

        QQC2.ComboBox {
            id: colourBox
            Kirigami.FormData.label: "Colour:"
            model: [{ text: "Automatic", value: "auto" },
                    { text: "Always", value: "always" },
                    { text: "Never", value: "never" }]
            textRole: "text"
            valueRole: "value"
        }

        Kirigami.Separator { Kirigami.FormData.label: "Filesystem"; Kirigami.FormData.isSection: true }

        QQC2.Label {
            Kirigami.FormData.label: "Device:"
            text: backend.settings.device !== undefined ? backend.settings.device : ""
            font.family: "monospace"
        }
        QQC2.Label {
            Kirigami.FormData.label: "Root subvolume:"
            text: backend.settings.root_subvolume !== undefined ? backend.settings.root_subvolume : ""
            font.family: "monospace"
        }
        QQC2.Label {
            Kirigami.FormData.label: "Snapshots in:"
            text: backend.settings.snapshots_dir !== undefined ? backend.settings.snapshots_dir : ""
            font.family: "monospace"
        }
        QQC2.Label {
            Kirigami.FormData.label: "Configuration:"
            text: backend.configPath
            font.family: "monospace"
        }
        QQC2.Label {
            text: "These come from the configuration file and are changed by editing it,\n" +
                  "or by running btrfs-patrol setup."
            font: Kirigami.Theme.smallFont
            opacity: 0.7
        }
    }

    Component.onCompleted: page.load()

    Connections {
        target: backend
        function onSettingsChanged() { page.load() }
    }
}
