"""Execution activity at the Harness boundary is independent of display output."""
from __future__ import annotations

import asyncio
import json

import pytest

from server.harness import RunConfig
from server.harness.codex import CODEX_WEB_LEAF as CODEX
from server.harness.run import HarnessRun


@pytest.mark.asyncio
async def test_empty_reasoning_updates_activity_without_display():
    run = HarnessRun(CODEX)
    ticks: list[float | None] = []
    token = run.bind_activity(ticks.append)
    await run._handle_line(json.dumps({
        "type": "item.completed",
        "item": {"id": "item_0", "type": "reasoning", "text": ""},
    }))
    assert len(ticks) == 1
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(anext(run.stream()), 0.01)
    run.unbind_activity(token)


@pytest.mark.parametrize("sessions_exists", [False, True])
async def test_native_rollout_published_after_thread_identity_is_still_observed(tmp_path, caplog, sessions_exists):
    from server.harness.codex_activity import CodexActivityObserver

    thread = "018f0000-1111-7222-8333-444444444444"
    turn = "018f0000-5555-7666-8777-888888888888"
    root = tmp_path / "sessions"
    if sessions_exists:
        root.mkdir()
    ticks = []
    observer = CodexActivityObserver({"CODEX_HOME": str(tmp_path)}, ticks.append,
                                    wall_clock=lambda: 101.0, monotonic=lambda: 11.0)
    observer.observe_stdout({"type": "thread.started", "thread_id": thread})
    observer.consume_stderr(f"2026-10-19T00:00:00Z INFO codex_exec: Sent prompt with event ID: {turn}")
    await observer.poll_once()
    root.mkdir(exist_ok=True)
    path = root / f"rollout-late-{thread}.jsonl"
    path.write_text(json.dumps({"type": "session_meta", "payload": {"id": thread}}) + "\n" + json.dumps({
        "type": "event_msg", "payload": {"type": "item_completed", "thread_id": thread,
        "turn_id": turn, "item": {"type": "Reasoning", "id": "late-reasoning"},
        "started_at_ms": 101000, "completed_at_ms": 101000},
    }) + "\n")
    await observer.drain_observations()
    assert ticks == [11.0]
    assert observer.identity_confirmed
    assert "rollout_missing" not in caplog.text
    await observer.close()


async def test_terminal_discovery_finishes_a_late_fork_rollout_in_a_large_store(tmp_path, caplog):
    from server.harness.codex_activity import CodexActivityObserver

    thread = "018f0000-1111-4222-8333-444444444444"
    turn = "018f0000-5555-7666-8777-888888888888"
    root = tmp_path / "sessions"
    root.mkdir()
    for index in range(3000):
        (root / f"unrelated-{index}.jsonl").touch()
    ticks = []
    observer = CodexActivityObserver({"CODEX_HOME": str(tmp_path)}, ticks.append,
                                    wall_clock=lambda: 101.0, monotonic=lambda: 11.0)
    observer.observe_stdout({"type": "thread.started", "thread_id": thread})
    observer.consume_stderr(f"2026-10-19T00:00:00Z INFO codex_exec: Sent prompt with event ID: {turn}")
    for _ in range(6):
        await observer.poll_once()
    path = root / f"rollout-late-{thread}.jsonl"
    path.write_text(json.dumps({"type": "session_meta", "payload": {"id": thread}}) + "\n" + json.dumps({
        "type": "event_msg", "payload": {"type": "item_completed", "thread_id": thread,
        "turn_id": turn, "item": {"type": "Reasoning", "id": "late-fork"},
        "started_at_ms": 101000, "completed_at_ms": 101000},
    }) + "\n")
    await observer.drain_observations()
    observer.invalidate()
    await observer.close()
    assert ticks == [11.0]
    assert "rollout_missing" not in caplog.text


