"""Accepted browser input survives restart without replaying model work."""

import asyncio
import json
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from server import deps
from server.database import Database
from server.harness import get_harness
from server.sessions import SessionManager
from server.sessions.client_submissions import submission_status, submit_client_message
from server.users import UserManager
from tests.accounts import create_account


@pytest.fixture(autouse=True)
def offline_harness(monkeypatch):
    def reject_unexpected_execution(context):
        raise AssertionError("A browser input retained before execution reached process preparation")

    harness = get_harness("codex")
    monkeypatch.setattr(harness, "profile", replace(
        harness.profile, build_turn_argv=reject_unexpected_execution,
        prepare_resume=None, activity_observer=None,
    ))


async def _submit_paused(manager, session_id, content, client_id, *, attachment_ids=None):
    """Stop a public dequeued notification before this input can execute."""
    stopped = asyncio.Event()
    key = f"pause-browser-{client_id}"

    async def stop_unstarted(event):
        if event.get("type") == "dequeued" and event.get("session_id") == session_id:
            await manager.interrupt(session_id)
            stopped.set()

    manager.on_broadcast(key, stop_unstarted)
    try:
        reply = await submit_client_message(
            manager, session_id, content, attachment_ids or [], client_id, wiki_mode=None,
        )
        assert reply["accepted"] is True
        await asyncio.wait_for(stopped.wait(), 5)
        await manager.await_session_execution(session_id, timeout=5)
        async with asyncio.timeout(5):
            while manager.dispatch_snapshot(session_id)["active_request_id"] is not None:
                await asyncio.sleep(0.01)
        snapshot = manager.dispatch_snapshot(session_id)
        assert snapshot["paused"] is True
        assert reply["request_id"] in [item["request_id"] for item in snapshot["items"]]
        return reply
    finally:
        manager.remove_broadcast(key)


async def test_queued_input_is_recoverable_after_shutdown_and_database_reopen(tmp_path, monkeypatch):
    path = str(tmp_path / "recovery.db")
    db = Database(path)
    await db.initialize()
    await create_account(db, extra_roots=[tmp_path])
    manager = SessionManager()
    await manager.initialize(db)
    agent = await db.get_system_agent()
    session = await manager.create_session(agent["id"], "recovery", str(tmp_path), backend="codex")
    reply = await _submit_paused(manager, session.id, "Keep my complete input 中文", "queued-input")
    assert reply["accepted"] is True
    await db.close()

    reopened = Database(path)
    await reopened.initialize()
    deps.set_user_manager(UserManager(reopened))
    restored = SessionManager()
    await restored.initialize(reopened)
    try:
        status = await submission_status(restored, session.id, "queued-input")
        assert status["accepted"] is True  # admission is an immutable historical fact
        assert status["request_id"] == reply["request_id"]
        assert status["execution"]["state"] == "withdrawn"
        assert status["execution"]["recoverable_input"] is True
        assert status["input"]["content"] == "Keep my complete input 中文"
        duplicate = await submit_client_message(restored, session.id, "Keep my complete input 中文", [], "queued-input", wiki_mode=None)
        assert duplicate == status
        assert restored.dispatch_snapshot(session.id)["items"] == []
        assert await reopened.load_messages(session.id) == []
    finally:
        await reopened.close()


def _lose_submission_process(tmp_path, mode):
    proof = tmp_path / f"{mode}-loss.json"
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-q", str(Path(__file__).resolve()),
         "-o", "python_functions=_submission_process_loss_child"],
        cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=15,
        env={**os.environ, "OCTOPUS_TEST_SUBMISSION_LOSS_PROOF": str(proof),
             "OCTOPUS_TEST_SUBMISSION_LOSS_MODE": mode, "PYTHONDONTWRITEBYTECODE": "1"},
    )
    assert result.returncode == 73, result.stdout + result.stderr
    return json.loads(proof.read_text())


