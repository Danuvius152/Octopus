"""Native source identity survives the public transcript and WS boundaries."""

import asyncio
import json
from pathlib import Path

import pytest

from server.sessions.client_submissions import submit_client_message
from tests import native_app_server as native_tests
from tests.native_app_server import (
    ROOT,
    child_end,
    child_start,
    inject,
    item,
    start_output,
    terminal,
    until,
)
from tests.session_runtime import wait_dispatch_idle

native_runtime = native_tests.native_runtime

async def test_root_and_child_same_item_survive_storage_and_ws(native_runtime, tmp_path):
    manager, database, session, _, _, events = native_runtime
    receipt = await start_output(native_runtime)
    inject(tmp_path, child_start(), item("child", "child-turn", "same", "Child answer"), child_end(),
        item(ROOT, "turn-1", "same", "Root answer"), terminal())
    await wait_dispatch_idle(manager, session.id)
    rows = [row for row in await database.load_messages(session.id) if row["type"] == "text" and row["role"] == "assistant"]
    assert [row["content"] for row in rows] == ["Child answer", "Root answer"]
    child, root = rows
    assert child["runtime_metadata"]["request_id"] == receipt["request_id"]
    assert child["runtime_metadata"]["attempt_id"] == root["runtime_metadata"]["attempt_id"]
    assert child["runtime_metadata"].get("tool_call_id") is None
    assert child["runtime_metadata"]["native"]["thread_id"] == "child"
    assert root["runtime_metadata"]["native"]["thread_id"] == ROOT
    assert child["seq"] < root["seq"]
    wire = [event for event in events if event.get("type") == "assistant_text"]
    assert [event["runtime_metadata"] for event in wire] == [row["runtime_metadata"] for row in rows]
    assert session.claude_session_id == ROOT


async def test_root_terminal_retains_late_child_items_and_distinct_helper_outcomes(native_runtime, tmp_path):
    manager, database, session, _, _, _ = native_runtime
    receipt = await start_output(native_runtime)
    inject(tmp_path, child_start("finished", "round-1"), item("finished", "round-1", "answer", "Complete"),
        child_end("finished", "round-1"), child_start("remaining", "round-2"), terminal(),
        item("remaining", "round-2", "late", "Saved partial"))
    await wait_dispatch_idle(manager, session.id)
    rows = await database.load_messages(session.id)
    assert any(row["content"] == "Saved partial" for row in rows)
    cards = [row["tool_input"] for row in rows if row["type"] == "native_subagent"]
    assert next(card for card in reversed(cards) if card["task_id"] == "finished")["status"] == "completed"
    remaining = next(card for card in reversed(cards) if card["task_id"] == "remaining")
    assert (remaining["status"], remaining["stop_reason"]) == ("interrupted", "parent_done")
    assert (await database.read_session_result(session.id, receipt["request_id"])).outcome.status == "success"


async def test_multiple_receivers_rounds_stale_wait_and_duplicate_notifications(native_runtime, tmp_path):
    manager, database, session, _, _, _ = native_runtime
    await start_output(native_runtime)
    spawn = {"method": "item/completed", "params": {"threadId": ROOT, "turnId": "turn-1", "item": {
        "id": "spawn", "type": "collabAgentToolCall", "tool": "spawnAgent", "status": "completed",
        "receiverThreadIds": ["one", "two"], "agentsStates": {}, "prompt": "Two helpers"}}}
    wait = {"method": "item/started", "params": {"threadId": ROOT, "turnId": "turn-1", "item": {
        "id": "old-wait", "type": "collabAgentToolCall", "tool": "wait", "status": "inProgress",
        "receiverThreadIds": ["one"], "agentsStates": {}}}}
    old_wait_end = {"method": "item/completed", "params": {**wait["params"], "item": {
        **wait["params"]["item"], "status": "completed", "agentsStates": {"one": {"status": "completed"}}}}}
    inject(tmp_path, spawn, spawn, child_start("one", "first"), child_start("two", "only"), wait,
        item("one", "first", "answer", "First answer"), child_end("one", "first"),
        child_start("one", "second"), old_wait_end, terminal())
    await wait_dispatch_idle(manager, session.id)
    cards = [row["tool_input"] for row in await database.load_messages(session.id) if row["type"] == "native_subagent"]
    latest = {card["card_id"]: card for card in cards}
    assert len(latest) == 3
    rounds = {card["native_turn_id"]: card for card in latest.values()}
    assert rounds["first"]["status"] == "completed"
    assert rounds["second"]["status"] == "interrupted"
    assert rounds["only"]["status"] == "interrupted"


