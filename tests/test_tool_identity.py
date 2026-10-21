import json

import pytest

from server.harness.codex import CODEX
from server.harness.events import HarnessEvent, SubagentUpdate
from server.harness.run import HarnessRun
from server.harness.tool_identity import ToolIdentityTracker


def test_repeated_provider_id_pairs_only_with_captured_attempt():
    tracker = ToolIdentityTracker()
    first = HarnessEvent(type="tool_use", tool_use_id="item_1", tool_name="mcp__bg__run")
    tracker.normalize(first, "r1", "a1")
    result = HarnessEvent(type="tool_result", tool_use_id="item_1", content="Started bg task `old` (test).")
    tracker.normalize(result, "r1", "a1")
    second = HarnessEvent(type="tool_use", tool_use_id="item_1", tool_name="mcp__bg__run")
    tracker.normalize(second, "r2", "a2")
    result2 = HarnessEvent(type="tool_result", tool_use_id="item_1", content="Started bg task `new` (test).")
    tracker.normalize(result2, "r2", "a2")
    assert first.runtime_metadata["tool_call_id"] == result.runtime_metadata["tool_call_id"]
    assert second.runtime_metadata["tool_call_id"] == result2.runtime_metadata["tool_call_id"]
    assert first.runtime_metadata["tool_call_id"] != second.runtime_metadata["tool_call_id"]
    assert result.runtime_metadata["receipt"]["task_id"] == "old"
    assert result2.runtime_metadata["receipt"]["task_id"] == "new"


def test_receipt_requires_exact_call_and_successful_known_response():
    tracker = ToolIdentityTracker()
    missing = HarnessEvent(type="tool_result", tool_use_id="missing", content="Started bg task `evil`")
    tracker.normalize(missing, "r", "a")
    assert missing.runtime_metadata is None
    call = HarnessEvent(type="tool_use", tool_use_id="x", tool_name="Read")
    tracker.normalize(call, "r", "a")
    result = HarnessEvent(type="tool_result", tool_use_id="x", content="Started bg task `evil`")
    tracker.normalize(result, "r", "a")
    assert result.runtime_metadata["receipt"] is None


@pytest.mark.asyncio
async def test_harness_reader_freezes_provenance_before_consumer_context_changes():
    run = HarnessRun(CODEX)
    run.bind_tool_context("old-request", "old-attempt")
    await run._handle_line(json.dumps({"type": "item.started", "item": {
        "id": "item_1", "type": "mcp_tool_call", "server": "bg", "tool": "run",
        "arguments": {"command": "pytest"},
    }}))
    await run._handle_line(json.dumps({"type": "item.completed", "item": {
        "id": "item_1", "type": "mcp_tool_call",
        "result": {"content": [{"type": "text", "text": "Started bg task `old` (tests)."}]},
    }}))
    run.bind_tool_context("new-request", "new-attempt")
    stream = run.stream()
    call = await anext(stream)
    result = await anext(stream)
    await stream.aclose()
    assert call.runtime_metadata["request_id"] == "old-request"
    assert result.runtime_metadata["request_id"] == "old-request"
    assert call.runtime_metadata["tool_call_id"] == result.runtime_metadata["tool_call_id"]
    assert result.runtime_metadata["receipt"]["task_id"] == "old"


def test_held_native_progress_keeps_original_spawning_call_after_a_new_attempt():
    tracker = ToolIdentityTracker()
    spawn = HarnessEvent(type="tool_use", tool_use_id="reused", tool_name="Agent")
    tracker.normalize(spawn, "old", "a1")
    progress = HarnessEvent(type="subagent", subagent=SubagentUpdate(task_id="child", tool_use_id="reused"))
    tracker.normalize(progress, "old", "a1")
    new = HarnessEvent(type="tool_use", tool_use_id="reused", tool_name="Agent")
    tracker.normalize(new, "new", "a2")
    done = HarnessEvent(type="subagent", subagent=SubagentUpdate(task_id="child", tool_use_id="reused", status="completed"))
    tracker.normalize(done, "new", "a2")
    assert done.subagent.tool_call_id == spawn.runtime_metadata["tool_call_id"]
    assert done.subagent.tool_call_id != new.runtime_metadata["tool_call_id"]


def test_long_run_preserves_new_and_known_native_child_associations():
    tracker = ToolIdentityTracker()
    original = HarnessEvent(type="tool_use", tool_use_id="spawn-old", tool_name="Agent")
    tracker.normalize(original, "old", "a1")
    started = HarnessEvent(type="subagent", subagent=SubagentUpdate(task_id="child-old", tool_use_id="spawn-old"))
    tracker.normalize(started, "old", "a1")
    for index in range(5000):
        call = HarnessEvent(type="tool_use", tool_use_id=f"other-{index}", tool_name="Read")
        tracker.normalize(call, f"request-{index}", f"attempt-{index}")
    later = HarnessEvent(type="tool_use", tool_use_id="spawn-new", tool_name="Agent")
    tracker.normalize(later, "new", "new-attempt")
    unknown = HarnessEvent(type="subagent", subagent=SubagentUpdate(task_id="child-new", tool_use_id="spawn-new"))
    tracker.normalize(unknown, "new", "new-attempt")
    assert unknown.subagent.tool_call_id == later.runtime_metadata["tool_call_id"]
    known = HarnessEvent(type="subagent", subagent=SubagentUpdate(task_id="child-old", tool_use_id="spawn-old", status="completed"))
    tracker.normalize(known, "new", "new-attempt")
    assert known.subagent.tool_call_id == original.runtime_metadata["tool_call_id"]

    ordinary = HarnessEvent(type="tool_use", tool_use_id="bg", tool_name="mcp__bg__run")
    tracker.normalize(ordinary, "new", "new-attempt")
    result = HarnessEvent(type="tool_result", tool_use_id="bg", content="Started bg task `exact` (test).")
    tracker.normalize(result, "new", "new-attempt")
    assert result.runtime_metadata["tool_call_id"] == ordinary.runtime_metadata["tool_call_id"]
    assert result.runtime_metadata["receipt"]["task_id"] == "exact"


