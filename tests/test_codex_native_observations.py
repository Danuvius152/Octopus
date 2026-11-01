"""Verified native metadata projects missing exec observations without content."""
from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from server.harness.codex_activity import CodexActivityObserver
from server.harness.events import HarnessEvent

FIXTURE = Path(__file__).parent / "fixtures" / "codex-native-observations-0.162.1.json"
THREAD = "01a13969-6a7f-7dc0-8b8f-8b5655bebbb3"
TURN = "01a13969-6ab4-7d50-bc30-9949a44e0578"
CHILD = "01a13969-9310-7071-8417-04b08e38b486"
SPAWN = "call_a965539f5f964ea185e033f6eec17b52"
FOLLOWUP = "call_8cde9c27be634cfd9ff28d91becceb72"


def append_rows(path: Path, records: list[dict]) -> None:
    with path.open("a") as stream:
        for record in records:
            stream.write(json.dumps(record) + "\n")


@pytest.fixture
async def native_source(tmp_path):
    fixture = json.loads(FIXTURE.read_text())
    started = datetime.fromisoformat(fixture["captured_at"].replace("Z", "+00:00")).timestamp()
    path = tmp_path / "sessions" / f"rollout-probe-{THREAD}.jsonl"
    path.parent.mkdir()
    append_rows(path, [fixture["records"][0]])
    events: list[HarnessEvent] = []
    ticks: list[float] = []
    clock = {"wall": started, "mono": 10.0}
    observer = CodexActivityObserver(
        {"CODEX_HOME": str(tmp_path)}, ticks.append,
        wall_clock=lambda: clock["wall"], monotonic=lambda: clock["mono"],
    )
    observer.set_event_handler(events.extend)
    observer.observe_stdout({"type": "thread.started", "thread_id": THREAD})
    observer.consume_stderr(f"2026-10-14T07:56:12Z INFO codex_exec: Sent prompt with event ID: {TURN}")
    clock.update(wall=started + 30, mono=40.0)
    try:
        yield path, observer, events, ticks, clock, fixture["records"][1:]
    finally:
        await observer.close()


async def test_native_metrics_wait_for_lazily_created_first_rollout(tmp_path):
    started = datetime.now(UTC).timestamp()
    path = tmp_path / "sessions" / f"rollout-{THREAD}.jsonl"
    events = []
    observer = CodexActivityObserver({"CODEX_HOME": str(tmp_path)}, lambda _: None,
        native_thread_id=THREAD, rollout_path=str(path), started_at=started)
    observer.set_event_handler(events.extend)
    try:
        # thread/start returns a path before the first input creates the file.
        await observer.start()
        path.parent.mkdir()
        stamp = datetime.now(UTC).isoformat()
        append_rows(path, [
            {"type": "session_meta", "payload": {"id": THREAD}},
            {"timestamp": stamp, "type": "event_msg", "payload": {"type": "task_started", "turn_id": TURN}},
            {"timestamp": stamp, "type": "token_usage_record", "payload": {"thread_id": THREAD,
                "turn_id": TURN, "response_id": "first-response", "usage": {"output_tokens": 7}}},
            {"timestamp": stamp, "type": "event_msg", "payload": {"type": "task_complete", "turn_id": TURN}},
        ])
        await observer.poll_once()
        assert observer.metrics_status == "verified"
        assert [event.raw["output_tokens"] for event in events] == [7]
    finally:
        await observer.close()


