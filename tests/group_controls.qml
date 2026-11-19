import QtQuick
import Quickshell
Scope {
  Settings { id: panel }
  Component.onCompleted: {
    panel.players = [{id: "one", name: "Player one", available: true}, {id: "two", name: "Player two", available: true}]
    panel.addGroup()
  }
  Timer {
    interval: 500; running: true
    onTriggered: {
      var seen = []
      var name = null
      var buttons = []
      function walk(object) {
        if (!object || seen.indexOf(object) >= 0) return
        seen.push(object)
        if (object.placeholderText === "Group name") name = object
        if (object.text === "Player one" || object.text === "Player two") {
          if (typeof object.clicked === "function") buttons.push(object)
        }
        var lists = [object.children, object.resources, object.data]
        if (object.contentItem) walk(object.contentItem)
        for (var i = 0; i < lists.length; i++) {
          if (lists[i]) for (var j = 0; j < lists[i].length; j++) walk(lists[i][j])
        }
      }
      walk(panel)
      if (!name || buttons.length !== 2) { console.error("FAIL controls", name, buttons.length); Qt.quit(); return }
      if (panel.validGroups) { console.error("FAIL blank group valid"); Qt.quit(); return }
      name.text = "Together"
      name.textEdited()
      buttons[0].clicked()
      if (panel.validGroups) { console.error("FAIL one member valid"); Qt.quit(); return }
      buttons[1].clicked()
      if (!panel.validGroups) { console.error("FAIL two members invalid", JSON.stringify(panel.draft)); Qt.quit(); return }
      console.log("PASS group controls update saved draft", JSON.stringify(panel.draft))
      Qt.quit()
    }
  }
}
