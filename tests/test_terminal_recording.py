"""Terminal recording is an acknowledged, idempotent storage boundary."""

import asyncio
import sqlite3
import sys
from dataclasses import replace

import pytest

from server.database import Database
from server.harness import get_harness
from server.sessions import SessionManager
from server.sessions.turns import _Settlement


@pytest.fixture
async def database(tmp_path):
    db = Database(str(tmp_path / "runtime.db"))
    await db.initialize()
    await db.save_session("s", "test", "/tmp", "2026-10-14")
    yield db
    await db.close()


def terminal(**changes):
    return {**dict(session_id="s", request_id="request", seq=0, role="system",
                   type="result", content={"status": "success"}, created_at="2026-10-14"),
            **changes}


@pytest.mark.asyncio
async def test_terminal_ack_is_visible_to_independent_reader_and_retry_is_identical(database):
    await database.record_terminal(**terminal())
    with sqlite3.connect(database._db_path) as reader:
        assert reader.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 1
    await database.record_terminal(**terminal())
    assert await database.count_messages("s") == 1
    with pytest.raises(ValueError, match="conflict"):
        await database.record_terminal(**terminal(content={"status": "failed"}))


@pytest.mark.asyncio
async def test_commit_uncertainty_reconciles_without_duplicate(database, monkeypatch):
    commit = database.conn.commit

    async def uncertain():
        await commit()
        raise OSError("ack lost")

    monkeypatch.setattr(database.conn, "commit", uncertain)
    with pytest.raises(OSError, match="ack lost"):
        await database.record_terminal(**terminal())
    monkeypatch.setattr(database.conn, "commit", commit)
    await database.record_terminal(**terminal())
    assert await database.count_messages("s") == 1


@pytest.mark.asyncio
async def test_committed_reader_cannot_create_missing_database(database, monkeypatch, tmp_path):
    missing = tmp_path / "missing verification.db"
    monkeypatch.setattr(database, "_db_path", str(missing))
    with pytest.raises(sqlite3.OperationalError, match="unable to open"):
        await database.get_committed_terminal("s", "request")
    assert not missing.exists()


@pytest.mark.asyncio
async def test_append_during_commit_keeps_new_write_dirty(database, monkeypatch):
    await database.append_message("s", 0, "assistant", "text", "first")
    entered, release = asyncio.Event(), asyncio.Event()
    commit = database.conn.commit

    async def gated():
        await commit()
        entered.set()
        await release.wait()

    monkeypatch.setattr(database.conn, "commit", gated)
    flush = asyncio.create_task(database.flush())
    await entered.wait()
    append = asyncio.create_task(database.append_message("s", 1, "assistant", "text", "second"))
    await asyncio.sleep(0)
    release.set()
    await asyncio.gather(flush, append)
    monkeypatch.setattr(database.conn, "commit", commit)
    await database.flush()
    with sqlite3.connect(database._db_path) as reader:
        assert reader.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 2


@pytest.mark.asyncio
async def test_recording_failure_preserves_choice_and_storage_only_recovery(database, monkeypatch, tmp_path):
    manager = SessionManager()
    await manager.initialize(database)
    agent = await database.get_system_agent()
    session = await manager.create_session(agent["id"], "recording", str(tmp_path))
    settlement = _Settlement(request_id="chosen")
    record = database.record_terminal
    calls = []
    frames = []

    async def observe(frame):
        frames.append(frame)

    manager.on_broadcast("observe", observe)

    async def fail(**kwargs):
        calls.append(kwargs)
        raise OSError("disk failure")

    monkeypatch.setattr(database, "record_terminal", fail)
    first = await manager._settle_turn(session, settlement, status="success")
    second = await manager._settle_turn(session, settlement, status="interrupted")
    assert first == second and first["type"] == "recording_failed"
    assert settlement.outcome.status == "success"
    assert session._recording_blocked is settlement
    assert len(calls) == 1
    assert not any(frame["type"] == "result" for frame in frames)
    reserved_count = session._message_count
    monkeypatch.setattr(database, "record_terminal", record)
    assert await manager._reconcile_recording(session)
    assert session._message_count == reserved_count
    rows = await database.load_messages(session.id)
    assert len(rows) == 1 and rows[0]["content"]["status"] == "success"
    assert len([frame for frame in frames if frame["type"] == "result"]) == 1
    assert await manager._reconcile_recording(session)
    assert len([frame for frame in frames if frame["type"] == "result"]) == 1


@pytest.mark.asyncio
async def test_actual_dispatch_waits_for_storage_then_preserves_stop_pause(database, monkeypatch, tmp_path):
    manager = SessionManager()
    await manager.initialize(database)
    agent = await database.get_system_agent()
    session = await manager.create_session(agent["id"], "dispatch storage", str(tmp_path), backend="codex")
    starts = []

    def argv(context):
        starts.append(context.prompt)
        code = "import json\nprint(json.dumps({'type':'thread.started','thread_id':'test-thread'}),flush=True)\nprint(json.dumps({'type':'turn.completed','usage':{}}),flush=True)"
        return [sys.executable, "-u", "-c", code], {"cwd": context.working_dir}

    harness = get_harness("codex")
    monkeypatch.setattr(harness, "profile", replace(harness.profile, build_turn_argv=argv))
    original = database.record_terminal

    async def uncertain(**kwargs):
        await original(**kwargs)
        raise OSError("commit acknowledgement lost")

    monkeypatch.setattr(database, "record_terminal", uncertain)
    first = await manager.start_message(session.id, "first")
    completion = await asyncio.wait_for(first.wait(), 5)
    assert completion.state == "recording_failed" and completion.outcome.status == "success"
    await manager.interrupt(session.id)
    second = await manager.start_message(session.id, "second", source="bg")
    await asyncio.sleep(.02)
    assert len(starts) == 1 and session._dispatch_paused
    monkeypatch.setattr(database, "record_terminal", original)
    assert await manager.resume_dispatch(session.id, resume_queue=False)
    assert session._dispatch_paused and len(starts) == 1
    assert await manager.resume_dispatch(session.id)
    assert (await asyncio.wait_for(second.wait(), 5)).state == "completed"
    assert len(starts) == 2
    rows = await database.load_messages(session.id)
    results = [row for row in rows if row["type"] == "result"]
    assert len(results) == 2 and results[0]["content"]["request_id"] == first.request_id