async def _submission_process_loss_child(tmp_path):
    """Leave real committed admission evidence without graceful withdrawal."""
    proof = Path(os.environ["OCTOPUS_TEST_SUBMISSION_LOSS_PROOF"])
    mode = os.environ["OCTOPUS_TEST_SUBMISSION_LOSS_MODE"]
    path = str(proof.with_suffix(".db"))
    db = Database(path)
    await db.initialize()
    await create_account(db, extra_roots=[tmp_path])
    manager = SessionManager()
    await manager.initialize(db)
    agent = await db.get_system_agent()
    session = await manager.create_session(agent["id"], mode, str(tmp_path), backend="codex")
    content = "Potential side effects" if mode == "claimed" else "Possibly delivered steer"
    reply = await _submit_paused(manager, session.id, content, mode)
    owner = reply["request_id"] if mode == "claimed" else "parent-request"
    assert await db.claim_client_submission(session.id, reply["request_id"], owner, manager._dispatch_generation)
    if mode == "steer":
        await db.set_steer_delivery(session.id, reply["request_id"], confirmed=False)
        await db.record_terminal(
            request_id=owner, session_id=session.id, seq=0, role="system", type="result",
            content={"request_id": owner, "status": "success"}, created_at="2026-10-20T00:00:00Z",
        )
    proof.write_text(json.dumps({"database": path, "session_id": session.id, "request_id": reply["request_id"]}))
    os._exit(73)


async def test_claimed_input_is_unknown_and_never_replayed_after_restart(tmp_path):
    evidence = _lose_submission_process(tmp_path, "claimed")
    reopened = Database(evidence["database"])
    await reopened.initialize()
    deps.set_user_manager(UserManager(reopened))
    restored = SessionManager()
    await restored.initialize(reopened)
    try:
        status = await submission_status(restored, evidence["session_id"], "claimed")
        assert status["accepted"] is True
        assert status["request_id"] == evidence["request_id"]
        assert status["execution"]["state"] == "unknown"
        assert status["execution"]["recoverable_input"] is False
        assert status["input"]["content"] == "Potential side effects"
        denied = await submit_client_message(restored, evidence["session_id"], "Start another writer", [], "new", wiki_mode=None)
        assert denied["accepted"] is False
        assert "process exit is unconfirmed" in denied["message"]
        assert restored.dispatch_snapshot(evidence["session_id"])["items"] == []
    finally:
        await reopened.close()


async def test_startup_retirement_does_not_cover_later_unknown_inputs_even_after_dismissal(tmp_path):
    """Exercise the recovery handoff; real supervisor proof lives in its suite."""
    evidence = _lose_submission_process(tmp_path, "claimed")
    db = Database(evidence["database"])
    await db.initialize()
    deps.set_user_manager(UserManager(db))
    manager = SessionManager()
    await manager.initialize(db, previous_writers_retired=True)
    session_id = evidence["session_id"]
    try:
        await manager.interrupt(session_id)
        assert manager.dispatch_snapshot(session_id)["paused"]
        created, current = await db.admit_client_submission(
            session_id, "current", "current-digest", "current-request",
            payload={"content": "Current generation input", "attachment_ids": [], "attachments": [], "wiki_mode": None},
            receipt={"accepted": True, "disposition": "accepted"},
        )
        assert created
        snapshot = manager.dispatch_snapshot(session_id)
        assert await db.claim_client_submission(session_id, current["request_id"], current["request_id"], snapshot["generation"])
        queued = await manager.start_message(session_id, "Already accepted independent return", source="bg")
        assert queued.accepted
        await db.complete_client_submission(session_id, current["request_id"], owner=current["request_id"], state="unknown")
        assert await manager.dismiss_recoverable_input(session_id, "current")
        assert (await submission_status(manager, session_id, "current"))["execution"]["state"] == "unknown"
        with pytest.raises(ValueError, match="process exit is unconfirmed"):
            await manager.start_message(session_id, "New work after hiding the input")
        # An input accepted before uncertainty appeared must obey the same
        # retirement evidence when it eventually reaches actual dispatch.
        assert await manager.resume_dispatch(session_id)
        completion = await asyncio.wait_for(queued.wait(), 5)
        assert completion.outcome.status == "failed"
        assert "process exit is unconfirmed" in completion.outcome.message
        assert not [row for row in await db.load_messages(session_id) if row["role"] == "user"]
        assert (await submission_status(manager, session_id, "claimed"))["execution"]["state"] == "unknown"
    finally:
        await db.close()


