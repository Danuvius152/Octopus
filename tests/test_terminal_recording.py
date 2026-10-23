"""Terminal recording is an acknowledged, idempotent storage boundary."""

import asyncio
import json
import os
import sqlite3
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from server.database import Database
from server.harness import get_harness
from server.sessions import SessionManager


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


async def test_terminal_ack_also_confirms_the_recoverable_input(database):
    recovery = {"rejected_input": {"content": "Complete input", "attachment_ids": ["abcdef123456"]}}
    await database.record_terminal(**terminal(tool_input=recovery))
    await database.record_terminal(**terminal(tool_input=recovery))
    with pytest.raises(ValueError, match="conflict"):
        await database.record_terminal(**terminal(tool_input=None))
    assert (await database.load_messages("s"))[0]["tool_input"] == recovery


@pytest.mark.asyncio
@pytest.mark.parametrize("committed", [False, True])
async def test_uncertain_terminal_commit_exits_and_restart_keeps_one_outcome(tmp_path, committed):
    program = """
import asyncio
import sys
from pathlib import Path
from server.database import Database

async def main():
    database = Database(sys.argv[1])
    await database.initialize()
    await database.save_session("s", "test", "/tmp", "2026-10-14")
    commit = database.conn.commit
    async def uncertain():
        if sys.argv[2] == "True":
            await commit()
        raise OSError("ack lost")
    database.conn.commit = uncertain
    await database.record_terminal(session_id="s", request_id="request", seq=0,
        role="system", type="result", content={"status":"success"}, created_at="2026-10-14")
    Path(sys.argv[3]).touch()

asyncio.run(main())
"""
    path, success = tmp_path / "terminal.db", tmp_path / "success"
    result = subprocess.run(
        [sys.executable, "-c", program, str(path), str(committed), str(success)],
        cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=5,
    )
    assert result.returncode == 70, result.stderr
    assert not success.exists()
    database = Database(str(path))
    await database.initialize()
    try:
        assert await database.count_messages("s") == int(committed)
        await database.record_terminal(**terminal())
        await database.record_terminal(**terminal())
        assert await database.count_messages("s") == 1
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_committed_reader_cannot_create_missing_database(database, monkeypatch, tmp_path):
    await database.record_terminal(**terminal())
    missing = tmp_path / "missing verification.db"
    monkeypatch.setattr(database, "_db_path", str(missing))
    assert await database.get_committed_terminal("s", "request") == {"status": "success"}
    assert not missing.exists()


