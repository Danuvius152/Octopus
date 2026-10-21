"""Rewind holds its original resource and releases only its own reservation."""

from __future__ import annotations

import asyncio
import json
import sys
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from server.config import settings
from server.database import Database
from server.deps import get_session_manager
from server.harness import get_harness
from server.harness.profile import StdinMode
from server.harness.run import HarnessRun
from server.routers.sessions import router
from server.sessions import SessionManager
from server.sessions.owner import SessionOwnerUnavailable


@pytest.fixture
async def held_rewind(tmp_path, monkeypatch, request):
    directory = tmp_path / "original"
    directory.mkdir()
    source = directory / "source.txt"
    source.write_text("Keep the original workspace bytes.\n")
    model = directory / "held_model.py"
    identity_path, input_path = directory / "identity.json", directory / "input.json"
    retirement_path = directory / "retirement.json"
    model.write_text(
        "import json,os,signal,sys,time\nfrom pathlib import Path\n"
        "signal.signal(signal.SIGTERM,lambda *_:None)\n"
        "fields=Path(f'/proc/{os.getpid()}/stat').read_text().rsplit(')',1)[1].split()\n"
        "Path(sys.argv[1]).write_text(json.dumps({'pid':os.getpid(),'pgid':os.getpgrp(),'start_time':fields[19]}))\n"
        "Path(sys.argv[2]).write_text(sys.stdin.readline())\n"
        "print(json.dumps({'type':'thread.started','thread_id':'original-rewind-thread'}),flush=True)\n"
        "print(json.dumps({'type':'item.completed','item':{'id':'original-answer','type':'agent_message','text':'Original confirmed answer.'}}),flush=True)\n"
        "print(json.dumps({'type':'turn.completed','usage':{}}),flush=True)\n"
        "sys.stdin.read()\n"
        "began=time.monotonic()\n"
        "time.sleep(float(sys.argv[4]))\n"
        "Path(sys.argv[3]).write_text(json.dumps({'eof_at':began,'exited_at':time.monotonic()}))\n"
    )
    harness = get_harness("codex")
    monkeypatch.setattr(harness, "profile", replace(
        harness.profile, binary=sys.executable, stdin_mode=StdinMode.STREAM_JSON,
        activity_observer=None, prepare_resume=None,
        build_turn_argv=lambda context: (
            [sys.executable, "-u", str(model), str(identity_path), str(input_path),
             str(retirement_path), str(request.param)],
            {"cwd": context.working_dir},
        ),
    ))
    spawn, stream = asyncio.create_subprocess_exec, HarnessRun.stream
    processes, readers = [], set()

    async def capture_spawn(*args, **kwargs):
        process = await spawn(*args, **kwargs)
        if str(model) in args:
            processes.append(process)
        return process

    async def capture_stream(run):
        readers.add(asyncio.current_task())
        async for event in stream(run):
            yield event

    monkeypatch.setattr(asyncio, "create_subprocess_exec", capture_spawn)
    monkeypatch.setattr(HarnessRun, "stream", capture_stream)
    database = Database(str(tmp_path / "rewind.sqlite3"))
    await database.initialize()
    manager = SessionManager()
    await manager.initialize(database)
    agent = await database.get_system_agent()
    parent = await manager.create_session(agent["id"], "Original parent", str(directory), backend="codex")
    events = []

    async def observe(event):
        events.append(event)

    manager.on_broadcast("original-rewind", observe)
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_session_manager] = lambda: manager
    async with AsyncClient(
        transport=ASGITransport(app), base_url="http://test",
        headers={"Authorization": f"Bearer {settings.auth_token}"},
    ) as client:
        try:
            prompt = "Keep this original request and completed answer."
            receipt = await manager.start_message(parent.id, prompt, work_id=f"original-work-{parent.id}")
            completion = await asyncio.wait_for(receipt.wait(), 3)
            assert completion.outcome.status == "success" and completion.recording_state == "committed"
            assert len(processes) == len(readers) == 1
            identity = json.loads(identity_path.read_text())
            assert identity["pid"] == identity["pgid"] == processes[0].pid
            assert Path(f"/proc/{identity['pid']}").exists() and processes[0].returncode is None
            assert json.loads(input_path.read_text())["message"]["content"] == prompt
            assert all(reader.done() for reader in readers)
            stdout = {task for task in asyncio.all_tasks() if task.get_name() == "codex-stdout"}
            owner_tasks = {task for task in asyncio.all_tasks()
                           if task.get_name() == f"session-owner-{parent.id}" or task.get_name() == "session-publication"}
            assert len(stdout) == 1 and len(owner_tasks) == 2
            yield SimpleNamespace(
                manager=manager, database=database, parent=parent, client=client,
                events=events, receipt=receipt, completion=completion, identity=identity,
                processes=processes, readers=readers, stdout=stdout, owner_tasks=owner_tasks,
                source=source, retirement_path=retirement_path, prompt=prompt,
            )
        finally:
            try:
                await manager.drain_turns(timeout=7)
                await manager.stop_all_held_processes()
            finally:
                for process in processes:
                    if process.returncode is None:
                        process.kill()
                    await asyncio.wait_for(process.wait(), 3)
                await database.close()
                await asyncio.gather(*readers, return_exceptions=True)