async def test_card_eviction_preserves_all_native_items_and_retirement_observations(native_runtime, tmp_path):
    manager, database, session, _, _, _ = native_runtime
    await start_output(native_runtime)
    for index in range(65):
        inject(tmp_path, child_start(f"child-{index}", f"round-{index}"),
            item(f"child-{index}", f"round-{index}", "same", f"Partial {index}"))
        await until(lambda index=index: any(run.task_id == f"child-{index}" for run in session._subagents.values()))
    inject(tmp_path, terminal())
    await wait_dispatch_idle(manager, session.id)
    rows = await database.load_messages(session.id)
    partials = [row for row in rows if row["type"] == "text" and row["role"] == "assistant"]
    assert len(partials) == 65
    states = {row["tool_input"]["card_id"]: row["tool_input"] for row in rows if row["type"] == "native_subagent"}
    assert len(states) == 65
    assert all(card["status"] == "interrupted" and card["stop_reason"] == "parent_done" for card in states.values())
    assert len(session._subagents) == 64


async def test_same_raw_tool_id_keeps_separate_root_child_provenance(native_runtime, tmp_path):
    manager, database, session, _, _, _ = native_runtime
    await start_output(native_runtime)
    def tool(thread, turn, phase):
        return {"method": "item/" + phase, "params": {"threadId": thread, "turnId": turn, "item": {
            "id": "same", "type": "commandExecution", "command": "true", "aggregatedOutput": thread, "exitCode": 0}}}
    inject(tmp_path, child_start(), tool(ROOT, "turn-1", "started"), tool("child", "child-turn", "started"),
        tool(ROOT, "turn-1", "completed"), tool("child", "child-turn", "completed"), terminal())
    await wait_dispatch_idle(manager, session.id)
    rows = [row for row in await database.load_messages(session.id) if row["type"] in {"tool_use", "tool_result"}]
    assert len(rows) == 4
    assert rows[0]["runtime_metadata"]["tool_call_id"] == rows[2]["runtime_metadata"]["tool_call_id"]
    assert rows[1]["runtime_metadata"]["tool_call_id"] == rows[3]["runtime_metadata"]["tool_call_id"]
    assert rows[0]["runtime_metadata"]["tool_call_id"] != rows[1]["runtime_metadata"]["tool_call_id"]


async def test_fast_child_completion_then_late_started_patch_has_one_completed_card(native_runtime, tmp_path):
    manager, database, session, _, _, _ = native_runtime
    await start_output(native_runtime)
    inject(tmp_path, child_start(), item("child", "child-turn", "answer", "Fast answer"), child_end(),
        {"method": "item/completed", "params": {"threadId": ROOT, "turnId": "turn-1", "item": {
            "type": "subAgentActivity", "id": "spawn", "kind": "started", "agentThreadId": "child", "agentPath": "/root/fast"}}}, terminal())
    await wait_dispatch_idle(manager, session.id)
    cards = {row["tool_input"]["card_id"]: row["tool_input"] for row in await database.load_messages(session.id)
             if row["type"] == "native_subagent"}
    assert len(cards) == 1
    assert next(iter(cards.values()))["status"] == "completed"
    assert next(iter(cards.values()))["name"] == "/root/fast"


async def test_synthetic_helper_interruption_waits_for_process_group_exit(native_runtime, tmp_path, monkeypatch):
    from server.harness import run as run_module
    manager, _, session, mode, _, events = native_runtime
    mode[0] = "control_group"
    await submit_client_message(manager, session.id, "Native group", [], "output", wiki_mode=None)
    await until(lambda: (tmp_path / "native-group-ready").exists())
    entered, release = asyncio.Event(), asyncio.Event()
    wait_group = run_module.wait_group_exit

    async def hold_proof(*args, **kwargs):
        entered.set()
        await release.wait()
        return await wait_group(*args, **kwargs)

    monkeypatch.setattr(run_module, "wait_group_exit", hold_proof)
    try:
        inject(tmp_path, child_start(), terminal())
        await asyncio.wait_for(entered.wait(), 4)
        assert Path(f"/proc/{(tmp_path / 'native-child-pid').read_text()}").exists()
        assert not any(event.get("type") == "subagent" and event.get("status") == "interrupted" for event in events)
    finally:
        (tmp_path / "native-group-release").touch()
        release.set()
    await wait_dispatch_idle(manager, session.id)
    assert any(event.get("type") == "subagent" and event.get("status") == "interrupted" for event in events)


async def test_locked_native_nested_trace_keeps_actual_child_rounds(native_runtime, tmp_path):
    manager, database, session, _, _, _ = native_runtime
    await start_output(native_runtime)
    trace = json.loads((Path(__file__).parent / "fixtures" / "codex-app-server-0.166-nested.json").read_text())

    def rebase(value):
        if isinstance(value, dict):
            return {key: rebase(child) for key, child in value.items()}
        if isinstance(value, list):
            return [rebase(child) for child in value]
        if value == trace["root_thread_id"]:
            return ROOT
        if value == trace["root_turn_id"]:
            return "turn-1"
        return value

    inject(tmp_path, *rebase(trace["frames"]))
    await wait_dispatch_idle(manager, session.id)
    rows = await database.load_messages(session.id)
    cards = {row["tool_input"]["card_id"]: row["tool_input"] for row in rows if row["type"] == "native_subagent"}
    assert len(cards) == 3 and all(card["status"] == "completed" for card in cards.values())
    text = [row["content"] for row in rows if row["type"] == "text" and row["role"] == "assistant"]
    assert "NESTED_ROOT_DONE" in text and "GRAND_RESULT" in text and "FOLLOWUP_RESULT" in text
    assert session.claude_session_id == ROOT