async def test_terminal_discovery_reports_its_limit_without_adopting_an_unverified_match(tmp_path, caplog, monkeypatch):
    from contextlib import contextmanager

    from server.harness import codex_activity

    thread = "018f0000-1111-4222-8333-444444444444"
    turn = "018f0000-5555-7666-8777-888888888888"
    root = tmp_path / "sessions"
    root.mkdir()
    for index in range(100):
        (root / f"unrelated-{index}.jsonl").touch()
    ticks = []
    observer = codex_activity.CodexActivityObserver({"CODEX_HOME": str(tmp_path)}, ticks.append,
                                                  wall_clock=lambda: 101.0, monotonic=lambda: 11.0)
    observer.observe_stdout({"type": "thread.started", "thread_id": thread})
    observer.consume_stderr(f"2026-10-19T00:00:00Z INFO codex_exec: Sent prompt with event ID: {turn}")
    await observer.poll_once()
    path = root / f"rollout-late-{thread}.jsonl"
    path.write_text(json.dumps({"type": "session_meta", "payload": {"id": thread}}) + "\n" + json.dumps({
        "type": "event_msg", "payload": {"type": "item_completed", "thread_id": thread,
        "turn_id": turn, "item": {"type": "Reasoning", "id": "limited-fork"},
        "started_at_ms": 101000, "completed_at_ms": 101000},
    }) + "\n")
    scan = codex_activity.os.scandir

    @contextmanager
    def candidate_first(directory):
        with scan(directory) as entries:
            yield iter(sorted(entries, key=lambda entry: entry.name != path.name))

    monkeypatch.setattr(codex_activity.os, "scandir", candidate_first)
    monkeypatch.setattr(codex_activity, "_FINAL_DISCOVERY_ENTRIES", 8)
    await observer.drain_observations()
    observer.invalidate()
    await observer.close()
    assert ticks == []
    assert "rollout_discovery_limit" in caplog.text
    assert "rollout_missing" not in caplog.text

@pytest.mark.asyncio
async def test_native_metadata_requires_current_process_identity(tmp_path):
    from server.harness.codex_activity import CodexActivityObserver

    thread = "018f0000-1111-7222-8333-444444444444"
    turn = "018f0000-5555-7666-8777-888888888888"
    path = tmp_path / "sessions" / "2026" / "10" / "14" / f"rollout-test-{thread}.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"type": "session_meta", "payload": {"id": thread}}) + "\n")
    ticks = []
    observer = CodexActivityObserver({"CODEX_HOME": str(tmp_path)}, ticks.append,
                                    wall_clock=lambda: 100.0, monotonic=lambda: 10.0)
    observer.observe_stdout({"type": "thread.started", "thread_id": thread})
    await observer.poll_once()
    row = {"type": "event_msg", "payload": {"type": "item_completed",
           "thread_id": thread, "turn_id": turn, "item": {"type": "Reasoning", "id": "rs_1"},
           "started_at_ms": 100000, "completed_at_ms": 100000}}
    with path.open("a") as f:
        f.write(json.dumps(row) + "\n")
    await observer.poll_once()
    assert ticks == []
    assert observer.consume_stderr(f"2026-10-14T00:00:00Z  INFO codex_exec: Sent prompt with event ID: {turn}")
    with path.open("a") as f:
        f.write(json.dumps({**row, "payload": {**row["payload"], "item": {"type": "Reasoning", "id": "rs_2"}}}) + "\n")
    await observer.poll_once()
    assert ticks == [10.0]
    await observer.close()

@pytest.mark.asyncio
async def test_noise_retired_generation_and_terminal_cannot_update_clock():
    run = HarnessRun(CODEX)
    first: list[float | None] = []
    second: list[float | None] = []
    old = run.bind_activity(first.append)
    current = run.bind_activity(second.append)
    run.unbind_activity(old)
    for obj in ({"type": "unknown"}, {"type": "item.completed", "item": {"id": "x", "type": "unknown"}}):
        await run._handle_line(json.dumps(obj))
    assert first == second == []
    await run._handle_line(json.dumps({"type": "item.completed", "item": {"id": "item_0", "type": "reasoning"}}))
    assert first == [] and len(second) == 1
    await run._handle_line('{"type":"turn.completed"}')
    count = len(second)
    await run._handle_line(json.dumps({"type": "item.completed", "item": {"id": "item_1", "type": "reasoning"}}))
    assert len(second) == count
    run.unbind_activity(current)


