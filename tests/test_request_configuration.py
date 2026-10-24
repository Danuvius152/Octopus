"""Request defaults are captured together; captured credentials remain revocable."""

import asyncio
import json
import sqlite3
import sys
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from server.agent_manager import AgentManager
from server.codex_login import codex_home_for
from server.database import Database
from server.deps import get_session_manager
from server.harness import get_harness
from server.harness.model_settings import ModelSettings
from server.harness.profile import StdinMode
from server.routers import agents, credentials, sessions
from server.sessions import SessionManager
from tests.accounts import create_account


@pytest.fixture
async def configuration_host(tmp_path, monkeypatch):
    db = Database(":memory:")
    await db.initialize()
    _, bearer = await create_account(db, extra_roots=[tmp_path])
    manager = SessionManager()
    await manager.initialize(db)
    monkeypatch.setattr(agents, "_manager", AgentManager(db))
    monkeypatch.setattr(credentials, "_db", db)
    app = FastAPI()
    for router in (agents.router, credentials.router, sessions.router):
        app.include_router(router)
    app.dependency_overrides[get_session_manager] = lambda: manager
    effects = tmp_path / "effects.jsonl"
    model = tmp_path / "configuration_model.py"
    model.write_text(
        "import sys\nfrom pathlib import Path\n"
        "with Path(sys.argv[1]).open('a') as out: out.write(sys.argv[2]+'\\n')\n"
        "print('{\"type\":\"turn.completed\",\"usage\":{}}',flush=True)\n"
    )

    def argv(context):
        observed = {"model": context.model, "effort": context.reasoning_effort,
                    "tier": context.service_tier,
                    "credential": Path(context.credential.home_dir).name if context.credential else None}
        return [sys.executable, "-u", str(model), str(effects), json.dumps(observed)], {"cwd": context.working_dir}

    harness = get_harness("codex")
    monkeypatch.setattr(harness, "profile", replace(
        harness.profile, binary=sys.executable, build_turn_argv=argv,
        activity_observer=None, prepare_resume=None, stdin_mode=StdinMode.CLOSE_AFTER_SPAWN,
        model_settings=lambda credential: ModelSettings(reasoning_efforts=["low", "high"], service_tiers=["default", "fast"]),
    ))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test",
                           headers={"Authorization": f"Bearer {bearer}"}) as client:
        credential_ids = []
        for label in ("old", "new"):
            created = await client.post("/api/credentials", json={"backend": "codex", "label": label, "secret": "test-only"})
            assert created.status_code == 201, created.text
            credential_id = created.json()["id"]
            credential_ids.append(credential_id)
            home = Path(codex_home_for(credential_id))
            home.mkdir(parents=True, exist_ok=True)
            (home / "auth.json").write_text("{}")
        created = await client.post("/api/agents", json={
            "name": "Request configuration", "backend": "codex", "model": "agent-old",
            "reasoning_effort": "low", "service_tier": "default", "credential_id": credential_ids[0],
        })
        assert created.status_code == 201, created.text
        agent_id = created.json()["id"]
        session = await manager.create_session(agent_id, "Configuration", str(tmp_path), backend="codex")
        try:
            yield client, manager, session, credential_ids, effects
        finally:
            await manager.drain_turns()
            await manager.stop_all_held_processes()
            await db.close()


async def test_request_captures_session_and_agent_after_coordinated_updates(configuration_host):
    client, manager, session, credential_ids, effects = configuration_host
    response = await client.patch(f"/api/sessions/{session.id}", json={"model": "session-old"})
    assert response.status_code == 200, response.text
    input_recorded = asyncio.Event()

    async def observe_input(event):
        if event.get("session_id") == session.id and event.get("type") == "user_message":
            input_recorded.set()

    manager.on_broadcast("configuration-input", observe_input)
    # Hold the existing configuration boundary to order real HTTP updates
    # before request assembly; assertions concern the process's actual input.
    async with manager.configuration_lock:
        session_update = asyncio.create_task(client.patch(f"/api/sessions/{session.id}", json={"model": "session-new"}))
        agent_update = asyncio.create_task(client.patch(f"/api/agents/{session.agent_id}", json={
            "reasoning_effort": "high", "service_tier": "fast", "credential_id": credential_ids[1],
        }))
        await asyncio.sleep(.02)
        receipt = await manager.start_message(session.id, "Use a consistent configuration")
        await asyncio.wait_for(input_recorded.wait(), 3)
    try:
        replies = await asyncio.gather(session_update, agent_update)
        assert all(reply.status_code == 200 for reply in replies), [reply.text for reply in replies]
        assert (await asyncio.wait_for(receipt.wait(), 5)).outcome.status == "success"
        assert [json.loads(line) for line in effects.read_text().splitlines()] == [{
            "model": "session-new", "effort": "high", "tier": "fast", "credential": credential_ids[1],
        }]
    finally:
        manager.remove_broadcast("configuration-input")


