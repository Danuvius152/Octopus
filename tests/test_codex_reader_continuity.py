"""Real native stdout must share the loop with consumers and RPC waiters."""

import asyncio

import pytest

from server.harness import get_harness
from server.sessions.client_submissions import submit_client_message
from tests.native_app_server import (
    ROOT,
    inject,
    item,
    question_request,
    start_output,
    terminal,
    until,
)
from tests.native_app_server import native_runtime as native_runtime
from tests.session_runtime import wait_dispatch_idle


async def test_one_binding_batch_delivers_to_consumer_before_its_last_event(native_runtime, tmp_path):
    _, _, _, mode, _, _ = native_runtime
    mode[0] = "control_delayed_ack"
    run = get_harness("codex").create_run()
    run.bind_tool_context("original-input", "original-attempt")
    async def record(_):
        pass
    run.bind_input_recorder(record)
    backlog = []
    collected = []

    async def consume():
        async for value in run.stream():
            if value.type == "text":
                backlog.append(len(run._event_queue))
                collected.append(value.content)

    starting = asyncio.create_task(run.start("Original input", str(tmp_path)))
    consuming = asyncio.create_task(consume())
    try:
        await until(lambda: (tmp_path / "native-business.jsonl").exists())
        inject(tmp_path, *[item(ROOT, "turn-1", f"part-{i}", f"Part {i}") for i in range(20)])
        await until(lambda: len(run._driver._buffer) == 21)  # turn/started plus the items
        inject(tmp_path, {"method": "item/completed", "params": {"threadId": ROOT, "turnId": "turn-1",
            "item": {"id": "input", "type": "userMessage", "content": [], "clientId": "original-input"}}})
        await asyncio.wait_for(starting, 3)
        await until(lambda: len(collected) == 20)
        assert backlog[0] < 19, "Consumer first ran only after the entire bind batch was queued"
        inject(tmp_path, terminal())
        await asyncio.wait_for(consuming, 3)
        assert collected == [f"Part {i}" for i in range(20)]
        assert run.collection_error is None and run.retirement_state == "proven"
    finally:
        await run.stop()
        await asyncio.gather(starting, consuming, return_exceptions=True)


@pytest.mark.parametrize("kind", ["diagnostic", "ignored", "duplicate"])
async def test_prebuffered_zero_event_frames_allow_rpc_waiter_to_advance(native_runtime, tmp_path, monkeypatch, kind):
    await start_output(native_runtime)
    run = native_runtime.session._run.backend
    entered, release = asyncio.Event(), asyncio.Event()
    handle = run._handle_line
    processed = []

    async def gated(line):
        if line == "INFO gate-reader":
            entered.set()
            await release.wait()
        await handle(line)
        if "zero-event-marker" in line:
            processed.append(line)

    monkeypatch.setattr(run, "_handle_line", gated)
    inject(tmp_path, {"__raw_stdout": "INFO gate-reader"})
    await asyncio.wait_for(entered.wait(), 3)
    if kind == "diagnostic":
        frames = [{"__raw_stdout": f"INFO zero-event-marker {i}"} for i in range(50)]
    elif kind == "ignored":
        frames = [{"method": "future/diagnostic", "params": {"zero-event-marker": i}} for i in range(50)]
    else:
        frames = [{"method": "turn/started", "params": {"threadId": ROOT,
            "turn": {"id": "turn-1"}, "zero-event-marker": i}} for i in range(50)]
    with run._driver._rpc.request("test/control", {}) as request:
        async def wait_for_rpc():
            assert await request.response == {"control": "received"}
            return len(processed)

        waiting = asyncio.create_task(wait_for_rpc())
        try:
            inject(tmp_path, {"id": request.identifier, "result": {"control": "received"}}, *frames)
            # Force the actual StreamReader to have the complete suffix ready;
            # the test cannot pass through accidental pipe-read suspensions.
            await until(lambda: b"zero-event-marker" in run._process.stdout._buffer
                        and run._process.stdout._buffer.count(b"zero-event-marker") == 50)
            release.set()
            seen = await asyncio.wait_for(waiting, 3)
            assert seen < 50, "RPC waiter resumed only after all prebuffered frames were processed"
            await until(lambda: len(processed) == 50)
            assert run.collection_error is None
        finally:
            release.set()
            await asyncio.gather(waiting, return_exceptions=True)
    inject(tmp_path, terminal())


async def test_stop_during_binding_delivery_keeps_the_captured_prefix_and_closes_question(native_runtime, tmp_path):
    manager, database, session, mode, starts, _ = native_runtime
    mode[0] = "control_delayed_ack"
    stopping = []

    async def stop_after_first(event):
        if event.get("type") == "assistant_text" and not stopping:
            stopping.append(asyncio.create_task(manager.interrupt(session.id)))

    manager.on_broadcast("stop-during-bind", stop_after_first)
    receipt = await submit_client_message(manager, session.id, "Stop during delivery", [], "bind-stop", wiki_mode=None)
    await until(lambda: (tmp_path / "native-business.jsonl").exists())
    run = session._run.backend
    inject(tmp_path, *[item(ROOT, "turn-1", f"part-{i}", f"Saved part {i}") for i in range(10)],
           question_request("captured-question"),
           *[item(ROOT, "turn-1", f"part-{i}", f"Saved part {i}") for i in range(10, 20)])
    await until(lambda: len(run._driver._buffer) == 22)
    inject(tmp_path, {"method": "item/completed", "params": {"threadId": ROOT, "turnId": "turn-1",
        "item": {"id": "original-input", "type": "userMessage", "content": [], "clientId": receipt["request_id"]}}})
    await until(lambda: stopping)
    await asyncio.wait_for(stopping[0], 4)
    await wait_dispatch_idle(manager, session.id)
    saved = await database.read_session_result(session.id, receipt["request_id"])
    assert saved.outcome.status == "interrupted"
    assert saved.fragments == tuple(f"Saved part {i}" for i in range(20))
    assert not manager.get_pending_questions(session.id)
    assert run.retirement_state == "proven" and len(starts) == 1
    rows = [row for row in await database.load_messages(session.id) if row["type"] == "text" and row["role"] == "assistant"]
    assert all(row["runtime_metadata"]["request_id"] == receipt["request_id"] for row in rows)
    assert len({row["runtime_metadata"]["attempt_id"] for row in rows}) == 1
