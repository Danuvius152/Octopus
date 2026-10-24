"""Retired installation authentication cannot resume an unfinished operation."""

import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from server.config import settings
from server.crypto import encrypt
from server.database import Database
from tests.test_auth_routes import client as client
from tests.test_service_runtime import installation_settings

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("method,path", [("POST", "/bootstrap"), ("POST", "/bootstrap/resume"),
                                         ("GET", "/bootstrap/status")])
async def test_account_adoption_routes_are_retired_without_mutating_accounts(client, method, path):
    http, users, _ = client
    owner = await users.create_user(username="current-owner", password="current-owned-password", is_admin=True)
    token = await users.issue_token(owner["id"])
    before = await users.db.list_users(), await users.db.load_agents(include_archived=True)
    response = await http.request(method, "/api/auth" + path, headers={"Authorization": f"Bearer {token}"},
                                  json={"username": "replacement", "password": "replacement-password"})
    assert response.status_code in {404, 405}
    schema = (await http.get("/openapi.json")).json()
    assert "/api/auth" + path not in schema["paths"]
    assert (await users.db.list_users(), await users.db.load_agents(include_archived=True)) == before
    assert (await http.get("/api/auth/identity", headers={"Authorization": f"Bearer {token}"})).status_code == 200


@pytest.mark.parametrize("crashed_wal", [False, True])
@pytest.mark.parametrize("foreign_key", [False, True])
def test_unfinished_rotation_is_rejected_before_preparing_storage(tmp_path, crashed_wal, foreign_key):
    database = tmp_path / "retained.sqlite3"
    environment = tmp_path / "startup.env"
    environment.write_text("OCTOPUS_AUTH_TOKEN=retained-test-token\n")
    payload = encrypt('{"version":1}', "foreign-master") if foreign_key else "opaque-invalid-payload"
    with sqlite3.connect(database) as writer:
        writer.execute("CREATE TABLE token_rotation_intent(id INTEGER PRIMARY KEY, encrypted_payload TEXT)")
        if not crashed_wal:
            # No payload interpretation or key lookup is needed to reject it.
            writer.execute("INSERT INTO token_rotation_intent VALUES(1, ?)", (payload,))
    writer.close()
    if crashed_wal:
        with sqlite3.connect(database) as writer:
            writer.execute("PRAGMA journal_mode=WAL")
        writer.close()
        main_bytes = database.read_bytes()
        subprocess.run([sys.executable, "-c", """
import os, sqlite3, sys
writer = sqlite3.connect(sys.argv[1])
writer.execute('PRAGMA wal_autocheckpoint=0')
writer.execute("INSERT INTO token_rotation_intent VALUES(1, ?)", (sys.argv[2],))
writer.commit()
os._exit(0)
""", str(database), payload], check=True, timeout=10)
        assert database.read_bytes() == main_bytes
        assert Path(str(database) + "-wal").stat().st_size > 0

    retained = {p: p.read_bytes() for p in [database, environment, Path(str(database) + "-wal")]
                if p.exists()}
    # A short private socket path also works under long pytest temp roots.
    with tempfile.TemporaryDirectory(prefix="octopus-journal-", dir="/var/tmp") as directory:
        journal = str(Path(directory) / "journal")
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as receiver:
            receiver.bind(journal)
            receiver.settimeout(1)
            result = subprocess.run([sys.executable, "-c", """
import asyncio, sys
from server import service_health
from server.database import Database
service_health._JOURNAL_SOCKET = sys.argv[2]
async def start():
    database = Database(sys.argv[1])
    await database.initialize()
    await database.close()
asyncio.run(start())
""", str(database), journal], cwd=ROOT, env=dict(os.environ, OCTOPUS_ENV_FILE=str(environment)),
                capture_output=True, text=True, timeout=20)
            assert result.returncode == 70, result.stderr
            diagnostic = receiver.recv(8192).decode()
            assert "OCTOPUS_CAUSE=UnfinishedTokenRotation" in diagnostic
            assert "payload" not in diagnostic and "retained-test-token" not in diagnostic
    assert {p: p.read_bytes() for p in retained} == retained
    with sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True) as reader:
        assert reader.execute("SELECT COUNT(*) FROM token_rotation_intent").fetchone()[0] == 1
        assert reader.execute("SELECT COUNT(*) FROM sqlite_schema WHERE name='agents'").fetchone()[0] == 0
    reader.close()


