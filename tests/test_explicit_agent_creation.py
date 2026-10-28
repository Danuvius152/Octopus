"""Creation has an explicit, authorized agent before any resource is made."""

from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from server import deps
from server.agent_manager import AgentManager
from server.config import settings
from server.database import Database
from server.routers import agents, schedules, sessions
from server.scheduler import ScheduleRunner
from server.sessions import SessionManager
from server.users import UserManager
from server.workspace import paths_for


@pytest.fixture
async def creation(tmp_path, monkeypatch):
    db = Database(str(tmp_path / "creation.sqlite3"))
    await db.initialize()
    users = UserManager(db)
    monkeypatch.setattr(deps, "_user_manager", users)
    owner = await users.create_user(username="owner", password="password-owner")
    foreign = await users.create_user(username="foreign", password="password-foreign")
    token = await users.issue_token(owner["id"])
    manager = SessionManager()
    await manager.initialize(db)
    agent_manager = AgentManager(db)
    monkeypatch.setattr(agents, "_manager", agent_manager)
    a = await agent_manager.create_agent(name="Agent A", user_id=owner["id"], backend="codex")
    b = await agent_manager.create_agent(name="Agent B", user_id=owner["id"])
    other = await agent_manager.create_agent(name="Foreign", user_id=foreign["id"])
    origin = await manager.create_session(a["id"], "Origin")
    b_origin = await manager.create_session(b["id"], "Other agent origin")
    foreign_origin = await manager.create_session(other["id"], "Foreign origin")
    runner = ScheduleRunner(manager, db)
    await runner.initialize(paused=True)
    monkeypatch.setattr(schedules, "_db", db)
    monkeypatch.setattr(schedules, "_runner", runner)
    app = FastAPI()
    app.include_router(sessions.router)
    app.include_router(agents.router)
    app.include_router(schedules.router)
    app.dependency_overrides[deps.get_session_manager] = lambda: manager
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
        headers={"Authorization": f"Bearer {token}"},
    ) as client:
        yield SimpleNamespace(
            client=client, db=db, manager=manager, runner=runner,
            a=a["id"], b=b["id"], foreign=other["id"],
            origin=origin.id, b_origin=b_origin.id, foreign_origin=foreign_origin.id,
            workspace=paths_for(owner["id"]).workspace,
        )
    await runner.shutdown()
    await db.close()


async def resources(env):
    return (
        tuple(s.id for s in env.manager.list_sessions()),
        await env.db.load_sessions(include_archived=True),
        await env.db.load_schedules(),
        frozenset(job.id for job in env.runner._scheduler.get_jobs()),
        frozenset(str(p) for p in Path(settings.users_root).rglob("*") if p.is_dir()),
    )


@pytest.mark.parametrize("owner, expected", [
    ("missing", 422), (None, 422), ("", 422), ("unknown", 404), ("foreign", 404),
])
async def test_global_session_rejects_unusable_owner_without_resources(creation, owner, expected):
    env = creation
    before = await resources(env)
    body = {"name": "Rejected", "working_dir": str(env.workspace / "must-not-exist")}
    if owner != "missing":
        body["agent_id"] = env.foreign if owner == "foreign" else owner
    response = await env.client.post("/api/sessions", json=body)
    assert response.status_code == expected, response.text
    assert await resources(env) == before


@pytest.mark.parametrize("kind", ["sessions", "schedules"])
@pytest.mark.parametrize("redundant_owner", [False, True])
async def test_scoped_creation_uses_url_owner(creation, kind, redundant_owner):
    env = creation
    body = {"name": "URL owns this"}
    if kind == "schedules":
        body.update(prompt="Check status", interval_seconds=3600, session_id=env.origin)
    if redundant_owner:
        body["agent_id"] = env.b
    response = await env.client.post(f"/api/agents/{env.a}/{kind}", json=body)
    assert response.status_code == 201, response.text
    created = response.json()
    assert created["agent_id"] == env.a
    if kind == "sessions":
        assert created["backend"] == "codex"
        assert env.manager.get_session(created["id"]).agent_id == env.a
    else:
        assert created["origin_session_id"] == env.origin
        assert env.runner.next_run_at(created["id"]) is not None


@pytest.mark.parametrize("kind", ["sessions", "schedules"])
async def test_body_owner_cannot_override_foreign_url(creation, kind):
    env = creation
    before = await resources(env)
    body = {"name": "Rejected", "agent_id": env.a}
    if kind == "schedules":
        body.update(prompt="Check status", interval_seconds=3600, session_id=env.origin)
    response = await env.client.post(f"/api/agents/{env.foreign}/{kind}", json=body)
    assert response.status_code == 404, response.text
    assert await resources(env) == before


