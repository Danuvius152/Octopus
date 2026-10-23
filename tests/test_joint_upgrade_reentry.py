"""Mixed old inputs, Wiki effects and account ownership survive repeated exits."""

import json
import os
import subprocess
import sys
from pathlib import Path

from tests.test_service_runtime import installation_settings


def test_joint_upgrade_can_be_interrupted_again_without_replaying_effects(tmp_path):
    config, configuration = installation_settings(tmp_path / "install")
    root = configuration.parent
    environment = dict(os.environ, CODEX_HOME=str(root / "provider-codex"), CLAUDE_CONFIG_DIR=str(root / "provider-claude"))
    program = Path(__file__).with_name("joint_upgrade_process.py")
    project = program.parent.parent
    environment["PYTHONPATH"] = str(project)

    def run(phase, expected=0):
        result = subprocess.run([sys.executable, str(program), str(configuration), phase],
                                cwd=project, env=environment, capture_output=True, text=True, timeout=25)
        assert result.returncode == expected, (phase, result.stdout, result.stderr)
        assert not (root / "model-started").exists()

    run("seed")
    seed = json.loads((root / "seed.json").read_text())
    note = root / "vault" / seed["note"]["path"]
    applied = note.read_bytes()
    run("input", 71)
    run("wiki", 72)
    assert note.read_bytes() == applied
    run("account", 73)
    # A later user version is outside both DB transactions. Publishing a
    # prepared account snapshot must never restore the old article bytes.
    later = applied + b"\nLater human condition after account commit.\n"
    note.write_bytes(later)
    journal = Path(config.users_root) / ".first-account-upgrade.json"
    identity = json.loads(journal.read_text())["upgrade_id"]
    run("reentry", 74)
    assert json.loads(journal.read_text())["upgrade_id"] == identity
    assert note.read_bytes() == later
    run("finish")
    run("reopen")
    assert note.read_bytes() == later
    assert json.loads((root / "finish.json").read_text()) == json.loads((root / "reopen.json").read_text())