@pytest.mark.asyncio
async def test_real_native_metadata_observes_spawn_and_two_successful_rounds(tmp_path):
    fixture = json.loads(FIXTURE.read_text())
    started = datetime.fromisoformat(fixture["captured_at"].replace("Z", "+00:00")).timestamp()
    path = tmp_path / "sessions" / f"rollout-probe-{THREAD}.jsonl"
    path.parent.mkdir()
    append_rows(path, fixture["records"])
    events: list[HarnessEvent] = []
    ticks: list[float] = []
    clock = {"wall": started, "mono": 10.0}
    observer = CodexActivityObserver(
        {"CODEX_HOME": str(tmp_path)}, ticks.append,
        wall_clock=lambda: clock["wall"], monotonic=lambda: clock["mono"],
    )
    observer.set_event_handler(events.extend)
    observer.observe_stdout({"type": "thread.started", "thread_id": THREAD})
    observer.consume_stderr(f"2026-10-14T07:56:12Z INFO codex_exec: Sent prompt with event ID: {TURN}")
    clock.update(wall=started + 30, mono=40.0)
    try:
        await observer.poll_once()
        updates = [event.subagent for event in events if event.subagent is not None]
        assert [update.status for update in updates] == ["running", "completed", "running", "completed"]
        assert {update.task_id for update in updates} == {CHILD}
        assert [(event.tool_name, event.tool_use_id) for event in events if event.type == "tool_use"] == [
            ("subagent:spawn_agent", SPAWN), ("subagent:followup_task", FOLLOWUP),
        ]
        assert all(not update.summary and not update.prompt for update in updates)
        assert all("[redacted]" not in repr(event) for event in events)
        assert ticks == []
    finally:
        await observer.close()


@pytest.mark.asyncio
async def test_response_metrics_exclude_long_tool_execution_even_when_usage_arrives_late(native_source):
    path, observer, events, ticks, clock, _records = native_source
    started = clock["wall"] - 30

    def row(seconds, kind, payload):
        return {"timestamp": datetime.fromtimestamp(started + seconds, UTC).isoformat(),
                "type": kind, "payload": payload}

    def usage(seconds, output, reasoning):
        return row(seconds, "event_msg", {"type": "token_count", "info": {
            "last_token_usage": {"input_tokens": 10000, "output_tokens": output,
                                 "reasoning_output_tokens": reasoning},
            "total_token_usage": {"output_tokens": output},
        }})

    append_rows(path, [
        row(0, "event_msg", {"type": "task_started", "turn_id": TURN}),
        row(0, "turn_context", {"turn_id": TURN, "model": "gpt-test", "effort": "high"}),
        row(1, "response_item", {"type": "message", "role": "user", "content": "PRIVATE_PROMPT"}),
        row(3, "response_item", {"type": "reasoning", "text": "PRIVATE_REASONING"}),
        row(5, "response_item", {"type": "function_call", "name": "shell", "arguments": "PRIVATE_ARGUMENTS"}),
        row(185, "response_item", {"type": "function_call_output", "output": "PRIVATE_TOOL_OUTPUT"}),
        usage(185.001, 100, 40),
        row(189, "response_item", {"type": "message", "role": "assistant", "content": "PRIVATE_ANSWER"}),
        usage(189.001, 80, 20),
        row(190, "event_msg", {"type": "task_complete", "turn_id": TURN,
                              "duration_ms": 190000, "time_to_first_token_ms": 2000}),
    ])
    clock.update(wall=started + 200, mono=210.0)
    await observer.poll_once()
    measurements = [event for event in events if event.type == "model_measurement"]
    responses = [event for event in measurements if event.raw["kind"] == "codex_response"]
    assert len(responses) == 2
    assert [event.duration_ms for event in responses] == [4000, 4000]
    assert [event.raw["output_tokens"] for event in responses] == [100, 80]
    assert [event.raw["reasoning_tokens"] for event in responses] == [40, 20]
    assert all(event.raw["model"] == "gpt-test" for event in measurements)
    assert all("PRIVATE_" not in repr(event.raw) for event in measurements)
    assert ticks == []


@pytest.mark.asyncio
async def test_wrong_parent_stale_replays_and_retired_observations_cannot_change_work(native_source):
    path, observer, events, ticks, clock, records = native_source
    spawn = next(record for record in records if record.get("payload", {}).get("item", {}).get("kind") == "started")
    wrong_turn = {**spawn, "payload": {**spawn["payload"], "turn_id": "018f0000-aaaa-7bbb-8ccc-dddddddddddd"}}
    wrong_thread = {**spawn, "payload": {**spawn["payload"], "thread_id": "018f0000-aaaa-7bbb-8ccc-dddddddddddd"}}
    old = {**spawn, "payload": {**spawn["payload"], "started_at_ms": 1000}}
    append_rows(path, [wrong_turn, wrong_thread, old])
    await observer.poll_once()
    assert events == ticks == []
    append_rows(path, records)
    await observer.poll_once()
    original = list(events)
    clock.update(wall=clock["wall"] + 20, mono=60.0)
    append_rows(path, records)
    await observer.poll_once()
    assert events == original and ticks == []
    observer.invalidate()
    append_rows(path, [spawn])
    await observer.drain_observations()
    assert events == original and ticks == []


