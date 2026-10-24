"""Tests for the POST /api/sessions/import endpoint."""

import pytest
from httpx import ASGITransport, AsyncClient

from server.database import Database
from server.deps import set_user_manager
from server.main import app
from server.session_manager import session_manager
from server.users import UserManager


@pytest.fixture
async def client():
    db = Database(":memory:")
    await db.initialize()
    users = UserManager(db)
    owner = await users.create_user(username="import-owner", password="test-password")
    set_user_manager(users)
    token = await users.issue_token(owner["id"])
    session_manager.sessions.clear()
    session_manager._admission_closed = False
    await session_manager.initialize(db)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test", headers={"Authorization": f"Bearer {token}"}) as c:
        yield c

    await db.close()


@pytest.mark.asyncio
async def test_import_basic(client):
    resp = await client.post(
        "/api/sessions/import",
        headers=client.headers,
        json={
            "name": "Imported",
            "messages": [
                {"role": "user", "type": "text", "content": "hello"},
                {"role": "assistant", "type": "text", "content": "hi there"},
            ],
        },
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["name"] == "Imported"
    assert data["message_count"] == 2
    assert len(data["messages"]) == 2


@pytest.mark.asyncio
async def test_import_sets_claude_session_id(client):
    resp = await client.post(
        "/api/sessions/import",
        headers=client.headers,
        json={
            "name": "With Resume",
            "claude_session_id": "abc-123-def",
            "messages": [{"role": "user", "type": "text", "content": "test"}],
        },
    )
    assert resp.status_code == 201
    sid = resp.json()["id"]

    # Verify via GET that the session has the claude_session_id
    # (it's stored on the Session object; we can check the manager directly)
    session = session_manager.get_session(sid)
    assert session is not None
    assert session.claude_session_id == "abc-123-def"


@pytest.mark.asyncio
async def test_import_messages_retrievable(client):
    resp = await client.post(
        "/api/sessions/import",
        headers=client.headers,
        json={
            "name": "Persistent",
            "messages": [
                {"role": "user", "type": "text", "content": "msg1"},
                {
                    "role": "assistant",
                    "type": "tool_use",
                    "tool_name": "Read",
                    "tool_input": {"path": "/f"},
                    "tool_use_id": "t1",
                },
                {
                    "role": "tool",
                    "type": "tool_result",
                    "content": "file data",
                    "tool_use_id": "t1",
                },
            ],
        },
    )
    sid = resp.json()["id"]

    # GET the session and verify messages are persisted
    get_resp = await client.get(f"/api/sessions/{sid}", headers=client.headers)
    assert get_resp.status_code == 200
    messages = get_resp.json()["messages"]
    assert len(messages) == 3
    assert messages[0]["content"] == "msg1"
    assert messages[1]["tool_name"] == "Read"
    assert messages[2]["type"] == "tool_result"


@pytest.mark.asyncio
async def test_import_auth_required(client):
    resp = await client.post(
        "/api/sessions/import",
        headers={"Authorization": ""},
        json={"name": "No Auth", "messages": []},
    )
    assert resp.status_code in (401, 403)


async def test_each_accounts_import_uses_its_own_default_and_rejects_a_foreign_agent(client):
    database = session_manager.db
    users = UserManager(database)
    first = await users.resolve_token(client.headers["Authorization"].removeprefix("Bearer "))
    invite = await users.create_invite(created_by=first["id"])
    second = await users.register(invite_code=invite["code"], username="second-owner", password="another-password")
    second_token = await users.issue_token(second["id"])
    first_agent = await database.get_system_agent(first["id"])
    second_agent = await database.get_system_agent(second["id"])
    assert first_agent["id"] != second_agent["id"] and second_agent["is_system"]
    one = await client.post("/api/sessions/import", json={"name": "First import", "messages": []})
    two = await client.post("/api/sessions/import", json={"name": "Second import", "messages": []},
                            headers={"Authorization": f"Bearer {second_token}"})
    assert one.status_code == two.status_code == 201
    assert one.json()["agent_id"] == first_agent["id"]
    assert two.json()["agent_id"] == second_agent["id"]
    assert session_manager.get_session(one.json()["id"]).user_id == first["id"]
    assert session_manager.get_session(two.json()["id"]).user_id == second["id"]
    foreign = await client.post("/api/sessions/import", headers={"Authorization": f"Bearer {second_token}"},
                                json={"name": "Refused", "agent_id": first_agent["id"], "messages": []})
    assert foreign.status_code == 404 and len(await database.load_sessions()) == 2


@pytest.mark.asyncio
async def test_import_empty_messages(client):
    resp = await client.post(
        "/api/sessions/import",
        headers=client.headers,
        json={"name": "Empty"},
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["message_count"] == 0
    assert data["messages"] == []


@pytest.mark.asyncio
async def test_import_appears_in_list(client):
    await client.post(
        "/api/sessions/import",
        headers=client.headers,
        json={
            "name": "Listed Session",
            "messages": [{"role": "user", "type": "text", "content": "hey"}],
        },
    )

    resp = await client.get("/api/sessions", headers=client.headers)
    assert resp.status_code == 200
    sessions = resp.json()
    names = [s["name"] for s in sessions]
    assert "Listed Session" in names
