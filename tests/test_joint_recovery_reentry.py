"""Current account input, Wiki effects and memory survive repeated recovery exits."""

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

from tests.test_service_runtime import installation_settings


def test_owned_storage_recovery_can_be_interrupted_again_without_replaying_effects(tmp_path):
    config, configuration = installation_settings(tmp_path / "install")
    root = configuration.parent
    environment = dict(os.environ, CODEX_HOME=str(root / "provider-codex"), CLAUDE_CONFIG_DIR=str(root / "provider-claude"))
    program = Path(__file__).with_name("joint_recovery_process.py")
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
    first_recovery = json.loads((root / "input-recovery.json").read_text())
    assert first_recovery == {"generations": ["started-generation", "unknown-generation"],
                              "states": {"staged": "withdrawn", "started": "unknown", "unknown": "unknown",
                                         "exact": "done", "successor": "withdrawn"}}
    with sqlite3.connect(config.db_path) as reader:
        assert reader.execute("PRAGMA user_version").fetchone() == (7,)
    reader.close()
    run("wiki", 72)
    assert note.read_bytes() == applied
    run("input", 71)
    assert json.loads((root / "input-recovery.json").read_text()) == first_recovery
    # A later user version is outside both databases. Another recovery exit
    # must preserve it, together with the original unknown execution evidence.
    later = applied + b"\nLater human condition during recovery.\n"
    note.write_bytes(later)
    run("wiki", 72)
    assert not (Path(config.users_root) / ".first-account-upgrade.json").exists()
    assert note.read_bytes() == later
    run("finish")
    run("reopen")
    assert note.read_bytes() == later
    assert json.loads((root / "finish.json").read_text()) == json.loads((root / "reopen.json").read_text())
