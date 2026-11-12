"""Saved command previews retain received facts without duplicating the body."""

import copy
import json

import pytest

from server.harness.codex_items import item_events, validate_item
from tests.native_app_server import ROOT, inject, item, start_output, terminal
from tests.native_app_server import native_runtime as native_runtime
from tests.session_runtime import wait_dispatch_idle

NOTICE = "[输出已缩短：仅保存收到输出的头尾，中间未归档；以下不是完整日志。]\n"
SEPARATOR = "\n[... 中段已省略 ...]\n"


def command(**fields):
    return {"id": "command", "type": "commandExecution", "command": "produce-log", "cwd": "/tmp",
            "commandActions": [], "status": "completed", **fields}


def frame(payload, stage="completed"):
    return {"method": f"item/{stage}", "params": {"threadId": ROOT, "turnId": "turn-1", "item": payload}}


@pytest.mark.parametrize("fields,capture,content", [
    ({}, {"state": "absent"}, ""),
    ({"aggregatedOutput": None}, {"state": "null"}, ""),
    ({"aggregatedOutput": ""}, {"state": "inline", "received_chars": 0, "omitted_chars": 0}, ""),
    ({"aggregatedOutput": "ok\n"}, {"state": "inline", "received_chars": 3, "omitted_chars": 0}, "ok\n"),
])
def test_command_capture_distinguishes_received_output(fields, capture, content):
    payload = command(**fields)
    original = copy.deepcopy(payload)
    started = item_events("item/started", {"item": payload}).events[0]
    completed = item_events("item/completed", {"item": payload}).events[0]
    assert "output_capture" not in started.tool_input
    assert started.tool_input["command"] == "produce-log"
    assert completed.tool_input["output_capture"] == capture
    assert completed.content == content
    for event in (started, completed):
        assert event.tool_input["native_item"] == {k: v for k, v in original.items() if k != "aggregatedOutput"}
    assert payload == original


@pytest.mark.parametrize("boundary", ["x" * 8190, "中" * 1365, "😀" * 682 + "x" * 6, "\n" * 4095])
def test_command_json_budget_includes_quotes_and_escaping(boundary):
    assert len(json.dumps(boundary)) == 8192
    inline = item_events("item/completed", {"item": command(aggregatedOutput=boundary)}).events[0]
    assert inline.content == boundary
    assert inline.tool_input["output_capture"]["state"] == "inline"
    original = boundary + "x"
    shortened = item_events("item/completed", {"item": command(aggregatedOutput=original)}).events[0]
    assert len(json.dumps(shortened.content)) <= 8192
    assert shortened.content.startswith(NOTICE)
    head, tail = shortened.content[len(NOTICE):].split(SEPARATOR)
    assert original.startswith(head) and original.endswith(tail)
    assert shortened.tool_input["output_capture"] == {
        "state": "head_tail", "received_chars": len(original),
        "omitted_chars": len(original) - len(head) - len(tail),
    }
    assert len(head) + len(tail) < len(original)


def test_large_single_line_keeps_longest_head_and_tail_with_fixed_split():
    original = '中😀"\\\n\x00x' * 100_000
    event = item_events("item/completed", {"item": command(aggregatedOutput=original, exitCode=7)}).events[0]
    assert event.is_error and event.tool_input["native_item"]["exitCode"] == 7
    head, tail = event.content[len(NOTICE):].split(SEPARATOR)
    payload_budget = 8192 - len(json.dumps(NOTICE + SEPARATOR))
    head_budget, tail_budget = payload_budget // 2, payload_budget - payload_budget // 2
    assert len(json.dumps(head)) - 2 <= head_budget < len(json.dumps(original[:len(head) + 1])) - 2
    assert len(json.dumps(tail)) - 2 <= tail_budget < len(json.dumps(original[-len(tail) - 1:])) - 2
    assert len(json.dumps(event.content)) <= 8192


def test_structured_tool_and_complete_answer_are_not_command_previews():
    text = "完整业务正文😀" * 10_000
    mcp = {"id": "receipt", "type": "mcpToolCall", "server": "bg", "tool": "run", "arguments": {},
           "status": "completed", "result": {"content": [{"type": "text", "text": text}]}}
    final = {"id": "final", "type": "agentMessage", "text": text, "phase": "final_answer"}
    for payload in (mcp, final):
        value = item_events("item/completed", {"item": payload}).events[0]
        assert value.content == text and "output_capture" not in value.tool_input


def test_json_edge_search_handles_an_empty_tail_without_taking_the_entire_body():
    from server.harness.codex_items import _output_edge

    assert _output_edge("😀", 0, tail=True) == ""
    assert _output_edge("😀", 11, tail=True) == ""
    assert _output_edge("😀", 12, tail=True) == "😀"


@pytest.mark.parametrize("payload", [
    command(aggregatedOutput=42), command(status=None),
    {"id": "message", "type": "agentMessage", "text": 1},
    {"id": "message", "type": "agentMessage", "text": "ok", "phase": "wrong"},
    {"id": "reason", "type": "reasoning", "summary": [1]},
    {"id": "image", "type": "imageGeneration", "status": "completed", "savedPath": "relative"},
    {"id": "image", "type": "imageView", "path": None},
    {"id": "hook", "type": "hookPrompt", "fragments": [{}]},
    {"id": "search", "type": "webSearch", "query": "ok", "action": {"type": "search", "queries": [1]}},
])
def test_validation_and_conversion_reject_the_same_malformed_complete_facts(payload):
    with pytest.raises(ValueError):
        validate_item(payload)
    for method in ("item/started", "item/completed"):
        with pytest.raises(ValueError):
            item_events(method, {"item": payload})


@pytest.mark.parametrize("changed_middle", [False, True])
async def test_full_command_facts_are_compared_before_lossy_recording(native_runtime, tmp_path, changed_middle):
    manager, database, session, _, starts, _ = native_runtime
    receipt = await start_output(native_runtime)
    payload = command(aggregatedOutput="H" * 10_000 + "first" + "T" * 10_000, exitCode=0)
    duplicate = {**payload, "aggregatedOutput": payload["aggregatedOutput"].replace("first", "other")}
    inject(tmp_path, frame(payload, "started"), frame(payload), frame(duplicate if changed_middle else payload),
           item(ROOT, "turn-1", "final", "Command observed"), terminal())
    await wait_dispatch_idle(manager, session.id)
    saved = await database.read_session_result(session.id, receipt["request_id"])
    assert saved.outcome.status == ("failed" if changed_middle else "success")
    rows = [row for row in await database.load_messages(session.id) if row["type"] in {"tool_use", "tool_result"}]
    assert [row["type"] for row in rows] == ["tool_use", "tool_result"]
    assert all("aggregatedOutput" not in row["tool_input"]["native_item"] for row in rows)
    result = rows[-1]
    assert result["content"].startswith(NOTICE) and len(json.dumps(result["content"])) <= 8192
    assert result["tool_input"]["output_capture"]["state"] == "head_tail"
    assert result["runtime_metadata"]["request_id"] == receipt["request_id"]
    assert result["runtime_metadata"]["native"]["item_id"] == "command"
    assert len(starts) == 1
