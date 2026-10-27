"""Reasoning overrides change the next invocation and preserve its conversation."""

import asyncio
import json
from dataclasses import asdict

import pytest
from httpx import ASGITransport, AsyncClient

from server import deps
from server.agent_manager import AgentManager
from server.database import Database
from server.harness import RunConfig, get_harness
from server.harness.codex import read_model_settings
from server.harness.events import HarnessCredential
from server.harness.profile import OneShotContext
from server.main import app
from server.routers import agents
from server.session_manager import SessionManager, session_manager
from server.users import UserManager


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


def test_effort_reaches_new_resumed_and_oneshot_argv(catalog_home, tmp_path, monkeypatch):
    monkeypatch.setattr("server.harness.run._which_with_fallback", lambda name: "/fake/bin/codex")
    harness = get_harness("codex")
    run = harness.create_run(RunConfig(model="model-a", reasoning_effort="high", mcp_servers=[]))
    for resume_id in (None, "existing-thread"):
        argv, _ = run.build_argv("continue", str(tmp_path), resume_id)
        assert 'model_reasoning_effort="high"' in argv
        assert argv[argv.index("-m") + 1] == "model-a"
        if resume_id:
            assert argv.index('model_reasoning_effort="high"') < argv.index("resume")
            assert argv[argv.index("resume") + 1] == resume_id
    ctx = OneShotContext(prompt="hello", reasoning_effort="low")
    argv, _ = harness.profile.build_oneshot_argv(ctx)
    assert argv[argv.index("--effort") + 1] == "low"
    inherited = harness.create_run(RunConfig(mcp_servers=[]))
    assert not any(a.startswith("model_reasoning_effort=") for a in inherited.build_argv("hi", str(tmp_path))[0])
    changed = harness.create_run(RunConfig(model="model-a", reasoning_effort="low", mcp_servers=[]))
    assert changed.spawn_signature(str(tmp_path), None) != run.spawn_signature(str(tmp_path), None)


@pytest.mark.parametrize("tier, rendered", [(None, "default"), ("default", "default"), ("fast", "fast")])
def test_speed_reaches_new_resumed_and_oneshot_argv(catalog_home, tmp_path, tier, rendered, monkeypatch):
    monkeypatch.setattr("server.harness.run._which_with_fallback", lambda name: "/fake/bin/codex")
    harness = get_harness("codex")
    run = harness.create_run(RunConfig(service_tier=tier, mcp_servers=[]))
    flag = f'service_tier="{rendered}"'
    for resume_id in (None, "existing-thread"):
        argv, _ = run.build_argv("continue", str(tmp_path), resume_id)
        assert flag in argv
        assert "features.fast_mode=true" in argv
        if resume_id:
            assert argv.index(flag) < argv.index("resume")
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
async def test_independent_overrides_preserve_history_resume_and_reload(client):
    response = await client.post("/api/agents", headers=client.headers, json={
        "name": "Reasoner", "backend": "codex", "model": "model-a", "reasoning_effort": "low",
    })
    assert response.status_code == 201, response.text
    agent = response.json()
    response = await client.post("/api/sessions/import", headers=client.headers, json={
        "name": "Existing conversation", "agent_id": agent["id"], "backend": "codex",
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
    for backend, effort in (("codex", "typo"), ("claude-code", "high")):
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
    argv, _ = session_manager._make_run(current, changed.json()).build_argv("continue", "/tmp", "original-thread")
    assert 'service_tier="default"' in argv


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
    from server.routers import backends

    response = await client.post("/api/agents", headers=client.headers, json={
        "name": "Concurrent", "backend": "codex", "model": "model-a",
    })
    agent_id = response.json()["id"]
    path = f"/api/agents/{agent_id}"
    if entity == "sessions":
        response = await client.post(f"{path}/sessions", headers=client.headers, json={})
        path = f"/api/sessions/{response.json()['id']}"
    original = backends.model_settings_for

    async def delayed_catalog(*args, **kwargs):
        result = await original(*args, **kwargs)
        await asyncio.sleep(0.02)
        return result

    monkeypatch.setattr(backends, "model_settings_for", delayed_catalog)
    monkeypatch.setattr(agents, "model_settings_for", delayed_catalog)
    results = await asyncio.gather(
        client.patch(path, headers=client.headers, json={"model": "model-b"}),
        client.patch(path, headers=client.headers, json={"service_tier": "fast"}),
    )
    assert sorted(r.status_code for r in results) == [200, 400]
    saved = (await client.get(path, headers=client.headers)).json()
    assert not (saved["model"] == "model-b" and saved["service_tier"] == "fast")


@pytest.mark.asyncio
async def test_agent_speed_update_validates_inherited_session_model(client):
    response = await client.post("/api/agents", headers=client.headers, json={
        "name": "Inherited", "backend": "codex", "model": "model-a",
    })
    path = f"/api/agents/{response.json()['id']}"
    response = await client.post(f"{path}/sessions", headers=client.headers, json={})
    session_path = f"/api/sessions/{response.json()['id']}"
    assert (await client.patch(session_path, headers=client.headers, json={"model": "model-b"})).status_code == 200
    rejected = await client.patch(path, headers=client.headers, json={"service_tier": "fast"})
    assert rejected.status_code == 400
    assert "Session" in rejected.json()["detail"]
    assert (await client.get(path, headers=client.headers)).json()["service_tier"] is None
    assert (await client.patch(session_path, headers=client.headers, json={"service_tier": "default"})).status_code == 200
    assert (await client.patch(path, headers=client.headers, json={"service_tier": "fast"})).status_code == 200


async def test_agent_reasoning_update_validates_inherited_session_model(client):
    response = await client.post("/api/agents", headers=client.headers, json={
        "name": "Reasoning inheritance", "backend": "codex", "model": "model-a", "reasoning_effort": "low",
    })
    path = f"/api/agents/{response.json()['id']}"
    response = await client.post(f"{path}/sessions", headers=client.headers, json={})
    session_path = f"/api/sessions/{response.json()['id']}"
    assert (await client.patch(session_path, headers=client.headers, json={"model": "model-b"})).status_code == 200
    rejected = await client.patch(path, headers=client.headers, json={"reasoning_effort": "high"})
    assert rejected.status_code == 400
    assert "Session" in rejected.json()["detail"]
    assert (await client.get(path, headers=client.headers)).json()["reasoning_effort"] == "low"
    assert (await client.patch(session_path, headers=client.headers, json={"reasoning_effort": "medium"})).status_code == 200
    assert (await client.patch(path, headers=client.headers, json={"reasoning_effort": "high"})).status_code == 200


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
    assert "Fast speed is unavailable" in completion.outcome.message
    messages = await session_manager.db.load_messages(session.id)
    assert [row["content"] for row in messages if row["role"] == "user"] == ["hello"]
    assert len([row for row in messages if row["type"] == "result"]) == 1


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