@pytest.mark.asyncio
async def test_native_metadata_rejects_stale_wrong_turn_replay_and_cancel(tmp_path):
    from server.harness.codex_activity import CodexActivityObserver

    thread = "018f0000-1111-7222-8333-444444444444"
    turn = "018f0000-5555-7666-8777-888888888888"
    path = tmp_path / "sessions" / f"rollout-test-{thread}.jsonl"
    path.parent.mkdir()
    path.write_text(json.dumps({"type": "session_meta", "payload": {"id": thread}}) + "\n")
    clock = {"wall": 100.0, "mono": 10.0}
    ticks = []
    observer = CodexActivityObserver({"CODEX_HOME": str(tmp_path)}, ticks.append,
                                    wall_clock=lambda: clock["wall"], monotonic=lambda: clock["mono"])
    observer.observe_stdout({"type": "thread.started", "thread_id": thread})
    observer.consume_stderr(f"2026-10-14T00:00:00Z  INFO codex_exec: Sent prompt with event ID: {turn}")
    await observer.poll_once()

    def append(identifier, **fields):
        payload = {"type": "item_completed", "thread_id": thread, "turn_id": turn,
                   "item": {"type": "Reasoning", "id": identifier},
                   "started_at_ms": 100000, "completed_at_ms": 101000, **fields}
        with path.open("a") as f:
            f.write(json.dumps({"type": "event_msg", "payload": payload}) + "\n")

    clock.update(wall=103.0, mono=13.0)
    append("wrong", turn_id="018f0000-aaaa-7bbb-8ccc-dddddddddddd")
    append("old", started_at_ms=99000)
    append("valid")
    await observer.poll_once()
    # Use the actual completion time, not the observer's later read time.
    assert ticks == [11.0]
    clock.update(wall=110.0, mono=20.0)
    append("valid")
    await observer.poll_once()
    assert ticks == [11.0]
    clock.update(wall=150.0, mono=60.0)
    append("stale")
    await observer.poll_once()
    assert ticks == [11.0]
    observer.invalidate()
    append("after_cancel", completed_at_ms=150000)
    await observer.poll_once()
    assert ticks == [11.0]
    await observer.close()


@pytest.mark.asyncio
async def test_new_diagnostics_are_consumed_and_conflict_disables_native_source(tmp_path, caplog):
    from server.harness.codex_activity import CodexActivityObserver

    observer = CodexActivityObserver({"CODEX_HOME": str(tmp_path)}, lambda _: None)
    turn = "018f0000-5555-7666-8777-888888888888"
    assert observer.consume_stderr(f"2026-10-14T00:00:00Z INFO codex_exec: Sent prompt with event ID: {turn}")
    assert observer.consume_stderr("\x1b[32m2026-10-14T00:00:00Z  INFO codex_exec: init secret user metadata\x1b[0m")
    assert observer.consume_stderr("2026-10-14T00:00:00Z WARN codex_exec: private diagnostic details")
    assert not observer.consume_stderr("2026-10-14T00:00:00Z ERROR codex_exec: existing error")
    assert observer.consume_stderr("changed formatter INFO codex_exec Sent prompt with event ID: unsupported")
    assert "diagnostic_format" in caplog.text
    assert "private" not in caplog.text and "secret" not in caplog.text
    await observer.close()

@pytest.mark.asyncio
async def test_duplicate_stdout_records_do_not_grant_activity():
    run = HarnessRun(CODEX)
    ticks: list[float | None] = []
    run.bind_activity(ticks.append)
    obj = {"type": "item.completed", "item": {"id": "item_0", "type": "reasoning", "text": "readable"}}
    await run._handle_line(json.dumps(obj))
    await run._handle_line(json.dumps(obj))
    assert len(ticks) == 1


def test_stdout_activity_survives_long_attempt_and_accepts_old_tool_completion():
    parser = CODEX.new_event_parser()
    command = {"id": "item_0", "type": "command_execution", "command": "sleep 10"}
    assert parser.parse({"type": "item.started", "item": command}).activity is True
    fresh = 0
    for index in range(1, 5001):
        result = parser.parse({"type": "item.completed", "item": {
            "id": f"item_{index}", "type": "reasoning", "text": "",
        }})
        fresh += result.activity is True
    assert fresh == 5000
    completed = {"type": "item.completed", "item": {**command, "exit_code": 0}}
    assert parser.parse(completed).activity is True
    assert parser.parse(completed).activity is False
    assert parser.parse({"type": "item.started", "item": command}).activity is False
    assert parser.parse({"type": "item.completed", "item": {
        "id": "item_1", "type": "reasoning", "text": "",
    }}).activity is False


