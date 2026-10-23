"""Continuous real stdout reaches the owner's publication-pressure boundary."""

from __future__ import annotations

import asyncio
import hashlib
import json
import sys
import time
from dataclasses import replace
from pathlib import Path

import pytest

from server.database import Database
from server.harness import get_harness
from server.harness.profile import StdinMode
from server.harness.run import (
    _EVENT_BUFFER_BYTES,
    _STDOUT_LINE_LIMIT_BYTES,
    HarnessRun,
)
from server.sessions import SessionManager
from server.sessions.owner import SessionMailboxFull
from server.sessions.owner_runtime import RuntimeReader
from server.sessions.publication import _MAX_PUBLICATION_BYTES

_FIRST_PAYLOAD_BYTES = 1024 * 1024 + 256
_FOLLOWING_PAYLOAD_BYTES = 64 * 1024
_FRAME_COUNT = 1024


def _body(index: int) -> str:
    return f"part-{index:04d}\n" + ("L" * _FIRST_PAYLOAD_BYTES if index == 0 else "x" * _FOLLOWING_PAYLOAD_BYTES)


def _frame(content: str) -> dict:
    return {
        "index": int(content.partition("\n")[0].removeprefix("part-")),
        "bytes": len(content.encode()),
        "sha256": hashlib.sha256(content.encode()).hexdigest(),
    }


def _progress(path: Path) -> list[dict]:
    data = path.read_bytes() if path.exists() else b""
    # Only records whose actual progress write has completed are evidence.
    complete = data[:data.rfind(b"\n") + 1]
    return [json.loads(line) for line in complete.splitlines()]


async def _pipe_plateau(progress: Path, pid: int) -> tuple[list[dict], str]:
    async with asyncio.timeout(6):
        while True:
            before = _progress(progress)
            assert not any(row.get("finished") for row in before), "The supposedly backpressured producer completed all stdout writes"
            wait = Path(f"/proc/{pid}/wchan").read_text().strip()
            if len(before) > 1 and "pipe_write" in wait:
                await asyncio.sleep(.15)
                after = _progress(progress)
                if before == after and "pipe_write" in Path(f"/proc/{pid}/wchan").read_text():
                    return after, wait
            await asyncio.sleep(.01)


