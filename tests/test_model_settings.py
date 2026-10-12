"""Reasoning overrides change the next invocation and preserve its conversation."""

import json
from dataclasses import asdict

import pytest
from httpx import ASGITransport, AsyncClient

from server.agent_manager import AgentManager
from server.database import Database
from server.harness import RunConfig, get_harness
from server.harness.codex import read_model_settings
from server.harness.events import HarnessCredential
from server.harness.profile import OneShotContext
from server.main import app
from server.routers import agents
from server.session_manager import SessionManager, session_manager

HEADERS = {"Authorization": "Bearer changeme"}


@pytest.fixture
def catalog_home(tmp_path, monkeypatch):
    home = tmp_path / "codex"
    home.mkdir()
    (home / "config.toml").write_text('model = "model-a"\nmodel_reasoning_effort = "max"\nsecret = "DO_NOT_EXPOSE"\n')
    (home / "models_cache.json").write_text(json.dumps({"models": [
        {"slug": "model-a", "display_name": "Model A", "visibility": "list",
         "supported_reasoning_levels": [{"effort": e} for e in ("low", "high", "max", "ultra")],
         "default_reasoning_level": "high"},
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
    assert "DO_NOT_EXPOSE" not in json.dumps(asdict(catalog))

    other = tmp_path / "credential"
    other.mkdir()
    (other / "config.toml").write_text('model = "other-model"\nmodel_reasoning_effort = "low"\n')
    credential = HarnessCredential(backend="codex", auth_type="oauth", home_dir=str(other))
    assert read_model_settings(credential).default_model == "other-model"


@pytest.mark.parametrize("cache", ["invalid json", "[]", '{"models":null}', '{"models":[null,{}]}'])
def test_unavailable_catalog_keeps_basic_controls(catalog_home, cache):
    (catalog_home / "models_cache.json").write_text(cache)
    assert read_model_settings().reasoning_efforts == ["low", "medium", "high", "xhigh"]


def test_effort_reaches_new_resumed_and_oneshot_argv(catalog_home, tmp_path):
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
    assert 'model_reasoning_effort="low"' in argv
    inherited = harness.create_run(RunConfig(mcp_servers=[]))
    assert not any(a.startswith("model_reasoning_effort=") for a in inherited.build_argv("hi", str(tmp_path))[0])
    changed = harness.create_run(RunConfig(model="model-a", reasoning_effort="low", mcp_servers=[]))
    assert changed.spawn_signature(str(tmp_path), None) != run.spawn_signature(str(tmp_path), None)


@pytest.fixture
async def client(catalog_home):
    db = Database(":memory:")
    await db.initialize()
    session_manager.sessions.clear()
    await session_manager.initialize(db)
    agents.set_manager(AgentManager(db))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client
    await db.close()
    session_manager.sessions.clear()


@pytest.mark.asyncio
async def test_independent_overrides_preserve_history_resume_and_reload(client):
    response = await client.post("/api/agents", headers=HEADERS, json={
        "name": "Reasoner", "backend": "codex", "model": "model-a", "reasoning_effort": "low",
    })
    assert response.status_code == 201, response.text
    agent = response.json()
    response = await client.post("/api/sessions/import", headers=HEADERS, json={
        "name": "Existing conversation", "agent_id": agent["id"], "backend": "codex",
        "claude_session_id": "original-thread", "messages": [
            {"role": "user", "type": "text", "content": "Remember MARIGOLD"},
            {"role": "assistant", "type": "text", "content": "OK"},
        ],
    })
    assert response.status_code == 201, response.text
    session_id = response.json()["id"]
    path = f"/api/sessions/{session_id}"
    before = (await client.get(path, headers=HEADERS)).json()
    current = session_manager.get_session(session_id)
    first_run = session_manager._make_run(current, agent)
    assert first_run._config.reasoning_effort == "low"

    response = await client.patch(path, headers=HEADERS, json={"reasoning_effort": "high"})
    assert response.status_code == 200, response.text
    assert response.json()["model"] is None
    assert response.json()["reasoning_effort"] == "high"
    assert first_run._config.reasoning_effort == "low", "an invocation already built keeps its setting"
    assert session_manager._make_run(current, agent)._config.reasoning_effort == "high"
    assert (await client.get(f"/api/agents/{agent['id']}", headers=HEADERS)).json()["reasoning_effort"] == "low"

    after = (await client.get(path, headers=HEADERS)).json()
    assert after["messages"] == before["messages"]
    assert after["claude_session_id"] == "original-thread"
    restored = SessionManager()
    await restored.initialize(session_manager.db)
    assert restored.get_session(session_id).reasoning_effort == "high"
    assert restored.get_session(session_id).claude_session_id == "original-thread"

    rejected = await client.patch(path, headers=HEADERS, json={"model": "model-b"})
    assert rejected.status_code == 400
    assert current.model is None
    valid = await client.patch(path, headers=HEADERS, json={"model": "model-b", "reasoning_effort": "medium"})
    assert valid.status_code == 200
    cleared = await client.patch(path, headers=HEADERS, json={"reasoning_effort": None})
    assert cleared.status_code == 200
    assert cleared.json()["model"] == "model-b"
    assert session_manager._make_run(current, agent)._config.reasoning_effort == "low"

    await session_manager.archive_session(session_id, replace=False)
    archived = (await client.get(path, headers=HEADERS)).json()
    assert archived["model"] == "model-b"
    assert archived["messages"] == before["messages"]
    unarchived = await session_manager.unarchive_session(session_id)
    assert unarchived.model == "model-b"
    assert unarchived.claude_session_id == "original-thread"


@pytest.mark.asyncio
async def test_invalid_effort_and_backend_are_rejected(client):
    for backend, effort in (("codex", "typo"), ("claude-code", "high")):
        response = await client.post("/api/agents", headers=HEADERS, json={
            "name": "Invalid", "backend": backend, "reasoning_effort": effort,
        })
        assert response.status_code == 400
    response = await client.get("/api/backends/codex/models", headers=HEADERS)
    assert response.status_code == 200
    assert response.json()["default_reasoning_effort"] == "max"
    assert "DO_NOT_EXPOSE" not in response.text
    response = await client.get("/api/backends/codex/models?credential_id=missing", headers=HEADERS)
    assert response.status_code == 404
