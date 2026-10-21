"""Flow control reaches the real child without losing ordered stream output."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from server.harness import (
    EventParser,
    Harness,
    HarnessEvent,
    ParseOutput,
    RuntimeProfile,
    StdinMode,
)
from server.harness.codex_activity import CodexActivityObserver


class _Parser(EventParser):
    def parse(self, obj: dict[str, Any]) -> ParseOutput:
        return ParseOutput(
            events=[HarnessEvent(type=obj["type"], content=obj.get("content"), raw=obj)],
            end_of_stream=obj["type"] == "result",
        )


def _profile(script: Path, *arguments: Path | str) -> RuntimeProfile:
    return RuntimeProfile(
        backend="flow-control-test",
        binary=sys.executable,
        tools_prompt="",
        credential_style="env_secret",
        premature_exit_recovery=False,
        stdin_mode=StdinMode.CLOSE_AFTER_SPAWN,
        build_turn_argv=lambda context: (
            [sys.executable, "-u", str(script), *map(str, arguments)],
            {"cwd": context.working_dir},
        ),
        new_event_parser=_Parser,
        build_oneshot_argv=lambda context: ([sys.executable], {}),
        parse_oneshot_stdout=lambda stdout: stdout,
    )


def _writer(tmp_path: Path, *, count: int = 512, payload_bytes: int = 65536) -> tuple[Path, Path]:
    script, progress = tmp_path / "writer.py", tmp_path / "writes.jsonl"
    script.write_text(
        "import json,os,sys\n"
        "with open(sys.argv[1], 'a', buffering=1) as progress:\n"
        f"    for index in range({count}):\n"
        f"        line = (json.dumps({{'type':'text','index':index,'content':'x'*{payload_bytes}}})+'\\n').encode()\n"
        "        pending = memoryview(line)\n"
        "        while pending:\n"
        "            pending = pending[os.write(1, pending):]\n"
        "        progress.write(json.dumps({'written':index})+'\\n')\n"
        "    os.write(1, b'{\"type\":\"result\"}\\n')\n"
        "    progress.write('{\"finished\":true}\\n')\n"
    )
    return script, progress


def _written(progress: Path) -> list[dict[str, Any]]:
    if not progress.exists():
        return []
    return [json.loads(line) for line in progress.read_text().splitlines()]


async def _wait_for_stalled_writer(progress: Path) -> int:
    async with asyncio.timeout(5):
        while not _written(progress):
            await asyncio.sleep(0.01)
        previous = len(_written(progress))
        while True:
            await asyncio.sleep(0.1)
            records = _written(progress)
            assert not any(record.get("finished") for record in records), (
                "The child completed every OS write while the Harness stream had no consumer."
            )
            if len(records) == previous:
                print(f"Producer OS-write plateau: completed={previous}, finished=False", flush=True)
                return previous
            previous = len(records)


async def _collect(run) -> list[HarnessEvent]:
    return [event async for event in run.stream()]


async def test_paused_stream_backpressures_os_writes_then_drains_in_order(tmp_path):
    script, progress = _writer(tmp_path)
    run = Harness(_profile(script, progress)).create_run()
    try:
        await run.start("prompt", str(tmp_path))
        stalled_at = await _wait_for_stalled_writer(progress)
        assert 0 < stalled_at < 512
        events = await asyncio.wait_for(_collect(run), 10)
        assert [event.raw["index"] for event in events[:-1]] == list(range(512))
        assert events[-1].type == "result"
        assert all(event.content == "x" * 65536 for event in events[:-1])
        async with asyncio.timeout(2):
            while not any(record.get("finished") for record in _written(progress)):
                await asyncio.sleep(0.01)
    finally:
        await run.stop()


async def test_legal_near_four_mib_lines_are_delivered_whole(tmp_path):
    payload_bytes = 4 * 1024 * 1024 - 64
    script, progress = _writer(tmp_path, count=2, payload_bytes=payload_bytes)
    run = Harness(_profile(script, progress)).create_run()
    try:
        await run.start("prompt", str(tmp_path))
        events = await asyncio.wait_for(_collect(run), 5)
        assert [(event.type, event.raw.get("index")) for event in events] == [
            ("text", 0), ("text", 1), ("result", None),
        ]
        assert all(event.content == "x" * payload_bytes for event in events[:-1])
        assert run.protocol_issues == {}
    finally:
        await run.stop()


async def test_escaped_json_string_remains_accepted_by_the_stream(tmp_path):
    script = tmp_path / "escaped.py"
    records = '{"type":"text","content":"\\ud800"}\n{"type":"result"}\n'
    script.write_text(f"import os\nos.write(1, {records.encode('ascii')!r})\n")
    run = Harness(_profile(script)).create_run()
    try:
        await run.start("prompt", str(tmp_path))
        events = await asyncio.wait_for(_collect(run), 2)
        assert [(event.type, event.content) for event in events] == [
            ("text", chr(0xD800)), ("result", None),
        ]
        assert run.protocol_issues == {}
    finally:
        await run.stop()


async def test_full_stream_buffer_preserves_terminal_and_end_after_consumption_resumes(tmp_path):
    terminal_seen = asyncio.Event()

    class TerminalParser(_Parser):
        def parse(self, obj: dict[str, Any]) -> ParseOutput:
            output = super().parse(obj)
            if output.end_of_stream:
                terminal_seen.set()
            return output

    script, progress = _writer(tmp_path, count=256, payload_bytes=0)
    run = Harness(replace(_profile(script, progress), new_event_parser=TerminalParser)).create_run()
    try:
        await run.start("prompt", str(tmp_path))
        await asyncio.wait_for(terminal_seen.wait(), 2)
        events = await asyncio.wait_for(_collect(run), 2)
        assert [event.raw["index"] for event in events[:-1]] == list(range(256))
        assert events[-1].type == "result"
    finally:
        await run.stop()


@pytest.mark.parametrize("pipe,ignore_term", [("stdout", False), ("stderr", False), ("stdout", True)])
async def test_stop_retires_a_backpressured_child_without_a_stream_consumer(tmp_path, pipe, ignore_term):
    original_tasks = asyncio.all_tasks()
    script, progress = _writer(tmp_path)
    descriptor = 1
    if pipe == "stderr":
        descriptor = 2
        script.write_text(
            "import json,os,sys\n"
            "with open(sys.argv[1], 'a', buffering=1) as progress:\n"
            "    for index in range(512):\n"
            "        pending = memoryview(b'x'*65536)\n"
            "        while pending: pending = pending[os.write(2, pending):]\n"
            "        progress.write(json.dumps({'written':index})+'\\n')\n"
            "    progress.write('{\"finished\":true}\\n')\n"
        )
    if ignore_term:
        script.write_text("import signal\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\n" + script.read_text())
    run = Harness(_profile(script, progress)).create_run()
    stopping = None
    draining = None
    try:
        await run.start("prompt", str(tmp_path))
        pid = run.process_group_id
        await _wait_for_stalled_writer(progress)
        output_pipe = os.readlink(f"/proc/{pid}/fd/{descriptor}")
        stopping = asyncio.create_task(run.stop())
        await asyncio.wait_for(asyncio.shield(stopping), 5)
        assert not run.is_alive()
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
        events = await asyncio.wait_for(_collect(run), 2)
        if pipe == "stdout":
            assert events
            assert [event.raw["index"] for event in events] == list(range(len(events)))
            assert len(events) < 512
        else:
            assert events == []
        assert not [task for task in asyncio.all_tasks() - original_tasks if not task.done()]
        for descriptor in Path("/proc/self/fd").iterdir():
            try:
                target = os.readlink(descriptor)
            except FileNotFoundError:
                continue
            assert target != output_pipe, f"Stop retained the child's backpressured {pipe} pipe."
    finally:
        # Even a failing Stop assertion must release the test's actual child.
        if stopping is not None and not stopping.done():
            draining = asyncio.create_task(_collect(run))
        await asyncio.wait_for(run.stop(), 5)
        if draining is not None:
            await asyncio.wait_for(draining, 2)


async def test_cancelling_one_stream_waiter_keeps_the_child_and_its_output(tmp_path):
    script, ready, release = tmp_path / "waiting.py", tmp_path / "ready", tmp_path / "release"
    script.write_text(
        "import sys,time\n"
        "from pathlib import Path\n"
        "Path(sys.argv[1]).touch()\n"
        "while not Path(sys.argv[2]).exists(): time.sleep(0.01)\n"
        "print('{\"type\":\"text\",\"content\":\"original output\"}', flush=True)\n"
        "print('{\"type\":\"result\"}', flush=True)\n"
    )
    run = Harness(_profile(script, ready, release)).create_run()
    try:
        await run.start("prompt", str(tmp_path))
        async with asyncio.timeout(2):
            while not ready.exists():
                await asyncio.sleep(0.01)
        waiter = asyncio.create_task(anext(run.stream()))
        await asyncio.sleep(0)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert run.is_alive()
        release.touch()
        events = await asyncio.wait_for(_collect(run), 2)
        assert [(event.type, event.content) for event in events] == [
            ("text", "original output"), ("result", None),
        ]
    finally:
        release.touch()
        await run.stop()


_THREAD = "018f0000-1111-4222-8333-444444444444"
_TURN = "018f0000-5555-7666-8777-888888888888"
_CHILD = "018f0000-aaaa-4bbb-8ccc-dddddddddddd"


def _native_started(identifier: str) -> dict[str, Any]:
    return {"type": "event_msg", "payload": {
        "type": "item_completed", "thread_id": _THREAD, "turn_id": _TURN,
        "item": {"type": "SubAgentActivity", "id": identifier, "kind": "started",
                 "agent_thread_id": _CHILD, "agent_path": "/root/child"},
        "started_at_ms": 100000, "completed_at_ms": 100000,
    }}


def _append_native(path: Path, *records: dict[str, Any]) -> None:
    with path.open("a") as stream:
        for record in records:
            stream.write(json.dumps(record) + "\n")


def _native_source(tmp_path: Path) -> tuple[Path, CodexActivityObserver]:
    path = tmp_path / "sessions" / f"rollout-test-{_THREAD}.jsonl"
    path.parent.mkdir()
    _append_native(
        path, {"type": "session_meta", "payload": {"id": _THREAD}},
        {"type": "event_msg", "payload": {"type": "task_started", "turn_id": _TURN}},
    )
    observer = CodexActivityObserver(
        {"CODEX_HOME": str(tmp_path)}, lambda timestamp: None,
        wall_clock=lambda: 100.0, monotonic=lambda: 10.0,
    )
    observer.observe_stdout({"type": "thread.started", "thread_id": _THREAD})
    observer.consume_stderr(f"2026-10-14T00:00:00Z INFO codex_exec: Sent prompt with event ID: {_TURN}")
    return path, observer


async def test_native_drain_waits_for_backpressured_poll_delivery_in_source_order(tmp_path):
    path, observer = _native_source(tmp_path)
    entered, release = asyncio.Event(), asyncio.Event()
    delivered = []

    async def receive(events):
        if not entered.is_set():
            entered.set()
            await release.wait()
        delivered.extend(event.tool_use_id for event in events if event.type == "tool_use")

    observer.set_event_handler(receive)
    _append_native(path, _native_started("first-call"))
    polling = asyncio.create_task(observer.poll_once())
    draining = None
    try:
        await asyncio.wait_for(entered.wait(), 2)
        _append_native(path, _native_started("second-call"))
        draining = asyncio.create_task(observer.drain_observations())
        await asyncio.sleep(0.05)
        assert not draining.done(), "Terminal drain overtook an undelivered native prefix."
        assert delivered == []
        release.set()
        await asyncio.wait_for(asyncio.gather(polling, draining), 2)
        assert delivered == ["first-call", "second-call"]
    finally:
        release.set()
        await asyncio.gather(polling, *([draining] if draining else []), return_exceptions=True)
        await observer.close()


async def test_native_batched_drain_keeps_its_captured_eof_while_delivering(tmp_path):
    path, observer = _native_source(tmp_path)
    identifiers = [f"call-{index}" for index in range(4500)]
    _append_native(path, *(_native_started(identifier) for identifier in identifiers))
    delivered = []

    async def receive(events):
        if not delivered:
            _append_native(path, _native_started("appended-during-delivery"))
            await asyncio.sleep(0)
        delivered.extend(event.tool_use_id for event in events if event.type == "tool_use")

    observer.set_event_handler(receive)
    try:
        await asyncio.wait_for(observer.drain_observations(), 3)
        assert delivered == identifiers
        await observer.poll_once()
        assert delivered == [*identifiers, "appended-during-delivery"]
    finally:
        await observer.close()


@pytest.mark.parametrize("finish", ["resume", "stop"])
async def test_harness_closes_a_backpressured_native_stream(tmp_path, finish):
    original_tasks = asyncio.all_tasks()
    source = tmp_path / "sessions" / f"rollout-test-{_THREAD}.jsonl"
    source.parent.mkdir()
    _append_native(
        source, {"type": "session_meta", "payload": {"id": _THREAD}},
        {"type": "event_msg", "payload": {"type": "task_started", "turn_id": _TURN}},
    )
    observer = CodexActivityObserver(
        {"CODEX_HOME": str(tmp_path)}, lambda timestamp: None,
        wall_clock=lambda: 100.0, monotonic=lambda: 10.0,
    )
    script, ready, release = tmp_path / "native_writer.py", tmp_path / "ready", tmp_path / "release"
    script.write_text(
        "import json,sys,time\n"
        "from pathlib import Path\n"
        f"print(json.dumps({{'type':'thread.started','thread_id':{_THREAD!r}}}), flush=True)\n"
        f"print('2026-10-14T00:00:00Z INFO codex_exec: Sent prompt with event ID: {_TURN}', file=sys.stderr, flush=True)\n"
        "Path(sys.argv[1]).touch()\n"
        "while not Path(sys.argv[2]).exists(): time.sleep(0.01)\n"
        "for index in range(256):\n"
        "    print(json.dumps({'type':'text','index':index,'content':'stdout'}), flush=True)\n"
        "print('{\"type\":\"result\"}', flush=True)\n"
    )
    profile = replace(
        _profile(script, ready, release), activity_observer=lambda *args, **kwargs: observer,
    )
    run = Harness(profile).create_run()
    run.bind_tool_context("original-request", "original-attempt")
    polling = None
    try:
        await run.start("prompt", str(tmp_path))
        pid = run.process_group_id
        async with asyncio.timeout(2):
            while not ready.exists() or not observer.identity_confirmed:
                await asyncio.sleep(0.01)
        records = []
        identifiers = [f"native-call-{index}" for index in range(4500)]
        for index, identifier in enumerate(identifiers):
            record = _native_started(identifier)
            record["payload"]["item"]["agent_thread_id"] = f"018f0000-aaaa-4bbb-8ccc-{index:012x}"
            records.append(record)
        _append_native(source, *records)
        release.touch()
        polling = asyncio.create_task(observer.poll_once())
        await asyncio.sleep(0.05)
        assert not polling.done(), "Native observations bypassed the held Harness stream."
        if finish == "stop":
            await asyncio.wait_for(run.stop(), 5)
        events = await asyncio.wait_for(_collect(run), 8)
        await asyncio.wait_for(polling, 2)
        stdout_indices = [event.raw["index"] for event in events if event.type == "text"]
        calls = [event for event in events if event.type == "tool_use"]
        assert all(event.runtime_metadata["request_id"] == "original-request" for event in calls)
        if finish == "resume":
            assert stdout_indices == list(range(256))
            assert [event.tool_use_id for event in calls] == identifiers
            assert events[-1].type == "result"
        else:
            assert stdout_indices == list(range(len(stdout_indices)))
            assert [event.tool_use_id for event in calls] == identifiers[:len(calls)]
            assert not run.is_alive()
            with pytest.raises(ProcessLookupError):
                os.kill(pid, 0)
            assert not [task for task in asyncio.all_tasks() - original_tasks if not task.done()]
    finally:
        release.touch()
        await run.stop()
        if polling is not None:
            await asyncio.gather(polling, return_exceptions=True)
