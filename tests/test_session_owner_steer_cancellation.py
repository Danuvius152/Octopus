"""Withdrawing an original input also revokes its pending preparation."""

import asyncio
import json
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from server.harness import get_harness
from server.harness.claude_code import ClaudeDriver
from tests import test_question_lifecycle as question_tests

question_host = question_tests.question_host


@pytest.mark.parametrize("operation", ["cancel_request", "close_work"])
async def test_cancelled_steer_does_not_deliver_after_its_git_preparation_finishes(
    question_host, tmp_path, monkeypatch, operation,
):
    manager, original, client, _events = question_host
    session = await manager.create_session(
        original.agent_id, "Cancel a preparing correction", str(tmp_path), backend="codex",
    )
    for arguments in (
        ("init", "-q"),
        ("-c", "user.name=Offline", "-c", "user.email=offline@example.invalid",
         "commit", "--allow-empty", "-qm", "original anchor"),
    ):
        process = await asyncio.create_subprocess_exec(
            "git", *arguments, cwd=tmp_path,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        assert process.returncode == 0, (stdout + stderr).decode()

    frames = tmp_path / "received.jsonl"
    finish = tmp_path / "finish-original"
    model = tmp_path / "correction_model.py"
    model.write_text(
        "import json,select,sys\nfrom pathlib import Path\n"
        "received=Path(sys.argv[1]); finish=Path(sys.argv[2])\n"
        "initial=json.loads(sys.stdin.readline())\n"
        "received.write_text(json.dumps(initial)+'\\n')\n"
        "print(json.dumps({'type':'thread.started','thread_id':'cancel-original-thread'}),flush=True)\n"
        "print(json.dumps({'type':'item.completed','item':{'id':'ready','type':'agent_message','text':'Original request remains active'}}),flush=True)\n"
        "index=0\n"
        "while not finish.exists():\n"
        "    if not select.select([sys.stdin],[],[],.01)[0]: continue\n"
        "    line=sys.stdin.readline()\n"
        "    if not line: sys.exit(0)\n"
        "    with received.open('a') as output: output.write(line)\n"
        "    index+=1\n"
        "    print(json.dumps({'type':'item.completed','item':{'id':str(index),'type':'agent_message','text':'Received: '+json.loads(line)['message']['content']}}),flush=True)\n"
        "print(json.dumps({'type':'turn.completed','usage':{}}),flush=True)\n"
        "sys.stdin.read()\n"
    )
    harness = get_harness("codex")
    monkeypatch.setattr(harness, "profile", replace(
        harness.profile, binary=sys.executable, activity_observer=None, prepare_resume=None,
        new_driver=ClaudeDriver, can_steer=True, reusable=True,
        build_turn_argv=lambda context: (
            [sys.executable, "-u", str(model), str(frames), str(finish)],
            {"cwd": context.working_dir},
        ),
    ))
    ready, anchor_entered, release_anchor, following_received = (asyncio.Event() for _ in range(4))
    processes, preparations = [], []
    gate_next_anchor = False
    spawn = asyncio.create_subprocess_exec

    async def observe_original_spawn(*arguments, **kwargs):
        nonlocal gate_next_anchor
        process = await spawn(*arguments, **kwargs)
        if str(model) in arguments:
            processes.append(process)
        if gate_next_anchor and arguments[:3] == ("git", "rev-parse", "HEAD"):
            gate_next_anchor = False
            preparations.append((asyncio.current_task(), process))
            anchor_entered.set()
            await release_anchor.wait()
        return process

    async def observe_original_output(event):
        if event.get("session_id") != session.id or event.get("type") != "assistant_text":
            return
        if event.get("content") == "Original request remains active":
            ready.set()
        if event.get("content") == "Received: Apply the following valid correction.":
            following_received.set()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", observe_original_spawn)
    manager.on_broadcast("cancelled-original-correction", observe_original_output)
    prompt = "Keep the first original request active."
    withdrawn = "Never deliver this withdrawn correction."
    following = "Apply the following valid correction."
    work_cleanup = None
    try:
        initial = await manager.start_message(session.id, prompt, work_id="original-active-work")
        await asyncio.wait_for(ready.wait(), 2)
        gate_next_anchor = True
        correction = await manager.start_message(
            session.id, withdrawn, steerable=True, work_id="withdrawn-input-work",
        )
        assert correction.disposition == "steered"
        await asyncio.wait_for(anchor_entered.wait(), 2)
        if operation == "cancel_request":
            assert await manager.cancel_request(session.id, correction.request_id)
        else:
            original_work = await manager.close_work("withdrawn-input-work")
            work_cleanup = asyncio.create_task(manager.finish_work_cancellation([original_work]))
            done, _ = await asyncio.wait({work_cleanup}, timeout=.05)
            assert not done, "Work cleanup forgot the original preparation still holding its Git handle"
        completed = await asyncio.wait_for(correction.wait(), 2)
        assert completed.reason == "withdrawn" and completed.outcome is None
        assert completed.input_request_id == correction.request_id
        assert not preparations[0][0].done() and processes[0].returncode is None
        assert manager.dispatch_snapshot(session.id)["items"] == []

        release_anchor.set()
        if work_cleanup is not None:
            await asyncio.wait_for(work_cleanup, 2)
        next_correction = await manager.start_message(session.id, following, steerable=True)
        assert next_correction.disposition == "steered"
        await asyncio.wait_for(following_received.wait(), 2)
        delivered = [json.loads(line)["message"]["content"] for line in frames.read_text().splitlines()]
        assert delivered == [prompt, following], "The withdrawn input reached the original live process"
        assert preparations[0][0].done() and preparations[0][1].returncode == 0
        assert len(processes) == 1 and processes[0].returncode is None
        assert (await correction.wait()) is completed
        finish.touch()
        assert (await asyncio.wait_for(initial.wait(), 2)).outcome.status == "success"
        assert (await asyncio.wait_for(next_correction.wait(), 2)).outcome.status == "success"
        detail = await client.get(f"/api/sessions/{session.id}", headers=client.headers)
        assert detail.status_code == 200
        assert [row["content"] for row in detail.json()["messages"] if row["role"] == "user"] == [prompt, following]
        assert len([row for row in detail.json()["messages"] if row["type"] == "result"]) == 1
        await manager.interrupt(session.id)
        await manager.await_session_execution(session.id, timeout=3)
        assert processes[0].returncode == 0 and not Path(f"/proc/{processes[0].pid}").exists()
    finally:
        release_anchor.set()
        finish.touch()
        await manager.interrupt(session.id)
        await manager.await_session_execution(session.id, timeout=5)
        if work_cleanup is not None:
            await asyncio.gather(work_cleanup, return_exceptions=True)
