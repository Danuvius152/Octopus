"""App Server configuration through the saved Agent and public request seams."""

import json

import pytest

from server.agent_manager import AgentError, AgentManager
from server.harness import codex_app_server
from server.harness.codex_app_server import verify_binary
from server.sessions.client_submissions import submission_status, submit_client_message
from tests.native_app_server import native_runtime as native_runtime
from tests.session_runtime import wait_dispatch_idle


@pytest.mark.parametrize("collaboration", [{"mode": "plan", "settings": {}}, {"mode": "future", "settings": {}}, None])
async def test_incompatible_resume_is_refused_before_business_input(native_runtime, tmp_path, collaboration):
    manager, database, session, _, starts, _ = native_runtime
    await submit_client_message(manager, session.id, "Establish history", [], "first", wiki_mode=None)
    await wait_dispatch_idle(manager, session.id)
    (tmp_path / "native-collaboration.json").write_text(json.dumps(collaboration))
    await submit_client_message(manager, session.id, "Do not convert this history", [], "second", wiki_mode=None)
    await wait_dispatch_idle(manager, session.id)
    result = await submission_status(manager, session.id, "second")
    assert result["outcome"]["status"] == "failed"
    assert "collaboration mode" in result["outcome"]["message"].lower()
    assert result["execution"]["native_inputs"][0]["state"] == "not_sent"
    frames = [json.loads(line) for line in (tmp_path / "native-frames.jsonl").read_text().splitlines()]
    assert len([frame for frame in frames if frame.get("method") == "turn/start"]) == 1
    assert len([frame for frame in frames if frame.get("method") == "thread/start"]) == 1
    assert len(starts) == 2


@pytest.mark.parametrize("enabled,source,accepted", [(True, "external", False), (False, "external", True),
                                                    (None, "external", True), (True, "system", True)])
async def test_effective_external_clock_requires_a_supported_client(native_runtime, tmp_path, enabled, source, accepted):
    manager, _, session, _, _, _ = native_runtime
    (tmp_path / "native-config.json").write_text(json.dumps({"features": {
        "fast_mode": True, "wake_on_agent_messages": False, "goals": False, "sleep_tool": False,
        "multi_agent": True, "multi_agent_v2": {"enabled": True, "wait_agent_enabled": True},
        "current_time_reminder": {"enabled": enabled, "clock_source": source},
    }}))
    await submit_client_message(manager, session.id, "Run with effective time configuration", [], "clock", wiki_mode=None)
    await wait_dispatch_idle(manager, session.id)
    status = await submission_status(manager, session.id, "clock")
    assert status["outcome"]["status"] == ("success" if accepted else "failed")
    assert (tmp_path / "native-business.jsonl").exists() == accepted


async def test_saved_large_chinese_controller_uses_the_selected_transport(native_runtime, tmp_path):
    manager, database, session, mode, starts, events = native_runtime
    persona = "中文配置必须完整保留。" * 16000
    saved = await AgentManager(database).update_agent(
        session.agent_id, user_id=session.user_id, backend="codex", system_prompt=persona,
    )
    assert saved["system_prompt"] == persona
    await submit_client_message(manager, session.id, "Use my saved configuration", [], "configured", wiki_mode=None)
    await wait_dispatch_idle(manager, session.id)
    assert (await submission_status(manager, session.id, "configured"))["outcome"]["status"] == "success"
    history = [json.loads(line) for line in (tmp_path / "native-history.jsonl").read_text().splitlines()]
    assert persona in history[1]["payload"]["content"][0]["text"]
    assert len((tmp_path / "native-business.jsonl").read_text().splitlines()) == 1


async def test_unavailable_fast_mode_fails_before_business_input(native_runtime, tmp_path):
    manager, database, session, mode, starts, events = native_runtime
    await AgentManager(database).update_agent(session.agent_id, user_id=session.user_id,
                                             backend="codex", service_tier="fast")
    mode[0] = "bad_fast_config"
    await submit_client_message(manager, session.id, "Use the saved Fast configuration", [], "fast", wiki_mode=None)
    await wait_dispatch_idle(manager, session.id)
    status = await submission_status(manager, session.id, "fast")
    assert status["outcome"]["status"] == "failed"
    assert status["execution"]["native_inputs"][0]["state"] == "not_sent"
    assert status["execution"]["recoverable_input"]
    assert not (tmp_path / "native-business.jsonl").exists()


async def test_clearing_saved_controls_uses_current_defaults_when_resuming(native_runtime, tmp_path):
    manager, database, session, mode, starts, events = native_runtime
    agents = AgentManager(database)
    await agents.update_agent(session.agent_id, user_id=session.user_id, backend="codex",
                              model="fixture-old", reasoning_effort="high")
    await submit_client_message(manager, session.id, "Start with explicit settings", [], "explicit", wiki_mode=None)
    await wait_dispatch_idle(manager, session.id)
    first = await submission_status(manager, session.id, "explicit")
    assert first["outcome"]["status"] == "success"
    assert json.loads((tmp_path / "native-selected.json").read_text()) == {"model": "fixture-old", "effort": "high"}

    (tmp_path / "native-defaults.json").write_text(json.dumps({"model": "default-two", "effort": "medium"}))
    await agents.update_agent(session.agent_id, user_id=session.user_id, model=None, reasoning_effort=None)
    await submit_client_message(manager, session.id, "Use current defaults", [], "defaults", wiki_mode=None)
    await wait_dispatch_idle(manager, session.id)
    second = await submission_status(manager, session.id, "defaults")
    assert second["outcome"]["status"] == "success"
    assert json.loads((tmp_path / "native-selected.json").read_text()) == {"model": "default-two", "effort": "medium"}
    assert second["execution"]["native_inputs"][0]["thread_id"] == first["execution"]["native_inputs"][0]["thread_id"]


