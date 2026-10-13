"""Closing the database has to unwind what is still using it.

Both halves here were found the same way: an intermittently *hanging* test run —
not a failing one. A turn cancelled after its connection has gone runs its own
`finally`, which flushes and broadcasts, has nothing to await, and cannot be
cancelled out of the attempt; the event loop then stops making progress. What
you see is a suite that passes twice and stalls forever on the third run.
"""

from __future__ import annotations

import asyncio
import sys
from dataclasses import replace

import pytest

from server.database import Database
from server.harness import get_harness
from server.session_manager import SessionManager


@pytest.mark.asyncio
async def test_close_hooks_run_while_the_connection_still_answers():
    """A hook is for work that needs the database one last time."""
    db = Database(":memory:")
    await db.initialize()
    seen: list[int] = []

    async def hook() -> None:
        cursor = await db.conn.execute("SELECT COUNT(*) FROM sessions")
        seen.append((await cursor.fetchone())[0])

    db.add_close_hook(hook)
    await db.close()
    assert seen == [0], "the hook could not reach the database"


@pytest.mark.asyncio
async def test_close_hooks_unwind_in_reverse_and_refuse_unconfirmed_teardown():
    db = Database(":memory:")
    await db.initialize()
    order: list[str] = []

    async def first() -> None:
        order.append("first")

    failed = True

    async def boom() -> None:
        order.append("boom")
        if failed:
            raise RuntimeError("no")

    db.add_close_hook(first)
    db.add_close_hook(boom)
    with pytest.raises(RuntimeError, match="teardown"):
        await db.close()

    assert order == ["boom", "first"], order
    # Necessary producers still own the connection until their teardown is
    # confirmed; a retry may finish without abandoning them into a dead DB.
    await db._ensure_connected()
    failed = False
    await db.close()
    with pytest.raises(asyncio.CancelledError):
        await db._ensure_connected()


@pytest.mark.asyncio
async def test_a_closed_database_is_closed_before_its_connection_goes():
    """The flag order, which is the race on its own.

    `await conn.close()` yields. A task that reaches `_ensure_connected` in that
    window used to pass the check and submit work to a worker thread that was
    stopping — a future nothing would ever resolve.
    """
    db = Database(":memory:")
    await db.initialize()

    closed_during_hook: list[bool] = []

    async def hook() -> None:
        # Hooks run *before* the flag, so they can still use the connection.
        closed_during_hook.append(db._closed)

    db.add_close_hook(hook)
    await db.close()

    assert closed_during_hook == [False]
    assert db._closed is True
    with pytest.raises(asyncio.CancelledError):
        await db._ensure_connected()


@pytest.mark.asyncio
async def test_the_session_manager_drains_its_turns_when_the_db_closes():
    """The hook that matters: a turn in flight is stopped while it can still
    unwind, rather than cancelled into a connection that has gone."""
    db = Database(":memory:")
    await db.initialize()
    mgr = SessionManager()
    mgr.sessions.clear()
    mgr._admission_closed = False
    await mgr.initialize(db)

    agent = await db.get_system_agent()
    session = await mgr.create_session(agent["id"], "work", "/tmp")
    started, unwound = asyncio.Event(), asyncio.Event()

    async def never_ends() -> None:
        started.set()
        try:
            await asyncio.sleep(3600)
        finally:
            unwound.set()

    session._active_task = asyncio.create_task(never_ends())
    await started.wait()
    await db.close()
    assert unwound.is_set(), "the turn was not drained before the DB closed"
    assert session._active_task.done()
    mgr.sessions.clear()
    mgr._admission_closed = False


@pytest.mark.asyncio
async def test_shutdown_keeps_database_open_until_terminal_writer_finishes(tmp_path, monkeypatch):
    db = Database(str(tmp_path / "shutdown.db"))
    await db.initialize()
    manager = SessionManager()
    await manager.initialize(db)
    agent = await db.get_system_agent()
    session = await manager.create_session(agent["id"], "Shutdown", str(tmp_path), backend="codex")
    starts = []

    def argv(context):
        starts.append(context.prompt)
        code = "import json\nprint(json.dumps({'type':'thread.started','thread_id':'test-thread'}),flush=True)\nprint(json.dumps({'type':'turn.completed','usage':{}}),flush=True)"
        return [sys.executable, "-u", "-c", code], {"cwd": context.working_dir}

    harness = get_harness("codex")
    monkeypatch.setattr(harness, "profile", replace(harness.profile, build_turn_argv=argv))
    entered, release = asyncio.Event(), asyncio.Event()
    record = db.record_terminal

    async def blocked_record(**kwargs):
        entered.set()
        await release.wait()
        return await record(**kwargs)

    async def bounded_drain():
        await manager.drain_turns(timeout=.03)

    monkeypatch.setattr(db, "record_terminal", blocked_record)
    db._close_hooks[-1] = bounded_drain
    receipt = await manager.start_message(session.id, "finish once")
    try:
        await asyncio.wait_for(entered.wait(), 3)
        with pytest.raises(RuntimeError, match="teardown"):
            await db.close()
        assert not db._closed
        await db._ensure_connected()
        assert manager._finalization_tasks
        assert (await manager.start_message(session.id, "late", source="bg")).disposition == "closed"
        release.set()
        completion = await asyncio.wait_for(receipt.wait(), 3)
        assert completion.outcome.status == "success"
        assert completion.recording_state == "committed"
        assert len(starts) == 1
        await db.close()
        assert db._closed and not manager._finalization_tasks
    finally:
        release.set()
        await manager.drain_turns()
        await db.close()


@pytest.mark.asyncio
async def test_failed_question_cleanup_keeps_database_open_for_remaining_questions(tmp_path, monkeypatch):
    db = Database(str(tmp_path / "questions-shutdown.db"))
    await db.initialize()
    manager = SessionManager()
    await manager.initialize(db)
    agent = await db.get_system_agent()
    session = await manager.create_session(agent["id"], "Question shutdown", str(tmp_path))
    first = await manager.create_pending_question(session.id, [{"question": "first", "options": []}])
    second = await manager.create_pending_question(session.id, [{"question": "second", "options": []}])
    append = db.append_message
    failures = []

    async def fail_first_closure(*args, **kwargs):
        if kwargs.get("type") == "question_closed" and not failures:
            failures.append(kwargs.get("tool_use_id"))
            raise OSError("first question closure could not be saved")
        return await append(*args, **kwargs)

    monkeypatch.setattr(db, "append_message", fail_first_closure)
    try:
        with pytest.raises(RuntimeError, match="teardown"):
            await asyncio.wait_for(db.close(), 2)
        assert failures == [first]
        assert not manager.has_pending_question(session.id, first)
        assert manager.has_pending_question(session.id, second)
        # This public storage read succeeds only while teardown keeps the
        # original connection available; retry can close the remaining form.
        assert await db.count_messages(session.id) >= 2
        await asyncio.wait_for(db.close(), 2)
        assert not manager.has_pending_question(session.id, second)
    finally:
        monkeypatch.setattr(db, "append_message", append)
        await db.close()
