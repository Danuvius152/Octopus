"""Reasoning overrides change the next invocation and preserve its conversation."""

import asyncio
import json
import sys
from dataclasses import asdict, replace
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from server import deps
from server.agent_manager import AgentManager
from server.database import Database
from server.harness import RunConfig, get_harness
from server.harness.codex import read_model_settings
from server.harness.events import HarnessCredential
from server.harness.model_settings import ModelSettings
from server.harness.profile import OneShotContext
from server.main import app
from server.routers import agents
from server.session_manager import SessionManager, session_manager
from server.users import UserManager
from tests.harness_profiles import native_submission, process_profile
from tests.test_codex_app_server_questions import wire_frames
from tests.test_codex_app_server_submission import native_runtime as native_runtime


@pytest.fixture
def catalog_home(tmp_path, monkeypatch):
    home = tmp_path / "codex"
    home.mkdir()
    (home / "config.toml").write_text('model = "model-a"\nmodel_reasoning_effort = "max"\nsecret = "DO_NOT_EXPOSE"\n')
    (home / "models_cache.json").write_text(json.dumps({"models": [
        {"slug": "model-a", "display_name": "Model A", "visibility": "list",
         "supported_reasoning_levels": [{"effort": e} for e in ("low", "high", "max", "ultra")],
         "default_reasoning_level": "high",
         "service_tiers": [{"id": "priority", "name": "Fast"}, {"id": "ultrafast"}]},
        {"slug": "model-b", "display_name": "Model B", "visibility": "list",
         "supported_reasoning_levels": [{"effort": e} for e in ("low", "medium")],
         "default_reasoning_level": "medium"},
        {"slug": "hidden", "visibility": "hide"},
    ]}))
    monkeypatch.setenv("CODEX_HOME", str(home))
    return home


def test_catalog_uses_cli_metadata_without_exposing_config(catalog_home, tmp_path):
    catalog = read_model_settings()
    assert [m.id for m in catalog.models] == ["model-a", "model-b"]
    assert catalog.default_model == "model-a"
    assert catalog.default_reasoning_effort == "max"
    assert catalog.efforts_for("model-b") == ["low", "medium"]
    assert "ultra" in catalog.efforts_for("model-a")
    assert catalog.default_service_tier == "default"
    assert catalog.tiers_for("model-a") == ["default", "fast"]
    assert catalog.tiers_for("model-b") == ["default"]
    assert catalog.tiers_for("hidden") == ["default"]
    assert "hidden" not in json.dumps(asdict(catalog))
    assert "DO_NOT_EXPOSE" not in json.dumps(asdict(catalog))

    other = tmp_path / "credential"
    other.mkdir()
    (other / "config.toml").write_text('model = "other-model"\nmodel_reasoning_effort = "low"\n')
    credential = HarnessCredential(backend="codex", auth_type="oauth", home_dir=str(other))
    assert read_model_settings(credential).default_model == "other-model"


@pytest.mark.parametrize("metadata", [
    {"service_tiers": [{"id": "fast"}]},
    {"additional_speed_tiers": ["fast"]},
    {"service_tiers": None, "additional_speed_tiers": ["fast"]},
])
def test_fast_metadata_and_normal_default_override_cli_preferences(catalog_home, metadata):
    (catalog_home / "config.toml").write_text('model = "model-a"\nservice_tier = "fast"\n')
    (catalog_home / "models_cache.json").write_text(json.dumps({"models": [
        {"slug": "model-a", "visibility": "list", **metadata},
    ]}))
    catalog = read_model_settings()
    assert catalog.default_service_tier == "default"
    assert catalog.tiers_for("model-a") == ["default", "fast"]


@pytest.mark.parametrize("cache", ["invalid json", "[]", '{"models":null}', '{"models":[null,{}]}'])
def test_unavailable_catalog_keeps_basic_controls(catalog_home, cache):
    (catalog_home / "models_cache.json").write_text(cache)
    assert read_model_settings().reasoning_efforts == ["low", "medium", "high", "xhigh"]


