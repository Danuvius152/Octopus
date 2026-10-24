"""Agent binding behavior through the account-scoped REST and manager seams."""

import sqlite3

import pytest
from httpx import ASGITransport, AsyncClient

from server import deps
from server.agent_manager import AgentError, AgentManager
from server.database import Database
from server.main import app
from server.routers import agents, credentials
from server.session_manager import session_manager
from server.users import UserManager


@pytest.fixture
async def bindings(monkeypatch):
    db = Database(":memory:")
    await db.initialize()
    session_manager.sessions.clear()
    session_manager._admission_closed = False
    await session_manager.initialize(db)
    manager = AgentManager(db)
    monkeypatch.setattr(agents, "_manager", manager)
    monkeypatch.setattr(credentials, "_db", db)
    users = UserManager(db)
    deps.set_user_manager(users)
    owner = await users.create_user(username="owner", password="password1")
    token = await users.issue_token(owner["id"])
    headers = {"Authorization": f"Bearer {token}"}
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    try:
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            yield client, headers, manager, owner["id"]
    finally:
        await db.close()
        session_manager.sessions.clear()
        session_manager._admission_closed = False


async def _create_credential(client, headers, backend="codex"):
    response = await client.post(
        "/api/credentials",
        headers=headers,
        json={"backend": backend, "label": "Test binding", "secret": "test-secret"},
    )
    assert response.status_code == 201
    return response.json()["id"]


async def test_creating_agent_rejects_missing_credential_without_creating_it(bindings):
    client, headers, _, _ = bindings
    response = await client.post(
        "/api/agents",
        headers=headers,
        json={"name": "Invalid binding", "backend": "codex", "credential_id": "missing"},
    )
    assert response.status_code == 404
    assert response.json() == {"detail": "Credential not found"}
    listing = await client.get("/api/agents", headers=headers)
    assert "Invalid binding" not in {agent["name"] for agent in listing.json()}


async def test_updating_agent_rejects_missing_binding_without_changing_other_fields(bindings):
    client, headers, _, _ = bindings
    created = await client.post(
        "/api/agents", headers=headers, json={"name": "Unchanged", "backend": "codex"}
    )
    assert created.status_code == 201
    agent_id = created.json()["id"]
    response = await client.patch(
        f"/api/agents/{agent_id}",
        headers=headers,
        json={"name": "Partly changed", "credential_id": "missing"},
    )
    assert response.status_code == 404
    assert response.json() == {"detail": "Credential not found"}
    current = await client.get(f"/api/agents/{agent_id}", headers=headers)
    assert current.json()["name"] == "Unchanged"
    assert current.json()["credential_id"] is None


async def test_backend_only_update_refuses_incompatible_existing_credential(bindings):
    client, headers, _, _ = bindings
    credential_id = await _create_credential(client, headers)
    created = await client.post(
        "/api/agents",
        headers=headers,
        json={"name": "Keep my binding", "backend": "codex", "credential_id": credential_id},
    )
    assert created.status_code == 201
    agent_id = created.json()["id"]
    response = await client.patch(
        f"/api/agents/{agent_id}", headers=headers, json={"backend": "claude-code"}
    )
    assert response.status_code == 400
    assert "compatible credential" in response.json()["detail"]
    current = await client.get(f"/api/agents/{agent_id}", headers=headers)
    assert current.json()["backend"] == "codex"
    assert current.json()["credential_id"] == credential_id


@pytest.mark.parametrize("operation", ["create", "update"])
async def test_empty_credential_clears_binding_like_explicit_null(bindings, operation):
    client, headers, _, _ = bindings
    if operation == "create":
        response = await client.post(
            "/api/agents",
            headers=headers,
            json={"name": "Unbound", "backend": "codex", "credential_id": ""},
        )
        assert response.status_code == 201
    else:
        credential_id = await _create_credential(client, headers)
        created = await client.post(
            "/api/agents",
            headers=headers,
            json={"name": "Clear binding", "backend": "codex", "credential_id": credential_id},
        )
        assert created.status_code == 201
        response = await client.patch(
            f"/api/agents/{created.json()['id']}",
            headers=headers,
            json={"credential_id": "", "backend": "claude-code"},
        )
        assert response.status_code == 200
    current = await client.get(f"/api/agents/{response.json()['id']}", headers=headers)
    assert current.json()["credential_id"] is None


