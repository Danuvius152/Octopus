"""Public regressions for the fixed runtime owner review findings."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from pathlib import Path

import pytest

from server.crypto import encrypt
from server.database import Database
from server.harness import get_harness
from server.harness.run import HarnessRun
from server.sessions import SessionManager
from server.sessions.owner import SessionMailboxFull, SessionOwnerUnavailable
from server.users import UserManager
from tests import test_question_lifecycle as question_tests
from tests.accounts import create_account
from tests.harness_profiles import process_profile
from tests.test_session_retirement import _retire_original_test_process
from tests.test_submission_recovery import _submit_paused

question_host = question_tests.question_host
owned_question_host = question_tests.owned_question_host


async def held_python(manager, original, directory, monkeypatch, *, credential=None):
    gate, identity = directory / "late-output", directory / "original.pid"
    model = directory / "held_model.py"
    model.write_text(
        "import json,os,sys,time\nfrom pathlib import Path\n"
        "Path(sys.argv[2]).write_text(str(os.getpid()))\n"
        "sys.stdin.readline()\n"
        "print(json.dumps({'type':'system','subtype':'init','session_id':'review-held-thread'}),flush=True)\n"
        "print(json.dumps({'type':'assistant','message':{'content':[{'type':'text','text':'Initial confirmed body'}]}}),flush=True)\n"
        "print(json.dumps({'type':'result','subtype':'success','is_error':False,'session_id':'review-held-thread'}),flush=True)\n"
        "while not Path(sys.argv[1]).exists(): time.sleep(.005)\n"
        "print(json.dumps({'type':'assistant','message':{'content':[{'type':'text','text':'Late original held body'}]}}),flush=True)\n"
        "sys.stdin.read()\n"
    )
    harness = get_harness("claude-code")
    monkeypatch.setattr(harness, "profile", process_profile(
        harness.profile, binary=sys.executable, prepare_resume=None, activity_observer=None,
        build_turn_argv=lambda context: ([sys.executable, "-u", str(model), str(gate), str(identity)],
                                          {"cwd": context.working_dir}),
    ))
    session = await manager.create_session(
        original.agent_id, "Original reviewed transport", str(directory), backend="claude-code",
        credential_id=credential,
    )
    receipt = await manager.start_message(session.id, "Preserve the original input")
    completion = await asyncio.wait_for(receipt.wait(), 3)
    assert completion.recording_state == "committed" and completion.outcome.status == "success"
    assert Path(f"/proc/{int(identity.read_text())}").exists()
    return session, gate, identity, receipt


async def test_recovery_dismissal_keeps_the_original_publication_order(question_host):
    manager, session, client, _events = question_host
    accepted = await _submit_paused(manager, session.id, "Recover this input", "dismiss-owned")
    assert accepted["accepted"]
    assert await manager.cancel_request(session.id, accepted["request_id"])
    entered, release, delivered = asyncio.Event(), asyncio.Event(), asyncio.Event()
    order = []

    async def hold_original(event):
        if event.get("type") == "delivery_notice":
            entered.set()
            await release.wait()
            order.append("original")

    async def observe(event):
        if entered.is_set() and event.get("type") == "dispatch_state":
            order.append("dismissed")
            delivered.set()

    manager.on_broadcast("hold-dismiss-prefix", hold_original)
    manager.on_broadcast("observe-dismiss-order", observe)
    assert await manager.retain_result_notice(session.id, "dismiss-prefix", "Original prefix")
    try:
        await asyncio.wait_for(entered.wait(), 2)
        response = await client.delete(f"/api/sessions/{session.id}/submissions/dismiss-owned/recovery", headers=client.headers)
        assert response.status_code == 204
        assert order == [], "Recovery dismissal bypassed the original session publisher"
        assert (await manager.db.get_client_submission(session.id, "dismiss-owned"))["dismissed"]
    finally:
        release.set()
    await asyncio.wait_for(delivered.wait(), 2)
    assert order == ["original", "dismissed"]


@pytest.mark.parametrize("disposition", ["archived", "failed"])
async def test_recovery_dismissal_stays_available_after_runtime_retirement(question_host, monkeypatch, disposition):
    manager, session, client, _events = question_host
    admitted = await _submit_paused(manager, session.id, "Retain recovery evidence", "retired-dismiss")
    assert await manager.cancel_request(session.id, admitted["request_id"])
    if disposition == "archived":
        await manager.archive_session(session.id, replace=False)
    else:
        read = manager.db.has_delivery_notice

        async def original_read_failure(*args):
            await read(*args)
            raise RuntimeError("Original owner programming failure")

        monkeypatch.setattr(manager.db, "has_delivery_notice", original_read_failure)
        with pytest.raises(SessionOwnerUnavailable):
            await manager.retain_result_notice(session.id, "original-failure", "Unconfirmed material")
    response = await client.delete(f"/api/sessions/{session.id}/submissions/retired-dismiss/recovery", headers=client.headers)
    assert response.status_code == 204
    assert (await manager.db.get_client_submission(session.id, "retired-dismiss"))["dismissed"]
    if disposition == "failed":
        with pytest.raises(SessionOwnerUnavailable):
            await manager.start_message(session.id, "Do not restart a failed owner")


async def test_saturated_notifications_refuse_submit_before_acceptance_and_keep_stop_live(question_host):
    manager, session, _client, _events = question_host
    await manager.interrupt(session.id)
    entered, release, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def hold_original(event):
        if event.get("type") == "delivery_notice":
            entered.set()
            await release.wait()
            finished.set()

    manager.on_broadcast("saturate-original-budget", hold_original)
    assert await manager.retain_result_notice(session.id, "one-large-frame", "M" * (1024 * 1024 + 100))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        for index in range(8):
            with pytest.raises(SessionMailboxFull):
                await manager.start_message(session.id, f"Unaccepted return {index}", source="bg", delivery_id=f"budget-{index}")
        assert manager.dispatch_snapshot(session.id)["items"] == []
        await asyncio.wait_for(manager.interrupt(session.id), 1)
        assert manager.dispatch_snapshot(session.id)["paused"]
    finally:
        release.set()
    await asyncio.wait_for(finished.wait(), 2)
    receipt = await manager.start_message(session.id, "Retry the original identity", source="bg", delivery_id="budget-0")
    assert receipt.accepted
    duplicate = await manager.start_message(session.id, "Retry the original identity", source="bg", delivery_id="budget-0")
    assert duplicate.disposition == "duplicate" and duplicate.request_id == receipt.request_id
    assert [row["summary"] for row in manager.dispatch_snapshot(session.id)["items"]] == ["Retry the original identity"]


@pytest.mark.parametrize("required", [False, True])
@pytest.mark.parametrize("failure_type", [RuntimeError, asyncio.CancelledError])
async def test_late_held_publication_uses_its_original_credential(question_host, tmp_path, monkeypatch, caplog, required, failure_type):
    manager, original, _client, events = question_host
    secret = "originalheldpublicationcredential"
    await manager.db.save_credential(
        "review-held-credential", "claude-code", "Held diagnostic", "api_key",
        encrypt(secret, await UserManager(manager.db).data_key_for(original.user_id)), "2026-10-21T00:00:00+00:00",
        user_id=original.user_id,
    )
    session, gate, _identity, _receipt = await held_python(manager, original, tmp_path, monkeypatch, credential="review-held-credential")
    detail = "Original held subscriber transport cancellation"
    failure, cause = failure_type(f"{detail}; {secret}"), RuntimeError(f"Original held cause: {secret}")
    failed = asyncio.Event()

    async def cancel_original(event):
        if event.get("session_id") == session.id and event.get("content") == "Late original held body":
            failed.set()
            raise failure from cause

    manager.on_broadcast("held-original-credential", cancel_original, required=required)
    with caplog.at_level(logging.ERROR, logger="server"):
        gate.touch()
        await asyncio.wait_for(failed.wait(), 2)
        await asyncio.wait_for(manager.interrupt(session.id), 2)
        await manager.await_session_execution(session.id, timeout=3)
    diagnostic = "\n".join(logging.Formatter().format(record) for record in caplog.records if record.name.startswith("server."))
    assert detail in diagnostic and "Original held cause" in diagnostic
    assert "Traceback (most recent call last)" in diagnostic
    assert secret not in diagnostic and "[redacted]" in diagnostic
    assert failure.args == (f"{detail}; {secret}",) and failure.__cause__ is cause
    assert len([event for event in events if event.get("session_id") == session.id and event.get("type") == "result"]) == 1


async def test_subscriber_archive_drains_captured_held_output_behind_its_original_notice(owned_question_host, monkeypatch):
    manager, original, client, events = owned_question_host
    entered, allow_archive, callback_finished, idle_entered = (asyncio.Event() for _ in range(4))
    failures = []
    original_readers, archive_proof = [], []
    install_handler = HarnessRun.set_idle_handler

    def capture_handler(run, handler):
        async def original_idle(event):
            if event.content == "Late original held body":
                original_readers.append(asyncio.current_task())
                idle_entered.set()
            await handler(event)
        install_handler(run, original_idle)

    monkeypatch.setattr(HarnessRun, "set_idle_handler", capture_handler)
    directory = Path(original.working_dir) / "review-held"
    directory.mkdir(parents=True)
    session, gate, identity, receipt = await held_python(manager, original, directory, monkeypatch)
    owner_frames, foreign_frames = [], []

    async def observe_owner(event):
        if event.get("content") == "Late original held body":
            owner_frames.append(event)

    async def observe_foreign(event):
        if event.get("session_id") == session.id:
            foreign_frames.append(event)

    manager.on_broadcast("original-captured-account", observe_owner, user_id=session.user_id)
    manager.on_broadcast("other-account-must-not-receive", observe_foreign, user_id="different-review-owner")

    async def archive_from_notice(event):
        if event.get("session_id") == session.id and event.get("type") == "delivery_notice":
            entered.set()
            await allow_archive.wait()
            try:
                async with asyncio.timeout(.5):
                    await manager.archive_session(session.id, replace=False)
                archive_proof.append({
                    "process_exited": not Path(f"/proc/{int(identity.read_text())}").exists(),
                    "reader_joined": all(reader.done() for reader in original_readers),
                    "late_still_ordered": not any(event.get("content") == "Late original held body" for event in events),
                })
            except TimeoutError as error:
                failures.append(error)
            finally:
                callback_finished.set()

    manager.on_broadcast("archive-after-original-idle-handoff", archive_from_notice)
    assert await manager.retain_result_notice(session.id, "exclusive-original-prefix", "N" * (1024 * 1024 + 100))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        gate.touch()
        await asyncio.wait_for(idle_entered.wait(), 2)
        allow_archive.set()
        await asyncio.wait_for(callback_finished.wait(), 2)
        assert failures == [], "Archive joined the held output waiting on its calling subscriber"
        assert archive_proof == [{"process_exited": True, "reader_joined": True, "late_still_ordered": True}]
        assert not Path(f"/proc/{int(identity.read_text())}").exists()
    finally:
        allow_archive.set()
        await asyncio.wait_for(manager.drain_turns(timeout=3), 4)
    await asyncio.wait_for(manager.close_session_owners(), 3)
    detail = await client.get(f"/api/sessions/{session.id}")
    assert detail.status_code == 200 and detail.json()["archived"]
    rows = detail.json()["messages"]
    assert [row["content"] for row in rows if row["role"] == "user"] == ["Preserve the original input"]
    assert [row["content"] for row in rows if row["type"] == "text" and row["role"] == "assistant"] == [
        "Initial confirmed body", "Late original held body",
    ]
    late = [event for event in events if event.get("session_id") == session.id and event.get("content") == "Late original held body"]
    assert len(late) == 1
    assert late[0]["request_id"] == receipt.request_id
    assert late[0]["attempt_id"] == next(event["attempt_id"] for event in events
                                        if event.get("type") == "attempt_started" and event.get("request_id") == receipt.request_id)
    assert late[0]["work_id"] is None
    assert owner_frames == late and foreign_frames == []
    assert len([row for row in rows if row["type"] == "result"]) == 1
    assert (await receipt.wait()).outcome.status == "success"


@pytest.mark.parametrize("operation", ["stop", "reaper"])
async def test_idle_retirement_failure_seals_the_original_owner_without_starting_queued_work(tmp_path, operation):
    artifacts = tmp_path / "idle-retirement"
    artifacts.mkdir()
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-q", "-s", str(Path(__file__).resolve()),
        "-o", "python_functions=_idle_retirement_failure_child", "--basetemp", str(artifacts / "child-tests"),
        cwd=Path(__file__).resolve().parents[1],
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "OCTOPUS_TEST_IDLE_RETIREMENT": operation},
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    output = asyncio.create_task(process.communicate())
    try:
        stdout, stderr = await asyncio.wait_for(asyncio.shield(output), 12)
        (artifacts / "service.log").write_bytes(stdout + stderr)
        assert process.returncode == 70, (stdout + stderr).decode(errors="replace")
        proof = json.loads((artifacts / "proof.json").read_text())
        assert proof["operation"] == operation and proof["successor_accepted"]
        assert (artifacts / "stop-failed").read_text() == "Original held exit is unconfirmed"
        assert not (artifacts / "successor-started").exists()
        database = Database(proof["database"])
        try:
            await database.initialize()
            rows = await database.load_messages(proof["session_id"])
            assert [row["content"] for row in rows if row["role"] == "user"] == ["Preserve the original input"]
            results = [row["content"] for row in rows if row["type"] == "result"]
            assert len(results) == 1 and results[0]["request_id"] == proof["original_request_id"]
            assert results[0]["status"] == "success"
        finally:
            await database.close()
    finally:
        if process.returncode is None:
            process.kill()
        await asyncio.wait_for(output, 5)
        if (artifacts / "proof.json").exists():
            await _retire_original_test_process(json.loads((artifacts / "proof.json").read_text())["model"])


async def _idle_retirement_failure_child(tmp_path, tmp_path_factory, monkeypatch):
    """Selected only in an isolated process, with one real original stop failure."""
    from server.sessions import processes

    artifacts = tmp_path_factory.getbasetemp().parent
    operation = os.environ["OCTOPUS_TEST_IDLE_RETIREMENT"]
    database_path = tmp_path / "original.db"
    database = Database(str(database_path))
    await database.initialize()
    await create_account(database, extra_roots=[tmp_path])
    manager = SessionManager()
    await manager.initialize(database)
    agent = await database.get_system_agent()
    original = await manager.create_session(agent["id"], "Original installation", str(tmp_path))
    session, _gate, identity, receipt = await held_python(manager, original, tmp_path, monkeypatch)
    entered, release = asyncio.Event(), asyncio.Event()
    failure = RuntimeError("Original held exit is unconfirmed")

    async def original_stop(run):
        entered.set()
        await release.wait()
        (artifacts / "stop-failed").write_text(str(failure))
        raise failure

    monkeypatch.setattr(HarnessRun, "stop", original_stop)
    harness = get_harness("claude-code")
    original_argv = harness.profile.build_turn_argv

    def successor_argv(context):
        (artifacts / "successor-started").write_text(context.prompt)
        return original_argv(context)

    monkeypatch.setattr(harness, "profile", process_profile(harness.profile, build_turn_argv=successor_argv))
    if operation == "stop":
        await manager.interrupt(session.id)
        sweep = None
    else:
        monkeypatch.setattr(processes, "_HELD_PROCESS_IDLE_SECONDS", 0)
        sweep = asyncio.create_task(manager.reap_held_processes())
    await asyncio.wait_for(entered.wait(), 2)
    successor = await manager.start_message(session.id, "Keep this successor pending", source="bg")
    assert successor.accepted
    await manager.resume_dispatch(session.id)
    assert [row["request_id"] for row in manager.dispatch_snapshot(session.id)["items"]] == [successor.request_id]
    pid = int(identity.read_text())
    fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    (artifacts / "proof.json").write_text(json.dumps({
        "operation": operation, "database": str(database_path), "session_id": session.id,
        "original_request_id": receipt.request_id, "successor_request_id": successor.request_id,
        "successor_accepted": successor.accepted,
        "model": {"pid": pid, "process_group": os.getpgid(pid), "start_time": fields[19]},
    }))
    release.set()
    if sweep is not None:
        await sweep
    # The unchanged Manager must consume this original failed retirement.
    # No close or second fault is introduced to turn an idle stall into70.
    await asyncio.sleep(.75)
    os._exit(42)
