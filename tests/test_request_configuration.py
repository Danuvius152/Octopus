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
from server.harness.claude_code import ClaudeDriver
from server.harness.driver import ArgvDriver
from server.harness.model_settings import ModelSettings
from server.routers import agents, credentials, sessions
from server.sessions import SessionManager
from tests.accounts import create_account
from tests.harness_profiles import process_profile


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
    monkeypatch.setattr(harness, "profile", process_profile(
        harness.profile, binary=sys.executable, build_turn_argv=argv,
        activity_observer=None, prepare_resume=None, new_driver=ArgvDriver,
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


async def test_request_captures_session_and_agent_after_coordinated_updates(configuration_host, monkeypatch):
    client, manager, session, credential_ids, effects = configuration_host
    response = await client.patch(f"/api/sessions/{session.id}", json={"model": "session-old"})
    assert response.status_code == 200, response.text
    input_recorded = asyncio.Event()

    async def observe_input(event):
        if event.get("session_id") == session.id and event.get("type") == "user_message":
            input_recorded.set()

    manager.on_broadcast("configuration-input", observe_input)
    waiting = asyncio.Queue()
    acquire = manager.configuration_lock.acquire

    async def observe_acquire():
        waiting.put_nowait(None)
        return await acquire()

    # Order real HTTP updates before capture, then release the lock before
    # awaiting the Owner. Configuration writes now use that same Owner.
    async with manager.configuration_lock:
        monkeypatch.setattr(manager.configuration_lock, "acquire", observe_acquire)
        session_update = asyncio.create_task(client.patch(f"/api/sessions/{session.id}", json={"model": "session-new"}))
        await asyncio.wait_for(waiting.get(), 1)
        agent_update = asyncio.create_task(client.patch(f"/api/agents/{session.agent_id}", json={
            "reasoning_effort": "high", "service_tier": "fast", "credential_id": credential_ids[1],
        }))
        await asyncio.wait_for(waiting.get(), 1)
        submission = asyncio.create_task(manager.start_message(session.id, "Use a consistent configuration"))
    try:
        receipt = await asyncio.wait_for(submission, 3)
        await asyncio.wait_for(input_recorded.wait(), 3)
        replies = await asyncio.wait_for(asyncio.gather(session_update, agent_update), 3)
        assert all(reply.status_code == 200 for reply in replies), [reply.text for reply in replies]
        assert (await asyncio.wait_for(receipt.wait(), 5)).outcome.status == "success"
        assert [json.loads(line) for line in effects.read_text().splitlines()] == [{
            "model": "session-new", "effort": "high", "tier": "fast", "credential": credential_ids[1],
        }]
    finally:
        manager.remove_broadcast("configuration-input")


async def test_default_save_operation_waits_for_the_configuration_capture_boundary(configuration_host, monkeypatch):
    _client, manager, session, _credential_ids, _effects = configuration_host
    saving = asyncio.Event()
    lock = manager.configuration_lock
    acquire = lock.acquire

    async def observe_acquire():
        saving.set()
        return await acquire()

    async with lock:
        monkeypatch.setattr(lock, "acquire", observe_acquire)
        operation = asyncio.create_task(AgentManager(manager.db).update_agent(session.agent_id, model="saved-together"))
        await asyncio.wait_for(saving.wait(), 1)
        # Observe the public saved definition while capture owns the boundary.
        assert not operation.done()
        assert (await manager.db.get_agent(session.agent_id))["model"] == "agent-old"
    assert (await operation)["model"] == "saved-together"


async def test_default_changes_after_capture_do_not_change_retry_configuration(configuration_host, tmp_path, monkeypatch):
    from server.config import settings

    client, manager, session, credential_ids, effects = configuration_host
    monkeypatch.setattr(settings, "turn_idle_max_retries", 0)
    monkeypatch.setattr(manager, "_TRANSIENT_RETRY_BASE_DELAY", 0)
    script = tmp_path / "configuration_model.py"
    defaults_saved = tmp_path / "defaults-saved"
    script.write_text(
        "import sys,time\nfrom pathlib import Path\n"
        "path=Path(sys.argv[1])\n"
        "with path.open('a') as out: out.write(sys.argv[2]+'\\n')\n"
        "print('{\"type\":\"thread.started\",\"thread_id\":\"captured-thread\"}',flush=True)\n"
        "if len(path.read_text().splitlines()) == 1:\n"
        " print('{\"type\":\"item.completed\",\"item\":{\"type\":\"agent_message\",\"text\":\"Partial work\"}}',flush=True)\n"
        f" while not Path({str(defaults_saved)!r}).exists(): time.sleep(.005)\n"
        " print('{\"type\":\"turn.failed\",\"error\":{\"message\":\"server overloaded\",\"code\":\"server_overloaded\"}}',flush=True)\n"
        "else: print('{\"type\":\"turn.completed\",\"usage\":{}}',flush=True)\n"
    )
    changed = asyncio.Event()

    async def change_defaults(event):
        if event.get("type") == "assistant_text" and not changed.is_set():
            changed.set()
            response = await client.patch(f"/api/agents/{session.agent_id}", json={
                "model": "agent-new", "reasoning_effort": "high", "service_tier": "fast", "credential_id": credential_ids[1],
            })
            assert response.status_code == 200, response.text
            defaults_saved.touch()

    manager.on_broadcast("change-before-retry", change_defaults)
    try:
        receipt = await manager.start_message(session.id, "Keep this request's captured configuration")
        result = await asyncio.wait_for(receipt.wait(), 5)
        assert result.outcome.status == "success" and changed.is_set()
        assert result.outcome.recovery["transient"] == 1
        assert [json.loads(line) for line in effects.read_text().splitlines()] == [
            {"model": "agent-old", "effort": "low", "tier": "default", "credential": credential_ids[0]},
            {"model": "agent-old", "effort": "low", "tier": "default", "credential": credential_ids[0]},
        ]
    finally:
        manager.remove_broadcast("change-before-retry")


async def test_default_updates_after_capture_apply_to_the_queued_request(configuration_host, monkeypatch):
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
        second = await manager.start_message(session.id, "Use the new defaults")
        updated = await client.patch(f"/api/sessions/{session.id}", json={"model": "session-new"})
        assert updated.status_code == 200, updated.text
        updated = await client.patch(f"/api/agents/{session.agent_id}", json={
            "model": "agent-new", "reasoning_effort": "high", "service_tier": "fast", "credential_id": credential_ids[1],
        })
        assert updated.status_code == 200, updated.text
        release.set()
        assert (await asyncio.wait_for(first.wait(), 5)).outcome.status == "success"
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
    monkeypatch.setattr(harness, "profile", process_profile(
        harness.profile, new_driver=ClaudeDriver, can_steer=True, reusable=True,
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


@pytest.mark.parametrize('setting', ['credential', 'tool_policy'])
async def test_backend_default_change_preserves_old_session_and_explains_its_inherited_failure(
    configuration_host, setting,
):
    client, manager, session, credential_ids, effects = configuration_host
    candidate = {'backend': 'claude-code', 'credential_id': None, 'model': 'new-default-model',
                 'reasoning_effort': 'low', 'service_tier': None}
    if setting == 'credential':
        credential = await client.post('/api/credentials', json={
            'backend': 'claude-code', 'label': 'New backend credential', 'secret': 'test-only',
        })
        assert credential.status_code == 201, credential.text
        candidate['credential_id'] = credential.json()['id']
    else:
        candidate['tool_deny'] = 'Write'
    agent_path = f'/api/agents/{session.agent_id}'
    saved = await client.patch(agent_path, json=candidate)
    assert saved.status_code == 200, saved.text
    fresh = await client.post('/api/sessions', json={'agent_id': session.agent_id})
    assert fresh.status_code == 201 and fresh.json()['backend'] == 'claude-code'
    receipt = await manager.start_message(session.id, 'Keep the old backend and check its actual configuration')
    failed = await asyncio.wait_for(receipt.wait(), 5)
    assert failed.outcome.status == 'failed' and not effects.exists()
    assert 'codex' in failed.outcome.message
    assert ('credential_id' if setting == 'credential' else 'tool_deny') in failed.outcome.message
    assert 'Agent settings' in failed.outcome.message
    if setting == 'credential':
        repaired = await client.patch(f'/api/sessions/{session.id}', json={'credential_id': credential_ids[0]})
    else:
        repaired = await client.patch(agent_path, json={'tool_deny': ''})
    assert repaired.status_code == 200, repaired.text
    receipt = await manager.start_message(session.id, 'Continue this same conversation after correction')
    completion = await asyncio.wait_for(receipt.wait(), 5)
    assert completion.outcome.status == 'success', completion.outcome.message
    detail = (await client.get(f'/api/sessions/{session.id}')).json()
    assert detail['backend'] == 'codex'
    assert [m['content'] for m in detail['messages'] if m['role'] == 'user'] == [
        'Keep the old backend and check its actual configuration', 'Continue this same conversation after correction',
    ]
    assert json.loads(effects.read_text())['credential'] == (credential_ids[0] if setting == 'credential' else None)


async def test_old_session_repairs_inherited_credential_and_effort_as_one_atomic_candidate(configuration_host):
    client, manager, session, credential_ids, effects = configuration_host
    credential = await client.post('/api/credentials', json={
        'backend': 'claude-code', 'label': 'New Claude default', 'secret': 'test-only',
    })
    assert credential.status_code == 201, credential.text
    saved = await client.patch(f'/api/agents/{session.agent_id}', json={
        'backend': 'claude-code', 'model': 'new-default-model', 'reasoning_effort': 'max',
        'service_tier': None, 'credential_id': credential.json()['id'],
    })
    assert saved.status_code == 200, saved.text
    path = f'/api/sessions/{session.id}'
    before = (await client.get(path)).json()
    rejected = await client.patch(path, json={'credential_id': credential_ids[0]})
    assert rejected.status_code == 400 and 'reasoning_effort' in rejected.json()['detail']
    rejected = await client.patch(path, json={'reasoning_effort': 'low'})
    assert rejected.status_code == 400 and 'Credential backend' in rejected.json()['detail']
    rejected = await client.patch(path, json={'credential_id': credential_ids[0], 'reasoning_effort': 'max'})
    assert rejected.status_code == 400 and 'reasoning_effort' in rejected.json()['detail']
    unchanged = (await client.get(path)).json()
    for field in ('credential_id', 'reasoning_effort', 'model', 'backend', 'claude_session_id', 'messages'):
        assert unchanged[field] == before[field]
    corrected = await client.patch(path, json={'credential_id': credential_ids[0], 'reasoning_effort': 'low'})
    assert corrected.status_code == 200, corrected.text
    receipt = await manager.start_message(session.id, 'Continue the same history after the combined correction')
    completion = await asyncio.wait_for(receipt.wait(), 5)
    assert completion.outcome.status == 'success', completion.outcome.message
    assert manager.get_session(session.id).backend == 'codex'
    assert json.loads(effects.read_text()) == {
        'model': 'new-default-model', 'effort': 'low', 'tier': None, 'credential': credential_ids[0],
    }


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
    monkeypatch.setattr(harness, "profile", process_profile(
        harness.profile, new_driver=ClaudeDriver, can_steer=True, reusable=True, credential_style=credential_style,
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