async def test_default_updates_after_capture_apply_to_the_next_request(configuration_host, monkeypatch):
    client, manager, session, credential_ids, effects = configuration_host
    captured, release = asyncio.Event(), asyncio.Event()
    read = manager.db.get_enabled_connectors_for_agent

    async def hold_assembly(agent_id):
        rows = await read(agent_id)
        captured.set()
        await release.wait()
        return rows

    monkeypatch.setattr(manager.db, "get_enabled_connectors_for_agent", hold_assembly)
    first = await manager.start_message(session.id, "Keep the original request's defaults")
    try:
        await asyncio.wait_for(captured.wait(), 3)
        updated = await client.patch(f"/api/sessions/{session.id}", json={"model": "session-new"})
        assert updated.status_code == 200, updated.text
        updated = await client.patch(f"/api/agents/{session.agent_id}", json={
            "model": "agent-new", "reasoning_effort": "high", "service_tier": "fast", "credential_id": credential_ids[1],
        })
        assert updated.status_code == 200, updated.text
        release.set()
        assert (await asyncio.wait_for(first.wait(), 5)).outcome.status == "success"
        second = await manager.start_message(session.id, "Use the new defaults")
        assert (await asyncio.wait_for(second.wait(), 5)).outcome.status == "success"
        assert [json.loads(line) for line in effects.read_text().splitlines()] == [
            {"model": "agent-old", "effort": "low", "tier": "default", "credential": credential_ids[0]},
            {"model": "session-new", "effort": "high", "tier": "fast", "credential": credential_ids[1]},
        ]
    finally:
        release.set()


@pytest.mark.parametrize("revocation", ["delete", "needs_reconnect"])
async def test_captured_credential_is_rechecked_before_os_spawn(configuration_host, monkeypatch, revocation):
    client, manager, session, credential_ids, effects = configuration_host
    authorized, release = asyncio.Event(), asyncio.Event()
    harness = get_harness("codex")
    create = harness.create_run

    def gate_authorization(config):
        check = config.authorize_spawn

        async def authorize():
            authorized.set()
            await release.wait()
            await check()

        return create(replace(config, authorize_spawn=authorize))

    monkeypatch.setattr(harness, "create_run", gate_authorization)
    receipt = await manager.start_message(session.id, "Do not use a revoked credential")
    try:
        await asyncio.wait_for(authorized.wait(), 3)
        if revocation == "delete":
            deleted = await client.delete(f"/api/credentials/{credential_ids[0]}")
            assert deleted.status_code == 204, deleted.text
        else:
            await manager.db.update_credential(credential_ids[0], needs_reconnect=True, status="needs_reconnect")
        release.set()
        completion = await asyncio.wait_for(receipt.wait(), 5)
        assert completion.outcome.status == "failed"
        assert "credential" in completion.outcome.message.lower()
        assert not effects.exists()
        detail = await client.get(f"/api/sessions/{session.id}")
        assert any(row["role"] == "user" and row["content"] == "Do not use a revoked credential"
                   for row in detail.json()["messages"])
    finally:
        release.set()


async def test_credential_deleted_after_spawn_cannot_receive_initial_input(configuration_host, tmp_path, monkeypatch):
    client, manager, session, credential_ids, effects = configuration_host
    born, release = asyncio.Event(), asyncio.Event()
    model = tmp_path / "stdin_model.py"
    model.write_text(
        "import sys\nfrom pathlib import Path\n"
        "line=sys.stdin.readline()\n"
        "if line:\n Path(sys.argv[1]).write_text(line)\n"
        "print('{\"type\":\"turn.completed\",\"usage\":{}}',flush=True)\n"
    )
    harness = get_harness("codex")
    monkeypatch.setattr(harness, "profile", replace(
        harness.profile, stdin_mode=StdinMode.STREAM_JSON,
        build_turn_argv=lambda context: ([sys.executable, "-u", str(model), str(effects)], {"cwd": context.working_dir}),
    ))
    spawn = asyncio.create_subprocess_exec
    processes = []

    async def hold_spawn(*args, **kwargs):
        process = await spawn(*args, **kwargs)
        if str(model) in args:
            processes.append(process)
            born.set()
            await release.wait()
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", hold_spawn)
    receipt = await manager.start_message(session.id, "Keep this input from a revoked process")
    try:
        await asyncio.wait_for(born.wait(), 3)
        assert len(processes) == 1 and processes[0].returncode is None
        deleted = await client.delete(f"/api/credentials/{credential_ids[0]}")
        assert deleted.status_code == 204, deleted.text
        release.set()
        completion = await asyncio.wait_for(receipt.wait(), 5)
        assert completion.outcome.status == "failed"
        assert "credential" in completion.outcome.message.lower()
        assert not effects.exists()
        assert await asyncio.wait_for(processes[0].wait(), 3) == 0
    finally:
        release.set()