@pytest.mark.asyncio
async def test_append_during_commit_cannot_share_another_operations_acknowledgment(database, monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()
    commit = database.conn.commit

    async def gated():
        await commit()
        entered.set()
        await release.wait()

    monkeypatch.setattr(database.conn, "commit", gated)
    first = asyncio.create_task(database.append_message("s", 0, "assistant", "text", "first"))
    await entered.wait()
    append = asyncio.create_task(database.append_message("s", 1, "assistant", "text", "second"))
    await asyncio.sleep(0)
    assert not first.done() and not append.done()
    release.set()
    await asyncio.gather(first, append)
    monkeypatch.setattr(database.conn, "commit", commit)
    with sqlite3.connect(database._db_path) as reader:
        assert reader.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 2


def _crash_runtime_terminal(tmp_path, committed):
    """Lose the real terminal transaction acknowledgment of one public input."""
    program = r'''
import asyncio,json,sys
from dataclasses import replace
from pathlib import Path
from server.database import Database
from server.harness import get_harness
from server.sessions import SessionManager
from server.sessions.client_submissions import submit_client_message

async def main():
    artifacts = Path(sys.argv[1])
    database = Database(str(artifacts / "runtime.db"))
    await database.initialize()
    manager = SessionManager()
    await manager.initialize(database)
    agent = await database.get_system_agent()
    session = await manager.create_session(agent["id"], "Original terminal", str(artifacts), backend="codex")
    def argv(context):
        with (artifacts / "starts.jsonl").open("a") as stream:
            stream.write(json.dumps({"prompt":context.prompt}) + "\n")
        code = "import json\nprint(json.dumps({'type':'thread.started','thread_id':'original-thread'}),flush=True)\nprint(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':'Original answer'}}),flush=True)\nprint(json.dumps({'type':'turn.completed','usage':{}}),flush=True)"
        return [sys.executable,"-u","-c",code], {"cwd":context.working_dir}
    harness = get_harness("codex")
    harness.profile = replace(harness.profile,build_turn_argv=argv,prepare_resume=None,activity_observer=None)
    receipts=[]
    start = manager.start_message
    async def capture(*args,**kwargs):
        receipt = await start(*args,**kwargs)
        receipts.append(receipt)
        return receipt
    manager.start_message = capture
    async def observe(event):
        if event.get("session_id") == session.id and event.get("type") == "result":
            (artifacts / "result-published").touch()
    manager.on_broadcast("terminal-fault-proof",observe)
    selected=None
    execute,commit=database.conn.execute,database.conn.commit
    async def observe_sql(sql,parameters=()):
        nonlocal selected
        cursor=await execute(sql,parameters)
        if sql.startswith("INSERT INTO messages ") and parameters[3] == "result":
            selected=json.loads(parameters[4])
        return cursor
    async def uncertain_commit():
        if selected is None:
            return await commit()
        applied=sys.argv[2] == "True"
        if applied:
            await commit()
        (artifacts / "proof.json").write_text(json.dumps({"database":str(artifacts / "runtime.db"),"session_id":session.id,"request_id":selected["request_id"],"selected":selected,"committed":applied}))
        raise OSError("actual terminal commit acknowledgment lost")
    database.conn.execute=observe_sql
    database.conn.commit=uncertain_commit
    reply=await submit_client_message(manager,session.id,"Original input 中文",[],"original",wiki_mode=None)
    assert reply["accepted"]
    await receipts[0].wait()
    (artifacts / "completion-returned").touch()
asyncio.run(main())
'''
    artifacts = tmp_path / "terminal-process"
    artifacts.mkdir()
    result = subprocess.run(
        [sys.executable, "-c", program, str(artifacts), str(committed)],
        cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=10,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )
    assert result.returncode == 70, result.stdout + result.stderr
    proof = json.loads((artifacts / "proof.json").read_text())
    assert proof["committed"] is committed
    assert proof["selected"]["status"] == "success"
    assert not (artifacts / "result-published").exists()
    assert not (artifacts / "completion-returned").exists()
    assert len((artifacts / "starts.jsonl").read_text().splitlines()) == 1
    return proof, artifacts


@pytest.mark.parametrize("committed", [False, True])
async def test_recording_failure_preserves_choice_without_reexecution(tmp_path, committed):
    from server.sessions.client_submissions import (
        submission_status,
        submit_client_message,
    )

    proof, _ = _crash_runtime_terminal(tmp_path, committed)
    database = Database(proof["database"])
    await database.initialize()
    manager = SessionManager()
    await manager.initialize(database)
    try:
        rows = await database.load_messages(proof["session_id"])
        assert [row["content"] for row in rows if row["role"] == "user"] == ["Original input 中文"]
        assert [row["content"] for row in rows if row["role"] == "assistant"] == ["Original answer"]
        results = [row["content"] for row in rows if row["type"] == "result"]
        assert results == ([proof["selected"]] if committed else [])
        status = await submission_status(manager, proof["session_id"], "original")
        assert status["accepted"] is True
        assert status["request_id"] == proof["request_id"]
        assert status["input"]["content"] == "Original input 中文"
        assert status["execution"]["state"] == ("done" if committed else "unknown")
        duplicate = await submit_client_message(manager, proof["session_id"], "Original input 中文", [], "original", wiki_mode=None)
        assert duplicate == status
        assert manager.dispatch_snapshot(proof["session_id"])["items"] == []
    finally:
        await database.close()


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
    monkeypatch.setattr(harness, "profile", replace(harness.profile, build_turn_argv=argv, prepare_resume=None))
    original = database.record_terminal

    entered, release = asyncio.Event(), asyncio.Event()

    async def confirming(**kwargs):
        entered.set()
        await release.wait()
        return await original(**kwargs)

    monkeypatch.setattr(database, "record_terminal", confirming)
    first = await manager.start_message(session.id, "first")
    await asyncio.wait_for(entered.wait(), 5)
    stop = asyncio.create_task(manager.interrupt(session.id))
    release.set()
    completion = await asyncio.wait_for(first.wait(), 5)
    await stop
    assert completion.recording_state == "committed" and completion.outcome.status == "success"
    second = await manager.start_message(session.id, "second", source="bg")
    await asyncio.sleep(.02)
    assert len(starts) == 1 and manager.dispatch_snapshot(session.id)["paused"]
    monkeypatch.setattr(database, "record_terminal", original)
    assert await manager.resume_dispatch(session.id, resume_queue=False)
    assert manager.dispatch_snapshot(session.id)["paused"] and len(starts) == 1
    assert await manager.resume_dispatch(session.id)
    assert (await asyncio.wait_for(second.wait(), 5)).state == "completed"
    assert len(starts) == 2
    rows = await database.load_messages(session.id)
    results = [row for row in rows if row["type"] == "result"]
    assert len(results) == 2 and results[0]["content"]["request_id"] == first.request_id