def test_unversioned_updates_cannot_renew_stdout_activity():
    parser = CODEX.new_event_parser()
    item = {"id": "item_0", "type": "todo_list", "items": []}
    assert parser.parse({"type": "item.started", "item": item}).activity is True
    for status in ("pending", "done", "pending"):
        changed = {**item, "items": [{"text": "plan", "completed": status == "done"}]}
        assert parser.parse({"type": "item.updated", "item": changed}).activity is False
    completed = {"type": "item.completed", "item": item}
    assert parser.parse(completed).activity is True
    assert parser.parse(completed).activity is False


def test_repeated_startup_and_malformed_items_cannot_refresh_activity():
    parser = CODEX.new_event_parser()
    startup = {"type": "thread.started", "thread_id": "018f0000-1111-7222-8333-444444444444"}
    assert parser.parse(startup).activity is True
    assert parser.parse(startup).activity is False
    assert parser.parse({"type": "turn.started"}).activity is True
    assert parser.parse({"type": "turn.started"}).activity is False
    assert parser.parse({"type": "item.completed", "item": {
        "id": "item_0", "type": ["unknown"],
    }}).activity is False


def test_unknown_stdout_identity_falls_back_without_exposing_payload(caplog):
    parser = CODEX.new_event_parser()
    result = parser.parse({"type": "item.completed", "item": {
        "id": "future-format", "type": "agent_message", "text": "private reply",
    }})
    assert result.activity is None
    assert [event.content for event in result.events] == ["private reply"]
    assert "identity unsupported" in caplog.text and "private reply" not in caplog.text
    assert parser.parse({"type": "item.completed", "item": {
        "id": "item_0", "type": "reasoning", "text": "",
    }}).activity is True


@pytest.mark.asyncio
async def test_native_activity_continues_past_4096_and_replays_cannot_renew_it(tmp_path):
    from server.harness.codex_activity import CodexActivityObserver

    thread = "018f0000-1111-7222-8333-444444444444"
    turn = "018f0000-5555-7666-8777-888888888888"
    path = tmp_path / "sessions" / f"rollout-test-{thread}.jsonl"
    path.parent.mkdir()
    path.write_text(json.dumps({"type": "session_meta", "payload": {"id": thread}}) + "\n")
    clock = {"wall": 100.0, "mono": 10.0}
    ticks = []
    observer = CodexActivityObserver(
        {"CODEX_HOME": str(tmp_path)}, ticks.append,
        wall_clock=lambda: clock["wall"], monotonic=lambda: clock["mono"],
    )
    observer.observe_stdout({"type": "thread.started", "thread_id": thread})
    observer.consume_stderr(f"2026-10-14T00:00:00Z INFO codex_exec: Sent prompt with event ID: {turn}")
    await observer.poll_once()

    def record(index):
        return {"type": "event_msg", "payload": {
            "type": "item_completed", "thread_id": thread, "turn_id": turn,
            "item": {"type": "Reasoning", "id": f"rs_{index}"},
            "started_at_ms": 100000, "completed_at_ms": 100000 + index,
        }}

    try:
        for batch in range(5):
            with path.open("a") as stream:
                for index in range(batch * 1000 + 1, (batch + 1) * 1000 + 1):
                    stream.write(json.dumps(record(index)) + "\n")
            clock.update(wall=101.0 + batch, mono=11.0 + batch)
            await observer.poll_once()
        assert len(ticks) == 5000
        assert ticks[-1] == 15.0
        clock.update(wall=110.0, mono=20.0)
        with path.open("a") as stream:
            for index in (1, 4096, 5000):
                stream.write(json.dumps(record(index)) + "\n")
        await observer.poll_once()
        assert len(ticks) == 5000 and ticks[-1] == 15.0
    finally:
        await observer.close()


@pytest.mark.asyncio
async def test_delayed_native_activity_uses_source_time_instead_of_a_fresh_idle_window(tmp_path):
    from server.harness.codex_activity import CodexActivityObserver

    thread = "018f0000-1111-7222-8333-444444444444"
    turn = "018f0000-5555-7666-8777-888888888888"
    path = tmp_path / "sessions" / f"rollout-test-{thread}.jsonl"
    path.parent.mkdir()
    path.write_text(json.dumps({"type": "session_meta", "payload": {"id": thread}}) + "\n")
    clock = {"wall": 100.0, "mono": 10.0}
    ticks = []
    observer = CodexActivityObserver(
        {"CODEX_HOME": str(tmp_path)}, ticks.append,
        wall_clock=lambda: clock["wall"], monotonic=lambda: clock["mono"],
    )
    observer.observe_stdout({"type": "thread.started", "thread_id": thread})
    observer.consume_stderr(f"2026-10-14T00:00:00Z INFO codex_exec: Sent prompt with event ID: {turn}")
    await observer.poll_once()
    row = {"type": "event_msg", "payload": {
        "type": "item_completed", "thread_id": thread, "turn_id": turn,
        "item": {"type": "Reasoning", "id": "rs_delayed"},
        "started_at_ms": 100000, "completed_at_ms": 110000,
    }}
    try:
        clock.update(wall=150.0, mono=60.0)
        with path.open("a") as stream:
            stream.write(json.dumps(row) + "\n")
        await observer.poll_once()
        assert ticks == [20.0]
        clock.update(wall=500.0, mono=410.0)
        with path.open("a") as stream:
            stream.write(json.dumps(row) + "\n")
        await observer.poll_once()
        assert ticks == [20.0]
    finally:
        await observer.close()


