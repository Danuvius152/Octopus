"""A handoff observation preserves the authoritative storage failure boundary."""

import subprocess
import sys
from pathlib import Path

import pytest

from tests.test_service_runtime import installation_settings

PROGRAM = r'''
import json, os, sqlite3, sys
from pathlib import Path
from server.config import Settings, settings
base = Path(sys.argv[1]).parent
os.environ["CODEX_HOME"] = str(base / "default-codex")
os.environ["CLAUDE_CONFIG_DIR"] = str(base / "default-claude")
config = Settings(_env_file=None, **json.loads(Path(sys.argv[1]).read_text()))
for name, value in config.model_dump().items():
    setattr(settings, name, value)
from server.wiki.registry import WikiRegistry
from server.wiki import job_notifications
registry = WikiRegistry()
root = base / "vault"
root.mkdir()
registry.configure("a" * 12, root, "curator")
registry.health.open_admission()
job_notifications.registry = registry
real_connect = sqlite3.connect
code = getattr(sqlite3, "SQLITE_" + sys.argv[2].upper())
class Failure(sqlite3.Connection):
    def execute(self, sql, *args, **kwargs):
        if sql.startswith("SELECT 1 FROM wiki_job_deliveries"):
            error = sqlite3.OperationalError("Injected authoritative observation failure")
            error.sqlite_errorcode = code
            raise error
        return super().execute(sql, *args, **kwargs)
def connect(*args, **kwargs):
    kwargs["factory"] = Failure
    return real_connect(*args, **kwargs)
sqlite3.connect = connect
assert job_notifications.drain_blockers()
assert registry.health.state == "ready"
Path(sys.argv[3]).write_text("ordinary busy refusal")
registry.close()
'''


@pytest.mark.parametrize("fault", ["busy", "locked", "ioerr", "corrupt", "notadb"])
def test_notice_drain_refuses_normal_competition_but_exits_on_untrusted_storage(tmp_path, fault):
    _config, config_file = installation_settings(tmp_path / "installation")
    marker = tmp_path / "continued"
    result = subprocess.run(
        [sys.executable, "-c", PROGRAM, str(config_file), fault, str(marker)],
        cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=10,
    )
    fatal = fault not in {"busy", "locked"}
    assert result.returncode == (70 if fatal else 0), result.stderr
    assert marker.exists() is not fatal
