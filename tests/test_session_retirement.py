"""Explicit archival retires admitted work and keeps uncertain results recoverable."""

import asyncio
import json
import os
import select
import signal
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from server.database import Database
from server.harness import get_harness
from server.sessions import SessionManager
from server.sessions.client_submissions import submission_status, submit_client_message
from tests.accounts import create_account


@pytest.fixture
async def runtime(tmp_path, short_process_shutdown):
    database = Database(str(tmp_path / "retirement.db"))
    await database.initialize()
    await create_account(database, extra_roots=[tmp_path])
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
    monkeypatch.setattr(harness, "profile", replace(harness.profile, build_turn_argv=argv, prepare_resume=None))
    return starts


@pytest.mark.parametrize("action", ["archive", "evict"])
async def test_uncertain_recording_exits_and_cold_read_preserves_without_reexecution(tmp_path, action):
    proof, artifacts = await _crash_retirement_recording(tmp_path, action, committed=True)
    database = Database(proof["database"])
    restored = SessionManager()
    try:
        await database.initialize()
        await restored.initialize(database)
        status = await submission_status(restored, proof["session_id"], "original-input")
        assert status["accepted"] is True
        assert status["request_id"] == proof["request_id"]
        assert status["input"]["content"] == "execute once"
        assert status["execution"]["execution_owner_request_id"] == proof["request_id"]
        assert status["execution"]["state"] == "done"
        assert status["outcome"]["status"] == "success"
        assert status["outcome"]["request_id"] == proof["request_id"]
        duplicate = await submit_client_message(
            restored, proof["session_id"], "execute once", [], "original-input", wiki_mode=None,
        )
        assert duplicate == status
        row = next(row for row in await database.load_sessions() if row["id"] == proof["session_id"])
        assert not row["archived"]
        assert restored.get_session(proof["session_id"]) is not None
        messages = await database.load_messages(proof["session_id"])
        assert [message["content"] for message in messages if message["role"] == "user"] == ["execute once"]
        assert [message["content"] for message in messages if message["role"] == "assistant"] == ["ready"]
        results = [message["content"] for message in messages if message["type"] == "result"]
        assert results == [status["outcome"]]
        assert restored.dispatch_snapshot(proof["session_id"])["items"] == []
        assert (artifacts / "driver-commit-applied").exists()
    finally:
        await _retire_original_test_process(proof["model"])
        await database.close()


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
    try:
        await asyncio.wait_for(entered.wait(), 2)
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
        late = await manager.start_message(session.id, "late background return", source="bg")
        assert late.disposition == "closed"
        assert (await asyncio.wait_for(late.wait(), 2)).outcome is None
    finally:
        release.set()
        await asyncio.wait_for(archived.wait(), 2)
        await asyncio.gather(waiting, return_exceptions=True)
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


async def test_storage_failure_during_archive_keeps_cancelled_outcome_recoverable(tmp_path):
    proof, artifacts = await _crash_retirement_recording(tmp_path, "archive", committed=False)
    database = Database(proof["database"])
    restored = SessionManager()
    try:
        await database.initialize()
        await restored.initialize(database)
        status = await submission_status(restored, proof["session_id"], "original-input")
        assert status["accepted"] is True
        assert status["request_id"] == proof["request_id"]
        assert status["input"]["content"] == "cancel once"
        assert status["execution"]["execution_owner_request_id"] == proof["request_id"]
        assert status["execution"]["state"] == "unknown"
        assert status["execution"]["recoverable_input"] is False
        assert "outcome" not in status
        assert proof["selected_outcome"]["status"] == "interrupted"
        assert proof["selected_outcome"]["request_id"] == proof["request_id"]
        assert not (artifacts / "driver-commit-applied").exists()
        row = next(row for row in await database.load_sessions() if row["id"] == proof["session_id"])
        assert not row["archived"]
        assert restored.get_session(proof["session_id"]) is not None
        messages = await database.load_messages(proof["session_id"])
        assert [message["content"] for message in messages if message["role"] == "user"] == ["cancel once"]
        assert [message["content"] for message in messages if message["role"] == "assistant"] == ["ready"]
        assert not [message for message in messages if message["type"] == "result"]
        duplicate = await submit_client_message(
            restored, proof["session_id"], "cancel once", [], "original-input", wiki_mode=None,
        )
        assert duplicate == status
        with pytest.raises(ValueError, match="process exit is unconfirmed"):
            await restored.start_message(proof["session_id"], "Do not replace the unknown writer")
        assert restored.dispatch_snapshot(proof["session_id"])["items"] == []
    finally:
        await _retire_original_test_process(proof["model"])
        await database.close()


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
        async with asyncio.timeout(2):
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


