"""Pinned native item semantics through submission, recording and WS replay."""

import json
from pathlib import Path

import pytest

from tests.native_app_server import (
    ROOT,
    inject,
    item,
    start_output,
    terminal,
)
from tests.native_app_server import (
    native_runtime as native_runtime,
)
from tests.session_runtime import wait_dispatch_idle


def test_item_conversion_covers_the_pinned_generated_schema():
    from server.harness.codex_items import item_events, observation_frame

    fixture = json.loads((Path(__file__).parent / "fixtures/codex-app-server-0.166-contracts.json").read_text())
    examples = {
        "userMessage": {"content": []}, "hookPrompt": {"fragments": []}, "agentMessage": {"text": ""},
        "reasoning": {}, "commandExecution": {"command": "true", "commandActions": [], "cwd": "/", "status": "completed"},
        "fileChange": {"changes": [], "status": "completed"},
        "mcpToolCall": {"arguments": {}, "server": "example", "tool": "lookup", "status": "completed"},
        "dynamicToolCall": {"arguments": {}, "tool": "unregistered", "status": "failed", "success": False},
        "collabAgentToolCall": {"tool": "wait", "status": "interrupted", "agentsStates": {},
                               "receiverThreadIds": [], "senderThreadId": ROOT},
        "subAgentActivity": {"agentPath": "/root/child", "agentThreadId": "child", "kind": "started"},
        "webSearch": {"query": "example"}, "imageView": {"path": "/image.png"},
        "imageGeneration": {"status": "completed", "result": ""}, "contextCompaction": {},
    }
    unopened = {"plan", "functionCallOutput", "sleep", "enteredReviewMode", "exitedReviewMode"}
    assert set(fixture["thread_items"]) == set(examples) | unopened
    for kind, fields in examples.items():
        payload = {"id": "record", "type": kind, **fields}
        assert set(fixture["thread_items"][kind]) <= set(payload)
        frame = observation_frame(native_item(payload))
        output = item_events(frame["method"], frame["params"])
        assert output.events or kind == "userMessage"
    for kind in unopened:
        with pytest.raises(ValueError, match="Unsupported complete native item"):
            item_events("item/completed", {"item": {"id": "record", "type": kind}})


def native_item(payload, stage="completed", thread=ROOT, turn="turn-1"):
    return {"method": f"item/{stage}", "params": {"threadId": thread, "turnId": turn, "item": payload}}


@pytest.mark.parametrize("payload", [
    {"id": "call", "type": "mcpToolCall", "server": "example", "tool": "lookup", "arguments": {},
     "status": "failed", "result": {"content": [{"type": "text", "text": "Business failure body"}]}, "error": None},
    {"id": "call", "type": "commandExecution", "command": "false", "status": "failed",
     "exitCode": None, "aggregatedOutput": "Command failed"},
    {"id": "call", "type": "commandExecution", "command": "denied", "status": "declined",
     "exitCode": None, "aggregatedOutput": ""},
    {"id": "call", "type": "fileChange", "changes": [], "status": "declined"},
    {"id": "call", "type": "fileChange", "changes": [], "status": "failed"},
    {"id": "call", "type": "collabAgentToolCall", "tool": "wait", "receiverThreadIds": [],
     "agentsStates": {}, "status": "interrupted"},
])
async def test_unsuccessful_native_tool_does_not_look_successful_or_fail_the_root(native_runtime, tmp_path, payload):
    manager, database, session, _, starts, events = native_runtime
    receipt = await start_output(native_runtime)
    complete = native_item(payload)
    inject(tmp_path, complete, complete, item(ROOT, "turn-1", "final", "Root recovered"), terminal())
    await wait_dispatch_idle(manager, session.id)
    rows = [row for row in await database.load_messages(session.id) if row["type"] == "tool_result"]
    assert len(rows) == 1 and rows[0]["is_error"]
    assert rows[0]["tool_input"]["native_item"] == payload
    if payload["type"] == "mcpToolCall":
        assert rows[0]["content"] == "Business failure body"
    assert rows[0]["runtime_metadata"]["native"]["item_id"] == "call"
    wire = next(event for event in events if event.get("type") == "tool_result")
    assert wire["is_error"] and wire["tool_input"] == rows[0]["tool_input"]
    result = await database.read_session_result(session.id, receipt["request_id"])
    assert result.outcome.status == "success" and result.fragments == ("Root recovered",)
    assert len(starts) == 1