async def native_rounds(tmp_path, **options):
    (tmp_path / "native-defaults.json").write_text(json.dumps({"model": "model-a", "effort": "high"}))
    identity = None
    for index in range(2):
        run = get_harness("codex").create_run(RunConfig(mcp_servers=[], **options))
        proofs = native_submission(run, f"settings-{index}", f"settings-attempt-{index}")
        try:
            await run.start("continue", str(tmp_path), identity)
            events = [event async for event in run.stream()]
            result, = [event for event in events if event.type == "result"]
            assert not result.is_error
            identity = result.session_id
            assert any(proof.state == "accepted" for proof in proofs)
        finally:
            await run.stop()
    return [frame for frame in wire_frames(tmp_path) if frame.get("method") in {"thread/start", "thread/resume"}]


async def test_effort_reaches_new_resumed_native_requests_and_oneshot(catalog_home, native_runtime, tmp_path):
    harness = get_harness("codex")
    run = harness.create_run(RunConfig(model="model-a", reasoning_effort="high", mcp_servers=[]))
    frames = await native_rounds(tmp_path, model="model-a", reasoning_effort="high")
    assert [frame["method"] for frame in frames] == ["thread/start", "thread/resume"]
    assert all(frame["params"]["model"] == "model-a" and
               frame["params"]["config"]["model_reasoning_effort"] == "high" for frame in frames)
    assert frames[1]["params"]["threadId"] == "exact-native-thread"
    ctx = OneShotContext(prompt="hello", reasoning_effort="low")
    argv, _ = harness.profile.build_oneshot_argv(ctx)
    assert argv[argv.index("--effort") + 1] == "low"
    inherited = harness.create_run(RunConfig(mcp_servers=[]))
    assert inherited._config.reasoning_effort is None
    changed = harness.create_run(RunConfig(model="model-a", reasoning_effort="low", mcp_servers=[]))
    assert changed.spawn_signature(str(tmp_path), None) != run.spawn_signature(str(tmp_path), None)


@pytest.mark.parametrize("tier, rendered", [(None, "default"), ("default", "default"), ("fast", "fast")])
async def test_speed_reaches_new_resumed_native_requests_and_oneshot(catalog_home, native_runtime, tmp_path, tier, rendered):
    harness = get_harness("codex")
    run = harness.create_run(RunConfig(service_tier=tier, mcp_servers=[]))
    frames = await native_rounds(tmp_path, service_tier=tier)
    assert [frame["method"] for frame in frames] == ["thread/start", "thread/resume"]
    assert all(frame["params"]["config"]["service_tier"] == rendered and
               frame["params"]["config"]["features.fast_mode"] is True for frame in frames)
    argv, _ = harness.profile.build_oneshot_argv(OneShotContext(prompt="hello", service_tier=tier))
    assert argv[argv.index("--tier") + 1] == rendered
    other = harness.create_run(RunConfig(service_tier="default" if tier == "fast" else "fast", mcp_servers=[]))
    assert other.spawn_signature(str(tmp_path), None) != run.spawn_signature(str(tmp_path), None)


