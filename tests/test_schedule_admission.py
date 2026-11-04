"""Schedules must retain the owning agent's authority at admission and fire."""

import asyncio
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient

from server.agent_manager import AgentManager
from server.database import Database
from server.deps import get_session_manager, set_user_manager
from server.knowledge import encode, for_account
from server.main import app
from server.routers import agents, schedules
from server.scheduler import ScheduleRunner
from server.session_manager import SessionManager
from server.users import UserManager


@pytest.fixture
async def domain(monkeypatch):
    db = Database(":memory:")
    await db.initialize()
    users = UserManager(db)
    owner = await users.create_user(username="schedule-owner", password="test-password")
    set_user_manager(users)
    manager = SessionManager()
    await manager.initialize(db)
    runner = ScheduleRunner(manager, db)
    manager.set_schedule_runner(runner)
    agent_manager = AgentManager(db)
    agent = await agent_manager.create_agent(name="Scheduled Codex", backend="codex", user_id=owner["id"])
    other = await agent_manager.create_agent(name="Other agent", user_id=owner["id"])
    monkeypatch.setattr(schedules, "_db", db)
    monkeypatch.setattr(schedules, "_runner", runner)
    monkeypatch.setattr(agents, "_manager", agent_manager)
    app.dependency_overrides[get_session_manager] = lambda: manager
    try:
        yield db, manager, runner, agent, other
    finally:
        app.dependency_overrides.pop(get_session_manager, None)
        await runner.shutdown()
        await db.close()


async def save(db, agent, **fields):
    context = for_account(session_id=fields.get("origin_session_id") or "schedule-entry", request_id="schedule-request",
                          user_id=agent["user_id"], prompt="current prompt", selected=fields.get("wiki_mode"))
    await db.save_schedule(
        schedule_id="schedule", agent_id=agent["id"], name="check",
        prompt="current prompt", interval_seconds=3600,
        created_at="2026-10-17T00:00:00+00:00", knowledge_context=encode(context), **fields,
    )


async def test_internal_session_creation_inherits_agent_backend(domain):
    _db, manager, _runner, agent, _other = domain
    inherited = await manager.create_session(agent["id"], origin="schedule")
    explicit = await manager.create_session(agent["id"], backend="claude-code")
    assert inherited.backend == "codex"
    assert explicit.backend == "claude-code"


async def test_rebinding_a_manager_does_not_call_a_retired_database_scheduler():
    old, current = Database(":memory:"), Database(":memory:")
    await old.initialize()
    await current.initialize()
    manager = SessionManager()
    await manager.initialize(old)
    runner = ScheduleRunner(manager, old)
    await runner.shutdown()
    await old.close()
    try:
        await manager.initialize(current)
        agent = await AgentManager(current).create_agent(name="Current database agent")
        await asyncio.wait_for(manager.archive_agent(agent["id"]), 2)
        assert (await current.get_agent(agent["id"]))["archived"] is True
    finally:
        await current.close()


@pytest.mark.parametrize("state", ["deleted", "disabled", "archived"])
async def test_stale_timer_cannot_launch_work(domain, monkeypatch, state):
    db, manager, runner, agent, _other = domain
    await save(db, agent, enabled=state != "disabled")
    if state == "deleted":
        await db.delete_schedule("schedule")
    if state == "archived":
        await db.archive_agent(agent["id"])
    create = AsyncMock(side_effect=ValueError("unexpected launch"))
    monkeypatch.setattr(manager, "create_session", create)
    await runner._fire("schedule")
    create.assert_not_called()


async def test_fire_reloads_target_and_trusted_prompt(domain, monkeypatch):
    db, manager, runner, agent, other = domain
    origin = await manager.create_session(agent["id"])
    await manager.create_session(other["id"])
    await save(db, agent, origin_session_id=origin.id, wiki_mode="read")
    receipt = AsyncMock()
    receipt.accepted = True
    start = AsyncMock(return_value=receipt)
    monkeypatch.setattr(manager, "start_message", start)
    await runner._fire("schedule")
    args, kwargs = start.call_args
    assert args[0] == origin.id
    assert args[1].endswith("current prompt")
    assert kwargs["knowledge_context"].trusted_request == "current prompt"
    assert kwargs["knowledge_context"].mode == "read"


async def test_legacy_mismatched_target_is_quarantined(domain, monkeypatch):
    db, manager, runner, agent, other = domain
    wrong = await manager.create_session(other["id"])
    await save(db, agent, origin_session_id=wrong.id)
    start = AsyncMock()
    monkeypatch.setattr(manager, "start_message", start)
    await runner._fire("schedule")
    start.assert_not_called()
    row = (await db.load_schedules())[0]
    assert row["enabled"] is False
    assert row["invalid_reason"] == "origin_agent_mismatch"
    assert row["origin_session_id"] == wrong.id
    assert row["prompt"] == "current prompt"


