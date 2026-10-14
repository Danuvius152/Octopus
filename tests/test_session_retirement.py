"""Explicit archival retires admitted work and keeps uncertain results recoverable."""

import asyncio
import sys
from dataclasses import replace

import pytest

from server.database import Database
from server.harness import get_harness
from server.sessions import SessionManager


@pytest.fixture
async def runtime(tmp_path):
    database = Database(str(tmp_path / "retirement.db"))
    await database.initialize()
    manager = SessionManager()
    await manager.initialize(database)
    agent = await database.get_system_agent()
    session = await manager.create_session(agent["id"], "Archive receipts", str(tmp_path), backend="codex")
    try:
        yield manager, session
    finally:
        await database.close()


@pytest.mark.parametrize("action", ["archive", "evict"])
async def test_explicit_archival_finishes_waiters_for_unstarted_requests(runtime, action):
    manager, session = runtime
    await manager.interrupt(session.id)
    receipt = await manager.start_message(session.id, "queued background result", source="bg")

    if action == "archive":
        await manager.archive_session(session.id, replace=False)
    else:
        assert await manager.evict_agent_sessions(session.agent_id) == [session.id]

    completion = await asyncio.wait_for(receipt.wait(), .2)
    assert completion.state == "withdrawn"
    assert completion.outcome is None
    assert manager.get_session(session.id) is None


def scripted_codex(monkeypatch, *, hold=False):
    starts = []
    script = (
        "import json,time\n"
        "print(json.dumps({'type':'thread.started','thread_id':'test-thread'}),flush=True)\n"
        "print(json.dumps({'type':'item.completed','item':{'id':'ready','type':'agent_message','text':'ready'}}),flush=True)\n"
        + ("time.sleep(60)\n" if hold else "print(json.dumps({'type':'turn.completed','usage':{}}),flush=True)\n")
    )

    def argv(context):
        starts.append(context.prompt)
        return [sys.executable, "-u", "-c", script], {"cwd": context.working_dir}

    harness = get_harness("codex")
    monkeypatch.setattr(harness, "profile", replace(harness.profile, build_turn_argv=argv))
    return starts


@pytest.mark.parametrize("action", ["archive", "evict"])
async def test_uncertain_recording_stays_live_and_can_be_verified_without_reexecution(runtime, monkeypatch, action):
    manager, session = runtime
    starts = scripted_codex(monkeypatch)
    record = manager.db.record_terminal

    async def uncertain(**kwargs):
        await record(**kwargs)
        raise OSError("lost commit acknowledgement")

    monkeypatch.setattr(manager.db, "record_terminal", uncertain)
    receipt = await manager.start_message(session.id, "execute once")
    completion = await asyncio.wait_for(receipt.wait(), 5)
    assert completion.state == "recording_failed"
    assert completion.outcome.status == "success"
    with pytest.raises(ValueError, match="recording"):
        if action == "archive":
            await manager.archive_session(session.id, replace=False)
        else:
            await manager.evict_agent_sessions(session.agent_id)
    assert manager.get_session(session.id) is session
    assert manager.dispatch_snapshot(session.id)["recording_failure"]["request_id"] == receipt.request_id

    monkeypatch.setattr(manager.db, "record_terminal", record)
    assert await manager.resume_dispatch(session.id, resume_queue=False)
    assert len(starts) == 1
    messages = await manager.db.load_messages(session.id)
    results = [message for message in messages if message["type"] == "result"]
    assert len(results) == 1
    assert results[0]["content"]["request_id"] == receipt.request_id


async def test_archival_owns_cleanup_when_http_waiter_is_cancelled(runtime, monkeypatch):
    manager, session = runtime
    await manager.interrupt(session.id)
    entered, release, archived = asyncio.Event(), asyncio.Event(), asyncio.Event()
    update = manager.db.update_session_field

    async def delayed(session_id, **fields):
        if fields.get("archived"):
            entered.set()
            await release.wait()
        return await update(session_id, **fields)

    async def observe(event):
        if event.get("type") == "session_archived" and event.get("old_session_id") == session.id:
            archived.set()

    manager.on_broadcast("observe-archive", observe)
    monkeypatch.setattr(manager.db, "update_session_field", delayed)
    waiting = asyncio.create_task(manager.archive_session(session.id, replace=False))
    await asyncio.wait_for(entered.wait(), 2)
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting
    late = await manager.start_message(session.id, "late background return", source="bg")
    assert late.disposition == "closed"
    assert (await asyncio.wait_for(late.wait(), 2)).outcome is None
    release.set()
    await asyncio.wait_for(archived.wait(), 2)
    assert manager.get_session(session.id) is None