@pytest.fixture
async def client(catalog_home):
    db = Database(":memory:")
    await db.initialize()
    users = UserManager(db)
    user = await users.create_user(username="models-owner", password="password-for-models")
    deps.set_user_manager(users)
    headers = {"Authorization": "Bearer " + await users.issue_token(user["id"])}
    session_manager.sessions.clear()
    session_manager._admission_closed = False
    await session_manager.initialize(db)
    agents.set_manager(AgentManager(db))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=headers) as client:
        yield client
    await db.close()
    session_manager.sessions.clear()
    session_manager._admission_closed = False


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["codex", "claude-code"])
async def test_independent_overrides_preserve_history_resume_and_reload(client, backend):
    response = await client.post("/api/agents", headers=client.headers, json={
        "name": "Reasoner", "backend": backend, "model": "model-a", "reasoning_effort": "low",
    })
    assert response.status_code == 201, response.text
    agent = response.json()
    response = await client.post("/api/sessions/import", headers=client.headers, json={
        "name": "Existing conversation", "agent_id": agent["id"], "backend": backend,
        "claude_session_id": "original-thread", "messages": [
            {"role": "user", "type": "text", "content": "Remember MARIGOLD"},
            {"role": "assistant", "type": "text", "content": "OK"},
        ],
    })
    assert response.status_code == 201, response.text
    session_id = response.json()["id"]
    path = f"/api/sessions/{session_id}"
    before = (await client.get(path, headers=client.headers)).json()
    current = session_manager.get_session(session_id)
    first_run = session_manager._make_run(current, agent)
    assert first_run._config.reasoning_effort == "low"

    response = await client.patch(path, headers=client.headers, json={"reasoning_effort": "high"})
    assert response.status_code == 200, response.text
    assert response.json()["model"] is None
    assert response.json()["reasoning_effort"] == "high"
    assert first_run._config.reasoning_effort == "low", "an invocation already built keeps its setting"
    assert session_manager._make_run(current, agent)._config.reasoning_effort == "high"
    assert (await client.get(f"/api/agents/{agent['id']}", headers=client.headers)).json()["reasoning_effort"] == "low"

    after = (await client.get(path, headers=client.headers)).json()
    assert after["messages"] == before["messages"]
    assert after["claude_session_id"] == "original-thread"
    restored = SessionManager()
    await restored.initialize(session_manager.db)
    assert restored.get_session(session_id).reasoning_effort == "high"
    assert restored.get_session(session_id).claude_session_id == "original-thread"

    if backend == "codex":
        rejected = await client.patch(path, headers=client.headers, json={"model": "model-b"})
        assert rejected.status_code == 400
        assert current.model is None
    valid = await client.patch(path, headers=client.headers, json={"model": "model-b", "reasoning_effort": "medium"})
    assert valid.status_code == 200
    cleared = await client.patch(path, headers=client.headers, json={"reasoning_effort": None})
    assert cleared.status_code == 200
    assert cleared.json()["model"] == "model-b"
    assert session_manager._make_run(current, agent)._config.reasoning_effort == "low"

    await session_manager.archive_session(session_id, replace=False)
    archived = (await client.get(path, headers=client.headers)).json()
    assert archived["model"] == "model-b"
    assert archived["messages"] == before["messages"]
    unarchived = await session_manager.unarchive_session(session_id)
    assert unarchived.model == "model-b"
    assert unarchived.claude_session_id == "original-thread"


@pytest.mark.asyncio
async def test_invalid_effort_and_backend_are_rejected(client):
    for backend, effort in (("codex", "typo"), ("claude-code", "ultra")):
        response = await client.post("/api/agents", headers=client.headers, json={
            "name": "Invalid", "backend": backend, "reasoning_effort": effort,
        })
        assert response.status_code == 400
    response = await client.get("/api/backends/codex/models", headers=client.headers)
    assert response.status_code == 200
    assert response.json()["default_reasoning_effort"] == "max"
    assert "DO_NOT_EXPOSE" not in response.text
    response = await client.get("/api/backends/codex/models?credential_id=missing", headers=client.headers)
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_speed_inheritance_clear_reload_and_history(client):
    response = await client.post("/api/agents", headers=client.headers, json={
        "name": "Fast agent", "backend": "codex", "model": "model-a", "service_tier": "fast",
    })
    assert response.status_code == 201, response.text
    agent = response.json()
    response = await client.post("/api/sessions/import", headers=client.headers, json={
        "name": "Speed conversation", "agent_id": agent["id"], "backend": "codex",
        "claude_session_id": "original-thread", "messages": [
            {"role": "user", "type": "text", "content": "Remember MARIGOLD"},
        ],
    })
    assert response.status_code == 201, response.text
    sid = response.json()["id"]
    path = f"/api/sessions/{sid}"
    before = (await client.get(path, headers=client.headers)).json()
    current = session_manager.get_session(sid)
    original_run = session_manager._make_run(current, agent)
    assert original_run._config.service_tier == "fast"
    changed = await client.patch(path, headers=client.headers, json={"service_tier": "default"})
    assert changed.status_code == 200, changed.text
    assert changed.json()["model"] is None
    assert changed.json()["reasoning_effort"] is None
    assert original_run._config.service_tier == "fast"
    assert session_manager._make_run(current, agent)._config.service_tier == "default"
    restored = SessionManager()
    await restored.initialize(session_manager.db)
    assert restored.get_session(sid).service_tier == "default"
    assert restored.get_session(sid).claude_session_id == "original-thread"
    cleared = await client.patch(path, headers=client.headers, json={"service_tier": None})
    assert cleared.status_code == 200
    assert cleared.json()["service_tier"] is None
    assert session_manager._make_run(current, agent)._config.service_tier == "fast"
    after = (await client.get(path, headers=client.headers)).json()
    assert after["messages"] == before["messages"]
    assert after["claude_session_id"] == "original-thread"
    # Clearing the durable default returns existing conversations to Normal.
    changed = await client.patch(f"/api/agents/{agent['id']}", headers=client.headers, json={"service_tier": None})
    assert changed.status_code == 200
    assert changed.json()["service_tier"] is None
    assert session_manager._make_run(current, changed.json())._config.service_tier is None