@pytest.mark.parametrize("entry", ["standalone", "agent", "from_text"])
async def test_http_rejects_other_agents_origin_before_parsing(domain, monkeypatch, entry):
    import server.schedule_ai as parser

    db, manager, _runner, agent, other = domain
    wrong = await manager.create_session(other["id"])
    parse = AsyncMock(side_effect=AssertionError("must authorize before model parse"))
    monkeypatch.setattr(parser, "parse_schedule_text", parse)
    url = "/api/schedules" if entry == "standalone" else f"/api/agents/{agent['id']}/schedules"
    body = {"agent_id": agent["id"], "session_id": wrong.id,
            "name": "check", "prompt": "check", "interval_seconds": 3600}
    if entry == "from_text":
        url += "/from_text"
        body = {"session_id": wrong.id, "text": "every morning check"}
    token = await UserManager(db).issue_token(agent["user_id"])
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(url, json=body, headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 409
    parse.assert_not_called()
    assert await db.load_schedules() == []


@pytest.mark.parametrize("entry", ["standalone", "agent", "from_text"])
async def test_http_does_not_disclose_or_execute_another_owners_origin(domain, monkeypatch, entry):
    import server.schedule_ai as parser

    db, manager, _runner, _legacy_agent, _other = domain
    users = UserManager(db)
    set_user_manager(users)
    caller = await users.create_user(username="caller", password="test-password")
    foreign_owner = await users.create_user(username="foreign", password="test-password")
    caller_token = await users.issue_token(caller["id"])
    foreign_token = await users.issue_token(foreign_owner["id"])
    agent_manager = AgentManager(db)
    agent = await agent_manager.create_agent(name="Caller", backend="codex", user_id=caller["id"])
    foreign_agent = await agent_manager.create_agent(name="Foreign", user_id=foreign_owner["id"])
    foreign = await manager.create_session(foreign_agent["id"])
    assert foreign.user_id == foreign_owner["id"]
    parse = AsyncMock(side_effect=AssertionError("unauthorized origin reached parser"))
    monkeypatch.setattr(parser, "parse_schedule_text", parse)
    url = "/api/schedules" if entry == "standalone" else f"/api/agents/{agent['id']}/schedules"
    body = {"agent_id": agent["id"], "session_id": foreign.id,
            "name": "check", "prompt": "check", "interval_seconds": 3600}
    if entry == "from_text":
        url += "/from_text"
        body = {"session_id": foreign.id, "text": "every morning check"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        for owner, token, owned_agent in (
            (caller, caller_token, agent),
            (foreign_owner, foreign_token, foreign_agent),
        ):
            headers = {"Authorization": f"Bearer {token}"}
            identity = await client.get("/api/auth/identity", headers=headers)
            assert identity.status_code == 200, identity.text
            assert identity.json()["user_id"] == owner["id"]
            own_agent = await client.get(f"/api/agents/{owned_agent['id']}", headers=headers)
            assert own_agent.status_code == 200, own_agent.text
            assert own_agent.json()["id"] == owned_agent["id"]
        response = await client.post(url, json=body, headers={"Authorization": f"Bearer {caller_token}"})
    assert response.status_code == 404
    parse.assert_not_called()
    assert await db.load_schedules() == []


async def test_archive_closes_creation_restore_and_existing_session_sends(domain, monkeypatch):
    db, manager, runner, agent, _other = domain
    origin = await manager.create_session(agent["id"])
    archived = await manager.create_session(agent["id"])
    await manager.archive_session(archived.id, replace=False)
    await save(db, agent)
    await runner.reconcile((await db.load_schedules())[0]["id"])
    entered, release = asyncio.Event(), asyncio.Event()
    evict = manager.evict_agent_sessions

    async def delayed_evict(agent_id):
        entered.set()
        await release.wait()
        return await evict(agent_id)

    monkeypatch.setattr(manager, "evict_agent_sessions", delayed_evict)
    closing = asyncio.create_task(manager.archive_agent(agent["id"]))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        assert runner._scheduler.get_job("schedule") is None
        with pytest.raises(ValueError, match="closed"):
            await manager.create_session(agent["id"])
        with pytest.raises(ValueError, match="closed"):
            await manager.import_session("import", agent_id=agent["id"])
        with pytest.raises(ValueError, match="closed"):
            await manager.unarchive_session(archived.id)
        with pytest.raises(ValueError, match="closed"):
            await manager.start_message(origin.id, "too late", steerable=True)
        with pytest.raises(ValueError, match="not confirmed"):
            await manager.unarchive_agent(agent["id"], agent["user_id"])
        create = AsyncMock(side_effect=AssertionError("closed agent must not run"))
        monkeypatch.setattr(manager, "create_session", create)
        await runner._fire("schedule")
        create.assert_not_called()
        assert (await db.load_schedules())[0]["enabled"] is True
    finally:
        release.set()
        await closing
    assert (await db.get_agent(agent["id"]))["archived"] is True
    await manager.unarchive_agent(agent["id"], agent["user_id"])
    assert runner._scheduler.get_job("schedule") is not None


async def test_archive_joins_admitted_creation_without_holding_the_gate(domain, monkeypatch):
    db, manager, _runner, agent, _other = domain
    entered, release = asyncio.Event(), asyncio.Event()
    save_session = db.save_session

    async def delayed_save(**fields):
        entered.set()
        await release.wait()
        await save_session(**fields)

    monkeypatch.setattr(db, "save_session", delayed_save)
    creating = asyncio.create_task(manager.create_session(agent["id"]))
    await asyncio.wait_for(entered.wait(), 2)
    closing = asyncio.create_task(manager.archive_agent(agent["id"]))
    try:
        async def wait_until_closed():
            while agent["id"] not in manager._closed_agents:
                await asyncio.sleep(0.001)

        await asyncio.wait_for(wait_until_closed(), 2)
        assert agent["id"] in manager._closed_agents
        assert not closing.done()
        assert not manager._agent_lifecycle_lock.locked()
        with pytest.raises(ValueError, match="closed"):
            await manager.create_session(agent["id"])
    finally:
        release.set()
        created = await creating
        await closing
    assert manager.get_session(created.id) is None
    assert (await db.get_session_binding(created.id))["archived"] is True


async def test_archive_joins_admitted_session_restoration(domain, monkeypatch):
    db, manager, _runner, agent, _other = domain
    original = await manager.create_session(agent["id"])
    await manager.archive_session(original.id, replace=False)
    entered, release = asyncio.Event(), asyncio.Event()
    update = db.update_session_field

    async def held_restore(session_id, **fields):
        if session_id == original.id and fields.get("archived") is False:
            entered.set()
            await release.wait()
        await update(session_id, **fields)

    monkeypatch.setattr(db, "update_session_field", held_restore)
    restoring = asyncio.create_task(manager.unarchive_session(original.id))
    await asyncio.wait_for(entered.wait(), 2)
    closing = asyncio.create_task(manager.archive_agent(agent["id"]))
    try:
        async with asyncio.timeout(2):
            while agent["id"] not in manager._closed_agents:
                await asyncio.sleep(0.001)
        assert not closing.done()
        assert not manager._agent_lifecycle_lock.locked()
    finally:
        release.set()
        await restoring
        await closing
    assert manager.get_session(original.id) is None
    assert (await db.get_session_binding(original.id))["archived"] is True


async def test_archived_agent_stays_closed_after_restart_and_retains_disabled_preference(domain):
    db, manager, runner, agent, _other = domain
    session = await manager.create_session(agent["id"])
    await save(db, agent, enabled=False)
    await manager.archive_agent(agent["id"])
    await manager.initialize(db)
    await runner.initialize()
    assert runner._scheduler.get_job("schedule") is None
    with pytest.raises(ValueError, match="archived"):
        await manager.unarchive_session(session.id)
    await manager.unarchive_agent(agent["id"], agent["user_id"])
    assert (await db.load_schedules())[0]["enabled"] is False
    assert runner._scheduler.get_job("schedule") is None


async def test_unconfirmed_archive_cannot_be_reopened(domain, monkeypatch):
    _db, manager, _runner, agent, _other = domain
    evict = manager.evict_agent_sessions
    monkeypatch.setattr(manager, "evict_agent_sessions", AsyncMock(side_effect=ValueError("recording unconfirmed")))
    with pytest.raises(ValueError, match="unconfirmed"):
        await manager.archive_agent(agent["id"])
    with pytest.raises(ValueError, match="not confirmed"):
        await manager.unarchive_agent(agent["id"], agent["user_id"])
    monkeypatch.setattr(manager, "evict_agent_sessions", evict)
    await manager.archive_agent(agent["id"])
    await manager.unarchive_agent(agent["id"], agent["user_id"])


async def test_startup_quarantines_orphans_and_bad_relationships_without_hiding_them(domain):
    db, manager, runner, agent, other = domain
    wrong = await manager.create_session(other["id"])
    await save(db, agent, origin_session_id=wrong.id)
    await db.conn.execute(
        "INSERT INTO schedules(id, name, prompt, enabled, created_at, interval_seconds) "
        "VALUES('orphan', 'orphan', 'preserve me', 1, '2026-10-17', 3600)"
    )
    await db.conn.commit()
    await runner.initialize()
    token = await UserManager(db).issue_token(agent["user_id"])
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test",
                           headers={"Authorization": f"Bearer {token}"}) as client:
        response = await client.get("/api/schedules")
        assert response.status_code == 200
        rows = {row["id"]: row for row in response.json()}
        assert "orphan" not in rows  # No account owns an unbound schedule.
        orphan = next(row for row in await db.load_schedules() if row["id"] == "orphan")
        assert orphan["invalid_reason"] == "agent_not_found" and orphan["enabled"] is False
        assert runner._scheduler.get_job("orphan") is None
        assert rows["schedule"]["invalid_reason"] == "origin_agent_mismatch"
        for row in rows.values():
            assert row["enabled"] is False
            assert row["interval_seconds"] == 3600
            assert runner._scheduler.get_job(row["id"]) is None
        refused = await client.patch("/api/schedules/schedule", json={"enabled": True})
        assert refused.status_code == 409
        assert (await db.load_schedules())[0]["enabled"] is False