@pytest.mark.asyncio
async def test_native_discovery_continues_across_large_fork_store(tmp_path, caplog):
    from server.harness.codex_activity import CodexActivityObserver

    # Native fork copies use UUIDv4 and cannot be located by a birth-date hint.
    thread = "018f0000-1111-4222-8333-444444444444"
    turn = "018f0000-5555-7666-8777-888888888888"
    root = tmp_path / "sessions"
    root.mkdir()
    for index in range(1500):
        (root / f"unrelated-{index}.jsonl").touch()
    path = root / f"rollout-fork-{thread}.jsonl"
    path.write_text(json.dumps({"type": "session_meta", "payload": {"id": thread}}) + "\n")
    with path.open("a") as stream:
        stream.write(json.dumps({"type": "event_msg", "payload": {
            "type": "item_completed", "thread_id": thread, "turn_id": turn,
            "item": {"type": "Reasoning", "id": "rs_fork"},
            "started_at_ms": 100000, "completed_at_ms": 100000,
        }}) + "\n")
    ticks = []
    observer = CodexActivityObserver(
        {"CODEX_HOME": str(tmp_path)}, ticks.append,
        wall_clock=lambda: 100.0, monotonic=lambda: 10.0,
    )
    observer.observe_stdout({"type": "thread.started", "thread_id": thread})
    observer.consume_stderr(f"2026-10-14T00:00:00Z INFO codex_exec: Sent prompt with event ID: {turn}")
    try:
        for _ in range(3):
            await observer.poll_once()
        assert ticks == [10.0]
        assert observer.identity_confirmed
        assert caplog.text == ""
    finally:
        await observer.close()


@pytest.mark.asyncio
async def test_confirmed_process_identity_can_wait_for_incremental_discovery(tmp_path):
    import asyncio

    from server.harness.codex_activity import CodexActivityObserver

    thread = "018f0000-1111-4222-8333-444444444444"
    turn = "018f0000-5555-7666-8777-888888888888"
    root = tmp_path / "sessions"
    root.mkdir()
    for index in range(4000):
        (root / f"unrelated-{index}.jsonl").touch()
    path = root / f"rollout-fork-{thread}.jsonl"
    path.write_text(json.dumps({"type": "session_meta", "payload": {"id": thread}}) + "\n")
    with path.open("a") as stream:
        stream.write(json.dumps({"type": "event_msg", "payload": {
            "type": "item_completed", "thread_id": thread, "turn_id": turn,
            "item": {"type": "Reasoning", "id": "rs_fork"},
            "started_at_ms": 100000, "completed_at_ms": 100000,
        }}) + "\n")
    clock = {"wall": 100.0, "mono": 10.0}
    ready = asyncio.Event()
    ticks = []

    def notify(tick):
        ticks.append(tick)
        ready.set()

    observer = CodexActivityObserver(
        {"CODEX_HOME": str(tmp_path)}, notify,
        wall_clock=lambda: clock["wall"], monotonic=lambda: clock["mono"],
    )
    observer.observe_stdout({"type": "thread.started", "thread_id": thread})
    observer.consume_stderr(f"2026-10-14T00:00:00Z INFO codex_exec: Sent prompt with event ID: {turn}")
    clock.update(wall=140.0, mono=50.0)
    try:
        await observer.start()
        await asyncio.wait_for(ready.wait(), 1)
        assert ticks == [10.0]
    finally:
        await observer.close()