async def _joined_original_process(env):
    await asyncio.wait_for(env.processes[0].wait(), 6)
    assert env.processes[0].returncode == 0
    assert not Path(f"/proc/{env.identity['pid']}").exists()
    assert all(task.done() for task in env.readers | env.stdout)
    return json.loads(env.retirement_path.read_text())


async def _unchanged_original_history(env):
    detail = await env.client.get(f"/api/sessions/{env.parent.id}")
    assert detail.status_code == 200
    rows = detail.json()["messages"]
    assert [row["content"] for row in rows if row["role"] == "user"] == [env.prompt]
    assert [row["content"] for row in rows if row["role"] == "assistant" and row["type"] == "text"] == ["Original confirmed answer."]
    assert [row["content"] for row in rows if row["type"] == "result"] == [env.completion.outcome.model_dump()]
    assert env.source.read_text() == "Keep the original workspace bytes.\n"
    assert len(env.processes) == 1
    return detail.json()


@pytest.mark.parametrize("held_rewind", [.1], indirect=True)
async def test_failed_owner_resource_join_does_not_wait_for_its_calling_subscriber(held_rewind, monkeypatch):
    env = held_rewind
    entered, enter_retirement, retired, release_notice = (
        asyncio.Event(), asyncio.Event(), asyncio.Event(), asyncio.Event()
    )
    read = env.database.has_delivery_notice

    async def subscriber(event):
        if event.get("session_id") == env.parent.id and event.get("type") == "delivery_notice":
            entered.set()
            await enter_retirement.wait()
            await env.manager.stop_all_held_processes()
            retired.set()
            await release_notice.wait()

    async def fail_original_owner(session_id, delivery_id):
        result = await read(session_id, delivery_id)
        if delivery_id == "failed-resource-owner":
            raise RuntimeError("Original owner failed while its subscriber still owns publication")
        return result

    env.manager.on_broadcast("retirement-reentry", subscriber)
    closing = None
    try:
        assert await env.manager.retain_result_notice(env.parent.id, "original-resource-notice", "Original retained material")
        await asyncio.wait_for(entered.wait(), 2)
        monkeypatch.setattr(env.database, "has_delivery_notice", fail_original_owner)
        with pytest.raises(SessionOwnerUnavailable):
            await env.manager.retain_result_notice(env.parent.id, "failed-resource-owner", "No second notice")
        closing = asyncio.create_task(env.manager.close_session_owners())
        enter_retirement.set()
        await asyncio.wait_for(retired.wait(), 3)
        await _joined_original_process(env)
        assert not closing.done(), "Resource confirmation incorrectly ended the original publication drain"
        assert any(not task.done() for task in env.owner_tasks)
        assert (await env.database.get_committed_terminal(env.parent.id, env.receipt.request_id))["status"] == "success"
        release_notice.set()
        await asyncio.wait_for(closing, 3)
        assert all(task.done() for task in env.owner_tasks)
        assert [event["content"] for event in env.events if event["type"] == "delivery_notice"] == ["Original retained material"]
    finally:
        enter_retirement.set()
        release_notice.set()
        if closing is not None:
            await asyncio.gather(closing, return_exceptions=True)