@pytest.mark.asyncio
async def test_only_exact_fresh_followup_header_can_identify_new_execution(native_source):
    path, observer, events, ticks, _clock, records = native_source
    header = next(record for record in records if record.get("payload", {}).get("name") == "followup_task")
    interacted = next(record for record in records if record.get("payload", {}).get("item", {}).get("kind") == "interacted")
    foreign_start = {"type": "event_msg", "payload": {
        "type": "task_started", "turn_id": "018f0000-aaaa-7bbb-8ccc-dddddddddddd",
    }}
    append_rows(path, [foreign_start, header, interacted])
    await observer.poll_once()
    assert events == []
    current_start = next(record for record in records if record.get("payload", {}).get("type") == "task_started")
    private_arguments: dict[str, Any] = {"name": "spawn_agent", "call_id": "fake-spawn", "private": "NATIVE_SECRET"}
    safe_header = {**header, "payload": {**header["payload"], "arguments": json.dumps(private_arguments)}}
    append_rows(path, [current_start, safe_header, interacted])
    await observer.poll_once()
    assert [(event.tool_name, event.tool_use_id) for event in events if event.type == "tool_use"] == [
        ("subagent:followup_task", FOLLOWUP),
    ]
    assert all("NATIVE_SECRET" not in repr(event) and "fake-spawn" not in repr(event) for event in events)
    assert ticks == []


@pytest.mark.asyncio
async def test_normal_retirement_drains_an_existing_prefix_larger_than_one_poll(native_source):
    path, observer, events, ticks, _clock, records = native_source
    # Irrelevant native content must neither consume the observation identity
    # nor prevent the already-written lifecycle records from being collected.
    noise = {"type": "response_item", "payload": {
        "type": "reasoning", "text": "PRIVATE_NATIVE_REASONING" * 1000,
    }}
    append_rows(path, [noise] * 70 + records)
    await observer.drain_observations()
    updates = [event.subagent for event in events if event.subagent]
    assert [update.status for update in updates] == ["running", "completed", "running", "completed"]
    assert all("PRIVATE_NATIVE_REASONING" not in repr(event) for event in events)
    assert ticks == []


@pytest.mark.asyncio
async def test_final_drain_does_not_follow_a_concurrent_appender(native_source, monkeypatch):
    path, observer, events, _ticks, _clock, records = native_source
    append_rows(path, records)
    spawn = next(record for record in records if record.get("payload", {}).get("item", {}).get("kind") == "started")
    next_child = "018f0000-aaaa-7bbb-8ccc-dddddddddddd"
    appended = {**spawn, "payload": {**spawn["payload"], "item": {
        **spawn["payload"]["item"], "id": "call_after_snapshot", "agent_thread_id": next_child,
    }}}
    real_read = os.read
    inode = path.stat().st_ino
    matching_reads = 0

    def read_with_appender(fd, count):
        nonlocal matching_reads
        data = real_read(fd, count)
        if os.fstat(fd).st_ino == inode:
            matching_reads += 1
            # The header read precedes snapshot capture; the actual cursor
            # read follows it. This models another writer at the OS seam.
            if matching_reads == 2:
                append_rows(path, [appended])
        return data

    monkeypatch.setattr(os, "read", read_with_appender)
    await observer.drain_observations()
    assert {event.subagent.task_id for event in events if event.subagent} == {CHILD}
    await observer.poll_once()
    assert {event.subagent.task_id for event in events if event.subagent} == {CHILD, next_child}