async def test_manual_resubmission_has_new_identity_and_one_recovery_owner(tmp_path, monkeypatch):
    db = Database(":memory:")
    await db.initialize()
    await create_account(db, extra_roots=[tmp_path])
    manager = SessionManager()
    await manager.initialize(db)
    agent = await db.get_system_agent()
    session = await manager.create_session(agent["id"], "manual", str(tmp_path), backend="codex")
    original = await _submit_paused(manager, session.id, "Original", "original")
    assert await manager.cancel_request(session.id, original["request_id"])
    stopped = asyncio.Event()

    async def stop_recovery(event):
        if event.get("type") == "dequeued" and event.get("session_id") == session.id:
            await manager.interrupt(session.id)
            stopped.set()

    manager.on_broadcast("pause-recovery", stop_recovery)
    try:
        replies = await asyncio.gather(*(
            submit_client_message(manager, session.id, "Edited by user", [], client, wiki_mode=None, recovery_of_client_id="original")
            for client in ("recovered-a", "recovered-b")
        ))
        accepted = [row for row in replies if row["accepted"]]
        assert len(accepted) == 1
        await asyncio.wait_for(stopped.wait(), 5)
        await manager.await_session_execution(session.id, timeout=5)
        assert accepted[0]["request_id"] != original["request_id"]
        assert accepted[0]["recovery_of_client_id"] == "original"
        assert len(manager.dispatch_snapshot(session.id)["items"]) == 1
        repeated = await submit_client_message(manager, session.id, "Edited by user", [], accepted[0]["client_request_id"], wiki_mode=None, recovery_of_client_id="original")
        assert repeated["request_id"] == accepted[0]["request_id"]
        assert len(manager.dispatch_snapshot(session.id)["items"]) == 1
        conflict = await submit_client_message(manager, session.id, "Changed again", [], accepted[0]["client_request_id"], wiki_mode=None, recovery_of_client_id="original")
        assert conflict["code"] == "submission_conflict"
    finally:
        manager.remove_broadcast("pause-recovery")
        await db.close()