async def test_interleaved_deltas_keep_source_until_their_own_complete_item(native_runtime, tmp_path):
    manager, _, session, _, _, events = native_runtime
    await start_output(native_runtime)
    def delta(thread, turn, text):
        return {"method": "item/agentMessage/delta", "params": {
            "threadId": thread, "turnId": turn, "itemId": "same", "delta": text}}
    inject(tmp_path, child_start(), delta(ROOT, "turn-1", "Root one"), delta("child", "child-turn", "Child one"),
        delta(ROOT, "turn-1", "Root two"), item("child", "child-turn", "same", "Child full"),
        item(ROOT, "turn-1", "same", "Root full"), child_end(), terminal())
    await wait_dispatch_idle(manager, session.id)
    deltas = [event for event in events if event.get("type") == "assistant_delta"]
    assert [(event["content"], event["runtime_metadata"]["native"]["thread_id"]) for event in deltas] == [
        ("Root one", ROOT), ("Child one", "child"), ("Root two", ROOT)]


async def test_unreadable_necessary_record_cannot_be_hidden_by_root_success(native_runtime, tmp_path):
    manager, database, session, _, _, _ = native_runtime
    receipt = await start_output(native_runtime)
    inject(tmp_path, item(ROOT, "turn-1", "prefix", "Saved prefix"), {"__raw_stdout": '{"method":'}, terminal())
    await wait_dispatch_idle(manager, session.id)
    result = await database.read_session_result(session.id, receipt["request_id"])
    assert result.outcome.status == "failed" and result.fragments == ("Saved prefix",)


async def test_waiting_on_grandchild_does_not_rewrite_its_actual_parent(native_runtime, tmp_path):
    manager, database, session, _, _, _ = native_runtime
    await start_output(native_runtime)
    def spawned(parent, turn, child):
        return {"method": "item/completed", "params": {"threadId": parent, "turnId": turn, "item": {
            "id": "spawn-" + child, "type": "subAgentActivity", "kind": "started", "agentThreadId": child, "agentPath": "/root/middle/grand"}}}
    inject(tmp_path, child_start("middle", "middle-turn"), spawned("middle", "middle-turn", "grand"),
        child_start("grand", "grand-turn"), {"method": "item/completed", "params": {"threadId": ROOT, "turnId": "turn-1", "item": {
            "id": "observe-grand", "type": "collabAgentToolCall", "tool": "wait", "status": "completed", "receiverThreadIds": ["grand"], "agentsStates": {}}}},
        item("grand", "grand-turn", "body", "Grandchild partial"), terminal())
    await wait_dispatch_idle(manager, session.id)
    rows = await database.load_messages(session.id)
    text = next(row for row in rows if row["content"] == "Grandchild partial")
    assert text["runtime_metadata"]["native"]["parent_thread_id"] == "middle"
    states = [row for row in rows if row["type"] == "native_subagent" and row["tool_input"]["task_id"] == "grand"]
    assert states[-1]["tool_input"]["parent_thread_id"] == "middle"
    assert states[-1]["runtime_metadata"]["native"]["item_id"] is None


@pytest.mark.parametrize("followup_finished", [False, True])
async def test_mailbox_interaction_alone_does_not_create_another_work_round(native_runtime, tmp_path, followup_finished):
    manager, database, session, _, _, _ = native_runtime
    await start_output(native_runtime)
    frames = [child_start(), child_end()]
    if followup_finished:
        frames.extend([child_start(turn="fast-followup"), child_end(turn="fast-followup")])
    frames.append({"method": "item/completed", "params": {"threadId": ROOT, "turnId": "turn-1", "item": {
        "id": "mailbox-delivery", "type": "subAgentActivity", "kind": "interacted", "agentThreadId": "child", "agentPath": "/root/helper"}}})
    inject(tmp_path, *frames, terminal())
    await wait_dispatch_idle(manager, session.id)
    rows = await database.load_messages(session.id)
    cards = {row["tool_input"]["card_id"]: row["tool_input"] for row in rows if row["type"] == "native_subagent"}
    assert len(cards) == (2 if followup_finished else 1)
    assert all(card["status"] == "completed" and card["native_turn_id"] for card in cards.values())
    assert any(row["type"] == "tool_result" and "mailbox-delivery" in row["content"] for row in rows)