def test_non_spawn_reuse_cannot_claim_an_unknown_child_of_an_older_spawn():
    tracker = ToolIdentityTracker()
    spawn = HarnessEvent(type="tool_use", tool_use_id="shared", tool_name="Agent")
    tracker.normalize(spawn, "r", "a")
    known = HarnessEvent(type="subagent", subagent=SubagentUpdate(task_id="known", tool_use_id="shared"))
    tracker.normalize(known, "r", "a")
    ordinary = HarnessEvent(type="tool_use", tool_use_id="shared", tool_name="Read")
    tracker.normalize(ordinary, "r", "a")
    unknown = HarnessEvent(type="subagent", subagent=SubagentUpdate(task_id="unknown", tool_use_id="shared"))
    tracker.normalize(unknown, "r", "a")
    assert unknown.subagent.tool_call_id is None
    done = HarnessEvent(type="subagent", subagent=SubagentUpdate(task_id="known", tool_use_id="shared", status="completed"))
    tracker.normalize(done, "r", "a")
    assert done.subagent.tool_call_id == spawn.runtime_metadata["tool_call_id"]


def test_unclaimed_idle_spawn_cannot_be_rebound_to_a_new_call():
    tracker = ToolIdentityTracker()
    idle = HarnessEvent(type="tool_use", tool_use_id="shared", tool_name="Agent")
    tracker.normalize_idle(idle)
    claimed = HarnessEvent(type="tool_use", tool_use_id="shared", tool_name="Agent")
    tracker.normalize(claimed, "r", "a")
    child = HarnessEvent(type="subagent", subagent=SubagentUpdate(task_id="unknown", tool_use_id="shared"))
    tracker.normalize(child, "r", "a")
    assert child.subagent.tool_call_id is None


def test_idle_events_resolve_captured_receipts_and_children_without_creating_new_calls():
    tracker = ToolIdentityTracker()
    call = HarnessEvent(type="tool_use", tool_use_id="pending", tool_name="mcp__bg__run")
    tracker.normalize(call, "finished-request", "finished-attempt")
    spawn = HarnessEvent(type="tool_use", tool_use_id="spawn", tool_name="Agent")
    tracker.normalize(spawn, "finished-request", "finished-attempt")

    idle_call = HarnessEvent(type="tool_use", tool_use_id="idle", tool_name="mcp__bg__run")
    tracker.normalize_idle(idle_call)
    assert idle_call.runtime_metadata is None and idle_call.identity_checked
    idle_result = HarnessEvent(type="tool_result", tool_use_id="idle", content="Started bg task `unowned` (test).")
    tracker.normalize_idle(idle_result)
    assert idle_result.runtime_metadata is None

    receipt = HarnessEvent(type="tool_result", tool_use_id="pending", content="Started bg task `owned` (test).")
    tracker.normalize_idle(receipt)
    assert receipt.runtime_metadata["tool_call_id"] == call.runtime_metadata["tool_call_id"]
    assert receipt.runtime_metadata["request_id"] == "finished-request"
    child = HarnessEvent(type="subagent", subagent=SubagentUpdate(task_id="child", tool_use_id="spawn", status="completed"))
    tracker.normalize_idle(child)
    assert child.subagent.tool_call_id == spawn.runtime_metadata["tool_call_id"]


@pytest.mark.asyncio
async def test_terminal_retires_call_context_but_keeps_captured_native_child_progress():
    from server.harness.claude_code import CLAUDE_CODE

    run = HarnessRun(CLAUDE_CODE)
    run.bind_tool_context("finished-request", "finished-attempt")
    await run._handle_line(json.dumps({"type": "assistant", "message": {"content": [{
        "type": "tool_use", "id": "spawn", "name": "Agent", "input": {},
    }]}}))
    stream = run.stream()
    spawn = await anext(stream)
    await stream.aclose()
    await run._handle_line(json.dumps({"type": "result", "subtype": "success", "is_error": False}))
    idle = []

    async def observe(event):
        idle.append(event)

    run.set_idle_handler(observe)
    await run._handle_line(json.dumps({"type": "assistant", "message": {"content": [{
        "type": "tool_use", "id": "idle", "name": "mcp__bg__run", "input": {},
    }]}}))
    await run._handle_line(json.dumps({"type": "system", "subtype": "task_notification", "task_id": "child",
                                     "tool_use_id": "spawn", "status": "completed"}))
    assert idle[0].identity_checked and idle[0].runtime_metadata is None
    assert idle[1].subagent.tool_call_id == spawn.runtime_metadata["tool_call_id"]