async def test_input_persistence_failure_never_opens_dispatch(tmp_path, monkeypatch):
    db = Database(":memory:")
    await db.initialize()
    await create_account(db, extra_roots=[tmp_path])
    manager = SessionManager()
    await manager.initialize(db)
    agent = await db.get_system_agent()
    session = await manager.create_session(agent["id"], "saving", str(tmp_path), backend="codex")

    async def fail(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(db, "admit_client_submission", fail)
    try:
        reply = await submit_client_message(manager, session.id, "Must not execute", [], "unsaved", wiki_mode=None)
        assert reply["accepted"] is False and reply["state"] == "unknown"
        assert manager.dispatch_snapshot(session.id)["items"] == []
        assert not manager.session_is_executing(session)
        assert await db.load_messages(session.id) == []
    finally:
        await db.close()


async def test_recovery_listing_is_paginated_private_and_dismissible(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from server.deps import get_session_manager
    from server.routers.sessions import router

    db = Database(":memory:")
    await db.initialize()
    _, bearer = await create_account(db, extra_roots=[tmp_path])
    manager = SessionManager()
    await manager.initialize(db)
    agent = await db.get_system_agent()
    session = await manager.create_session(agent["id"], "pages", str(tmp_path), backend="codex")
    for number in range(3):
        await _submit_paused(manager, session.id, f"Input {number}", f"input-{number}")
    await manager.drain_turns()
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_session_manager] = lambda: manager
    async with AsyncClient(transport=ASGITransport(app), base_url="http://test", headers={"Authorization": f"Bearer {bearer}"}) as client:
        try:
            base = f"/api/sessions/{session.id}/submissions"
            first = (await client.get(base, params={"limit": 2})).json()
            assert len(first["items"]) == 2 and first["next_cursor"] is not None
            assert "input" not in first["items"][0]
            second = (await client.get(base, params={"before": first["next_cursor"], "limit": 2})).json()
            assert len(second["items"]) == 1 and second["next_cursor"] is None
            assert {row["client_request_id"] for row in first["items"] + second["items"]} == {"input-0", "input-1", "input-2"}
            assert (await client.get(base, params={"limit": 101})).status_code == 422
            assert (await client.delete(f"{base}/input-0/recovery")).status_code == 204
            assert len((await client.get(base)).json()["items"]) == 2
            _, foreign_bearer = await create_account(db, username="another-owner")
            client.headers["Authorization"] = f"Bearer {foreign_bearer}"
            assert (await client.get(base)).status_code == 404
            assert (await client.get(f"{base}/input-1")).status_code == 404
            assert (await client.delete(f"{base}/input-1/recovery")).status_code == 404
        finally:
            await db.close()


from tests.test_runtime_review_races import wait_dispatch_idle


@pytest.fixture
async def runtime(monkeypatch, tmp_path):
    """Exercise browser input against a real offline process and SQLite."""
    from server.config import settings

    monkeypatch.setattr(settings, "agents_dir", str(tmp_path / "agents"))
    monkeypatch.setattr(settings, "users_root", str(tmp_path / "users"))
    db = Database(":memory:")
    await db.initialize()
    _, bearer = await create_account(db, extra_roots=[tmp_path])
    manager = SessionManager()
    await manager.initialize(db)
    agent = await db.get_system_agent()
    session = await manager.create_session(agent["id"], "Browser recovery", str(tmp_path), backend="codex")
    starts, events = asyncio.Queue(), []

    async def observe(event):
        events.append(event)

    manager.on_broadcast("browser-recovery-events", observe)

    def argv(context):
        starts.put_nowait(context)
        code = "import json,time\n"
        code += "print(json.dumps({'type':'thread.started','thread_id':'recovery-thread'}),flush=True)\n"
        code += "print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':'Reply.'}}),flush=True)\n"
        if "held" in context.prompt:
            code += "time.sleep(30)\n"
        code += "print(json.dumps({'type':'turn.completed','usage':{}}),flush=True)\n"
        return [sys.executable, "-u", "-c", code], {"cwd": context.working_dir}

    harness = get_harness("codex")
    monkeypatch.setattr(harness, "profile", replace(harness.profile, build_turn_argv=argv, prepare_resume=None))
    try:
        yield manager, db, session, None, starts, events
    finally:
        await db.close()


async def test_real_dispatch_withdraws_second_input_without_erasing_first_history(runtime, short_process_shutdown):
    import asyncio

    manager, db, session, _, starts, _ = runtime
    await manager.start_message(session.id, "held first input")
    await asyncio.wait_for(starts.get(), 5)
    second = await submit_client_message(manager, session.id, "Second input must survive", [], "second", wiki_mode=None)
    assert second["execution"]["state"] == "pending"
    await manager.drain_turns()
    status = await submission_status(manager, session.id, "second")
    assert status["execution"]["state"] == "withdrawn"
    assert status["input"]["content"] == "Second input must survive"
    assert starts.empty()
    history = await db.load_messages(session.id)
    assert any(row["role"] == "user" and row["content"] == "held first input" for row in history)
    assert not any(row["role"] == "user" and row["content"] == "Second input must survive" for row in history)


async def test_browser_result_status_uses_committed_terminal(runtime):
    manager, db, session, _, _, _ = runtime
    reply = await submit_client_message(manager, session.id, "One short response", [], "finished", wiki_mode=None)
    await wait_dispatch_idle(manager, session.id)
    await manager.drain_turns()
    status = await submission_status(manager, session.id, "finished")
    assert status["execution"]["state"] == "done"
    assert status["outcome"]["request_id"] == reply["request_id"]
    assert status["execution"]["recoverable_input"] is False
    assert not (await db.list_recoverable_submissions(session.id))[0]


@pytest.mark.parametrize("committed_before_error", [False, True])
async def test_lost_claim_commit_ack_never_starts_a_model_and_retains_input(tmp_path, committed_before_error):
    evidence = _crash_submission_commit(tmp_path, "claim", committed_before_error)
    db = Database(evidence["database"])
    await db.initialize()
    deps.set_user_manager(UserManager(db))
    try:
        original = await db.get_client_submission(evidence["session_id"], "confirm")
        assert original["execution_state"] == ("running" if committed_before_error else "pending")
        assert original["request_id"] == evidence["request_id"]
        assert original["payload"]["content"] == "Retain responsibility 中文\nKeep the exact input."
        manager = SessionManager()
        await manager.initialize(db)
        status = await submission_status(manager, evidence["session_id"], "confirm")
        assert status["accepted"] is True
        assert status["request_id"] == evidence["request_id"]
        assert status["execution"]["state"] == ("unknown" if committed_before_error else "withdrawn")
        assert status["execution"]["recoverable_input"] is (not committed_before_error)
        assert status["input"]["content"] == "Retain responsibility 中文\nKeep the exact input."
        duplicate = await submit_client_message(
            manager, evidence["session_id"], status["input"]["content"], [], "confirm", wiki_mode=None,
        )
        assert duplicate == status
        assert manager.dispatch_snapshot(evidence["session_id"])["items"] == []
        assert not await db.load_messages(evidence["session_id"])
    finally:
        await db.close()


async def test_recovery_attachments_are_bound_to_saved_bytes_and_owner(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from server.attachments import save_upload
    from server.config import settings
    from server.deps import get_session_manager
    from server.routers.sessions import router

    monkeypatch.setattr(settings, "users_root", str(tmp_path / "users"))
    db = Database(":memory:")
    await db.initialize()
    _, bearer = await create_account(db, extra_roots=[tmp_path])
    manager = SessionManager()
    await manager.initialize(db)
    agent = await db.get_system_agent()
    session = await manager.create_session(agent["id"], "files", str(tmp_path), backend="codex")
    file = save_upload(session.id, "source.txt", b"original", user_id=session.user_id)
    await _submit_paused(manager, session.id, "Full input", "with-file", attachment_ids=[file.id])
    await manager.drain_turns()
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_session_manager] = lambda: manager
    async with AsyncClient(transport=ASGITransport(app), base_url="http://test", headers={"Authorization": f"Bearer {bearer}"}) as client:
        try:
            endpoint = f"/api/sessions/{session.id}/submissions/with-file/attachments/{file.id}"
            response = await client.get(endpoint)
            assert response.status_code == 200 and response.content == b"original"
            file.path.write_bytes(b"modified")  # same length; size alone cannot validate this
            assert (await client.get(endpoint)).status_code == 409
            file.path.unlink()
            assert (await client.get(endpoint)).status_code == 409
            _, foreign_bearer = await create_account(db, username="another-owner")
            client.headers["Authorization"] = f"Bearer {foreign_bearer}"
            assert (await client.get(endpoint)).status_code == 404
        finally:
            await db.close()


async def test_current_format_preserves_a_receipt_without_inventing_missing_input(tmp_path):
    import sqlite3

    path = str(tmp_path / "legacy.db")
    db = Database(path)
    await db.initialize()
    await create_account(db, extra_roots=[tmp_path])
    manager = SessionManager()
    await manager.initialize(db)
    agent = await db.get_system_agent()
    session = await manager.create_session(agent["id"], "legacy", str(tmp_path), backend="codex")
    await db.close()
    with sqlite3.connect(path) as legacy:
        legacy.execute("INSERT INTO client_submissions(session_id,client_id,payload_sha256,request_id,receipt,execution_state) "
                       "VALUES(?,?,?,?,?,'unknown')", (session.id, "legacy", "digest", "legacy-request", '{"accepted":true,"disposition":"accepted"}'))
    legacy.close()
    reopened = Database(path)
    await reopened.initialize()
    deps.set_user_manager(UserManager(reopened))
    restored = SessionManager()
    await restored.initialize(reopened)
    try:
        status = await submission_status(restored, session.id, "legacy")
        assert status["accepted"] is True
        assert status["execution"]["state"] == "unknown"
        assert status["execution"]["recoverable_input"] is False
        assert "input" not in status
        assert not (await reopened.list_recoverable_submissions(session.id))[0]
    finally:
        await reopened.close()


async def test_close_waits_for_the_owned_submission_completion(runtime, monkeypatch):
    manager, db, session, _, starts, _ = runtime
    complete = db.record_terminal
    start = manager.start_message
    committed, release, notified = asyncio.Event(), asyncio.Event(), asyncio.Event()
    attempts = 0
    receipts = []

    async def capture_receipt(*args, **kwargs):
        receipt = await start(*args, **kwargs)
        receipts.append(receipt)
        return receipt

    async def acknowledge_after_release(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        result = await complete(*args, **kwargs)
        committed.set()
        await release.wait()
        return result

    async def observe(event):
        if event.get("type") == "result" and event.get("session_id") == session.id:
            notified.set()

    manager.on_broadcast("completion-close-prefix", observe)
    monkeypatch.setattr(manager, "start_message", capture_receipt)
    monkeypatch.setattr(db, "record_terminal", acknowledge_after_release)
    reply = await submit_client_message(manager, session.id, "Keep responsibility", [], "recorder", wiki_mode=None)
    await asyncio.wait_for(committed.wait(), 5)
    assert not notified.is_set()
    stored = await db.get_client_submission(session.id, "recorder")
    assert stored["execution_state"] == "done"
    assert stored["request_id"] == reply["request_id"]
    waiting = asyncio.create_task(receipts[0].wait())
    closing = asyncio.create_task(db.close())
    try:
        await asyncio.sleep(0)
        assert not closing.done()
        assert not waiting.done()
        release.set()
        completion = await asyncio.wait_for(waiting, 5)
        await asyncio.wait_for(closing, 5)
        assert completion.request_id == reply["request_id"]
        assert completion.outcome.status == "success"
        assert completion.recording_state == "committed"
        assert attempts == 1
        assert starts.qsize() == 1
    finally:
        release.set()
        await asyncio.wait_for(waiting, 5)
        await asyncio.wait_for(closing, 5)
        manager.remove_broadcast("completion-close-prefix")


async def test_restart_keeps_uncertain_steer_unknown_even_when_its_parent_has_a_result(tmp_path):
    evidence = _lose_submission_process(tmp_path, "steer")
    db = Database(evidence["database"])
    await db.initialize()
    deps.set_user_manager(UserManager(db))
    manager = SessionManager()
    await manager.initialize(db)
    try:
        status = await submission_status(manager, evidence["session_id"], "steer")
        assert status["request_id"] == evidence["request_id"]
        assert status["execution"]["state"] == "unknown"
        assert status["execution"]["reason_code"] == "steer_delivery_unknown"
        assert status["execution"]["recoverable_input"] is False
        assert len((await db.list_recoverable_submissions(evidence["session_id"]))[0]) == 1
        assert manager.dispatch_snapshot(evidence["session_id"])["items"] == []
        denied = await submit_client_message(manager, evidence["session_id"], "Another writer", [], "after-steer", wiki_mode=None)
        assert denied["accepted"] is False and "process exit is unconfirmed" in denied["message"]
    finally:
        await db.close()


def _crash_submission_commit(tmp_path, operation, committed_before_error):
    proof = tmp_path / "commit-proof.json"
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-q", str(Path(__file__).resolve()),
         "-o", "python_functions=_submission_commit_failure_child"],
        cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=15,
        env={**os.environ, "OCTOPUS_TEST_SUBMISSION_COMMIT_PROOF": str(proof),
             "OCTOPUS_TEST_SUBMISSION_COMMIT_OPERATION": operation,
             "OCTOPUS_TEST_SUBMISSION_COMMIT_MODE": str(int(committed_before_error)), "PYTHONDONTWRITEBYTECODE": "1"},
    )
    assert result.returncode == 70, result.stdout + result.stderr
    evidence = json.loads(proof.read_text())
    assert not proof.with_suffix(".returned").exists()
    return evidence