async def test_active_stdout_backpressure_refuses_submit_and_preserves_the_original_prefix(
    tmp_path, monkeypatch, short_process_shutdown,
):
    script = tmp_path / "continuous_stdout.py"
    progress = tmp_path / "stdout-writes.jsonl"
    identity_path = tmp_path / "original-process.json"
    input_path = tmp_path / "original-input.json"
    script.write_text(
        "import fcntl,json,os,sys\nfrom pathlib import Path\n"
        "fields=Path(f'/proc/{os.getpid()}/stat').read_text().rsplit(')',1)[1].split()\n"
        "Path(sys.argv[2]).write_text(json.dumps({'pid':os.getpid(),'pgid':os.getpgrp(),'start_time':fields[19],"
        "'stdout_pipe_bytes':fcntl.fcntl(1,fcntl.F_GETPIPE_SZ),'stdout_pipe':os.readlink('/proc/self/fd/1')}))\n"
        "Path(sys.argv[3]).write_text(sys.stdin.readline())\n"
        "os.write(1,b'{\"type\":\"thread.started\",\"thread_id\":\"continuous-original-thread\"}\\n')\n"
        "with open(sys.argv[1],'a',buffering=1) as progress:\n"
        f" for index in range({_FRAME_COUNT}):\n"
        f"  text=f'part-{{index:04d}}\\n'+('L'*{_FIRST_PAYLOAD_BYTES} if index==0 else 'x'*{_FOLLOWING_PAYLOAD_BYTES})\n"
        "  line=(json.dumps({'type':'item.completed','item':{'id':f'original-{index}','type':'agent_message','text':text}})+'\\n').encode()\n"
        "  pending=memoryview(line)\n"
        "  while pending: pending=pending[os.write(1,pending):]\n"
        "  progress.write(json.dumps({'index':index,'wire_bytes':len(line)})+'\\n')\n"
        " progress.write('{\"finished\":true}\\n')\n"
        "os.write(1,b'{\"type\":\"turn.completed\",\"usage\":{}}\\n')\n"
        "sys.stdin.read()\n"
    )
    harness = get_harness("codex")
    monkeypatch.setattr(harness, "profile", replace(
        harness.profile, binary=sys.executable, activity_observer=None, prepare_resume=None,
        stdin_mode=StdinMode.STREAM_JSON,
        build_turn_argv=lambda context: (
            [sys.executable, "-u", str(script), str(progress), str(identity_path), str(input_path)],
            {"cwd": context.working_dir},
        ),
    ))
    original_spawn = asyncio.create_subprocess_exec
    original_stream = HarnessRun.stream
    original_take = RuntimeReader.take
    processes, offered, admitted, reader_tasks = [], [], [], set()

    async def observe_spawn(*args, **kwargs):
        process = await original_spawn(*args, **kwargs)
        if str(script) in args:
            processes.append(process)
        return process

    async def observe_original_stream(run):
        # Forward every actual Harness event unchanged. This observes the
        # offered prefix at the external I/O interface, without fabricating
        # Owner facts or changing its mailbox/buffer state.
        reader_tasks.add(asyncio.current_task())
        async for event in original_stream(run):
            if event.type == "text":
                offered.append(_frame(event.content))
            yield event

    def observe_original_take(reader):
        # An offered slot has not been admitted by Owner yet. Observe the
        # real take unchanged; this is the precise prefix Stop must retain.
        event = original_take(reader)
        if event.type == "text":
            admitted.append({**_frame(event.content), "request_id": reader.request_id, "attempt_id": reader.attempt_id})
        return event

    monkeypatch.setattr(asyncio, "create_subprocess_exec", observe_spawn)
    monkeypatch.setattr(HarnessRun, "stream", observe_original_stream)
    monkeypatch.setattr(RuntimeReader, "take", observe_original_take)
    database = Database(str(tmp_path / "pressure.sqlite3"))
    await database.initialize()
    manager = SessionManager()
    await manager.initialize(database)
    agent = await database.get_system_agent()
    session = await manager.create_session(agent["id"], "Continuous original output", str(tmp_path), backend="codex")
    entered, release = asyncio.Event(), asyncio.Event()
    delivered, publication_order, attempts = [], [], []
    prompt = "Keep this original input while notifications are backpressured."
    completion = None
    stdout_tasks = set()

    async def hold_first_original_text(event):
        if event.get("session_id") == session.id and event.get("type") == "assistant_text" and event["content"].startswith("part-0000\n"):
            assert len(event["content"].encode()) > _MAX_PUBLICATION_BYTES
            entered.set()
            await release.wait()

    async def observe_original_publication(event):
        if event.get("session_id") != session.id:
            return
        if event.get("type") == "attempt_started":
            attempts.append(event["attempt_id"])
        elif event.get("type") == "assistant_text":
            delivered.append({**_frame(event["content"]), "request_id": event["request_id"], "attempt_id": event["attempt_id"]})
            publication_order.append("text")
        elif event.get("type") == "result":
            publication_order.append("result")

    manager.on_broadcast("hold-original-full-publisher", hold_first_original_text)
    manager.on_broadcast("observe-original-pressure-prefix", observe_original_publication)
    try:
        receipt = await manager.start_message(session.id, prompt, request_id="continuous-original-request")
        assert receipt.accepted
        completion = asyncio.create_task(receipt.wait())
        await asyncio.wait_for(entered.wait(), 5)
        assert len(processes) == 1
        identity = json.loads(identity_path.read_text())
        pid = identity["pid"]
        assert pid == identity["pgid"] == processes[0].pid
        written, wait = await _pipe_plateau(progress, pid)
        assert [row["index"] for row in written] == list(range(len(written)))
        assert 1 < len(written) < _FRAME_COUNT
        assert Path(f"/proc/{pid}").exists() and processes[0].returncode is None
        assert json.loads(input_path.read_text())["message"]["content"] == prompt
        stdout_tasks = {task for task in asyncio.all_tasks() if task.get_name() == "codex-stdout"}
        assert len(stdout_tasks) == len(reader_tasks) == len(attempts) == 1
        assert not next(iter(reader_tasks)).done()
        offered_before_stop = list(offered)
        admitted_before_stop = list(admitted)
        rows_before_stop = await database.load_messages(session.id)
        persisted_before_stop = [_frame(row["content"]) for row in rows_before_stop
                                 if row["role"] == "assistant" and row["type"] == "text"]
        assert persisted_before_stop and len(admitted_before_stop) > 1
        assert publication_order == [] and not completion.done()

        # The stream has spent the actual publisher budget. Refusal cannot
        # leave a receipt, queue item or delivery identity behind.
        for _ in range(3):
            with pytest.raises(SessionMailboxFull, match="notification capacity is full"):
                await manager.start_message(
                    session.id, "Not admitted during publication pressure", source="bg",
                    request_id="pressure-retry-request", delivery_id="pressure-retry-delivery",
                )
        snapshot = manager.dispatch_snapshot(session.id)
        assert snapshot["active_request_id"] == receipt.request_id and snapshot["items"] == []
        assert _progress(progress) == written, "Control/refusal processing silently drained the blocked stdout prefix"
        stop_at = time.monotonic()
        assert await asyncio.wait_for(manager.interrupt(session.id), 2) is True
        stop_reply_seconds = time.monotonic() - stop_at
        assert not release.is_set() and not completion.done()
        await manager.await_session_execution(session.id, timeout=7)
        assert not Path(f"/proc/{pid}").exists() and processes[0].returncode is not None
        joined_before_release = all(task.done() for task in reader_tasks | stdout_tasks)
        assert joined_before_release
        assert not release.is_set() and publication_order == [] and not completion.done()
        offered_at_stop, admitted_at_stop = list(offered), list(admitted)
        assert offered_at_stop == offered_before_stop and admitted_at_stop == admitted_before_stop

        release.set()
        result = await asyncio.wait_for(completion, 3)
        assert result.recording_state == "committed" and result.outcome.status == "interrupted"
        assert result.request_id == result.input_request_id == receipt.request_id
        rows = await database.load_messages(session.id)
        persisted = [_frame(row["content"]) for row in rows if row["role"] == "assistant" and row["type"] == "text"]
        terminals = [row["content"] for row in rows if row["type"] == "result"]
        metrics = {
            "pid": pid, "process_returncode": processes[0].returncode,
            "kernel_stdout_wait": wait, "stdout_pipe_bytes": identity["stdout_pipe_bytes"],
            "producer_completed_frames": len(written), "producer_completed_wire_bytes": sum(row["wire_bytes"] for row in written),
            "producer_target_frames": _FRAME_COUNT,
            "harness_event_budget_bytes": _EVENT_BUFFER_BYTES, "stdout_line_limit_bytes": _STDOUT_LINE_LIMIT_BYTES,
            "publisher_budget_bytes": _MAX_PUBLICATION_BYTES,
            "offered_frames": offered_at_stop, "admitted_frames": admitted_at_stop, "persisted_before_stop": persisted_before_stop,
            "persisted_after_stop": persisted, "published_frames": delivered,
            "publication_order": publication_order, "stop_reply_seconds": stop_reply_seconds,
            "reader_joined_before_publication_release": joined_before_release,
            "original_request_id": receipt.request_id, "original_attempt_id": attempts[0],
            "terminal_count": len(terminals), "terminal_status": terminals[0]["status"] if terminals else None,
        }
        (tmp_path / "stream-pressure-metrics.json").write_text(json.dumps(metrics, indent=2) + "\n")
        print("Actual stream-pressure measurements: " + json.dumps(metrics, sort_keys=True), flush=True)
        assert offered_at_stop == [_frame(_body(index)) for index in range(len(offered_at_stop))]
        admitted_bytes = [{key: event[key] for key in ("index", "bytes", "sha256")} for event in admitted_at_stop]
        assert admitted_bytes == offered_at_stop[:len(admitted_bytes)]
        assert 0 <= len(offered_at_stop) - len(admitted_bytes) <= 1
        assert persisted == persisted_before_stop == admitted_bytes, "Stop discarded original Owner-admitted bytes"
        assert delivered == admitted_at_stop
        assert all(event["request_id"] == receipt.request_id and event["attempt_id"] == attempts[0] for event in delivered)
        assert publication_order == ["text"] * len(admitted_at_stop) + ["result"]
        assert len(terminals) == 1 and terminals[0]["request_id"] == receipt.request_id and terminals[0]["status"] == "interrupted"
        assert [row["content"] for row in rows if row["role"] == "user"] == [prompt]

        retry = await manager.start_message(
            session.id, "Retry after the original prefix drains", source="bg",
            request_id="pressure-retry-request", delivery_id="pressure-retry-delivery",
        )
        duplicate = await manager.start_message(
            session.id, "Retry after the original prefix drains", source="bg",
            request_id="pressure-retry-request", delivery_id="pressure-retry-delivery",
        )
        assert retry.disposition == "accepted" and duplicate.disposition == "duplicate"
        assert retry.request_id == duplicate.request_id == "pressure-retry-request"
        assert [row["request_id"] for row in manager.dispatch_snapshot(session.id)["items"]] == [retry.request_id]
        assert await manager.cancel_request(session.id, retry.request_id)
        assert (await retry.wait()).outcome is None
        assert len(processes) == 1
        retry_proof = {
            "refused_attempts": 3, "retry_disposition": retry.disposition,
            "duplicate_disposition": duplicate.disposition, "retry_request_id": retry.request_id,
            "process_count": len(processes),
        }
        metrics.update(retry_proof)
        (tmp_path / "stream-pressure-metrics.json").write_text(json.dumps(metrics, indent=2) + "\n")
        print("Actual pressure retry identity: " + json.dumps(retry_proof, sort_keys=True), flush=True)
    finally:
        release.set()
        try:
            await asyncio.wait_for(manager.interrupt(session.id), 2)
            await manager.await_session_execution(session.id, timeout=7)
        finally:
            for process in processes:
                if process.returncode is None:
                    process.kill()
                await asyncio.wait_for(process.wait(), 5)
            await asyncio.wait_for(manager.drain_turns(timeout=7), 8)
            if completion is not None:
                await asyncio.gather(completion, return_exceptions=True)
            await database.close()
            await asyncio.gather(*reader_tasks, *stdout_tasks, return_exceptions=True)
