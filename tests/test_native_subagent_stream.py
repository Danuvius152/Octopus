"""Missing Codex stdout observations, through the real HarnessRun boundary."""

from __future__ import annotations

import json
import os
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from server.harness import get_harness
from server.harness.codex import CODEX_WEB_LEAF as CODEX
from server.harness.run import HarnessRun
from tests.test_native_subagent_retirement import native_runtime as native_runtime

FIXTURE = Path(__file__).parent / "fixtures" / "codex-native-observations-0.162.1.json"


def native_profile(tmp_path, records, *, stdout_before=(), terminal=True):
    thread = records[0]["payload"]["id"]
    turn = records[1]["payload"]["turn_id"]
    home = tmp_path / "codex"
    sessions = home / "sessions"
    sessions.mkdir(parents=True)
    script = tmp_path / "cli.py"
    script.write_text(
        "import json,sys,time\n"
        "from datetime import datetime,UTC\n"
        "from pathlib import Path\n"
        f"records=json.loads({json.dumps(json.dumps(records))})\n"
        "base=int(time.time()*1000)+50\n"
        "for index,record in enumerate(records):\n"
        "    record['timestamp']=datetime.fromtimestamp((base+index)/1000,UTC).isoformat()\n"
        "    payload=record.get('payload',{})\n"
        "    if payload.get('type')=='item_completed':\n"
        "        payload['started_at_ms']=payload['completed_at_ms']=base+index\n"
        f"print('2026-10-14T08:00:00Z INFO codex_exec: Sent prompt with event ID: {turn}',file=sys.stderr,flush=True)\n"
        f"print({json.dumps(json.dumps({'type': 'thread.started', 'thread_id': thread}))},flush=True)\n"
        + "".join(f"print({json.dumps(json.dumps(record))},flush=True)\n" for record in stdout_before)
        +
        f"Path({str(sessions / ('rollout-' + thread + '.jsonl'))!r}).write_text(''.join(json.dumps(record)+'\\n' for record in records))\n"
        + ("print(json.dumps({'type':'turn.completed','usage':{}}),flush=True)\n" if terminal else "")
    )

    def argv(context):
        return [sys.executable, "-u", str(script)], {
            "cwd": context.working_dir, "env": {**os.environ, "CODEX_HOME": str(home)},
        }

    return replace(CODEX, build_turn_argv=argv)


async def collect_native(tmp_path, records, **options):
    run = HarnessRun(native_profile(tmp_path, records, **options))
    run.bind_tool_context("request", "attempt")
    run.bind_activity(lambda timestamp: None)
    try:
        await run.start("collect both helper rounds", str(tmp_path))
        events = [event async for event in run.stream()]
    finally:
        await run.stop()
    return events


async def test_native_helpers_complete_at_parent_terminal_even_without_stdout_tool_items(tmp_path):
    events = await collect_native(tmp_path, json.loads(FIXTURE.read_text())["records"])
    calls = [event for event in events if event.type == "tool_use"]
    assert [event.tool_name for event in calls] == ["subagent:spawn_agent", "subagent:followup_task"]
    assert all(event.runtime_metadata["attempt_id"] == "attempt" for event in calls)
    completions = [event for event in events if event.type == "subagent" and event.subagent.status == "completed"]
    assert len(completions) == 2
    assert [event.subagent.tool_call_id for event in completions] == [
        event.runtime_metadata["tool_call_id"] for event in calls
    ]
    assert completions[0].subagent.tool_call_id != completions[1].subagent.tool_call_id
    assert completions[0].subagent.task_id == completions[1].subagent.task_id
    assert all(event.subagent.summary == "" for event in completions)
    assert events[-1].type == "result"


async def test_overlapping_followup_is_unconfirmed_instead_of_borrowing_a_child_completion(tmp_path):
    records = json.loads(FIXTURE.read_text())["records"]
    records = records[:4] + records[6:8] + records[4:6] + records[8:]
    events = await collect_native(tmp_path, records)
    calls = [event for event in events if event.type == "tool_use"]
    updates = [event.subagent for event in events if event.type == "subagent"]
    assert len(calls) == 2
    assert [update.status for update in updates] == ["running", "unknown"]
    assert updates[-1].stop_reason == "overlapping_execution"
    assert updates[-1].tool_call_id == calls[-1].runtime_metadata["tool_call_id"]


async def test_message_to_a_completed_helper_does_not_invent_a_new_execution(tmp_path):
    records = json.loads(FIXTURE.read_text())["records"]
    records[6]["payload"]["name"] = "send_message"
    events = await collect_native(tmp_path, records)
    assert [event.tool_name for event in events if event.type == "tool_use"] == [
        "subagent:spawn_agent", "subagent:send_message",
    ]
    updates = [event.subagent for event in events if event.type == "subagent"]
    assert [update.status for update in updates] == ["running", "completed"]
    assert len({update.tool_call_id for update in updates}) == 1