@pytest.mark.parametrize("operation", ["delivery", "completion"])
@pytest.mark.parametrize("committed_before_error", [False, True])
async def test_submission_commit_failure_stops_the_process_and_restart_reads_actual_state(tmp_path, operation, committed_before_error):
    evidence = _crash_submission_commit(tmp_path, operation, committed_before_error)
    db = Database(evidence["database"])
    await db.initialize()
    deps.set_user_manager(UserManager(db))
    try:
        # Inspect the cold committed fact before SessionManager performs restart recovery.
        row = await db.get_client_submission(evidence["session_id"], "confirm")
        assert row["client_id"] == "confirm"
        assert row["request_id"] == evidence["request_id"]
        assert row["execution_owner"] == evidence["request_id"]
        assert row["payload"]["content"] == "Retain responsibility 中文\nKeep the exact input."
        completed = operation == "completion" and committed_before_error
        uncertain_delivery = operation == "delivery" and committed_before_error
        assert row["execution_state"] == ("done" if completed else "running")
        assert row["reason_code"] == ("steer_delivery_unknown" if uncertain_delivery else None)

        restored = SessionManager()
        await restored.initialize(db)
        status = await submission_status(restored, evidence["session_id"], "confirm")
        assert status["accepted"] is True
        assert status["session_id"] == evidence["session_id"]
        assert status["client_request_id"] == "confirm"
        assert status["request_id"] == evidence["request_id"]
        assert status["input"] == {"content": "Retain responsibility 中文\nKeep the exact input.",
                                   "attachment_ids": [], "attachments": [], "wiki_mode": "auto"}
        assert status["execution"]["state"] == ("done" if completed else "unknown")
        assert status["execution"]["execution_owner_request_id"] == evidence["request_id"]
        assert status["execution"]["recoverable_input"] is False
        expected_reason = "steer_delivery_unknown" if uncertain_delivery else "service_restart_execution_unknown"
        assert status["execution"]["reason_code"] == (None if completed else expected_reason)
        duplicate = await submit_client_message(restored, evidence["session_id"], status["input"]["content"], [], "confirm", wiki_mode=None)
        assert duplicate == status
        if not completed:
            with pytest.raises(ValueError, match="process exit is unconfirmed"):
                await restored.start_message(evidence["session_id"], "Another writer")
        messages = await db.load_messages(evidence["session_id"])
        assert len(messages) == int(completed)
        if completed:
            assert status["outcome"]["request_id"] == evidence["request_id"]
            assert status["outcome"]["status"] == "success"
    finally:
        await db.close()


