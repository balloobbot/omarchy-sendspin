#!/bin/bash
set -euo pipefail
cd -- "$(dirname -- "$0")"
command -v pactl >/dev/null
command -v parec >/dev/null
command -v pw-cli >/dev/null
omarchy plugin validate "$PWD/plugin"
runtime="${XDG_DATA_HOME:-$HOME/.local/share}/omarchy-sendspin"
plugin="${XDG_CONFIG_HOME:-$HOME/.config}/omarchy/plugins/paulus.sendspin"
mkdir -p "$runtime" "$plugin"
install -m 644 bridge.py pyproject.toml uv.lock "$runtime/"
uv sync --frozen --no-dev --project "$runtime"
unit_dir="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
mkdir -p "$unit_dir"
python3 - "$runtime" "$unit_dir/omarchy-sendspin.service" <<'PYUNIT'
import json
from pathlib import Path
import sys
runtime = Path(sys.argv[1])
command = " ".join(json.dumps(str(p).replace("%", "%%")) for p in
                   (runtime / ".venv/bin/python", runtime / "bridge.py"))
Path(sys.argv[2]).write_text("[Unit]\nDescription=Omarchy Sendspin audio outputs\n"
    "\n[Service]\nType=simple\nExecStart=" + command +
    "\nRestart=on-failure\nRestartSec=5\nTimeoutStopSec=30\n")
PYUNIT
systemctl --user daemon-reload
install -m 644 plugin/manifest.json plugin/Service.qml "$plugin/"
omarchy-shell shell rescanPlugins
omarchy plugin enable paulus.sendspin