async def test_explicit_helper_interrupt_is_not_overwritten_by_a_later_completion(tmp_path):
    records = json.loads(FIXTURE.read_text())["records"]
    records[6]["payload"]["name"] = "interrupt_agent"
    records[7]["payload"]["item"]["kind"] = "interrupted"
    events = await collect_native(tmp_path, records[:4] + records[6:8] + records[4:6] + records[8:])
    updates = [event.subagent for event in events if event.type == "subagent"]
    assert [update.status for update in updates] == ["running", "interrupted"]
    assert updates[-1].stop_reason == "native_interrupted"
    assert updates[-1].tool_call_id == updates[0].tool_call_id


async def test_cleanup_interrupt_cannot_rewrite_a_confirmed_helper_completion(tmp_path):
    records = json.loads(FIXTURE.read_text())["records"]
    records[6]["payload"]["name"] = "interrupt_agent"
    records[7]["payload"]["item"]["kind"] = "interrupted"
    events = await collect_native(tmp_path, records)
    updates = [event.subagent for event in events if event.type == "subagent"]
    assert [update.status for update in updates] == ["running", "completed"]
    assert any(event.tool_name == "subagent:interrupt_agent" for event in events)


async def test_stdout_spawn_and_verified_native_metadata_share_one_child_call(tmp_path):
    records = json.loads(FIXTURE.read_text())["records"]
    child = records[3]["payload"]["item"]["agent_thread_id"]
    stdout = [
        {"type": "item.started", "item": {"type": "collab_tool_call", "tool": "spawn_agent", "id": "item_0"}},
        {"type": "item.completed", "item": {"type": "collab_tool_call", "tool": "spawn_agent", "id": "item_0",
            "status": "completed", "receiver_thread_ids": [child], "agents_states": {child: {"status": "running"}}}},
    ]
    events = await collect_native(tmp_path, records, stdout_before=stdout)
    calls = [event for event in events if event.type == "tool_use"]
    assert [event.tool_use_id for event in calls] == ["item_0", records[6]["payload"]["call_id"]]
    updates = [event.subagent for event in events if event.type == "subagent"]
    assert [update.status for update in updates] == ["running", "completed", "running", "completed"]
    assert updates[0].tool_call_id == updates[1].tool_call_id == calls[0].runtime_metadata["tool_call_id"]


async def test_process_eof_without_a_result_still_reports_observed_helpers(tmp_path):
    records = json.loads(FIXTURE.read_text())["records"][:4]
    events = await collect_native(tmp_path, records, terminal=False)
    updates = [event.subagent for event in events if event.type == "subagent"]
    assert len(updates) == 1 and updates[0].status == "running"
    assert updates[0].tool_call_id
    assert not any(event.type == "result" for event in events)


async def test_stdout_explicit_continuation_preserves_the_previous_helper_card(tmp_path):
    records = json.loads(FIXTURE.read_text())["records"]
    child = records[3]["payload"]["item"]["agent_thread_id"]
    stdout = []
    for lifecycle, tool, identifier, status, answer in [
        ("item.started", "spawn_agent", "item_0", "", ""),
        ("item.completed", "spawn_agent", "item_0", "completed", "First confirmed answer"),
        ("item.started", "send_input", "item_1", "", ""),
        ("item.completed", "send_input", "item_1", "running", ""),
        ("item.completed", "wait", "item_2", "completed", "Second confirmed answer"),
    ]:
        stdout.append({"type": lifecycle, "item": {
            "id": identifier, "type": "collab_tool_call", "tool": tool,
            "status": "completed" if lifecycle == "item.completed" else "in_progress",
            "receiver_thread_ids": [child], "agents_states": {child: {"status": status, "message": answer}},
        }})
    events = await collect_native(tmp_path, records[:2], stdout_before=stdout)
    calls = [event for event in events if event.type == "tool_use"]
    done = [event.subagent for event in events if event.type == "subagent" and event.subagent.status == "completed"]
    assert [child.summary for child in done] == ["First confirmed answer", "Second confirmed answer"]
    assert [child.tool_call_id for child in done] == [call.runtime_metadata["tool_call_id"] for call in calls]
    assert done[0].tool_call_id != done[1].tool_call_id


@pytest.mark.parametrize("late_status", ["completed", "errored"])
async def test_wait_started_in_an_old_child_round_cannot_finish_a_new_round(tmp_path, late_status):
    records = json.loads(FIXTURE.read_text())["records"]
    child = records[3]["payload"]["item"]["agent_thread_id"]
    stdout = []
    for lifecycle, tool, identifier, state in [
        ("item.started", "spawn_agent", "item_0", {}),
        ("item.completed", "spawn_agent", "item_0", {"status": "completed", "message": "First answer"}),
        ("item.started", "wait", "item_1", {}),
        ("item.started", "send_input", "item_2", {}),
        ("item.completed", "send_input", "item_2", {}),
        ("item.completed", "wait", "item_1", {"status": late_status, "message": "Late observation"}),
    ]:
        stdout.append({"type": lifecycle, "item": {
            "id": identifier, "type": "collab_tool_call", "tool": tool,
            "status": "completed" if lifecycle == "item.completed" else "in_progress",
            "receiver_thread_ids": [child], "agents_states": {child: state},
        }})
    events = await collect_native(tmp_path, records[:2], stdout_before=stdout)
    new_call = next(event for event in events if event.type == "tool_use" and event.tool_name == "subagent:send_input")
    new_updates = [event.subagent for event in events if event.type == "subagent"
                   and event.subagent.tool_call_id == new_call.runtime_metadata["tool_call_id"]]
    assert [update.status for update in new_updates] == ["running"]
    assert new_updates[0].summary == ""
    assert events[-2].subagent.tool_call_id is None