async def test_unset_native_config_uses_the_native_catalog_default(native_runtime, tmp_path):
    manager, database, session, mode, starts, events = native_runtime
    (tmp_path / "native-defaults.json").write_text(json.dumps({"model": None, "effort": None}))
    await submit_client_message(manager, session.id, "Use the native default", [], "catalog", wiki_mode=None)
    await wait_dispatch_idle(manager, session.id)
    assert (await submission_status(manager, session.id, "catalog"))["outcome"]["status"] == "success"
    assert json.loads((tmp_path / "native-selected.json").read_text()) == {"model": "catalog-default", "effort": None}


@pytest.mark.parametrize("failure", ["unsupported_fast_model", "wrong_effective_tier"])
async def test_fast_cannot_silently_downgrade_the_saved_request(native_runtime, tmp_path, failure):
    manager, database, session, mode, starts, events = native_runtime
    await AgentManager(database).update_agent(session.agent_id, user_id=session.user_id, backend="codex", service_tier="fast")
    mode[0] = failure
    await submit_client_message(manager, session.id, "Require Fast", [], "require-fast", wiki_mode=None)
    await wait_dispatch_idle(manager, session.id)
    status = await submission_status(manager, session.id, "require-fast")
    assert status["outcome"]["status"] == "failed"
    assert status["execution"]["native_inputs"][0]["state"] == "not_sent"
    assert status["execution"]["recoverable_input"]
    assert not (tmp_path / "native-business.jsonl").exists()


async def test_fast_model_can_be_on_a_later_catalog_page(native_runtime, tmp_path):
    manager, database, session, mode, starts, events = native_runtime
    await AgentManager(database).update_agent(session.agent_id, user_id=session.user_id, backend="codex", service_tier="fast")
    mode[0] = "paged_fast_model"
    await submit_client_message(manager, session.id, "Use the advertised Fast model", [], "paged-fast", wiki_mode=None)
    await wait_dispatch_idle(manager, session.id)
    assert (await submission_status(manager, session.id, "paged-fast"))["outcome"]["status"] == "success"
    assert len((tmp_path / "native-business.jsonl").read_text().splitlines()) == 1


async def test_oversized_jsonl_controller_is_rejected_before_saving(native_runtime, monkeypatch):
    manager, database, session, mode, starts, events = native_runtime
    monkeypatch.setattr("server.harness.codex_rpc.FRAME_LIMIT_BYTES", 128 * 1024)
    before = await database.get_agent(session.agent_id)
    with pytest.raises(AgentError, match="JSONL.*bytes"):
        await AgentManager(database).update_agent(session.agent_id, user_id=session.user_id,
                                                 backend="codex", system_prompt="中" * 50000)
    assert (await database.get_agent(session.agent_id))["system_prompt"] == before["system_prompt"]


async def test_unverified_binary_fails_preparation_without_business_send(native_runtime, tmp_path, monkeypatch):
    manager, database, session, _, starts, _ = native_runtime
    # Restore the real verifier: the fixture's Python peer is intentionally
    # not the pinned Codex binary and must be rejected before it is spawned.
    monkeypatch.setattr(codex_app_server, "verify_binary", verify_binary)
    await submit_client_message(manager, session.id, "Keep this unsent input", [], "wrong-binary", wiki_mode=None)
    await wait_dispatch_idle(manager, session.id)
    status = await submission_status(manager, session.id, "wrong-binary")
    assert status["outcome"]["status"] == "failed"
    assert status["execution"]["state"] == "withdrawn"
    assert status["input"]["content"] == "Keep this unsent input"
    assert status["execution"]["recoverable_input"]
    assert status["execution"]["native_inputs"][0]["state"] == "not_sent"
    assert len(starts) == 1
    assert not (tmp_path / "native-frames.jsonl").exists()
    assert not (tmp_path / "native-business.jsonl").exists()


@pytest.mark.parametrize("characters,accepted", [(30000, True), (50000, False)])
async def test_jsonl_budget_counts_utf8_without_truncating_input(native_runtime, tmp_path, monkeypatch, characters, accepted):
    manager, database, session, mode, starts, events = native_runtime
    monkeypatch.setattr("server.harness.codex_rpc.FRAME_LIMIT_BYTES", 128 * 1024)
    content = "中" * characters
    await submit_client_message(manager, session.id, content, [], "utf8-input", wiki_mode=None)
    await wait_dispatch_idle(manager, session.id)
    status = await submission_status(manager, session.id, "utf8-input")
    assert status["input"]["content"] == content
    if accepted:
        assert status["outcome"]["status"] == "success"
        business = json.loads((tmp_path / "native-business.jsonl").read_text())
        assert business["input"][0]["text"] == content
    else:
        assert status["outcome"]["status"] == "failed"
        assert "JSONL" in status["outcome"]["message"]
        assert status["execution"]["native_inputs"][0]["state"] == "not_sent"
        assert status["execution"]["recoverable_input"]
        assert not (tmp_path / "native-business.jsonl").exists()
