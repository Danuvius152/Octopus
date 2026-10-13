"""Execution activity at the Harness boundary is independent of display output."""
from __future__ import annotations

import json

import pytest

from server.harness.codex import CODEX
from server.harness.run import HarnessRun


@pytest.mark.asyncio
async def test_empty_reasoning_updates_activity_without_display():
    run = HarnessRun(CODEX)
    ticks: list[float | None] = []
    token = run.bind_activity(ticks.append)
    await run._handle_line(json.dumps({
        "type": "item.completed",
        "item": {"id": "reasoning_1", "type": "reasoning", "text": ""},
    }))
    assert len(ticks) == 1
    assert run._event_queue.empty()
    run.unbind_activity(token)

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
    await run._handle_line(json.dumps({"type": "item.completed", "item": {"id": "r", "type": "reasoning"}}))
    assert first == [] and len(second) == 1
    await run._handle_line('{"type":"turn.completed"}')
    count = len(second)
    await run._handle_line(json.dumps({"type": "item.completed", "item": {"id": "late", "type": "reasoning"}}))
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
    obj = {"type": "item.completed", "item": {"id": "one", "type": "reasoning", "text": "readable"}}
    await run._handle_line(json.dumps(obj))
    await run._handle_line(json.dumps(obj))
    assert len(ticks) == 1


@pytest.mark.asyncio
async def test_diagnostic_reader_never_exposes_new_metadata(tmp_path):
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
    assert run.stderr_text == "2026-10-14T00:00:00Z ERROR codex_exec: existing error"
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
        def observe_stdout(self, obj):
            pass

        def consume_stderr(self, line):
            return False

        async def start(self):
            entered.set()
            await asyncio.Event().wait()

        def invalidate(self):
            pass

        async def close(self):
            closed.set()

    async def forbidden_spawn(*args, **kwargs):
        pytest.fail("cancelled startup spawned a CLI")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden_spawn)
    profile = replace(CODEX, activity_observer=lambda *args, **kwargs: WaitingObserver())
    run = HarnessRun(profile)
    run.bind_activity(lambda _: None)
    starting = asyncio.create_task(run.start("prompt", str(tmp_path)))
    await entered.wait()
    starting.cancel()
    await asyncio.gather(starting, return_exceptions=True)
    await run.stop()
    assert closed.is_set()


@pytest.mark.asyncio
async def test_unbound_run_scrubs_diagnostics_without_starting_native_reader(tmp_path):
    import sys
    from dataclasses import replace

    class Scrubber:
        def observe_stdout(self, obj):
            pass

        def consume_stderr(self, line):
            return True

        async def start(self):
            pytest.fail("unbound run started a native activity reader")

        def invalidate(self):
            pass

        async def close(self):
            pass

    profile = replace(CODEX, activity_observer=lambda *args, **kwargs: Scrubber(),
                      build_turn_argv=lambda _: ([sys.executable, "-c", "import sys; print('private diagnostic', file=sys.stderr)"], {}))
    run = HarnessRun(profile)
    await run.start("prompt", str(tmp_path))
    try:
        assert [event async for event in run.stream()] == []
    finally:
        await run.stop()
    assert run.stderr_text == ""


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
