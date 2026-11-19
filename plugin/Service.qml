import QtQuick
import Quickshell
import Quickshell.Io

Item {
  property var shell: null

  Process {
    command: ["systemctl", "--user", "start", "omarchy-sendspin.service"]
    running: true
  }

  Component.onDestruction: Quickshell.execDetached(["systemctl", "--user", "stop", "omarchy-sendspin.service"])
}