@pytest.mark.parametrize("operation", ["create", "update"])
async def test_credential_deleted_after_lookup_rejects_atomic_agent_write(
    bindings, monkeypatch, operation
):
    client, headers, manager, _ = bindings
    credential_id = await _create_credential(client, headers)
    agent_id = None
    if operation == "update":
        created = await client.post(
            "/api/agents", headers=headers, json={"name": "Unchanged", "backend": "codex"}
        )
        assert created.status_code == 201
        agent_id = created.json()["id"]

    # Coordinate a real deletion at the database write boundary after all
    # scoped validation. The INSERT/UPDATE and its foreign key remain SQLite.
    write_method = "save_agent" if operation == "create" else "update_agent"
    write = getattr(manager.db, write_method)

    async def delete_then_write(*args, **kwargs):
        await manager.db.delete_credential(credential_id)
        return await write(*args, **kwargs)

    monkeypatch.setattr(manager.db, write_method, delete_then_write)
    body = {"name": "Must not be saved", "backend": "codex", "credential_id": credential_id}
    if operation == "create":
        response = await client.post("/api/agents", headers=headers, json=body)
    else:
        response = await client.patch(f"/api/agents/{agent_id}", headers=headers, json=body)
    assert response.status_code == 404
    assert response.json() == {"detail": "Credential not found"}
    listing = await client.get("/api/agents", headers=headers)
    assert "Must not be saved" not in {agent["name"] for agent in listing.json()}
    if agent_id is not None:
        current = await client.get(f"/api/agents/{agent_id}", headers=headers)
        assert current.json()["name"] == "Unchanged"
        assert current.json()["credential_id"] is None


@pytest.mark.parametrize("operation", ["create", "update"])
async def test_reasoning_controls_do_not_change_invalid_binding_feedback(bindings, operation):
    client, headers, _, _ = bindings
    credential_id = await _create_credential(client, headers, "claude-code")
    body = {"name": "Wrong backend", "backend": "codex", "credential_id": credential_id}
    if operation == "create":
        ordinary = await client.post("/api/agents", headers=headers, json=body)
        controlled = await client.post(
            "/api/agents", headers=headers, json={**body, "reasoning_effort": "low"}
        )
    else:
        created = await client.post(
            "/api/agents", headers=headers, json={"name": "Unchanged", "backend": "codex"}
        )
        assert created.status_code == 201
        url = f"/api/agents/{created.json()['id']}"
        ordinary = await client.patch(url, headers=headers, json=body)
        controlled = await client.patch(
            url, headers=headers, json={**body, "reasoning_effort": "low"}
        )
        current = await client.get(url, headers=headers)
        assert current.json()["name"] == "Unchanged"
    assert ordinary.status_code == controlled.status_code == 400
    assert "compatible credential" in ordinary.json()["detail"]
    assert controlled.json() == ordinary.json()


async def test_valid_binding_round_trips_preserves_omission_and_accepts_null(bindings):
    client, headers, _, _ = bindings
    first = await _create_credential(client, headers)
    second = await _create_credential(client, headers)
    claude = await _create_credential(client, headers, "claude-code")
    created = await client.post(
        "/api/agents", headers=headers,
        json={"name": "Valid binding", "backend": "codex", "credential_id": first},
    )
    assert created.status_code == 201
    url = f"/api/agents/{created.json()['id']}"
    for change, expected_backend, expected_credential in (
        ({"description": "No credential edit"}, "codex", first),
        ({"credential_id": second}, "codex", second),
        ({"backend": "claude-code", "credential_id": claude}, "claude-code", claude),
        ({"credential_id": None}, "claude-code", None),
    ):
        updated = await client.patch(url, headers=headers, json=change)
        assert updated.status_code == 200
        current = await client.get(url, headers=headers)
        assert current.json()["backend"] == expected_backend
        assert current.json()["credential_id"] == expected_credential


