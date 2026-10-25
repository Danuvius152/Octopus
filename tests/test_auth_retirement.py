"""Retired installation authentication stays unavailable to current accounts."""

import pytest

from server.config import settings
from server.database import Database
from tests.test_auth_routes import client as client


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