async def test_failed_credential_delete_does_not_unbind_session_defaults(configuration_host):
    client, manager, session, credential_ids, _effects = configuration_host
    patched = await client.patch(f"/api/sessions/{session.id}", json={"credential_id": credential_ids[0]})
    assert patched.status_code == 200, patched.text
    async with manager.db.transaction(write=True) as tx:
        await tx.execute("CREATE TRIGGER reject_delete BEFORE DELETE ON backend_credentials "
                         "BEGIN SELECT RAISE(ABORT, 'credential delete rejected'); END")
    with pytest.raises(sqlite3.IntegrityError, match="credential delete rejected"):
        await client.delete(f"/api/credentials/{credential_ids[0]}")
    detail = await client.get(f"/api/sessions/{session.id}")
    assert detail.json()["credential_id"] == credential_ids[0]
    assert (await manager.db.get_credential(credential_ids[0])) is not None
    assert next(row for row in await manager.db.load_sessions() if row["id"] == session.id)["credential_id"] == credential_ids[0]


async def test_backend_default_update_keeps_current_session_backend_and_changes_new_sessions(configuration_host, monkeypatch):
    client, manager, session, credential_ids, effects = configuration_host
    captured, release = asyncio.Event(), asyncio.Event()
    read = manager.db.get_enabled_connectors_for_agent

    async def hold_assembly(agent_id):
        rows = await read(agent_id)
        captured.set()
        await release.wait()
        return rows

    monkeypatch.setattr(manager.db, "get_enabled_connectors_for_agent", hold_assembly)
    receipt = await manager.start_message(session.id, "Keep the current backend")
    try:
        await asyncio.wait_for(captured.wait(), 3)
        patched = await client.patch(f"/api/agents/{session.agent_id}", json={
            "backend": "claude-code", "credential_id": None,
            "model": None, "reasoning_effort": None, "service_tier": None,
        })
        assert patched.status_code == 200, patched.text
        created = await client.post("/api/sessions", json={"agent_id": session.agent_id, "name": "New backend default"})
        assert created.status_code == 201, created.text
        assert created.json()["backend"] == "claude-code"
        release.set()
        assert (await asyncio.wait_for(receipt.wait(), 5)).outcome.status == "success"
        assert json.loads(effects.read_text())["credential"] == credential_ids[0]
        detail = await client.get(f"/api/sessions/{session.id}")
        assert detail.json()["backend"] == "codex"
    finally:
        release.set()


@pytest.mark.parametrize("delete_previous", [False, True])
@pytest.mark.parametrize("credential_style", ["home_dir", "env_secret"])
async def test_next_request_uses_the_new_credential_instead_of_a_held_process(configuration_host, tmp_path, monkeypatch, delete_previous, credential_style):
    client, manager, session, credential_ids, effects = configuration_host
    model = tmp_path / "held_configuration_model.py"
    model.write_text(
        "import json,os,sys\nfrom pathlib import Path\n"
        "for line in sys.stdin:\n"
        " with Path(sys.argv[1]).open('a') as out:\n"
        "  out.write(json.dumps({'credential':sys.argv[2],'pid':os.getpid()})+'\\n')\n"
        " print('{\"type\":\"turn.completed\",\"usage\":{}}',flush=True)\n"
    )
    harness = get_harness("codex")
    monkeypatch.setattr(harness, "profile", replace(
        harness.profile, stdin_mode=StdinMode.STREAM_JSON, credential_style=credential_style,
        build_turn_argv=lambda context: (
            [sys.executable, "-u", str(model), str(effects),
             Path(context.credential.home_dir).name if context.credential.home_dir else context.credential.secret],
            {"cwd": context.working_dir},
        ),
    ))
    first = await manager.start_message(session.id, "Use the original credential")
    assert (await asyncio.wait_for(first.wait(), 5)).outcome.status == "success"
    before = json.loads(effects.read_text())
    expected = credential_ids if credential_style == "home_dir" else ["test-only", "test-only"]
    assert before["credential"] == expected[0] and Path(f"/proc/{before['pid']}").exists()
    patched = await client.patch(f"/api/sessions/{session.id}", json={"credential_id": credential_ids[1]})
    assert patched.status_code == 200, patched.text
    if delete_previous:
        deleted = await client.delete(f"/api/credentials/{credential_ids[0]}")
        assert deleted.status_code == 204, deleted.text
    second = await manager.start_message(session.id, "Use the new credential")
    completion = await asyncio.wait_for(second.wait(), 5)
    assert completion.outcome.status == "success", completion
    observed = [json.loads(line) for line in effects.read_text().splitlines()]
    assert [row["credential"] for row in observed] == expected
    assert observed[0]["pid"] != observed[1]["pid"]
    assert not Path(f"/proc/{before['pid']}").exists(), "The previous credential's process was not retired"
