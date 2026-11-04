"""Tests for schedules API endpoints."""

import asyncio

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
@pytest.mark.parametrize("entry", ["global", "session"])
@pytest.mark.parametrize("change", ["edit", "disable", "enable"])
async def test_committed_edit_finishes_registration_after_http_waiter_cancels(client, entry, change):
    runner = schedules_mod._get_runner()
    runner._scheduler.pause()
    agent = await session_manager.db.get_system_agent()
    created = await client.post("/api/schedules", json={
        "agent_id": agent["id"], "name": "owned edit", "prompt": "check",
        "interval_seconds": 3600,
    })
    assert created.status_code == 201
    schedule_id = created.json()["id"]
    if change == "enable":
        assert (await client.patch(f"/api/schedules/{schedule_id}", json={"enabled": False})).status_code == 200
    session_id = await _create_session(client)
    url = f"/api/schedules/{schedule_id}" if entry == "global" else f"/api/sessions/{session_id}/schedules/{schedule_id}"
    updates = {"interval_seconds": 7200} if change == "edit" else {"enabled": change == "enable"}

    await runner._registration_lock.acquire()
    editing = asyncio.create_task(client.patch(
        url, json=updates,
    ))
    try:
        async with asyncio.timeout(3):
            while True:
                row = await session_manager.db.get_schedule_definition(schedule_id)
                if all(row[key] == value for key, value in updates.items()):
                    break
                await asyncio.sleep(0)
        assert any(schedule_id in blocker for blocker in runner.drain_blockers())
        editing.cancel()
        with pytest.raises(asyncio.CancelledError):
            await editing
    finally:
        runner._registration_lock.release()
        if not editing.done():
            await editing

    # Join the original registration lock's queue, including any retained edit.
    async with runner._registration_lock:
        job = runner._scheduler.get_job(schedule_id)
        if change == "disable":
            assert job is None
        else:
            assert job is not None
            assert job.trigger.interval.total_seconds() == (7200 if change == "edit" else 3600)


@pytest.mark.parametrize("entry", ["global", "agent", "session", "from_text"])
async def test_committed_creation_finishes_registration_after_waiter_cancels(client, entry):
    from server.agent_manager import AgentManager
    from server.routers import agents

    agents.set_manager(AgentManager(session_manager.db))
    runner = schedules_mod._get_runner()
    runner._scheduler.pause()
    session_id = await _create_session(client)
    agent = await session_manager.db.get_system_agent()
    url = {"global": "/api/schedules", "agent": f"/api/agents/{agent['id']}/schedules",
           "from_text": f"/api/agents/{agent['id']}/schedules/from_text",
           "session": f"/api/sessions/{session_id}/schedules"}[entry]
    body = {"name": "owned creation", "prompt": "check", "interval_seconds": 3600}
    if entry == "global":
        body["agent_id"] = agent["id"]
    elif entry == "from_text":
        body = {"text": "60m check"}
    await runner._registration_lock.acquire()
    creating = asyncio.create_task(client.post(url, json=body))
    try:
        async with asyncio.timeout(3):
            while not (rows := await session_manager.db.load_schedules()):
                await asyncio.sleep(0)
        schedule_id = rows[0]["id"]
        creating.cancel()
        with pytest.raises(asyncio.CancelledError):
            await creating
    finally:
        runner._registration_lock.release()
        await asyncio.gather(creating, return_exceptions=True)
    async with runner._registration_lock:
        assert runner._scheduler.get_job(schedule_id) is not None
    assert len((await client.get("/api/schedules")).json()) == 1


@pytest.mark.parametrize("entry", ["global", "session"])
async def test_committed_deletion_unregisters_after_http_waiter_cancels(client, entry):
    runner = schedules_mod._get_runner()
    runner._scheduler.pause()
    session_id = await _create_session(client)
    created = await client.post(f"/api/sessions/{session_id}/schedules", json={
        "name": "owned deletion", "prompt": "check", "interval_seconds": 3600,
    })
    schedule_id = created.json()["id"]
    url = f"/api/schedules/{schedule_id}" if entry == "global" else f"/api/sessions/{session_id}/schedules/{schedule_id}"
    await runner._registration_lock.acquire()
    deleting = asyncio.create_task(client.delete(url))
    try:
        async with asyncio.timeout(3):
            while await session_manager.db.get_schedule_definition(schedule_id) is not None:
                await asyncio.sleep(0)
        deleting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await deleting
    finally:
        runner._registration_lock.release()
        await asyncio.gather(deleting, return_exceptions=True)
    async with runner._registration_lock:
        assert runner._scheduler.get_job(schedule_id) is None


@pytest.mark.parametrize("operation", ["create", "edit", "delete"])
async def test_registration_failure_reports_committed_identity_in_http_and_mcp(client, monkeypatch, caplog, operation):
    import httpx

    from server.mcp_servers.schedule import _error

    runner = schedules_mod._get_runner()
    runner._scheduler.pause()
    agent = await session_manager.db.get_system_agent()
    body = {"agent_id": agent["id"], "name": "registration failure", "prompt": "check", "interval_seconds": 3600}
    if operation != "create":
        created = await client.post("/api/schedules", json=body)
        schedule_id = created.json()["id"]

    def fail_registration(*args, **kwargs):
        raise RuntimeError("injected APScheduler registration failure")

    with monkeypatch.context() as patch:
        patch.setattr(runner._scheduler, "remove_job" if operation == "delete" else "add_job", fail_registration)
        if operation == "create":
            response = await client.post("/api/schedules", json=body)
        elif operation == "edit":
            response = await client.patch(f"/api/schedules/{schedule_id}", json={"name": "committed edit"})
        else:
            response = await client.delete(f"/api/schedules/{schedule_id}")
    assert response.status_code == 503
    detail = response.json()
    assert detail["code"] == "schedule_registration_unconfirmed"
    if operation != "create":
        assert detail["schedule_id"] == schedule_id
    schedule_id = detail["schedule_id"]
    assert schedule_id in detail["detail"]
    assert "configuration may have been saved; registration is unconfirmed" in detail["detail"]
    assert "do not repeat creation" in detail["detail"]
    assert schedule_id in caplog.text and "injected APScheduler registration failure" in caplog.text
    row = await session_manager.db.get_schedule_definition(schedule_id)
    if operation == "delete":
        assert row is None
    else:
        assert row["name"] == ("committed edit" if operation == "edit" else body["name"])
    message = _error(httpx.Response(503, json=response.json()), operation)
    assert detail["detail"] in message and schedule_id in message
    # Recover registration by the original identity, without another create or fire.
    await runner.reconcile(schedule_id)
    assert (runner._scheduler.get_job(schedule_id) is None) == (operation == "delete")


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