@pytest.mark.asyncio
async def test_speed_validation_is_atomic_and_model_specific(client):
    response = await client.post("/api/agents", headers=client.headers, json={
        "name": "Normal agent", "backend": "codex", "model": "model-a",
    })
    assert response.status_code == 201
    agent = response.json()
    assert agent["service_tier"] is None
    response = await client.post(f"/api/agents/{agent['id']}/sessions", headers=client.headers, json={})
    sid = response.json()["id"]
    path = f"/api/sessions/{sid}"
    assert (await client.patch(path, headers=client.headers, json={"service_tier": "fast"})).status_code == 200
    rejected = await client.patch(path, headers=client.headers, json={"model": "model-b"})
    assert rejected.status_code == 400
    current = session_manager.get_session(sid)
    assert current.model is None and current.service_tier == "fast"
    rejected = await client.patch(path, headers=client.headers, json={"model": "hidden"})
    assert rejected.status_code == 400
    changed = await client.patch(path, headers=client.headers, json={"model": "model-b", "service_tier": "default"})
    assert changed.status_code == 200
    assert (await client.patch(path, headers=client.headers, json={"service_tier": "ultrafast"})).status_code == 422
    for backend, model in (("claude-code", None), ("codex", "model-b")):
        rejected = await client.post("/api/agents", headers=client.headers, json={
            "name": "Unsupported", "backend": backend, "model": model, "service_tier": "fast",
        })
        assert rejected.status_code == 400
    rejected = await client.patch(f"/api/agents/{agent['id']}", headers=client.headers, json={
        "model": "model-b", "service_tier": "fast",
    })
    assert rejected.status_code == 400
    assert (await client.get(f"/api/agents/{agent['id']}", headers=client.headers)).json()["model"] == "model-a"


@pytest.mark.asyncio
@pytest.mark.parametrize("entity", ["agents", "sessions"])
async def test_concurrent_model_and_speed_updates_validate_the_committed_state(client, monkeypatch, entity):
    from server.harness import model_settings

    response = await client.post("/api/agents", headers=client.headers, json={
        "name": "Concurrent", "backend": "codex", "model": "model-a",
    })
    agent_id = response.json()["id"]
    path = f"/api/agents/{agent_id}"
    if entity == "sessions":
        response = await client.post(f"{path}/sessions", headers=client.headers, json={})
        path = f"/api/sessions/{response.json()['id']}"
    original = model_settings.model_settings_for

    async def delayed_catalog(*args, **kwargs):
        result = await original(*args, **kwargs)
        await asyncio.sleep(0.02)
        return result

    monkeypatch.setattr(model_settings, "model_settings_for", delayed_catalog)
    results = await asyncio.gather(
        client.patch(path, headers=client.headers, json={"model": "model-b"}),
        client.patch(path, headers=client.headers, json={"service_tier": "fast"}),
    )
    assert sorted(r.status_code for r in results) == [200, 400]
    saved = (await client.get(path, headers=client.headers)).json()
    assert not (saved["model"] == "model-b" and saved["service_tier"] == "fast")


