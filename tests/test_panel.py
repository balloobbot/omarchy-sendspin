import os
import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.skipif(
    not os.environ.get("SENDSPIN_UI_TEST"), reason="Requires Omarchy and Quickshell"
)
def test_group_controls_update_the_saved_draft(tmp_path):
    source = Path(__file__).resolve().parents[1]
    for name in ("Ui", "Commons"):
        (tmp_path / name).symlink_to(Path("/usr/share/omarchy/shell") / name)
    shutil.copy(source / "plugin/Settings.qml", tmp_path)
    shutil.copytree(source / "plugin/i18n", tmp_path / "i18n")
    shutil.copy(source / "tests/group_controls.qml", tmp_path / "shell.qml")
    result = subprocess.run(
        ["qs", "-p", str(tmp_path), "--no-color"],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    output = result.stdout + result.stderr
    assert result.returncode == 0 and "PASS group controls" in output, output
    assert "FAIL" not in output, output