@pytest.mark.parametrize("action", ["archive", "evict"])
async def test_active_archival_finishes_current_and_queued_requests_before_hiding(runtime, monkeypatch, action):
    manager, session = runtime
    starts = scripted_codex(monkeypatch, hold=True)
    ready = asyncio.Event()

    async def observe(event):
        if event.get("type") == "assistant_text" and event.get("content") == "ready":
            ready.set()

    manager.on_broadcast("observe-ready", observe)
    running = await manager.start_message(session.id, "running request")
    await asyncio.wait_for(ready.wait(), 3)
    pending = await manager.start_message(session.id, "later return", source="bg")
    if action == "archive":
        await asyncio.wait_for(manager.archive_session(session.id, replace=False), 8)
    else:
        await asyncio.wait_for(manager.evict_agent_sessions(session.agent_id), 8)

    completed = await asyncio.wait_for(running.wait(), .2)
    assert completed.state == "completed"
    assert completed.outcome.status == "interrupted"
    assert completed.recording_state == "committed"
    assert (await asyncio.wait_for(pending.wait(), .2)).state == "withdrawn"
    assert len(starts) == 1
    assert manager.get_session(session.id) is None


async def test_storage_failure_during_archive_keeps_cancelled_outcome_recoverable(runtime, monkeypatch):
    manager, session = runtime
    starts = scripted_codex(monkeypatch, hold=True)
    ready = asyncio.Event()

    async def observe(event):
        if event.get("type") == "assistant_text" and event.get("content") == "ready":
            ready.set()

    manager.on_broadcast("observe-ready", observe)
    running = await manager.start_message(session.id, "cancel once")
    await asyncio.wait_for(ready.wait(), 3)
    record = manager.db.record_terminal

    async def failed(**kwargs):
        raise OSError("storage failed during cancellation")

    monkeypatch.setattr(manager.db, "record_terminal", failed)
    with pytest.raises(ValueError, match="recording"):
        await asyncio.wait_for(manager.archive_session(session.id, replace=False), 8)
    completed = await asyncio.wait_for(running.wait(), .2)
    assert completed.state == "recording_failed"
    assert completed.outcome.status == "interrupted"
    assert manager.get_session(session.id) is session
    assert manager.dispatch_snapshot(session.id)["paused"]

    monkeypatch.setattr(manager.db, "record_terminal", record)
    assert await manager.resume_dispatch(session.id, resume_queue=False)
    assert manager.dispatch_snapshot(session.id)["paused"]
    assert len(starts) == 1


@pytest.mark.parametrize("contender", ["archive", "evict"])
async def test_second_archive_cannot_reopen_first_archives_admission(runtime, monkeypatch, contender):
    manager, session = runtime
    await manager.interrupt(session.id)
    entered, release = asyncio.Event(), asyncio.Event()
    update = manager.db.update_session_field

    async def delayed(session_id, **fields):
        if fields.get("archived"):
            entered.set()
            await release.wait()
        return await update(session_id, **fields)

    monkeypatch.setattr(manager.db, "update_session_field", delayed)
    first = asyncio.create_task(manager.archive_session(session.id, replace=False))
    await asyncio.wait_for(entered.wait(), 2)
    try:
        with pytest.raises(ValueError, match="already being archived"):
            if contender == "archive":
                await manager.archive_session(session.id, replace=False)
            else:
                await manager.evict_agent_sessions(session.agent_id)
        late = await manager.start_message(session.id, "late return", source="bg")
        assert late.disposition == "closed"
    finally:
        release.set()
        await asyncio.wait_for(first, 2)