@pytest.mark.parametrize('field,new_default,correction', [
    ('reasoning_effort', 'high', 'medium'), ('service_tier', 'fast', 'default'),
])
@pytest.mark.parametrize('archived', [False, True])
async def test_legal_defaults_save_before_an_old_session_is_repaired_and_resumed(
    client, monkeypatch, tmp_path, field, new_default, correction, archived,
):
    effects = tmp_path / 'continued-settings.jsonl'
    script = tmp_path / 'resume-configuration.py'
    script.write_text(
        "import json,sys\nfrom pathlib import Path\nrecord=json.loads(sys.argv[2])\n"
        "with Path(sys.argv[1]).open('a') as out: out.write(json.dumps(record)+'\\n')\n"
        "print(json.dumps({'type':'thread.started','thread_id':'original-thread'}),flush=True)\n"
        "print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':'Continued'}}),flush=True)\n"
        "print(json.dumps({'type':'turn.completed','usage':{}}),flush=True)\n"
    )
    harness = get_harness('codex')

    def controlled_process(context):
        captured = {'model': context.model, 'resume': context.resume_id,
                    'effort': context.reasoning_effort, 'tier': context.service_tier or 'default'}
        return [sys.executable, '-u', str(script), str(effects), json.dumps(captured)], {'cwd': context.working_dir}

    monkeypatch.setattr(harness, 'profile', process_profile(
        harness.profile, binary=sys.executable, build_turn_argv=controlled_process,
        activity_observer=None, prepare_resume=None,
    ))
    response = await client.post('/api/agents', json={
        'name': 'Legal defaults', 'backend': 'codex', 'model': 'model-a', 'reasoning_effort': 'low',
    })
    assert response.status_code == 201, response.text
    agent_path = f"/api/agents/{response.json()['id']}"
    response = await client.post('/api/sessions/import', json={
        'agent_id': response.json()['id'], 'backend': 'codex', 'name': 'Original history',
        'claude_session_id': 'original-thread',
        'messages': [{'role': 'user', 'type': 'text', 'content': 'Remember MARIGOLD'}],
    })
    assert response.status_code == 201, response.text
    sid = response.json()['id']
    session_path = f'/api/sessions/{sid}'
    assert (await client.patch(session_path, json={'model': 'model-b'})).status_code == 200
    before = (await client.get(session_path)).json()
    if archived:
        await session_manager.archive_session(sid, replace=False)
    saved = await client.patch(agent_path, json={field: new_default})
    assert saved.status_code == 200, saved.text
    assert saved.json()[field] == new_default
    if archived:
        await session_manager.unarchive_session(sid)
    receipt = await session_manager.start_message(sid, 'Try the new inherited default')
    failed = await asyncio.wait_for(receipt.wait(), 5)
    assert failed.outcome.status == 'failed' and not effects.exists()
    assert 'codex' in failed.outcome.message and f'{field} from Agent default' in failed.outcome.message
    assert 'Agent settings' in failed.outcome.message
    after = (await client.get(session_path)).json()
    assert after['backend'] == 'codex' and after['claude_session_id'] == 'original-thread'
    assert after['model'] == 'model-b' and after[field] is None
    assert after['messages'][:len(before['messages'])] == before['messages']
    repaired = await client.patch(session_path, json={field: correction})
    assert repaired.status_code == 200, repaired.text
    receipt = await session_manager.start_message(sid, 'Continue the original history after correction')
    succeeded = await asyncio.wait_for(receipt.wait(), 5)
    assert succeeded.outcome.status == 'success', succeeded.outcome.message
    assert [json.loads(line) for line in effects.read_text().splitlines()] == [{
        'model': 'model-b', 'resume': 'original-thread',
        'effort': 'medium' if field == 'reasoning_effort' else 'low',
        'tier': 'default',
    }]