async def test_empty_retained_intent_table_allows_normal_initialization(tmp_path):
    path = tmp_path / "empty-intent.sqlite3"
    with sqlite3.connect(path) as writer:
        # An empty retired table is admissible alongside the complete supported
        # unversioned baseline, and retires in the same format transaction.
        writer.executescript((ROOT / "tests/fixtures/main-baseline-v0.sql").read_text())
        writer.execute("CREATE TABLE token_rotation_intent(id INTEGER PRIMARY KEY, encrypted_payload TEXT)")
    writer.close()
    database = Database(str(path))
    await database.initialize()
    try:
        async with database.transaction(write=False) as view:
            assert await view.fetch_one("SELECT name FROM sqlite_schema WHERE name='token_rotation_intent'") is None
            assert await view.fetch_one("PRAGMA user_version") == (3,)
        assert await database.load_sessions() == []
    finally:
        await database.close()


async def test_fresh_database_does_not_prepare_retired_intent_storage(tmp_path):
    database = Database(str(tmp_path / "fresh.sqlite3"))
    await database.initialize()
    try:
        async with database.transaction(write=False) as view:
            assert await view.fetch_one(
                "SELECT 1 FROM sqlite_schema WHERE name='token_rotation_intent'",
            ) is None
    finally:
        await database.close()


async def test_account_cannot_call_retired_installation_rotation(client, tmp_path, monkeypatch):
    http, users, _ = client
    user = await users.create_user(username="owner", password="owner-password")
    token = await users.issue_token(user["id"])
    environment = tmp_path / "selected.env"
    environment.write_text(f"OCTOPUS_AUTH_TOKEN={settings.auth_token}\n")
    monkeypatch.setenv("OCTOPUS_ENV_FILE", str(environment))
    before = environment.read_bytes(), settings.auth_token
    response = await http.post("/api/auth/rotate", headers={"Authorization": f"Bearer {token}"},
                               json={"new_token": "retired-replacement-token"})
    # A built SPA's catch-all rejects unknown POSTs with 405; without it, 404.
    assert response.status_code in {404, 405}, response.text
    schema = await http.get("/openapi.json")
    assert schema.status_code == 200 and "/api/auth/rotate" not in schema.json()["paths"]
    assert (environment.read_bytes(), settings.auth_token) == before
    identity = await http.get("/api/auth/identity", headers={"Authorization": f"Bearer {token}"})
    assert identity.status_code == 200 and identity.json()["label"] == "owner"


def test_service_rejects_retained_intent_before_creating_a_missing_master_key(tmp_path):
    config, config_file = installation_settings(tmp_path)
    database = Path(config.db_path)
    with sqlite3.connect(database) as writer:
        writer.execute("CREATE TABLE token_rotation_intent(id INTEGER PRIMARY KEY, encrypted_payload TEXT)")
        writer.execute("INSERT INTO token_rotation_intent VALUES(1, 'retained-evidence')")
    writer.close()
    before = database.read_bytes()
    admitted = tmp_path / "admitted"
    result = subprocess.run([sys.executable, "-c", """
import asyncio, json, sys
from pathlib import Path
from server.config import settings
for name, value in json.loads(Path(sys.argv[1]).read_text()).items():
    setattr(settings, name, value)
settings.master_key = ''
from server.main import app
async def start():
    async with app.router.lifespan_context(app):
        Path(sys.argv[2]).touch()
asyncio.run(start())
""", str(config_file), str(admitted)], cwd=ROOT, capture_output=True, text=True, timeout=10)
    assert result.returncode == 70, result.stderr
    assert not admitted.exists()
    assert not Path(config.master_key_file).exists()
    assert database.read_bytes() == before