async def _submission_commit_failure_child(tmp_path, monkeypatch):
    """Selected only by the parent subprocess to observe real fail-stop."""
    proof = Path(os.environ["OCTOPUS_TEST_SUBMISSION_COMMIT_PROOF"])
    operation = os.environ["OCTOPUS_TEST_SUBMISSION_COMMIT_OPERATION"]
    committed_before_error = os.environ["OCTOPUS_TEST_SUBMISSION_COMMIT_MODE"] == "1"
    path = str(proof.with_suffix(".db"))
    db = Database(path)
    await db.initialize()
    await create_account(db, extra_roots=[tmp_path])
    manager = SessionManager()
    await manager.initialize(db)
    agent = await db.get_system_agent()
    session = await manager.create_session(agent["id"], "confirmation", str(tmp_path), backend="codex")
    reply = await _submit_paused(manager, session.id, "Retain responsibility 中文\nKeep the exact input.", "confirm")
    assert reply["accepted"] is True
    request = reply["request_id"]
    if operation != "claim":
        assert await db.claim_client_submission(session.id, request, request, manager._dispatch_generation)
    commit = db.conn.commit

    async def failed_commit():
        if committed_before_error:
            await commit()
        proof.write_text(json.dumps({"database": path, "session_id": session.id, "request_id": request}))
        raise OSError("submission commit outcome cannot be acknowledged")

    monkeypatch.setattr(db.conn, "commit", failed_commit)
    if operation == "claim":
        await db.claim_client_submission(session.id, request, request, manager._dispatch_generation)
    elif operation == "delivery":
        await db.set_steer_delivery(session.id, request, confirmed=False)
    else:
        await db.record_terminal(
            request_id=request, session_id=session.id, seq=0, role="system", type="result",
            content={"request_id": request, "status": "success"}, created_at="2026-10-23T00:00:00Z",
        )
    proof.with_suffix(".returned").touch()
    raise AssertionError("An unconfirmed submission commit returned to the caller")


