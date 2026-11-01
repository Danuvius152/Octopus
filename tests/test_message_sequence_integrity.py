"""Holes are legal; reused identities and ambiguous history are not."""

import asyncio
import sqlite3
import sys

import pytest

from server import deps
from server.database import Database
from server.deps import get_session_manager
from server.harness import get_harness
from server.harness.claude_code import ClaudeDriver
from server.main import app
from server.models import MessageContent
from server.routers.sessions import get_session
from server.session_manager import SessionManager
from server.users import UserManager
from tests.accounts import create_account
from tests.harness_profiles import process_profile


@pytest.fixture
async def history(tmp_path):
    db = Database(str(tmp_path / "messages.db"))
    await db.initialize()
    await create_account(db, extra_roots=[tmp_path])
    manager = SessionManager()
    await manager.initialize(db)
    agent = await db.get_system_agent()
    session = await manager.create_session(agent["id"], "history", str(tmp_path))
    app.dependency_overrides[get_session_manager] = lambda: manager
    try:
        yield db, manager, session
    finally:
        app.dependency_overrides.pop(get_session_manager, None)
        await manager.db.close()


async def restart(db, manager):
    path = db._db_path
    await db.close()
    reopened = Database(path)
    await reopened.initialize()
    deps.set_user_manager(UserManager(reopened))
    manager.sessions.clear()
    await manager.initialize(reopened)
    return reopened


async def test_append_failure_hole_survives_reload_and_unarchive(history, monkeypatch):
    db, manager, session = history
    append = db.append_message
    failed = False

    async def flaky(**fields):
        nonlocal failed
        if not failed:
            failed = True
            raise OSError("before insert")
        await append(**fields)

    monkeypatch.setattr(db, "append_message", flaky)
    with pytest.raises(OSError):
        await manager._persist_message(session, MessageContent(role="user", type="text", content="lost"))
    assert await manager._persist_message(session, MessageContent(role="user", type="text", content="kept")) == 1
    assert session._message_count == 1
    manager.sessions.clear()
    await manager.initialize(db)
    restored = manager.get_session(session.id)
    assert restored._message_count == 1 and restored._next_message_seq == 2
    await manager.archive_session(session.id, replace=False)
    restored = await manager.unarchive_session(session.id)
    assert await manager._persist_message(restored, MessageContent(role="user", type="text", content="after restore")) == 2
    detail = await get_session(manager, session.id, user_id=None)
    assert detail.message_count == 2
    assert detail.next_message_seq == 3
    assert detail.oldest_loaded_seq == 1 and detail.has_more_messages is False
    assert [message.seq for message in detail.messages] == [1, 2]


async def test_fork_floor_does_not_become_a_snapshot_watermark(history):
    db, manager, parent = history
    # A copied prefix may contain no surviving row near its replay cutoff.
    await db.update_session_field(parent.id, fork_after_seq=9)
    manager.sessions.clear()
    await manager.initialize(db)
    session = manager.get_session(parent.id)
    assert session._next_message_seq == 10 and session._message_count == 0
    detail = await get_session(manager, parent.id, user_id=None)
    assert detail.next_message_seq == 0 and detail.messages == []
    assert await manager._persist_message(session, MessageContent(role="user", type="text", content="new")) == 10


async def test_reserved_terminal_does_not_advance_snapshot_watermark(history, monkeypatch):
    db, manager, previous = history
    session = await manager.create_session(
        previous.agent_id, "Reserved terminal", previous.working_dir, backend="codex",
    )
    code = (
        "import json,sys\nsys.stdin.readline()\n"
        "print(json.dumps({'type':'thread.started','thread_id':'sequence-terminal'}),flush=True)\n"
        "print(json.dumps({'type':'turn.completed','usage':{}}),flush=True)\n"
    )
    harness = get_harness("codex")
    monkeypatch.setattr(harness, "profile", process_profile(
        harness.profile, binary=sys.executable, prepare_resume=None, activity_observer=None,
        new_driver=ClaudeDriver, can_steer=True, reusable=True,
        build_turn_argv=lambda context: ([sys.executable, "-u", "-c", code], {"cwd": context.working_dir}),
    ))
    entered, release = asyncio.Event(), asyncio.Event()
    record = db.record_terminal

    async def delayed(**fields):
        entered.set()
        await release.wait()
        await record(**fields)

    monkeypatch.setattr(db, "record_terminal", delayed)
    frames = []

    async def observe(frame):
        frames.append(frame)

    manager.on_broadcast("sequence-test", observe)
    receipt = await manager.start_message(session.id, "question")
    try:
        await asyncio.wait_for(entered.wait(), 2)
        detail = await get_session(manager, session.id, user_id=None)
        assert session._next_message_seq == 2
        assert detail.message_count == 1 and detail.next_message_seq == 1
    finally:
        release.set()
        completed = await asyncio.wait_for(receipt.wait(), 5)
    assert completed.state == "completed" and completed.recording_state == "committed"
    assert completed.outcome.status == "success"
    assert [frame["seq"] for frame in frames if frame["type"] == "result"] == [1]
    assert session._message_count == 2


async def test_unique_sequences_are_enforced_after_reopening_current_storage(history):
    db, manager, session = history
    await db.append_message(session.id, 5, "user", "text", "one")
    with pytest.raises(sqlite3.IntegrityError):
        await db.append_message(session.id, 5, "assistant", "text", "collision")
    db = await restart(db, manager)
    with pytest.raises(sqlite3.IntegrityError):
        await db.append_message(session.id, 5, "system", "text", "new collision")
    await db.append_message(session.id, 6, "user", "text", "safe new key")
    with pytest.raises(sqlite3.IntegrityError):
        async with db.transaction() as tx:
            await tx.execute("UPDATE messages SET seq = 5 WHERE session_id = ? AND seq = 6", (session.id,))
    rows = await db.load_messages(session.id)
    assert [row["content"] for row in rows] == ["one", "safe new key"]