@pytest.mark.parametrize("operation", ["create", "update"])
async def test_foreign_credential_has_same_feedback_as_missing_credential(bindings, operation):
    client, headers, manager, _ = bindings
    users = UserManager(manager.db)
    other = await users.create_user(username="other", password="password2")
    token = await users.issue_token(other["id"])
    other_headers = {"Authorization": f"Bearer {token}"}
    foreign = await _create_credential(client, other_headers)
    if operation == "create":
        url = "/api/agents"
        request = client.post
    else:
        created = await client.post(
            "/api/agents", headers=headers, json={"name": "Unchanged", "backend": "codex"}
        )
        assert created.status_code == 201
        url = f"/api/agents/{created.json()['id']}"
        request = client.patch
    responses = []
    for credential_id in ("missing", foreign):
        response = await request(
            url, headers=headers,
            json={"name": "Must not be saved", "backend": "codex", "credential_id": credential_id},
        )
        assert response.status_code == 404
        responses.append(response.json())
    assert responses == [{"detail": "Credential not found"}] * 2
    listing = await client.get("/api/agents", headers=headers)
    assert "Must not be saved" not in {agent["name"] for agent in listing.json()}
    if operation == "update":
        current = await client.get(url, headers=headers)
        assert current.json()["name"] == "Unchanged"
        assert current.json()["credential_id"] is None


@pytest.mark.parametrize("operation", ["create", "update"])
async def test_deleted_credential_selection_is_a_client_error(bindings, operation):
    client, headers, _, _ = bindings
    credential_id = await _create_credential(client, headers)
    deleted = await client.delete(f"/api/credentials/{credential_id}", headers=headers)
    assert deleted.status_code == 204
    body = {"name": "Must not be saved", "backend": "codex", "credential_id": credential_id}
    if operation == "create":
        response = await client.post("/api/agents", headers=headers, json=body)
    else:
        created = await client.post(
            "/api/agents", headers=headers, json={"name": "Unchanged", "backend": "codex"}
        )
        assert created.status_code == 201
        url = f"/api/agents/{created.json()['id']}"
        response = await client.patch(url, headers=headers, json=body)
        current = await client.get(url, headers=headers)
        assert current.json()["name"] == "Unchanged"
    assert response.status_code == 404
    assert response.json() == {"detail": "Credential not found"}


async def test_manager_enforces_binding_rules_without_request_routing(bindings):
    client, headers, manager, owner_id = bindings
    credential_id = await _create_credential(client, headers)
    created = await manager.create_agent(
        name="Manager binding", backend="codex", credential_id=credential_id, user_id=owner_id
    )
    with pytest.raises(AgentError, match="compatible credential"):
        await manager.update_agent(created["id"], owner_id, backend="claude-code", name="Changed")
    with pytest.raises(AgentError, match="Credential not found"):
        await manager.create_agent(name="No owner", backend="codex", credential_id=credential_id)
    with pytest.raises(AgentError, match="Credential not found"):
        await manager.update_agent(created["id"], owner_id, credential_id="missing", name="Changed")
    current = await manager.get_agent(created["id"], owner_id)
    assert current["name"] == "Manager binding"
    assert current["backend"] == "codex"
    assert current["credential_id"] == credential_id
    await manager.update_agent(created["id"], owner_id, credential_id="", backend="claude-code")
    current = await manager.get_agent(created["id"], owner_id)
    assert current["credential_id"] is None
    assert current["backend"] == "claude-code"


async def test_manager_checks_explicit_empty_backend_as_the_value_being_saved(bindings):
    client, headers, manager, owner_id = bindings
    credential_id = await _create_credential(client, headers)
    created = await manager.create_agent(
        name="Keep compatible", backend="codex", credential_id=credential_id, user_id=owner_id
    )
    with pytest.raises(AgentError, match="compatible credential"):
        await manager.update_agent(created["id"], owner_id, backend="")
    current = await manager.get_agent(created["id"], owner_id)
    assert current["backend"] == "codex"
    assert current["credential_id"] == credential_id


async def test_unrelated_constraint_failure_is_not_disguised_as_binding_error(
    bindings, monkeypatch
):
    client, headers, manager, owner_id = bindings
    credential_id = await _create_credential(client, headers)
    save = manager.db.save_agent

    async def competing_name_then_save(**fields):
        await manager.db.delete_credential(credential_id)
        await save(**{**fields, "agent_id": "competing-agent", "credential_id": None})
        await save(**fields)

    monkeypatch.setattr(manager.db, "save_agent", competing_name_then_save)
    # Both a credential removal and a name conflict occur. The actual UNIQUE
    # constraint must remain visible rather than being called a bad binding.
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE constraint failed"):
        await manager.create_agent(
            name="Concurrent name", backend="codex", credential_id=credential_id, user_id=owner_id
        )