@pytest.mark.asyncio
async def test_incompatible_native_lifecycle_falls_back_with_redacted_warning(native_source, caplog):
    path, observer, events, ticks, _clock, records = native_source
    spawn = next(record for record in records if record.get("payload", {}).get("item", {}).get("kind") == "started")
    incompatible = {**spawn, "payload": {**spawn["payload"], "item": {
        **spawn["payload"]["item"], "kind": "private-new-provider-kind",
    }}}
    append_rows(path, [incompatible])
    await observer.poll_once()
    assert events == ticks == []
    assert "native_observation_schema" in caplog.text
    assert "private-new-provider-kind" not in caplog.text and str(path) not in caplog.text


@pytest.mark.asyncio
async def test_observation_projection_failure_is_a_redacted_fallback(native_source, caplog):
    path, observer, events, ticks, _clock, records = native_source

    def unavailable_projection(_events):
        raise RuntimeError("PRIVATE_PROJECTION_DETAILS")

    observer.set_event_handler(unavailable_projection)
    append_rows(path, records)
    await observer.poll_once()
    assert events == ticks == []
    assert "native_projection_failed" in caplog.text
    assert "PRIVATE_PROJECTION_DETAILS" not in caplog.text


@pytest.mark.asyncio
async def test_conflicting_native_spawn_identity_does_not_claim_another_child(tmp_path, caplog):
    fixture = json.loads(FIXTURE.read_text())
    started = datetime.fromisoformat(fixture["captured_at"].replace("Z", "+00:00")).timestamp()
    spawn = next(record for record in fixture["records"] if record.get("payload", {}).get("item", {}).get("kind") == "started")
    path = tmp_path / "sessions" / f"rollout-probe-{THREAD}.jsonl"
    path.parent.mkdir()
    append_rows(path, [fixture["records"][0], spawn])
    events: list[HarnessEvent] = []
    clock = {"wall": started, "mono": 10.0}
    observer = CodexActivityObserver(
        {"CODEX_HOME": str(tmp_path)}, lambda _: None,
        wall_clock=lambda: clock["wall"], monotonic=lambda: clock["mono"],
    )
    observer.set_event_handler(events.extend)
    observer.observe_stdout({"type": "thread.started", "thread_id": THREAD})
    observer.consume_stderr(f"2026-10-14T07:56:12Z INFO codex_exec: Sent prompt with event ID: {TURN}")
    clock.update(wall=started + 30, mono=40.0)
    try:
        await observer.poll_once()
        conflicting = {**spawn, "payload": {**spawn["payload"], "item": {
            **spawn["payload"]["item"], "agent_thread_id": "018f0000-aaaa-7bbb-8ccc-dddddddddddd",
        }}}
        append_rows(path, [conflicting])
        await observer.poll_once()
        assert [event.subagent.task_id for event in events if event.subagent] == [CHILD]
        assert "native_call_identity_conflict" in caplog.text
        assert CHILD not in caplog.text and str(path) not in caplog.text
    finally:
        await observer.close()


@pytest.mark.asyncio
async def test_modern_usage_before_items_and_legacy_duplicate_are_one_response(native_source):
    path, observer, events, _ticks, clock, _records = native_source
    started = clock["wall"] - 30

    def row(seconds, kind, payload):
        return {"timestamp": datetime.fromtimestamp(started + seconds, UTC).isoformat(),
                "type": kind, "payload": payload}

    counts = {"input_tokens": 3000, "output_tokens": 60, "reasoning_output_tokens": 40}
    append_rows(path, [
        row(0, "event_msg", {"type": "task_started", "turn_id": TURN}),
        row(0, "turn_context", {"turn_id": TURN, "model": "gpt-modern"}),
        row(1, "response_item", {"type": "message", "role": "user"}),
        row(4, "token_usage_record", {"response_id": "response-1", "usage": counts}),
        row(4.001, "response_item", {"type": "reasoning"}),
        row(5, "response_item", {"type": "function_call"}),
        row(6, "event_msg", {"type": "token_count", "info": {"last_token_usage": counts}}),
        row(90, "response_item", {"type": "function_call_output"}),
        row(92, "token_usage_record", {"response_id": "response-2", "usage": counts}),
        row(94, "response_item", {"type": "message", "role": "assistant"}),
        row(95, "token_usage_record", {"response_id": "response-2", "usage": counts}),
        row(95, "event_msg", {"type": "task_complete", "turn_id": TURN}),
    ])
    clock.update(wall=started + 100, mono=110.0)
    await observer.drain_observations()
    responses = [event for event in events if event.raw.get("kind") == "codex_response"]
    assert [event.raw["response_id"] for event in responses] == ["response-1", "response-2"]
    assert [event.duration_ms for event in responses] == [4000, 4000]
    assert [event.raw["output_tokens"] for event in responses] == [60, 60]