async def test_unavailable_continuation_cannot_claim_the_old_helper_answer(tmp_path):
    records = json.loads(FIXTURE.read_text())["records"]
    child = records[3]["payload"]["item"]["agent_thread_id"]
    stdout = []
    for lifecycle, tool, identifier, state in [
        ("item.started", "spawn_agent", "item_0", {}),
        ("item.completed", "spawn_agent", "item_0", {"status": "completed", "message": "First answer"}),
        ("item.started", "send_input", "item_1", {}),
        ("item.completed", "send_input", "item_1", {"status": "not_found"}),
        ("item.completed", "wait", "item_2", {"status": "completed", "message": "First answer"}),
    ]:
        stdout.append({"type": lifecycle, "item": {
            "id": identifier, "type": "collab_tool_call", "tool": tool,
            "status": "completed" if lifecycle == "item.completed" else "in_progress",
            "receiver_thread_ids": [child], "agents_states": {child: state},
        }})
    events = await collect_native(tmp_path, records[:2], stdout_before=stdout)
    unavailable = next(event for event in events if event.type == "tool_use" and event.tool_name == "subagent:send_input")
    assert not any(event.type == "subagent" and event.subagent.tool_call_id == unavailable.runtime_metadata["tool_call_id"]
                   for event in events)


async def test_native_metadata_cards_and_readable_tools_agree_in_live_and_rest_views(native_runtime, monkeypatch, tmp_path):
    manager, session, client, events, _ = native_runtime
    records = json.loads(FIXTURE.read_text())["records"]
    monkeypatch.setattr(get_harness("codex"), "profile", native_profile(tmp_path, records))
    receipt = await manager.start_message(session.id, "Collect both native helper rounds")
    completion = await receipt.wait()
    assert completion.outcome.status == "success"
    response = await client.get(f"/api/sessions/{session.id}")
    assert response.status_code == 200
    snapshot = response.json()
    helpers = snapshot["subagents"]
    assert [helper["status"] for helper in helpers] == ["completed", "completed"]
    assert len({helper["task_id"] for helper in helpers}) == 1
    calls = [message for message in snapshot["messages"] if message["type"] == "tool_use"]
    assert [message["tool_name"] for message in calls] == ["subagent:spawn_agent", "subagent:followup_task"]
    assert [helper["tool_call_id"] for helper in helpers] == [message["runtime_metadata"]["tool_call_id"] for message in calls]
    live = [event for event in events if event.get("type") == "subagent" and event.get("status") == "completed"]
    assert [event["tool_call_id"] for event in live] == [helper["tool_call_id"] for helper in helpers]
    results = [message["content"] for message in snapshot["messages"] if message["type"] == "tool_result"]
    assert results == ["Started /root/wire_probe", "Accepted /root/wire_probe"]
    assert all(not helper["summary"] and not helper["prompt"] for helper in helpers)
    assert "[redacted]" not in json.dumps(snapshot)
    assert "Sent prompt with event ID:" not in json.dumps(snapshot)


@pytest.mark.parametrize("bad_state", [{"status": []}, {"status": {}}, {"status": "future_status"}, []])
async def test_incompatible_child_state_cannot_claim_a_continuation_or_crash_the_reader(tmp_path, caplog, bad_state):
    records = json.loads(FIXTURE.read_text())["records"]
    child = records[3]["payload"]["item"]["agent_thread_id"]
    stdout = [
        {"type": "item.started", "item": {"id": "item_0", "type": "collab_tool_call", "tool": "spawn_agent"}},
        {"type": "item.completed", "item": {"id": "item_0", "type": "collab_tool_call", "tool": "spawn_agent",
            "status": "completed", "receiver_thread_ids": [child], "agents_states": {child: {"status": "completed"}}}},
        {"type": "item.started", "item": {"id": "item_1", "type": "collab_tool_call", "tool": "send_input"}},
        {"type": "item.completed", "item": {"id": "item_1", "type": "collab_tool_call", "tool": "send_input",
            "status": "completed", "receiver_thread_ids": [child], "agents_states": {child: bad_state}}},
    ]
    events = await collect_native(tmp_path, records[:2], stdout_before=stdout)
    continuation = next(event for event in events if event.type == "tool_use" and event.tool_name == "subagent:send_input")
    assert not any(event.type == "subagent" and event.subagent.tool_call_id == continuation.runtime_metadata["tool_call_id"]
                   for event in events)
    assert events[-1].type == "result" and not events[-1].is_error
    assert "event parse crashed" not in caplog.text