async def test_competing_archive_is_rejected_before_owner_sets_reservation(runtime, monkeypatch):
    from server.sessions.owner_runtime import CloseExecution

    manager, session = runtime
    owner = manager._session_owner(session)
    call_runtime = owner.call_runtime
    entered, release = asyncio.Event(), asyncio.Event()

    async def delayed(command):
        if isinstance(command, CloseExecution) and command.reservation is not None:
            entered.set()
            await release.wait()
        return await call_runtime(command)

    monkeypatch.setattr(owner, "call_runtime", delayed)
    first = asyncio.create_task(manager.archive_session(session.id, replace=False))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        assert session._archive_reservation is None
        async with asyncio.timeout(2):
            with pytest.raises(ValueError, match="already being archived"):
                await manager.archive_session(session.id, replace=False)
        assert manager.get_session(session.id) is session
    finally:
        release.set()
        await asyncio.wait_for(first, 2)
    assert manager.get_session(session.id) is None


async def _retire_original_test_process(identity):
    try:
        descriptor = os.pidfd_open(identity["pid"])
    except ProcessLookupError:
        return
    try:
        try:
            fields = Path(f"/proc/{identity['pid']}/stat").read_text().rsplit(")", 1)[1].split()
        except FileNotFoundError:
            return
        if fields[19] != identity["start_time"]:
            return
        try:
            signal.pidfd_send_signal(descriptor, signal.SIGKILL)
        except ProcessLookupError:
            pass
        ready, _, _ = await asyncio.to_thread(select.select, [descriptor], [], [], 5)
        assert ready, f"Original offline model did not exit: {identity}"
    finally:
        os.close(descriptor)


async def _crash_retirement_recording(tmp_path, action, *, committed):
    artifacts = tmp_path / "terminal-transaction-fault"
    artifacts.mkdir()
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-q", "-s",
        str(Path(__file__).resolve()), "-o", "python_functions=_retirement_recording_child",
        "--basetemp", str(artifacts / "child-tests"),
        cwd=Path(__file__).resolve().parents[1],
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1",
             "OCTOPUS_TEST_RETIREMENT_ACTION": action,
             "OCTOPUS_TEST_RETIREMENT_COMMITTED": str(int(committed))},
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    output = asyncio.create_task(process.communicate())
    complete = False
    try:
        stdout, stderr = await asyncio.wait_for(asyncio.shield(output), 15)
        assert process.returncode == 70, (stdout + stderr).decode(errors="replace")
        proof = json.loads((artifacts / "proof.json").read_text())
        assert proof["action"] == action
        assert proof["committed"] is committed
        assert proof["process_group"] == proof["model"]["pid"] == proof["model"]["process_group"]
        assert (artifacts / "archive-entered").exists()
        assert not (artifacts / "archive-returned").exists()
        assert not (artifacts / "result-published").exists()
        assert len((artifacts / "starts.jsonl").read_text().splitlines()) == 1
        print(f"Actual terminal transaction fault exit=70 action={action} committed={committed} model={proof['model']}")
        complete = True
        return proof, artifacts
    finally:
        if process.returncode is None:
            process.kill()
        await asyncio.wait_for(output, 5)
        identity = artifacts / "model.json"
        if not complete and identity.exists():
            await _retire_original_test_process(json.loads(identity.read_text()))


