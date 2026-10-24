"""Retired installation authentication cannot resume an unfinished operation."""

import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from server.config import settings
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
