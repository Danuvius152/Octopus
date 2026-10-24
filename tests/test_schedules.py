"""Tests for schedules API endpoints."""

import pytest
from httpx import ASGITransport, AsyncClient

from server import deps
from server.database import Database
from server.main import app
from server.routers import schedules as schedules_mod
from server.scheduler import ScheduleRunner
from server.session_manager import session_manager
from server.users import UserManager


@pytest.fixture
async def client():
    db = Database(":memory:")
    await db.initialize()
    users = UserManager(db)
    user = await users.create_user(username="schedule-owner", password="password-for-schedules")
    deps.set_user_manager(users)
    headers = {"Authorization": "Bearer " + await users.issue_token(user["id"])}
    session_manager.sessions.clear()
    session_manager._admission_closed = False
    await session_manager.initialize(db)

    runner = ScheduleRunner(session_manager, db)
    await runner.initialize()
    schedules_mod._db = db
    schedules_mod._runner = runner

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test", headers=headers) as c:
        yield c

    await runner.shutdown()
    await db.close()


async def _create_session(client):
    agent = await session_manager.db.get_system_agent()
    assert agent is not None
    resp = await client.post(
        "/api/sessions", json={"agent_id": agent["id"], "name": "test"}, headers=client.headers
    )
    assert resp.status_code == 201
    return resp.json()["id"]


@pytest.mark.asyncio
async def test_list_schedules_empty(client):
    resp = await client.get("/api/schedules", headers=client.headers)
    assert resp.status_code == 200
    assert resp.json() == []


@pytest.mark.asyncio
async def test_create_schedule(client):
    session_id = await _create_session(client)
    agent = await session_manager.db.get_system_agent()
    assert agent is not None
    resp = await client.post(
        "/api/schedules",
        json={
            "agent_id": agent["id"],
            "session_id": session_id,
            "name": "daily check",
            "prompt": "check status",
            "interval_seconds": 60,
        },
        headers=client.headers,
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["name"] == "daily check"
    assert data["prompt"] == "check status"
    assert data["interval_seconds"] == 60
    assert data["enabled"] is True
    # The explicit owner must match the target session's owning agent.
    sess = (await client.get(f"/api/sessions/{session_id}", headers=client.headers)).json()
    assert data["agent_id"] == sess["agent_id"]
    # The originating session is remembered so fires append into it.
    assert data["origin_session_id"] == session_id


@pytest.mark.asyncio
async def test_create_schedule_invalid_session(client):
    agent = await session_manager.db.get_system_agent()
    assert agent is not None
    resp = await client.post(
        "/api/schedules",
        json={
            "agent_id": agent["id"],
            "session_id": "nonexistent",
            "name": "test",
            "prompt": "hello",
            "interval_seconds": 60,
        },
        headers=client.headers,
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_create_schedule_interval_too_short(client):
    session_id = await _create_session(client)
    agent = await session_manager.db.get_system_agent()
    assert agent is not None
    resp = await client.post(
        "/api/schedules",
        json={
            "agent_id": agent["id"],
            "session_id": session_id,
            "name": "test",
            "prompt": "hello",
            "interval_seconds": 30,
        },
        headers=client.headers,
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_list_schedules_after_create(client):
    session_id = await _create_session(client)
    agent = await session_manager.db.get_system_agent()
    assert agent is not None
    await client.post(
        "/api/schedules",
        json={
            "agent_id": agent["id"],
            "session_id": session_id,
            "name": "sched1",
            "prompt": "hi",
            "interval_seconds": 120,
        },
        headers=client.headers,
    )
    resp = await client.get("/api/schedules", headers=client.headers)
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["name"] == "sched1"


@pytest.mark.asyncio
async def test_update_schedule(client):
    session_id = await _create_session(client)
    agent = await session_manager.db.get_system_agent()
    assert agent is not None
    create_resp = await client.post(
        "/api/schedules",
        json={
            "agent_id": agent["id"],
            "session_id": session_id,
            "name": "original",
            "prompt": "hi",
            "interval_seconds": 60,
        },
        headers=client.headers,
    )
    sched_id = create_resp.json()["id"]

    resp = await client.patch(
        f"/api/schedules/{sched_id}",
        json={"name": "updated", "enabled": False},
        headers=client.headers,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["name"] == "updated"
    assert data["enabled"] is False


@pytest.mark.asyncio
async def test_update_schedule_not_found(client):
    resp = await client.patch(
        "/api/schedules/nonexistent",
        json={"name": "x"},
        headers=client.headers,
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_delete_schedule(client):
    session_id = await _create_session(client)
    agent = await session_manager.db.get_system_agent()
    assert agent is not None
    create_resp = await client.post(
        "/api/schedules",
        json={
            "agent_id": agent["id"],
            "session_id": session_id,
            "name": "to_delete",
            "prompt": "hi",
            "interval_seconds": 60,
        },
        headers=client.headers,
    )
    sched_id = create_resp.json()["id"]

    resp = await client.delete(f"/api/schedules/{sched_id}", headers=client.headers)
    assert resp.status_code == 204

    list_resp = await client.get("/api/schedules", headers=client.headers)
    assert list_resp.json() == []


@pytest.mark.asyncio
async def test_auth_required(client):
    resp = await client.get("/api/schedules", headers={"Authorization": ""})
    assert resp.status_code in (401, 403)