async def _retirement_recording_child(tmp_path, tmp_path_factory, monkeypatch):
    """Inject at the actual SQLite commit, after real terminal INSERT SQL."""
    artifacts = tmp_path_factory.getbasetemp().parent
    action = os.environ["OCTOPUS_TEST_RETIREMENT_ACTION"]
    committed = os.environ["OCTOPUS_TEST_RETIREMENT_COMMITTED"] == "1"
    path = tmp_path / "terminal.db"
    database = Database(str(path))
    await database.initialize()
    await create_account(database, extra_roots=[tmp_path])
    manager = SessionManager()
    await manager.initialize(database)
    agent = await database.get_system_agent()
    session = await manager.create_session(agent["id"], "Original retirement", str(tmp_path), backend="codex")
    code = (
        "import json,os,sys,time\n"
        "from pathlib import Path\n"
        "fields=Path(f'/proc/{os.getpid()}/stat').read_text().rsplit(')',1)[1].split()\n"
        "Path(sys.argv[1]).write_text(json.dumps({'pid':os.getpid(),'process_group':os.getpgrp(),'start_time':fields[19]}))\n"
        "print(json.dumps({'type':'thread.started','thread_id':'retirement-thread'}),flush=True)\n"
        "print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':'ready'}}),flush=True)\n"
        "while not Path(sys.argv[2]).exists(): time.sleep(.01)\n"
        "print(json.dumps({'type':'turn.completed','usage':{}}),flush=True)\n"
    )

    def argv(context):
        with (artifacts / "starts.jsonl").open("a") as stream:
            stream.write(json.dumps({"prompt": context.prompt}) + "\n")
        return [sys.executable, "-u", "-c", code, str(artifacts / "model.json"),
                str(artifacts / "finish-model")], {"cwd": context.working_dir}

    harness = get_harness("codex")
    monkeypatch.setattr(harness, "profile", replace(
        harness.profile, build_turn_argv=argv, prepare_resume=None, activity_observer=None,
    ))
    ready = asyncio.Event()

    async def observe(event):
        if event.get("session_id") != session.id:
            return
        if event["type"] == "assistant_text":
            ready.set()
        if event["type"] == "result":
            (artifacts / "result-published").touch()

    manager.on_broadcast("retirement-recording-proof", observe)
    content = "execute once" if committed else "cancel once"
    admitted = await submit_client_message(manager, session.id, content, [], "original-input", wiki_mode=None)
    assert admitted["accepted"] is True
    await asyncio.wait_for(ready.wait(), 5)
    original = await database.get_client_submission(session.id, "original-input")
    model = json.loads((artifacts / "model.json").read_text())
    assert original["process_group"] == model["pid"]
    commit_entered, release_commit, action_entered = asyncio.Event(), asyncio.Event(), asyncio.Event()
    terminal = None
    execute, commit = database.conn.execute, database.conn.commit

    async def observe_terminal_sql(sql, parameters=()):
        nonlocal terminal
        cursor = await execute(sql, parameters)
        if sql.startswith("INSERT INTO messages ") and parameters[3] == "result":
            assert parameters[-1] == admitted["request_id"]
            terminal = json.loads(parameters[4])
        return cursor

    async def lose_actual_commit_ack():
        if terminal is None:
            return await commit()
        commit_entered.set()
        await release_commit.wait()
        if committed:
            await commit()
            (artifacts / "driver-commit-applied").touch()
        (artifacts / "proof.json").write_text(json.dumps({
            "database": str(path), "session_id": session.id, "request_id": admitted["request_id"],
            "process_group": original["process_group"], "model": model,
            "selected_outcome": terminal, "action": action, "committed": committed,
        }))
        raise OSError("actual terminal commit acknowledgment lost")

    monkeypatch.setattr(database.conn, "execute", observe_terminal_sql)
    monkeypatch.setattr(database.conn, "commit", lose_actual_commit_ack)

    async def archive_original():
        (artifacts / "archive-entered").touch()
        action_entered.set()
        if action == "archive":
            await manager.archive_session(session.id, replace=False)
        else:
            await manager.evict_agent_sessions(session.agent_id)
        (artifacts / "archive-returned").touch()

    if committed:
        (artifacts / "finish-model").touch()
        await asyncio.wait_for(commit_entered.wait(), 5)
        archiving = asyncio.create_task(archive_original())
        await asyncio.wait_for(action_entered.wait(), 2)
    else:
        archiving = asyncio.create_task(archive_original())
        await asyncio.wait_for(commit_entered.wait(), 5)
    release_commit.set()
    await asyncio.wait_for(archiving, 5)
    pytest.fail("A terminal transaction failure returned to its caller")