@pytest.mark.asyncio
async def test_legacy_snapshot_then_modern_carrier_do_not_create_phantom_response(native_source):
    path, observer, events, _ticks, clock, _records = native_source
    started = clock["wall"] - 30

    def row(seconds, kind, payload):
        return {"timestamp": datetime.fromtimestamp(started + seconds, UTC).isoformat(),
                "type": kind, "payload": payload}

    counts = {"output_tokens": 60, "reasoning_output_tokens": 40}
    append_rows(path, [
        row(0, "event_msg", {"type": "task_started", "turn_id": TURN}),
        row(1, "response_item", {"type": "message", "role": "user"}),
        row(5, "response_item", {"type": "message", "role": "assistant"}),
        row(5.001, "event_msg", {"type": "token_count", "info": {"last_token_usage": counts}}),
        row(5.002, "token_usage_record", {"response_id": "response-1", "usage": counts}),
        row(6, "event_msg", {"type": "task_complete", "turn_id": TURN}),
    ])
    await observer.drain_observations()
    responses = [event for event in events if event.raw.get("kind") == "codex_response"]
    assert len(responses) == 1
    assert responses[0].duration_ms == 4000


@pytest.mark.asyncio
async def test_usage_between_reasoning_and_answer_waits_for_last_generated_item(native_source):
    path, observer, events, _ticks, clock, _records = native_source
    started = clock["wall"] - 30

    def row(seconds, kind, payload):
        return {"timestamp": datetime.fromtimestamp(started + seconds, UTC).isoformat(),
                "type": kind, "payload": payload}

    append_rows(path, [
        row(0, "event_msg", {"type": "task_started", "turn_id": TURN}),
        row(1, "response_item", {"type": "message", "role": "user"}),
        row(2, "response_item", {"type": "reasoning"}),
        row(3, "token_usage_record", {"response_id": "response-middle", "usage": {"output_tokens": 60}}),
        row(5, "response_item", {"type": "message", "role": "assistant"}),
        row(6, "event_msg", {"type": "task_complete", "turn_id": TURN}),
    ])
    await observer.drain_observations()
    responses = [event for event in events if event.raw.get("kind") == "codex_response"]
    assert len(responses) == 1
    assert responses[0].duration_ms == 4000
    assert responses[0].raw["completed_at"] == started + 5


@pytest.mark.asyncio
async def test_legacy_snapshot_after_tool_result_cannot_invalidate_next_response(native_source):
    path, observer, events, _ticks, clock, _records = native_source
    started = clock["wall"] - 30

    def row(seconds, kind, payload):
        return {"timestamp": datetime.fromtimestamp(started + seconds, UTC).isoformat(),
                "type": kind, "payload": payload}

    def usage(seconds, output):
        return row(seconds, "event_msg", {"type": "token_count", "info": {
            "last_token_usage": {"output_tokens": output}}})

    append_rows(path, [
        row(0, "event_msg", {"type": "task_started", "turn_id": TURN}),
        row(1, "response_item", {"type": "message", "role": "user"}),
        row(5, "response_item", {"type": "function_call"}), usage(6, 60),
        row(90, "response_item", {"type": "function_call_output"}), usage(90.001, 60),
        row(94, "response_item", {"type": "message", "role": "assistant"}), usage(94.001, 80),
        row(95, "event_msg", {"type": "task_complete", "turn_id": TURN}),
    ])
    clock.update(wall=started + 100, mono=110.0)
    await observer.drain_observations()
    responses = [event for event in events if event.raw.get("kind") == "codex_response"]
    assert [(event.raw["output_tokens"], event.duration_ms) for event in responses] == [(60, 4000), (80, 4000)]