@pytest.mark.asyncio
async def test_process_diagnostics_remain_local_and_do_not_publish_activity_metadata(tmp_path):
    import os
    import sys
    from dataclasses import replace

    thread = "018f0000-1111-7222-8333-444444444444"
    turn = "018f0000-5555-7666-8777-888888888888"
    prefix = f'2026-10-14T00:00:00Z INFO codex.exec{{otel.kind="internal" thread.id="{thread}"}}: codex_exec: '
    script = (
        "import sys,json; "
        f"print({prefix + 'private init metadata'!r},file=sys.stderr); "
        f"print({prefix + 'Sent prompt with event ID: ' + turn!r},file=sys.stderr); "
        "print('2026-10-14T00:00:00Z WARN codex_exec: private warning',file=sys.stderr); "
        "print('private warning continuation',file=sys.stderr); "
        "print('2026-10-14T00:00:00Z ERROR codex_exec: existing error',file=sys.stderr); "
        f"print({json.dumps({'type': 'thread.started', 'thread_id': thread})!r}); "
        "print('{\"type\":\"turn.completed\"}')"
    )
    profile = replace(CODEX, build_turn_argv=lambda _: (
        [sys.executable, "-c", script], {"env": {**os.environ, "CODEX_HOME": str(tmp_path)}}
    ))
    run = HarnessRun(profile)
    await run.start("synthetic prompt", str(tmp_path))
    try:
        events = [event async for event in run.stream()]
    finally:
        await run.stop()
    assert "private init metadata" in run.stderr_text
    assert run.stderr_text.endswith("2026-10-14T00:00:00Z ERROR codex_exec: existing error")
    assert all("private" not in (event.content or "") for event in events)


@pytest.mark.asyncio
async def test_native_wrong_header_falls_back_with_redacted_warning(tmp_path, caplog):
    from server.harness.codex_activity import CodexActivityObserver

    thread = "018f0000-1111-7222-8333-444444444444"
    path = tmp_path / "sessions" / f"rollout-test-{thread}.jsonl"
    path.parent.mkdir()
    path.write_text(json.dumps({"type": "session_meta", "payload": {"id": "private-invalid-identity"}}) + "\n")
    ticks = []
    observer = CodexActivityObserver({"CODEX_HOME": str(tmp_path)}, ticks.append)
    observer.observe_stdout({"type": "thread.started", "thread_id": thread})
    await observer.poll_once()
    assert not observer.identity_confirmed and ticks == []
    assert "rollout_identity_mismatch" in caplog.text
    assert "private-invalid-identity" not in caplog.text and str(tmp_path) not in caplog.text
    await observer.close()


@pytest.mark.asyncio
async def test_native_observer_refuses_sessions_symlink(tmp_path, caplog):
    from server.harness.codex_activity import CodexActivityObserver

    home = tmp_path / "home"
    home.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (home / "sessions").symlink_to(outside, target_is_directory=True)
    observer = CodexActivityObserver({"CODEX_HOME": str(home)}, lambda _: pytest.fail("symlink activity"))
    observer.observe_stdout({"type": "thread.started", "thread_id": "018f0000-1111-7222-8333-444444444444"})
    await observer.poll_once()
    assert not observer.identity_confirmed
    assert "rollout_root_symlink" in caplog.text
    assert str(outside) not in caplog.text
    await observer.close()


@pytest.mark.asyncio
async def test_cancel_during_activity_startup_drains_owned_observer(tmp_path, monkeypatch):
    import asyncio
    from dataclasses import replace

    entered = asyncio.Event()
    closed = asyncio.Event()

    class WaitingObserver:
        def set_event_handler(self, handler):
            pass

        async def drain_observations(self):
            pass

        def observe_stdout(self, obj):
            pass

        def consume_stderr(self, line):
            return False

        async def start(self):
            entered.set()
            await asyncio.Event().wait()

        def invalidate(self):
            pass

        async def close(self, *, deadline=None):
            closed.set()
            return True

    async def forbidden_spawn(*args, **kwargs):
        pytest.fail("cancelled startup spawned a CLI")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden_spawn)
    profile = replace(CODEX, activity_observer=lambda *args, **kwargs: WaitingObserver())
    run = HarnessRun(profile, RunConfig(web_research=True, mcp_servers=[]))
    run.bind_activity(lambda _: None)
    starting = asyncio.create_task(run.start("prompt", str(tmp_path)))
    await entered.wait()
    starting.cancel()
    await asyncio.gather(starting, return_exceptions=True)
    await run.stop()
    assert closed.is_set()


