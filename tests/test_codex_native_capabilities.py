"""Closed native capabilities retain their negative response and item tail."""

import json
from pathlib import Path

import pytest

from tests.native_app_server import (
    ROOT,
    inject,
    item,
    start_output,
    terminal,
    wire_frames,
)
from tests.native_app_server import native_runtime as native_runtime
from tests.session_runtime import wait_dispatch_idle

DECLINES = {
    "item/commandExecution/requestApproval": {"decision": "decline"},
    "item/fileChange/requestApproval": {"decision": "decline"},
    "item/permissions/requestApproval": {"permissions": {}, "scope": "turn"},
    "mcpServer/elicitation/request": {"action": "decline"},
    "execCommandApproval": {"decision": "denied"},
    "applyPatchApproval": {"decision": "denied"},
}
UNSUPPORTED_REQUESTS = ("account/chatgptAuthTokens/refresh", "attestation/generate", "currentTime/read")
UNOPENED_ITEMS = ("plan", "functionCallOutput", "sleep", "enteredReviewMode", "exitedReviewMode")


def test_request_policies_cover_the_pinned_generated_server_schema():
    fixture = json.loads((Path(__file__).parent / "fixtures/codex-app-server-0.166-contracts.json").read_text())
    assert set(fixture["server_requests"]) == set(DECLINES) | set(UNSUPPORTED_REQUESTS) | {
        "item/tool/requestUserInput", "item/tool/call"}  # Original question tests and dynamic observation tests own these.


@pytest.mark.parametrize("method,response", DECLINES.items())
async def test_native_denial_and_declined_item_close_without_failing_root(native_runtime, tmp_path, method, response):
    manager, database, session, _, _, _ = native_runtime
    receipt = await start_output(native_runtime)
    denied = {"id": "denied-item", "type": "fileChange", "status": "declined", "changes": []}
    inject(tmp_path, {"id": "approval", "method": method, "params": {
        "threadId": ROOT, "turnId": "turn-1", "itemId": "denied-item"}},
        {"method": "item/completed", "params": {"threadId": ROOT, "turnId": "turn-1", "item": denied}},
        item(ROOT, "turn-1", "answer", "Permission was declined"), terminal())
    await wait_dispatch_idle(manager, session.id)
    assert next(frame for frame in wire_frames(tmp_path) if frame.get("id") == "approval")["result"] == response
    assert next(row for row in await database.load_messages(session.id) if row["type"] == "tool_result")["is_error"]
    assert (await database.read_session_result(session.id, receipt["request_id"])).outcome.status == "success"


@pytest.mark.parametrize("kind", UNOPENED_ITEMS)
async def test_unopened_native_item_fails_with_the_valid_prefix_preserved(native_runtime, tmp_path, kind):
    manager, database, session, _, _, _ = native_runtime
    receipt = await start_output(native_runtime)
    inject(tmp_path, item(ROOT, "turn-1", "prefix", "Valid prefix"), {"method": "item/completed", "params": {
        "threadId": ROOT, "turnId": "turn-1", "item": {"id": "unsupported", "type": kind}}}, terminal())
    await wait_dispatch_idle(manager, session.id)
    result = await database.read_session_result(session.id, receipt["request_id"])
    assert result.outcome.status == "failed" and result.fragments == ("Valid prefix",)


@pytest.mark.parametrize("method", UNSUPPORTED_REQUESTS)
async def test_unsupported_native_request_is_explicitly_rejected(native_runtime, tmp_path, method):
    manager, database, session, _, _, _ = native_runtime
    receipt = await start_output(native_runtime)
    inject(tmp_path, {"id": "unsupported", "method": method, "params": {"threadId": ROOT}})
    await wait_dispatch_idle(manager, session.id)
    assert next(frame for frame in wire_frames(tmp_path) if frame.get("id") == "unsupported")["error"]["code"] == -32601
    assert (await database.read_session_result(session.id, receipt["request_id"])).outcome.status == "failed"


@pytest.mark.parametrize("external_clock", [False, True])
async def test_schedule_execution_obeys_the_same_effective_native_configuration(native_runtime, tmp_path, external_clock):
    from server.agent_manager import AgentManager
    from server.scheduler import ScheduleRunner
    from tests.test_scheduler import save_schedule

    manager, database, parent, _, starts, _ = native_runtime
    await AgentManager(database).update_agent(parent.agent_id, user_id=parent.user_id, backend="codex")
    starts.clear()
    if external_clock:
        (tmp_path / "native-config.json").write_text(json.dumps({"features": {
            "fast_mode": True, "wake_on_agent_messages": False, "goals": False, "sleep_tool": False,
            "multi_agent": True, "multi_agent_v2": {"enabled": True, "wait_agent_enabled": True},
            "current_time_reminder": {"enabled": True, "clock_source": "external"}}}))
    await save_schedule(database, schedule_id="native-config-schedule", agent_id=parent.agent_id,
        name="Native configuration", prompt="Run the scheduled request", interval_seconds=60,
        created_at="2026-11-02T00:00:00+00:00")
    await ScheduleRunner(manager, database)._fire("native-config-schedule")
    scheduled = next(row for row in await database.load_sessions(include_archived=True) if row["origin"] == "schedule")
    outcomes = [row["content"] for row in await database.load_messages(scheduled["id"]) if row["type"] == "result"]
    assert len(outcomes) == 1 and outcomes[0]["status"] == ("failed" if external_clock else "success")
    assert (tmp_path / "native-business.jsonl").exists() == (not external_clock)
    assert len(starts) == 1
