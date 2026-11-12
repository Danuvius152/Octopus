"""Work cancellation joins the original inline write without stopping its carrier."""

from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from server.harness import get_harness
from server.harness.claude_code import ClaudeDriver
from server.harness.run import HarnessRun
from tests import test_question_lifecycle as question_tests
from tests.harness_profiles import process_profile

question_host = question_tests.question_host


@pytest.mark.parametrize("write_result", ["confirmed", "unknown"])
async def test_claude_stdin_backpressure_publishes_output_before_the_original_steer(
    question_host, tmp_path, monkeypatch, write_result,
):
    manager, original, client, events = question_host
    session = await manager.create_session(original.agent_id, "Original stdin order", str(tmp_path), backend="claude-code")
    emit, read, finish, received = [tmp_path / name for name in ("emit-output", "read-input", "finish", "received.json")]
    model = tmp_path / "claude_stdin_order.py"
    model.write_text(
        "import json,sys,time\nfrom pathlib import Path\n"
        "emit,read,finish,received=map(Path,sys.argv[1:])\n"
        "initial=json.loads(sys.stdin.readline())\n"
        "print(json.dumps({'type':'system','subtype':'init','session_id':'11111111-2222-4333-8444-555555555555'}),flush=True)\n"
        "def text(value):\n"
        " print(json.dumps({'type':'assistant','message':{'role':'assistant','content':[{'type':'text','text':value}]}}),flush=True)\n"
        "text('Ready')\n"
        "while not emit.exists(): time.sleep(.005)\n"
        "print(json.dumps({'type':'stream_event','event':{'type':'content_block_delta','delta':{'type':'text_delta','text':'Output partial'}}}),flush=True)\n"
        "text('Output while stdin is blocked')\n"
        "while not read.exists(): time.sleep(.005)\n"
        "line=json.loads(sys.stdin.readline())\n"
        "partial=received.with_suffix('.partial')\n"
        "partial.write_text(json.dumps({'initial':initial,'inline':line}))\n"
        "partial.replace(received)\n"
        "while not finish.exists(): time.sleep(.005)\n"
        "print(json.dumps({'type':'result','subtype':'success','is_error':False,'result':''}),flush=True)\n"
        "sys.stdin.read()\n"
    )
    harness = get_harness("claude-code")
    native = harness.profile

    def argv(context):
        rendered, kwargs = native.build_turn_argv(context)
        assert rendered[rendered.index("--input-format") + 1] == "stream-json"
        return [sys.executable, "-u", str(model), str(emit), str(read), str(finish), str(received)], kwargs

    # Retain Claude's real JSONL driver, parser and retirement contract.
    monkeypatch.setattr(harness, "profile", replace(native, binary=sys.executable, build_turn_argv=argv))
    ready, blocked, output, delivered = (asyncio.Event() for _ in range(4))
    processes, writers = [], []
    spawn, drain = asyncio.create_subprocess_exec, asyncio.StreamWriter.drain
    drain_calls = 0

    async def capture_process(*args, **kwargs):
        process = await spawn(*args, **kwargs)
        if str(model) in args:
            processes.append(process)
        return process

    async def observe_drain(writer):
        nonlocal drain_calls
        if processes and writer is processes[0].stdin:
            drain_calls += 1
            if drain_calls == 2:
                writers.append(asyncio.current_task())
                blocked.set()
                await drain(writer)
                if write_result == "unknown":
                    raise OSError("Original stdin drain acknowledgement lost")
                return
        await drain(writer)

    async def observe(event):
        if event.get("session_id") != session.id:
            return
        if event.get("type") == "assistant_text":
            if event.get("content") == "Ready":
                ready.set()
            if event.get("content") == "Output while stdin is blocked":
                output.set()
        if event.get("type") == "steered":
            delivered.set()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", capture_process)
    monkeypatch.setattr(asyncio.StreamWriter, "drain", observe_drain)
    manager.on_broadcast("original-stdin-order", observe)
    correction = "Saved correction\n" + "x" * (1024 * 1024)
    try:
        primary = await manager.start_message(session.id, "First", request_id="stdin-original")
        await asyncio.wait_for(ready.wait(), 3)
        initial = (await client.get(f"/api/sessions/{session.id}")).json()
        inline = await manager.start_message(session.id, correction, steerable=True, request_id="stdin-inline")
        assert inline.disposition == "steered"
        await asyncio.wait_for(blocked.wait(), 3)
        writer = processes[0].stdin
        assert writer.transport.get_write_buffer_size() > writer.transport.get_write_buffer_limits()[1]
        stored = await manager.db.load_messages(session.id)
        assert stored[-1]["seq"] == 2 and stored[-1]["content"] == correction
        assert not delivered.is_set() and not writers[0].done()
        emit.touch()
        await asyncio.wait_for(output.wait(), 3)
        during = (await client.get(f"/api/sessions/{session.id}")).json()
        assert during["next_message_seq"] == 4
        assert [row["seq"] for row in during["messages"]] == [0, 1, 2, 3]
        assert not delivered.is_set() and not writers[0].done() and not received.exists()
        read.touch()
        if write_result == "confirmed":
            await asyncio.wait_for(delivered.wait(), 3)
        else:
            completion = await asyncio.wait_for(inline.wait(), 3)
            assert completion.state == "unavailable" and completion.reason == "steer_delivery_unknown"
            assert completion.input_request_id == inline.request_id and completion.request_id == primary.request_id
        async with asyncio.timeout(3):
            while not received.exists():
                await asyncio.sleep(.005)
        frames = json.loads(received.read_text())
        assert frames["inline"]["message"]["content"] == correction
        assert frames["inline"]["uuid"] != frames["initial"]["uuid"]
        finish.touch()
        assert (await asyncio.wait_for(primary.wait(), 3)).outcome.status == "success"
        await manager.await_session_execution(session.id, timeout=3)
        own_events = [event for event in events if event.get("session_id") == session.id]
        late = [event for event in own_events if event.get("type") == "steered"]
        assert len(late) == (1 if write_result == "confirmed" else 0)
        assert len(processes) == 1 and drain_calls == 2
        if write_result == "confirmed":
            replay = json.loads((Path(__file__).parents[1] / "web/src/lib/steeringReplay.json").read_text())
            attempt = next(event["attempt_id"] for event in own_events if event["type"] == "attempt_started")

            def identity(metadata):
                assert metadata["request_id"] == primary.request_id and metadata["attempt_id"] == attempt
                return {"request_id": "stdin-original", "attempt_id": "original-attempt"}

            def message(row):
                value = {key: row[key] for key in ("role", "type", "seq", "content")}
                if value["content"] == correction:
                    value["content"] = "Saved correction"
                # Claude stores the attempt on text and publishes it on the
                # WS envelope; compare the shared identity, not one encoding.
                context = row.get("runtime_metadata") or (row.get("tool_input") or {}).get("attempt_context")
                if context:
                    value["runtime_metadata"] = identity(context)
                return value

            for snapshot, expected in ((initial, replay["initial"]), (during, replay["during"])):
                assert {"messages": [message(row) for row in snapshot["messages"]],
                        "next_message_seq": snapshot["next_message_seq"]} == expected
            produced = next(event for event in own_events if event.get("content") == "Output while stdin is blocked")
            assert own_events.index(produced) < own_events.index(late[0])
            assert {"type": produced["type"], "seq": produced["seq"], "session_id": "stdin-order",
                    "content": produced["content"], **identity(produced)} == replay["output"]
            delta = next(event for event in own_events if event.get("type") == "assistant_delta")
            assert {"type": delta["type"], "session_id": "stdin-order", "content": delta["content"], **identity(delta)} == replay["delta"]
            assert late[0]["content"] == correction
            assert {"type": late[0]["type"], "seq": late[0]["seq"], "session_id": "stdin-order", "content": "Saved correction"} == replay["steer"]
    finally:
        emit.touch()
        read.touch()
        finish.touch()
        await manager.interrupt(session.id)
        await manager.await_session_execution(session.id, timeout=5)