@pytest.mark.parametrize("held_rewind", [2.35], indirect=True, ids=["slow-original-exit"])
async def test_rewind_joins_a_slow_original_process_before_archiving_its_parent(held_rewind):
    env = held_rewind
    started = time.monotonic()
    child = await asyncio.wait_for(env.manager.fork_session(env.parent.id, 0), 7)
    fork_seconds = time.monotonic() - started
    returned_before_exit = env.processes[0].returncode is None
    retirement = await _joined_original_process(env)
    assert retirement["exited_at"] - retirement["eof_at"] >= 2.3
    parent_live = env.manager.get_session(env.parent.id) is env.parent
    admission = None
    if parent_live:
        refused = await env.manager.start_message(env.parent.id, "Do not execute this diagnostic input.", source="bg")
        admission = refused.disposition
    print("Actual slow rewind boundary: " + json.dumps({
        "pid": env.identity["pid"], "fork_seconds": fork_seconds,
        "retirement_seconds": retirement["exited_at"] - retirement["eof_at"],
        "ready_child": child.id, "parent_live_after_exit": parent_live,
        "parent_input_disposition": admission, "returned_before_original_exit": returned_before_exit,
        "request_id": env.receipt.request_id, "process_returncode": env.processes[0].returncode,
    }), flush=True)
    assert not parent_live, "Ready rewind child left its live parent permanently closed after successful original retirement"
    assert not returned_before_exit
    assert child.fork_status == "ready" and env.manager.get_session(child.id) is child
    archived = await _unchanged_original_history(env)
    assert archived["archived"] is True
    swapped = [event for event in env.events if event["type"] == "session_archived"]
    assert len(swapped) == 1 and swapped[0]["old_session_id"] == env.parent.id and swapped[0]["new_session_id"] == child.id
    done, pending = await asyncio.wait(env.owner_tasks, timeout=3)
    assert not pending and all(not task.cancelled() and task.exception() is None for task in done)


@pytest.mark.parametrize("held_rewind", [0.0], indirect=True, ids=["precommit-domain-refusal"])
async def test_rewind_domain_tail_releases_its_original_reservation_for_retry(held_rewind, monkeypatch):
    env = held_rewind
    update = env.database.update_session_field
    refusals = []

    async def reject_once_before_the_archive_write(session_id, **fields):
        if session_id == env.parent.id and fields.get("archived") and not refusals:
            # The original resource already joined. Read the actual binding,
            # then refuse before entering the archive mutation at all.
            binding = await env.database.get_session_binding(session_id)
            assert binding is not None and not binding["archived"]
            assert env.processes[0].returncode == 0
            refusals.append(binding)
            raise ValueError("Original archive domain tail refused once")
        await update(session_id, **fields)

    monkeypatch.setattr(env.database, "update_session_field", reject_once_before_the_archive_write)
    child = await asyncio.wait_for(env.manager.fork_session(env.parent.id, 0), 5)
    await _joined_original_process(env)
    assert len(refusals) == 1 and not refusals[0]["archived"]
    assert not (await env.database.get_session_binding(env.parent.id))["archived"]
    assert child.fork_status == "ready" and env.manager.get_session(child.id) is child
    assert env.manager.get_session(env.parent.id) is env.parent
    assert not any(event["type"] == "session_archived" for event in env.events)
    assert await env.manager.retain_result_notice(
        env.parent.id, "after-domain-refusal", "Original parent remains available for a reviewed retry.",
    )
    queued = await env.manager.start_message(env.parent.id, "Retained retry input.", source="bg")
    assert queued.accepted
    assert await env.manager.cancel_request(env.parent.id, queued.request_id)
    assert (await queued.wait()).outcome is None
    await asyncio.wait_for(env.manager.archive_session(env.parent.id, replace=False), 3)
    assert env.manager.get_session(env.parent.id) is None and env.manager.get_session(child.id) is child
    done, pending = await asyncio.wait(env.owner_tasks, timeout=3)
    assert not pending and all(not task.cancelled() and task.exception() is None for task in done)
    archived = await _unchanged_original_history(env)
    assert archived["archived"] is True
    assert [row["content"] for row in archived["messages"] if row["type"] == "delivery_notice"] == [
        "Original parent remains available for a reviewed retry.",
    ]
    assert len([event for event in env.events if event.get("type") == "delivery_notice"]) == 1
    print("Actual rewind domain retry: " + json.dumps({
        "pid": env.identity["pid"], "ready_child": child.id, "request_id": env.receipt.request_id,
        "domain_refusals": len(refusals), "retry_disposition": queued.disposition,
        "process_count": len(env.processes), "process_returncode": env.processes[0].returncode,
        "parent_owner_and_publisher_joined": len(done),
    }), flush=True)
