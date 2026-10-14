"""Verified native metadata projects missing exec observations without content."""
from __future__ import annotations

import json
import os
from datetime import datetime
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