async def test_global_session_keeps_backend_and_working_directory(creation):
    env = creation
    directory = env.workspace / "explicit-session"
    directory.mkdir()
    response = await env.client.post("/api/sessions", json={
        "agent_id": env.a, "name": "Explicit", "working_dir": str(directory),
    })
    assert response.status_code == 201, response.text
    created = response.json()
    assert (created["agent_id"], created["backend"], created["working_dir"]) == (
        env.a, "codex", str(directory),
    )
    assert directory.is_dir()
    assert env.manager.get_session(created["id"]).agent_id == env.a


async def test_global_schedule_keeps_explicit_owner_and_legal_target(creation):
    env = creation
    response = await env.client.post("/api/schedules", json={
        "agent_id": env.a, "session_id": env.origin,
        "name": "Explicit", "prompt": "Check status", "interval_seconds": 3600,
    })
    assert response.status_code == 201, response.text
    created = response.json()
    assert (created["agent_id"], created["origin_session_id"]) == (env.a, env.origin)
    assert env.runner.next_run_at(created["id"]) is not None
    persisted = await env.db.get_schedule_definition(created["id"])
    assert (persisted["agent_id"], persisted["origin_session_id"]) == (env.a, env.origin)


@pytest.mark.parametrize("route", ["global", "scoped", "from_text"])
@pytest.mark.parametrize("target, expected", [("b_origin", 409), ("foreign_origin", 404)])
async def test_invalid_schedule_target_is_rejected_before_parsing(
    creation, monkeypatch, route, target, expected,
):
    from server import schedule_ai

    env = creation
    parsed = []

    async def forbidden_parse(*args, **kwargs):
        parsed.append(args)
        raise AssertionError("An invalid target must not reach model parsing")

    monkeypatch.setattr(schedule_ai, "parse_schedule_text", forbidden_parse)
    before = await resources(env)
    body = {
        "agent_id": env.a, "session_id": getattr(env, target),
        "name": "Rejected", "prompt": "Check status", "interval_seconds": 3600,
    }
    url = "/api/schedules" if route == "global" else f"/api/agents/{env.a}/schedules"
    if route == "from_text":
        url += "/from_text"
        body["text"] = "Please decide when to check status"
    response = await env.client.post(url, json=body)
    assert response.status_code == expected, response.text
    assert parsed == []
    assert await resources(env) == before


async def test_archive_replacement_inherits_owner_and_repoints_schedule(creation):
    env = creation
    response = await env.client.post("/api/schedules", json={
        "agent_id": env.a, "session_id": env.origin,
        "name": "Follow replacement", "prompt": "Check status", "interval_seconds": 3600,
    })
    assert response.status_code == 201, response.text
    schedule_id = response.json()["id"]
    response = await env.client.post(f"/api/sessions/{env.origin}/archive")
    assert response.status_code == 201, response.text
    replacement = response.json()
    assert replacement["id"] != env.origin
    assert replacement["agent_id"] == env.a
    assert replacement["backend"] == "codex"
    assert env.manager.get_session(env.origin) is None
    schedule = await env.db.get_schedule_definition(schedule_id)
    assert (schedule["agent_id"], schedule["origin_session_id"]) == (env.a, replacement["id"])
    assert env.runner.next_run_at(schedule_id) is not None


@pytest.mark.parametrize("owner", [None, "unknown"])
async def test_archive_with_unusable_owner_leaves_original_unchanged(creation, owner):
    env = creation
    original = env.manager.get_session(env.origin)
    # Model an already loaded legacy/corrupt binding, without replacing its
    # real Owner, persistence or the archive operation under test.
    original.agent_id = owner
    before = await resources(env)
    response = await env.client.post(f"/api/sessions/{env.origin}/archive")
    assert response.status_code == 404, response.text
    assert env.manager.get_session(env.origin) is original
    assert original.agent_id == owner
    assert await resources(env) == before


async def test_archive_with_database_keeps_explicit_owner(creation):
    manager = creation.manager
    original = await manager.create_session(creation.a, "Original", str(creation.workspace))
    replacement = await manager.archive_session(original.id)
    assert replacement.id != original.id
    assert replacement.agent_id == creation.a
    assert replacement.working_dir == str(creation.workspace)
    assert manager.get_session(original.id) is None
    saved = {row["id"]: row for row in await creation.db.load_sessions(include_archived=True)}
    assert saved[original.id]["archived"]
    assert saved[replacement.id]["agent_id"] == original.agent_id


@pytest.mark.parametrize("owner, expected", [
    ("missing", 422), (None, 422), ("", 422), ("unknown", 404), ("foreign", 404),
])
async def test_global_schedule_cannot_use_target_as_owner(creation, owner, expected):
    env = creation
    before = await resources(env)
    body = {
        "name": "Rejected", "prompt": "Check status", "interval_seconds": 3600,
        "session_id": env.origin,
    }
    if owner != "missing":
        body["agent_id"] = env.foreign if owner == "foreign" else owner
    response = await env.client.post("/api/schedules", json=body)
    assert response.status_code == expected, response.text
    assert await resources(env) == before