@pytest.mark.asyncio
async def test_invocation_rejects_unavailable_inherited_speed_before_spawning(client, monkeypatch):
    agent = await session_manager.db.get_system_agent()
    assert agent is not None
    response = await client.post("/api/sessions", headers=client.headers, json={"agent_id": agent["id"], "backend": "codex"})
    session = session_manager.get_session(response.json()["id"])
    assert (await client.patch(
        f"/api/sessions/{session.id}", headers=client.headers, json={"model": "model-b"},
    )).status_code == 200
    await session_manager.db.update_agent(session.agent_id, service_tier="fast")

    def unexpected_run(*args, **kwargs):
        pytest.fail("an invalid effective model/speed combination must not spawn a runtime")

    monkeypatch.setattr(session_manager, "_make_run", unexpected_run)
    receipt = await session_manager.start_message(session.id, "hello")
    assert receipt.accepted
    completion = await asyncio.wait_for(receipt.wait(), 5)
    assert completion.outcome.status == "failed"
    assert "service_tier from Agent default" in completion.outcome.message
    assert "model-b" in completion.outcome.message
    messages = await session_manager.db.load_messages(session.id)
    assert [row["content"] for row in messages if row["role"] == "user"] == ["hello"]
    assert len([row for row in messages if row["type"] == "result"]) == 1


@pytest.mark.parametrize('backend,field,value', [
    ('codex', 'reasoning_effort', 'low'),
    ('claude-code', 'reasoning_effort', 'low'),
    ('codex', 'service_tier', 'fast'),
])
@pytest.mark.parametrize('source', ['Agent default', 'session override'])
async def test_empty_capability_rejects_saved_controls_before_spawn(client, monkeypatch, backend, field, value, source):
    created = await client.post('/api/agents', json={
        'name': 'Original configuration', 'backend': backend, 'model': 'model-a',
        **({field: value} if source == 'Agent default' else {}),
    })
    assert created.status_code == 201, created.text
    agent_path = f"/api/agents/{created.json()['id']}"
    created = await client.post(f'{agent_path}/sessions', json={})
    assert created.status_code == 201, created.text
    session_path = f"/api/sessions/{created.json()['id']}"
    if source == 'session override':
        assert (await client.patch(session_path, json={field: value})).status_code == 200
    harness = get_harness(backend)
    monkeypatch.setattr(harness, 'profile', replace(
        harness.profile, binary=sys.executable, model_settings=lambda credential: ModelSettings(),
    ))
    edit_path = agent_path if source == 'Agent default' else session_path
    before = (await client.get(edit_path)).json()
    rejected = await client.patch(edit_path, json={field: value, 'name': 'Must not be saved'})
    assert rejected.status_code == 400, rejected.text
    assert (await client.get(edit_path)).json()['name'] == before['name']
    spawned = []
    spawn = asyncio.create_subprocess_exec

    async def forbidden_spawn(*argv, **kwargs):
        if Path(argv[0]).name == 'git':
            return await spawn(*argv, **kwargs)
        spawned.append(argv)
        raise RuntimeError('Unsupported controls reached process spawn')

    monkeypatch.setattr(asyncio, 'create_subprocess_exec', forbidden_spawn)
    receipt = await session_manager.start_message(created.json()['id'], 'Validate the actual backend before spawning')
    completion = await asyncio.wait_for(receipt.wait(), 5)
    assert completion.outcome.status == 'failed'
    assert spawned == []
    assert backend in completion.outcome.message and field in completion.outcome.message
    assert source in completion.outcome.message
    assert ('Agent settings' if source == 'Agent default' else '/model') in completion.outcome.message
    assert 'does not offer' in completion.outcome.message