async def test_uncertain_steer_write_is_owned_saved_and_never_requeued(runtime, tmp_path, monkeypatch):
    from server.harness.claude_code import ClaudeDriver
    from server.harness.run import HarnessRun

    manager, db, session, _, starts, _ = runtime
    ready, changed = asyncio.Event(), asyncio.Event()
    frames, finish = tmp_path / "received-frame.json", tmp_path / "finish-parent"
    code = (
        "import json,sys,time\n"
        "from pathlib import Path\n"
        "json.loads(sys.stdin.readline())\n"
        "print(json.dumps({'type':'thread.started','thread_id':'uncertain-steer'}),flush=True)\n"
        "print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':'Ready'}}),flush=True)\n"
        "frame=json.loads(sys.stdin.readline())\n"
        "Path(sys.argv[1]).write_text(json.dumps(frame))\n"
        "while not Path(sys.argv[2]).exists(): time.sleep(.01)\n"
        "print(json.dumps({'type':'turn.completed','usage':{}}),flush=True)\n"
    )

    def argv(context):
        starts.put_nowait(context)
        return [sys.executable, "-u", "-c", code, str(frames), str(finish)], {"cwd": context.working_dir}

    harness = get_harness("codex")
    monkeypatch.setattr(harness, "profile", replace(
        harness.profile, build_turn_argv=argv, new_driver=ClaudeDriver, can_steer=True, reusable=True,
        prepare_resume=None, activity_observer=None,
    ))
    attempted = []
    send = HarnessRun.send_input
    initial = None

    async def lose_frame_ack(backend, text, *, input_id=None):
        if text != "Do not send twice":
            return await send(backend, text, input_id=input_id)
        stored = await db.get_client_submission(session.id, "live-steer")
        assert stored["execution_owner"] == initial.request_id
        assert stored["reason_code"] == "steer_delivery_unknown"
        attempted.append(text)
        await send(backend, text, input_id=input_id)
        raise OSError("write acknowledgement lost")

    async def observe(event):
        if event.get("session_id") == session.id:
            if event.get("type") == "assistant_text":
                ready.set()
            if event.get("type") == "dispatch_state":
                changed.set()

    monkeypatch.setattr(HarnessRun, "send_input", lose_frame_ack)
    manager.on_broadcast("uncertain-steer-observer", observe)
    try:
        initial = await manager.start_message(session.id, "Original task")
        await asyncio.wait_for(ready.wait(), 5)
        reply = await submit_client_message(manager, session.id, "Do not send twice", [], "live-steer", wiki_mode=None)
        assert reply["accepted"] is True and reply["disposition"] == "steered"
        async with asyncio.timeout(5):
            while True:
                changed.clear()
                status = await submission_status(manager, session.id, "live-steer")
                if status["execution"]["state"] == "unknown":
                    break
                await changed.wait()
        async with asyncio.timeout(5):
            while not frames.exists():
                await asyncio.sleep(.01)
        assert json.loads(frames.read_text())["message"]["content"] == "Do not send twice"
        assert attempted == ["Do not send twice"]
        assert manager.dispatch_snapshot(session.id)["items"] == []
        finish.touch()
        assert (await asyncio.wait_for(initial.wait(), 5)).outcome.status == "success"
        status = await submission_status(manager, session.id, "live-steer")
        assert status["execution"]["state"] == "unknown"
        assert status["execution"]["reason_code"] == "steer_delivery_unknown"
        assert status["execution"]["execution_owner_request_id"] == initial.request_id
        assert status["input"]["content"] == "Do not send twice"
        duplicate = await submit_client_message(manager, session.id, "Do not send twice", [], "live-steer", wiki_mode=None)
        assert duplicate["request_id"] == reply["request_id"]
        assert duplicate["execution"]["state"] == "unknown"
        assert attempted == ["Do not send twice"]
        assert starts.qsize() == 1
        assert manager.dispatch_snapshot(session.id)["items"] == []
    finally:
        finish.touch()
        manager.remove_broadcast("uncertain-steer-observer")
