import QtQuick
import QtQuick.Layouts
import QtQuick.Controls as Controls
import Quickshell
import Quickshell.Io
import qs.Ui as Ui
import qs.Commons as Commons

Item {
  id: root
  Commons.Translations { id: strings; directory: Qt.resolvedUrl("i18n") }
  property var shell: null
  property var manifest: null
  property bool opened: false
  property var players: []
  property var draft: ({players: {}, groups: []})
  property string error: ""
  property bool loaded: false
  property int revision: 0
  readonly property bool validGroups: {
    revision
    return draft.groups.every(function(group) {
      return group.name.trim().length > 0 && group.members.length >= 2 && group.members.length <= 32
    })
  }
  readonly property string runtime: (Quickshell.env("XDG_DATA_HOME") || Quickshell.env("HOME") + "/.local/share") + "/omarchy-sendspin"

  function open(payload) {
    opened = true
    loaded = false
    error = ""
    reader.running = true
    Qt.callLater(function() { card.forceActiveFocus() })
  }
  function close() { opened = false }
  function dismiss() {
    if (shell) shell.hide("paulus.sendspin")
    else close()
  }
  function preference(id) { return draft.players[id] || {alias: "", hidden: false} }
  function editPlayer(id, field, value) {
    var entry = preference(id)
    entry[field] = value
    draft.players[id] = entry
  }
  function addGroup() {
    var groups = draft.groups.slice()
    groups.push({id: "", name: "", members: []})
    draft.groups = groups
    draftChanged()
  }
  function editGroup(index, field, value) {
    draft.groups[index][field] = value
    revision++
  }
  function deleteGroup(index) {
    var groups = draft.groups.slice()
    groups.splice(index, 1)
    draft.groups = groups
    draftChanged()
  }

  Process {
    id: reader
    command: [root.runtime + "/.venv/bin/python", root.runtime + "/settings.py", "read"]
    stdout: StdioCollector {
      onStreamFinished: {
        try {
          var data = JSON.parse(text)
          root.players = data.players
          root.draft = data.settings
          root.loaded = true
        } catch (e) { root.error = strings.t("loadError") }
      }
    }
    stderr: StdioCollector { onStreamFinished: if (text.trim()) root.error = text.trim() }
  }
  Process {
    id: writer
    command: [root.runtime + "/.venv/bin/python", root.runtime + "/settings.py", "save"]
    stdinEnabled: true
    onStarted: { write(JSON.stringify(root.draft) + "\n"); stdinEnabled = false }
    stderr: StdioCollector { onStreamFinished: if (text.trim()) root.error = text.trim() }
    onExited: function(code, status) {
      if (code === 0) root.dismiss()
    }
  }

  Ui.OverlayWindow {
    shown: root.opened
    color: "#99000000"
    Rectangle {
      id: card
      anchors.centerIn: parent
      width: Math.min(760, parent.width - 48)
      height: Math.min(800, parent.height - 48)
      color: Commons.Color.background
      border.color: Commons.Color.foreground
      radius: 12
      focus: true
      Keys.onEscapePressed: root.dismiss()
      ColumnLayout {
        anchors.fill: parent
        anchors.margins: 24
        spacing: 16
        Text {
          text: strings.t("title")
          color: Commons.Color.foreground
          font.family: Commons.Style.font.family
          font.pixelSize: 24
        }
        Text {
          Layout.fillWidth: true
          text: strings.t("description")
          wrapMode: Text.WordWrap
          color: Commons.Color.foreground
          font.pixelSize: 14
        }
        Controls.ScrollView {
          Layout.fillWidth: true
          Layout.fillHeight: true
          clip: true
          contentWidth: availableWidth
          ColumnLayout {
            width: parent.width
            spacing: 12
            Text { text: strings.t("players"); font.pixelSize: 18; color: Commons.Color.foreground }
            Text {
              visible: root.loaded && root.players.length === 0
              text: strings.t("noPlayers")
              Layout.fillWidth: true
              wrapMode: Text.WordWrap
              color: Commons.Color.foreground
            }
            Repeater {
              model: root.players
              delegate: RowLayout {
                required property var modelData
                Layout.fillWidth: true
                spacing: 12
                Ui.ToggleSwitch {
                  checked: !root.preference(modelData.id).hidden
                  activeFocusOnTab: true
                  hasCursor: activeFocus
                  Accessible.name: strings.t("showPlayer").arg(modelData.name)
                  Accessible.role: Accessible.CheckBox
                  Accessible.checked: checked
                  onToggled: { checked = !checked; root.editPlayer(modelData.id, "hidden", !checked) }
                  Keys.onSpacePressed: toggled()
                  Keys.onReturnPressed: toggled()
                }
                ColumnLayout {
                  Layout.fillWidth: true
                  spacing: 2
                  Text {
                    text: modelData.name
                    textFormat: Text.PlainText
                    color: Commons.Color.foreground
                    font.pixelSize: 14
                    elide: Text.ElideRight
                    Layout.fillWidth: true
                  }
                  Text {
                    text: modelData.available ? strings.t("available") : strings.t("offline")
                    color: Commons.Color.foreground
                    opacity: 0.6
                    font.pixelSize: 12
                  }
                }
                Ui.TextField {
                  Layout.preferredWidth: 240
                  text: root.preference(modelData.id).alias
                  placeholderText: strings.t("alias")
                  maximumLength: 128
                  onTextEdited: root.editPlayer(modelData.id, "alias", text)
                }
              }
            }
            RowLayout {
              Layout.fillWidth: true
              Layout.topMargin: 16
              Text { text: strings.t("groups"); font.pixelSize: 18; color: Commons.Color.foreground; Layout.fillWidth: true }
              Ui.Button { text: strings.t("addGroup"); bordered: true; focusable: true; enabled: root.loaded; onClicked: root.addGroup() }
            }
            Text {
              text: strings.t("groupDescription")
              color: Commons.Color.foreground
              font.pixelSize: 14
              wrapMode: Text.WordWrap
              Layout.fillWidth: true
            }
            Repeater {
              model: root.draft.groups
              delegate: ColumnLayout {
                id: groupRow
                required property var modelData
                required property int index
                readonly property var group: { root.revision; return root.draft.groups[index] }
                Layout.fillWidth: true
                spacing: 8
                RowLayout {
                  Layout.fillWidth: true
                  Ui.TextField {
                    text: groupRow.group.name
                    placeholderText: strings.t("groupName")
                    maximumLength: 128
                    Layout.fillWidth: true
                    onTextEdited: root.editGroup(groupRow.index, "name", text)
                  }
                  Ui.Button { text: strings.t("remove"); bordered: true; focusable: true; onClicked: root.deleteGroup(groupRow.index) }
                }
                Text {
                  Layout.fillWidth: true
                  text: {
                    root.revision
                    if (!groupRow.group.name.trim()) return strings.t("groupNeedsName")
                    if (groupRow.group.members.length < 2) return strings.t("groupNeedsPlayers")
                    if (groupRow.group.members.length > 32) return strings.t("groupTooManyPlayers")
                    return ""
                  }
                  visible: text !== ""
                  color: Commons.Color.foreground
                  font.pixelSize: 13
                }
                Flow {
                  Layout.fillWidth: true
                  spacing: 8
                  Repeater {
                    model: root.players
                    delegate: Ui.Button {
                      required property var modelData
                      text: root.preference(modelData.id).alias || modelData.name
                      selected: groupRow.group.members.indexOf(modelData.id) >= 0
                      bordered: true
                      focusable: true
                      onClicked: {
                        var members = groupRow.group.members.slice()
                        var position = members.indexOf(modelData.id)
                        if (position >= 0) members.splice(position, 1)
                        else members.push(modelData.id)
                        root.editGroup(groupRow.index, "members", members)
                        selected = position < 0
                      }
                    }
                  }
                }
              }
            }
          }
        }
        Text {
          text: root.error || strings.t("applyNote")
          textFormat: Text.PlainText
          color: Commons.Color.foreground
          opacity: root.error ? 1 : 0.6
          font.pixelSize: 13
          Layout.fillWidth: true
          wrapMode: Text.WordWrap
        }
        RowLayout {
          Layout.alignment: Qt.AlignRight
          spacing: 12
          Ui.Button { text: strings.t("cancel"); focusable: true; onClicked: root.dismiss() }
          Ui.Button {
            text: strings.t("save")
            bordered: true
            focusable: true
            enabled: root.loaded && root.validGroups && !writer.running
            onClicked: { root.error = ""; writer.stdinEnabled = true; writer.running = true }
          }
        }
      }
    }
  }
}