@pytest.mark.parametrize("write_result", ["confirmed", "exception", "cancelled"])
async def test_closing_inline_work_waits_for_its_original_write_and_preserves_the_carrier(
    question_host, tmp_path, monkeypatch, write_result,
):
    manager, original, client, _events = question_host
    session = await manager.create_session(
        original.agent_id, "Cancel work with its correction in stdin", str(tmp_path), backend="codex",
    )
    frames, finish = tmp_path / "received.jsonl", tmp_path / "finish-carrier"
    model = tmp_path / "work_write_model.py"
    model.write_text(
        "import json,select,sys\nfrom pathlib import Path\n"
        "received=Path(sys.argv[1]); finish=Path(sys.argv[2])\n"
        "initial=sys.stdin.readline()\n"
        "received.write_text(initial)\n"
        "print(json.dumps({'type':'thread.started','thread_id':'original-work-carrier'}),flush=True)\n"
        "print(json.dumps({'type':'item.completed','item':{'id':'ready','type':'agent_message','text':'Carrier A is still running'}}),flush=True)\n"
        "while not finish.exists():\n"
        "    if not select.select([sys.stdin],[],[],.01)[0]: continue\n"
        "    line=sys.stdin.readline()\n"
        "    if not line: sys.exit(0)\n"
        "    with received.open('a') as output: output.write(line)\n"
        "    print(json.dumps({'type':'item.completed','item':{'id':'correction','type':'agent_message','text':'Carrier received: '+json.loads(line)['message']['content']}}),flush=True)\n"
        "print(json.dumps({'type':'turn.completed','usage':{}}),flush=True)\n"
        "sys.stdin.read()\n"
    )
    harness = get_harness("codex")
    monkeypatch.setattr(harness, "profile", process_profile(
        harness.profile, binary=sys.executable, activity_observer=None, prepare_resume=None,
        new_driver=ClaudeDriver, can_steer=True, reusable=True,
        build_turn_argv=lambda context: (
            [sys.executable, "-u", str(model), str(frames), str(finish)],
            {"cwd": context.working_dir},
        ),
    ))
    ready, write_entered, release_write, received, confirmed = (asyncio.Event() for _ in range(5))
    spawn, stream, drain = asyncio.create_subprocess_exec, HarnessRun.stream, asyncio.StreamWriter.drain
    processes, readers, writes = [], set(), []
    prompt, correction = "Keep carrier A running.", "The original correction belonging only to work B."
    carrier_work, inline_work = f"carrier-work-A-{session.id}", f"inline-work-B-{session.id}"
    drain_calls = 0

    async def capture_original_process(*arguments, **kwargs):
        process = await spawn(*arguments, **kwargs)
        if str(model) in arguments:
            processes.append(process)
        return process

    async def capture_original_reader(backend):
        readers.add(asyncio.current_task())
        async for event in stream(backend):
            yield event

    async def hold_original_drain_result(writer):
        nonlocal drain_calls
        result = await drain(writer)
        if processes and writer is processes[0].stdin:
            drain_calls += 1
            if drain_calls == 2:
                writes.append(asyncio.current_task())
                write_entered.set()
                await release_write.wait()
                if write_result == "exception":
                    raise OSError("Original write acknowledgment became unavailable after its actual drain")
                if write_result == "cancelled":
                    raise asyncio.CancelledError("Original write waiter was cancelled after its actual drain")
        return result

    async def observe_original_output(event):
        if event.get("session_id") != session.id:
            return
        if event.get("type") == "assistant_text":
            if event.get("content") == "Carrier A is still running":
                ready.set()
            if event.get("content") == "Carrier received: " + correction:
                received.set()
        if event.get("type") == "steered" and event.get("content") == correction:
            confirmed.set()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", capture_original_process)
    monkeypatch.setattr(HarnessRun, "stream", capture_original_reader)
    monkeypatch.setattr(asyncio.StreamWriter, "drain", hold_original_drain_result)
    manager.on_broadcast("observe-original-work-write", observe_original_output)
    cancellation = None
    try:
        initial = await manager.start_message(session.id, prompt, work_id=carrier_work)
        await asyncio.wait_for(ready.wait(), 2)
        stdout_readers = {task for task in asyncio.all_tasks() if task.get_name() == "codex-stdout"}
        assert len(stdout_readers) == 1
        inline = await manager.start_message(session.id, correction, work_id=inline_work, steerable=True)
        assert inline.disposition == "steered"
        await asyncio.wait_for(write_entered.wait(), 2)
        await asyncio.wait_for(received.wait(), 2)
        assert len(writes) == len(processes) == 1 and not writes[0].done()
        assert processes[0].returncode is None and Path(f"/proc/{processes[0].pid}").exists()
        assert manager.dispatch_snapshot(session.id)["items"] == []
        retired = await manager.close_work(inline_work)
        cancellation = asyncio.create_task(manager.finish_work_cancellation([retired]))
        done, _ = await asyncio.wait({cancellation}, timeout=.05)
        assert not done, "Work B cancellation returned before its original in-flight stdin write ended"
        assert not writes[0].done() and processes[0].returncode is None
        assert manager.dispatch_snapshot(session.id)["items"] == []
        release_write.set()
        await asyncio.wait_for(cancellation, 2)
        assert writes[0].done()
        assert processes[0].returncode is None
        if write_result == "confirmed":
            await asyncio.wait_for(confirmed.wait(), 2)
        else:
            uncertain = await asyncio.wait_for(inline.wait(), 2)
            assert uncertain.state == "unavailable" and uncertain.outcome is None
            assert uncertain.input_request_id == inline.request_id
            assert uncertain.request_id == initial.request_id
            assert uncertain.notification_state == "unavailable"
            assert not confirmed.is_set()
        assert manager.dispatch_snapshot(session.id)["items"] == []
        finish.touch()
        outcome = await asyncio.wait_for(initial.wait(), 2)
        assert outcome.state == "completed" and outcome.outcome.status == "success"
        completed_inline = await asyncio.wait_for(inline.wait(), 2)
        assert completed_inline.input_request_id == inline.request_id
        assert completed_inline.request_id == initial.request_id
        if write_result == "confirmed":
            assert completed_inline.state == "completed" and completed_inline.outcome.status == "success"
        else:
            assert completed_inline is uncertain
        delivered = [json.loads(line)["message"]["content"] for line in frames.read_text().splitlines()]
        assert delivered == [prompt, correction]
        assert drain_calls == 2 and len(processes) == 1
        detail = await client.get(f"/api/sessions/{session.id}", headers=client.headers)
        assert detail.status_code == 200
        messages = detail.json()["messages"]
        assert [row["content"] for row in messages if row["role"] == "user"] == [prompt, correction]
        inline_row = next(row for row in messages if row["role"] == "user" and row["content"] == correction)
        context = inline_row["tool_input"]["request_context"]
        assert context["request_id"] == inline.request_id and context["work_id"] == inline_work
        assert len([row for row in messages if row["type"] == "result"]) == 1
        await manager.interrupt(session.id)
        await manager.await_session_execution(session.id, timeout=3)
        assert processes[0].returncode == 0 and not Path(f"/proc/{processes[0].pid}").exists()
        assert len(readers) == 1 and all(reader.done() for reader in readers | stdout_readers)
        await asyncio.wait_for(manager.drain_turns(timeout=3), 4)
    finally:
        release_write.set()
        finish.touch()
        await manager.interrupt(session.id)
        await manager.await_session_execution(session.id, timeout=5)
        if cancellation is not None:
            await asyncio.gather(cancellation, return_exceptions=True)