@pytest.mark.asyncio
async def test_unbound_run_keeps_local_diagnostics_without_starting_native_reader(tmp_path):
    import sys
    from dataclasses import replace

    class Scrubber:
        def set_event_handler(self, handler):
            pass

        async def drain_observations(self):
            pass

        def observe_stdout(self, obj):
            pass

        def consume_stderr(self, line):
            return True

        async def start(self):
            pytest.fail("unbound run started a native activity reader")

        def invalidate(self):
            pass

        async def close(self, *, deadline=None):
            return True

    profile = replace(CODEX, activity_observer=lambda *args, **kwargs: Scrubber(),
                      build_turn_argv=lambda _: ([sys.executable, "-c", "import sys; print('private diagnostic', file=sys.stderr)"], {}))
    run = HarnessRun(profile)
    await run.start("prompt", str(tmp_path))
    try:
        assert [event async for event in run.stream()] == []
    finally:
        await run.stop()
    assert run.stderr_text == "private diagnostic"


@pytest.mark.asyncio
@pytest.mark.parametrize("complete_line", [True, False])
async def test_oversize_activity_metadata_falls_back_with_redacted_warning(tmp_path, caplog, complete_line):
    from server.harness.codex_activity import CodexActivityObserver

    thread = "018f0000-1111-7222-8333-444444444444"
    turn = "018f0000-5555-7666-8777-888888888888"
    path = tmp_path / "sessions" / f"rollout-test-{thread}.jsonl"
    path.parent.mkdir()
    path.write_text(json.dumps({"type": "session_meta", "payload": {"id": thread}}) + "\n")
    ticks = []
    observer = CodexActivityObserver({"CODEX_HOME": str(tmp_path)}, ticks.append)
    observer.observe_stdout({"type": "thread.started", "thread_id": thread})
    observer.consume_stderr(f"2026-10-14T00:00:00Z INFO codex_exec: Sent prompt with event ID: {turn}")
    await observer.poll_once()
    row = {"type": "event_msg", "payload": {"type": "item_completed", "thread_id": thread,
           "turn_id": turn, "item": {"type": "Reasoning", "id": "large_item",
           "summary": ["private-oversize-metadata" + "x" * (300 * 1024)]},
           "started_at_ms": 100000, "completed_at_ms": 100000}}
    with path.open("a") as f:
        f.write(json.dumps(row) + ("\n" if complete_line else ""))
    await observer.poll_once()
    assert ticks == []
    assert "activity_line_limit" in caplog.text
    assert "private-oversize-metadata" not in caplog.text and str(path) not in caplog.text
    await observer.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("complete_line", [True, False])
async def test_oversize_unrelated_native_body_remains_safely_skipped(tmp_path, caplog, complete_line):
    from server.harness.codex_activity import CodexActivityObserver

    thread = "018f0000-1111-7222-8333-444444444444"
    turn = "018f0000-5555-7666-8777-888888888888"
    path = tmp_path / "sessions" / f"rollout-test-{thread}.jsonl"
    path.parent.mkdir()
    path.write_text(json.dumps({"type": "session_meta", "payload": {"id": thread}}) + "\n")
    ticks = []
    observer = CodexActivityObserver({"CODEX_HOME": str(tmp_path)}, ticks.append,
                                    wall_clock=lambda: 100.0, monotonic=lambda: 10.0)
    observer.observe_stdout({"type": "thread.started", "thread_id": thread})
    observer.consume_stderr(f"2026-10-14T00:00:00Z INFO codex_exec: Sent prompt with event ID: {turn}")
    await observer.poll_once()
    body = {"type": "response_item", "payload": {"type": "reasoning", "encrypted_content": "x" * (300 * 1024)}}
    with path.open("a") as f:
        f.write(json.dumps(body) + ("\n" if complete_line else ""))
    await observer.poll_once()
    assert ticks == [] and caplog.text == ""
    row = {"type": "event_msg", "payload": {"type": "item_completed", "thread_id": thread,
           "turn_id": turn, "item": {"type": "Reasoning", "id": "valid_after_body"},
           "started_at_ms": 100000, "completed_at_ms": 100000}}
    with path.open("a") as f:
        f.write(("" if complete_line else "\n") + json.dumps(row) + "\n")
    await observer.poll_once()
    assert ticks == [10.0] and caplog.text == ""
    await observer.close()