async def test_empty_reasoning_and_command_metadata_survive_recording(native_runtime, tmp_path):
    manager, database, session, _, _, _ = native_runtime
    await start_output(native_runtime)
    command = {"id": "command", "type": "commandExecution", "command": "true", "cwd": str(tmp_path),
               "status": "completed", "exitCode": 0, "aggregatedOutput": "", "durationMs": 12,
               "processId": "42", "commandActions": [], "source": "agent"}
    reasoning = {"id": "reason", "type": "reasoning", "summary": [], "content": []}
    inject(tmp_path, native_item(reasoning), native_item(command, "started"), native_item(command), terminal())
    await wait_dispatch_idle(manager, session.id)
    rows = await database.load_messages(session.id)
    assert next(row for row in rows if row["type"] == "thinking")["tool_input"]["native_item"] == reasoning
    assert next(row for row in rows if row["type"] == "tool_result")["tool_input"]["native_item"] == command


@pytest.mark.parametrize("kind", ["commandExecution", "fileChange", "mcpToolCall", "dynamicToolCall", "collabAgentToolCall"])
@pytest.mark.parametrize("status", [None, "unknown"])
async def test_missing_or_unknown_native_tool_status_cannot_become_success(native_runtime, tmp_path, kind, status):
    manager, database, session, _, starts, _ = native_runtime
    receipt = await start_output(native_runtime)
    payload = {"id": "invalid", "type": kind, "command": "true", "tool": "lookup", "server": "example",
               "changes": [], "arguments": {}, **({"status": status} if status is not None else {})}
    inject(tmp_path, item(ROOT, "turn-1", "prefix", "Valid prefix"), native_item(payload), terminal())
    await wait_dispatch_idle(manager, session.id)
    result = await database.read_session_result(session.id, receipt["request_id"])
    assert result.outcome.status == "failed" and result.fragments == ("Valid prefix",)
    assert len(starts) == 1


@pytest.mark.parametrize("payload", [
    {"id": "observation", "type": "imageView", "path": "/original/image.png"},
    {"id": "observation", "type": "hookPrompt", "fragments": [{"text": "Hook context", "hookRunId": "hook-1"}]},
    {"id": "observation", "type": "hookPrompt", "fragments": []},
    {"id": "observation", "type": "dynamicToolCall", "namespace": None, "tool": "unregistered", "arguments": {},
     "status": "failed", "success": False, "contentItems": [{"type": "inputText", "text": "Tool not registered"}]},
])
@pytest.mark.parametrize("complete_only", [False, True])
async def test_native_observations_keep_their_source_and_do_not_become_the_answer(native_runtime, tmp_path, payload, complete_only):
    from tests.native_app_server import child_end, child_start, wire_frames

    manager, database, session, _, _, events = native_runtime
    receipt = await start_output(native_runtime)
    if payload["type"] == "dynamicToolCall":
        inject(tmp_path, {"id": "dynamic-request", "method": "item/tool/call", "params": {
            "threadId": ROOT, "turnId": "turn-1", "callId": payload["id"], "tool": "unregistered", "arguments": {}}})
    inject(tmp_path, child_start())
    for thread, turn in [(ROOT, "turn-1"), ("child", "child-turn")]:
        if not complete_only:
            inject(tmp_path, native_item(payload, "started", thread, turn))
        completed = native_item(payload, thread=thread, turn=turn)
        inject(tmp_path, completed, completed)
    inject(tmp_path, child_end(), item(ROOT, "turn-1", "final", "Only final answer"), terminal())
    await wait_dispatch_idle(manager, session.id)
    rows = [row for row in await database.load_messages(session.id) if row["type"] == "tool_result"]
    assert len(rows) == 2
    assert [row["runtime_metadata"]["native"]["thread_id"] for row in rows] == [ROOT, "child"]
    assert all(row["tool_input"]["native_item"] == payload for row in rows)
    if payload["type"] == "dynamicToolCall":
        assert all(row["is_error"] for row in rows)
        response = next(frame for frame in wire_frames(tmp_path) if frame.get("id") == "dynamic-request")
        assert response["result"]["success"] is False
    wire = [event for event in events if event.get("type") == "tool_result"]
    assert [event["tool_input"] for event in wire] == [row["tool_input"] for row in rows]
    result = await database.read_session_result(session.id, receipt["request_id"])
    assert result.outcome.status == "success" and result.fragments == ("Only final answer",)