@pytest.mark.parametrize('backend', ['codex', 'claude-code'])
async def test_saved_custom_model_controls_reach_native_argv_and_process(
    client, monkeypatch, tmp_path, short_process_shutdown, backend,
):
    effects = tmp_path / 'applied-settings.jsonl'
    script = tmp_path / 'model-controls.py'
    script.write_text(
        "import json,sys\nfrom pathlib import Path\n"
        "backend=sys.argv[2]; args=sys.argv[3:]\n"
        "if backend=='codex':\n"
        " observed=json.loads(args[0])\n"
        "else:\n"
        " observed={'model':args[args.index('--model')+1]}\n"
        " observed['effort']=args[args.index('--effort')+1]\n"
        " observed['deny']=args[args.index('--disallowedTools')+1].split(',')\n"
        "with Path(sys.argv[1]).open('a') as out: out.write(json.dumps(observed)+'\\n')\n"
        "if backend=='codex':\n"
        " print(json.dumps({'type':'thread.started','thread_id':'configuration-thread'}),flush=True)\n"
        " print(json.dumps({'type':'turn.completed','usage':{}}),flush=True)\n"
        "else:\n"
        " for line in sys.stdin:\n"
        "  print(json.dumps({'type':'result','subtype':'success','is_error':False,"
        "'session_id':'configuration-thread','result':'OK'}),flush=True)\n"
    )
    harness = get_harness(backend)
    render = harness.profile.build_turn_argv

    def controlled_process(context):
        if backend == 'codex':
            captured = {'model': context.model, 'effort': context.reasoning_effort,
                        'tier': context.service_tier or 'default'}
            return [sys.executable, '-u', str(script), str(effects), backend, json.dumps(captured)], {'cwd': context.working_dir}
        argv, kwargs = render(context)
        return [sys.executable, '-u', str(script), str(effects), backend, *argv[1:]], kwargs

    monkeypatch.setattr(harness, 'profile', process_profile(
        harness.profile, binary=sys.executable, build_turn_argv=controlled_process,
        activity_observer=None, prepare_resume=None,
    ))
    selected = {'service_tier': 'fast'} if backend == 'codex' else {'tool_deny': 'Write'}
    created = await client.post('/api/agents', json={
        'name': 'Applied model controls', 'backend': backend, 'model': 'private-model-alias',
        'reasoning_effort': 'low', **selected,
    })
    assert created.status_code == 201, created.text
    created = await client.post(f"/api/agents/{created.json()['id']}/sessions", json={})
    assert created.status_code == 201, created.text
    try:
        receipt = await session_manager.start_message(created.json()['id'], 'Apply the saved configuration')
        completion = await asyncio.wait_for(receipt.wait(), 5)
        assert completion.outcome.status == 'success', completion.outcome.message
        expected = {'model': 'private-model-alias'}
        expected.update({'effort': 'low', 'tier': 'fast'}
                        if backend == 'codex' else {'effort': 'low', 'deny': ['AskUserQuestion', 'Write']})
        assert [json.loads(line) for line in effects.read_text().splitlines()] == [expected]
    finally:
        await session_manager.stop_all_held_processes()


@pytest.mark.asyncio
async def test_speed_survives_archive_replacement_restore_and_fork(client):
    agent = await session_manager.db.get_system_agent()
    assert agent is not None
    response = await client.post("/api/sessions", headers=client.headers, json={"agent_id": agent["id"], "backend": "codex"})
    sid = response.json()["id"]
    assert (await client.patch(f"/api/sessions/{sid}", headers=client.headers, json={"service_tier": "fast"})).status_code == 200
    old = session_manager.get_session(sid)
    replacement = await session_manager.archive_session(sid, replace=True)
    assert replacement.service_tier == "fast"
    archived = (await client.get(f"/api/sessions/{sid}", headers=client.headers)).json()
    assert archived["service_tier"] == "fast"
    unarchived = await session_manager.unarchive_session(sid)
    assert unarchived.service_tier == "fast"
    await session_manager.db.create_fork_session(
        fork_id="speed-fork", name="Fork", working_dir=old.working_dir, created_at=old.created_at,
        parent_id=sid, backend="codex", service_tier=old.service_tier,
        agent_id=old.agent_id, credential_id=None, resume_id="copied-thread", fork_after_seq=-1,
    )
    rows = await session_manager.db.load_sessions()
    fork = next(row for row in rows if row["id"] == "speed-fork")
    assert fork["service_tier"] == "fast"
