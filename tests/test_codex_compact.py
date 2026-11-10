"""Manual compaction through public session APIs and the native stdio peer."""

import asyncio
import json
import sqlite3
from contextlib import closing

import pytest

from server.routers.sessions import get_session, get_submission_status, submit_input
from server.sessions import SessionManager
from server.sessions.client_submissions import submit_client_message
from server.storage_verification import verify_main
from server.submission_models import ClientSubmissionRequest
from tests.native_app_server import ROOT, inject, until, wire_frames
from tests.native_app_server import native_runtime as native_runtime
from tests.session_api import session_client, session_socket
from tests.session_runtime import wait_dispatch_idle


@pytest.mark.parametrize("ack_order", ["first", "last", "lost"])
async def test_idle_compact_resumes_original_history_once_and_continues(native_runtime, tmp_path, ack_order):
    manager, _, session, mode, starts, _ = native_runtime
    await submit_client_message(manager, session.id, "Keep product history", [], "before", wiki_mode=None)
    await wait_dispatch_idle(manager, session.id)
    mode[0] = "compact_lost_ack" if ack_order == "lost" else "compact_ack_" + ack_order
    request = ClientSubmissionRequest.model_validate({
        "client_request_id": "compact-original", "operation": {"kind": "compact"},
    })
    accepted = await submit_input(manager, session.id, request, user_id=session.user_id)
    assert accepted["accepted"] and accepted["operation"] == "compact"
    await wait_dispatch_idle(manager, session.id)
    status = await get_submission_status(manager, session.id, "compact-original", user_id=session.user_id)
    assert status["execution"]["state"] == "done"
    assert status["outcome"]["status"] == "success"
    assert status["outcome"]["compact"]["effect"] == "applied"
    assert status["outcome"]["final_answer"] is None
    assert status["execution"]["native_inputs"][-1]["acknowledged"] == (ack_order != "lost")
    again = await submit_input(manager, session.id, request, user_id=session.user_id)
    assert again["request_id"] == accepted["request_id"]
    detail = await get_session(manager, session.id, user_id=session.user_id)
    assert detail.context_state.validity == "valid"
    assert detail.context_state.sample.used_tokens == 120
    assert [message.content for message in detail.messages if message.role.value == "user"] == ["Keep product history"]
    mode[0] = "normal"
    await submit_client_message(manager, session.id, "Continue original history", [], "after", wiki_mode=None)
    await wait_dispatch_idle(manager, session.id)
    frames = wire_frames(tmp_path, finished=True)
    compact = [frame for frame in frames if frame.get("method") == "thread/compact/start"]
    assert len(compact) == 1 and compact[0]["params"] == {"threadId": ROOT}
    assert len(starts) == 3 and all(context.resume_id == ROOT for context in starts[1:])
    assert len([frame for frame in frames if frame.get("method") == "turn/start"]) == 2
    detail = await get_session(manager, session.id, user_id=session.user_id)
    assert [message.content for message in detail.messages if message.role.value == "assistant" and message.type == "text"] == ["Native answer", "Native answer after compaction"]
    assert not any("Native summary" in str(message.content) for message in detail.messages)


async def test_ack_is_not_completion_and_second_identity_is_not_accepted(native_runtime, tmp_path):
    manager, database, session, mode, _, _ = native_runtime
    await submit_client_message(manager, session.id, "Create native history", [], "before", wiki_mode=None)
    await wait_dispatch_idle(manager, session.id)
    mode[0] = "compact_ack_only"
    request = ClientSubmissionRequest.model_validate({"client_request_id": "original", "operation": {"kind": "compact"}})
    accepted = await submit_input(manager, session.id, request, user_id=session.user_id)
    await until(lambda: (tmp_path / "native-compacts.jsonl").exists())
    async with asyncio.timeout(3):
        while True:
            status = await get_submission_status(manager, session.id, "original", user_id=session.user_id)
            if status["execution"]["native_inputs"][-1]["acknowledged"]:
                break
            await asyncio.sleep(0)
    assert status["execution"]["state"] == "running" and "outcome" not in status
    assert status["execution"]["native_inputs"][-1]["turn_id"] is None
    duplicate = await submit_input(manager, session.id, request, user_id=session.user_id)
    assert duplicate["request_id"] == accepted["request_id"] and duplicate["accepted"]
    refused = await submit_input(manager, session.id, ClientSubmissionRequest.model_validate({
        "client_request_id": "second", "operation": {"kind": "compact"},
    }), user_id=session.user_id)
    assert refused["accepted"] is False and refused["code"] == "compact_in_progress"
    assert refused["canonical_request_id"] == accepted["request_id"]
    assert refused["canonical_client_request_id"] == "original"
    assert await database.get_client_submission(session.id, "second") is None
    await manager.interrupt(session.id)
    await wait_dispatch_idle(manager, session.id)
    status = await get_submission_status(manager, session.id, "original", user_id=session.user_id)
    assert status["outcome"]["status"] == "interrupted"
    assert status["outcome"]["compact"]["effect"] == "unknown"
    assert not status["execution"]["recoverable_input"]


async def test_archive_after_compact_ack_preserves_original_unknown_effect(native_runtime, tmp_path):
    manager, _, session, mode, _, _ = native_runtime
    await submit_client_message(manager, session.id, "Create native history", [], "before", wiki_mode=None)
    await wait_dispatch_idle(manager, session.id)
    mode[0] = "compact_ack_only"
    accepted = await submit_input(manager, session.id, ClientSubmissionRequest.model_validate({
        "client_request_id": "archive-compact", "operation": {"kind": "compact"},
    }), user_id=session.user_id)
    async with asyncio.timeout(3):
        while True:
            status = await get_submission_status(manager, session.id, "archive-compact", user_id=session.user_id)
            if status["execution"]["native_inputs"] and status["execution"]["native_inputs"][-1]["acknowledged"]:
                break
            await asyncio.sleep(0)
    owner = manager._session_owners[id(session)]

    await asyncio.wait_for(manager.archive_session(session.id, replace=False), 5)
    assert not owner.failed
    result = await get_submission_status(manager, session.id, "archive-compact", user_id=session.user_id)
    assert result["request_id"] == accepted["request_id"]
    assert result["outcome"]["status"] == "interrupted"
    assert result["outcome"]["compact"]["effect"] == "unknown"
    state = (await get_session(manager, session.id, user_id=session.user_id)).context_state
    assert state.validity == "stale" and state.invalidation.reason == "operation_unconfirmed"


@pytest.mark.parametrize("failure", ["no_effect", "native_failed", "disconnect", "conflicting_root", "error_after_root"])
async def test_native_compact_failure_never_replays_or_claims_noop(native_runtime, tmp_path, failure):
    manager, _, session, mode, starts, _ = native_runtime
    await submit_client_message(manager, session.id, "Create native history", [], "before", wiki_mode=None)
    await wait_dispatch_idle(manager, session.id)
    mode[0] = "compact_" + failure
    await submit_input(manager, session.id, ClientSubmissionRequest.model_validate({
        "client_request_id": "original", "operation": {"kind": "compact"},
    }), user_id=session.user_id)
    await wait_dispatch_idle(manager, session.id)
    status = await get_submission_status(manager, session.id, "original", user_id=session.user_id)
    assert status["outcome"]["status"] == "failed"
    assert status["outcome"]["compact"]["effect"] == "unknown"
    assert status["execution"]["state"] in {"done", "unknown"}
    assert len(starts) == 2 and len((tmp_path / "native-compacts.jsonl").read_text().splitlines()) == 1
    assert not any(status["outcome"]["recovery"].values())
    state = (await get_session(manager, session.id, user_id=session.user_id)).context_state
    assert state.validity == "stale"


async def test_http_and_ws_share_typed_durable_compact_without_alias_or_recovery(native_runtime, tmp_path):
    manager, database, session, mode, starts, _ = native_runtime
    async with session_client(manager, session) as client:
        endpoint = f"/api/sessions/{session.id}/submissions"
        unavailable = await client.post(endpoint, json={"client_request_id": "empty", "operation": {"kind": "compact"}})
        assert unavailable.json()["code"] == "compact_unavailable" and not unavailable.json()["accepted"]
        assert not starts
        await client.post(endpoint, json={"client_request_id": "before", "content": "Keep this input"})
        await wait_dispatch_idle(manager, session.id)
        mode[0] = "compact_ack_last"
        async with session_socket(manager, session) as submit:
            accepted = await submit({"session_id": session.id, "client_request_id": "compact-ws", "operation": {"kind": "compact"}})
            assert accepted["accepted"] and accepted["operation"] == "compact"
        await wait_dispatch_idle(manager, session.id)
        status = (await client.get(endpoint + "/compact-ws")).json()
        assert status["outcome"]["compact"]["effect"] == "applied" and status["input"] == {"kind": "compact"}
        assert "final_answer" not in status["outcome"] or status["outcome"]["final_answer"] is None
        conflict = (await client.post(endpoint, json={"client_request_id": "compact-ws", "content": "/compact"})).json()
        assert conflict["code"] == "submission_conflict" and not conflict["accepted"]
        mixed = await client.post(endpoint, json={"client_request_id": "mixed", "operation": {"kind": "compact"}, "attachment_ids": ["unwanted"]})
        assert mixed.status_code == 422
        recovery = (await client.post(endpoint, json={"client_request_id": "recover-compact", "content": "Continue",
            "recovery_of_client_id": "compact-ws"})).json()
        assert recovery["code"] == "recovery_unavailable" and not recovery["accepted"]
    with sqlite3.connect(tmp_path / "product.db") as reader:
        assert verify_main(reader) == 9


@pytest.mark.parametrize("point", ["admission", "claim", "possible_send"])
async def test_compact_cannot_execute_before_durable_commit(native_runtime, tmp_path, monkeypatch, point):
    manager, database, session, mode, _, _ = native_runtime
    await submit_client_message(manager, session.id, "Create native history", [], "before", wiki_mode=None)
    await wait_dispatch_idle(manager, session.id)
    mode[0] = "compact_ack_first"
    entered, release = asyncio.Event(), asyncio.Event()
    original = getattr(database, {"admission": "admit_client_submission", "claim": "claim_client_submission", "possible_send": "record_native_input"}[point])

    async def blocked(*args, **kwargs):
        should_block = point != "possible_send" or args[2].state == "unknown"
        if should_block:
            entered.set()
            await release.wait()
        return await original(*args, **kwargs)

    monkeypatch.setattr(database, {"admission": "admit_client_submission", "claim": "claim_client_submission", "possible_send": "record_native_input"}[point], blocked)
    submitting = asyncio.create_task(submit_input(manager, session.id, ClientSubmissionRequest.model_validate({
        "client_request_id": "commit-gated", "operation": {"kind": "compact"},
    }), user_id=session.user_id))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        assert not (tmp_path / "native-compacts.jsonl").exists()
    finally:
        release.set()
    assert (await submitting)["accepted"]
    await wait_dispatch_idle(manager, session.id)
    assert len((tmp_path / "native-compacts.jsonl").read_text().splitlines()) == 1


async def test_stop_of_dequeued_unstarted_compact_withdraws_without_resurrection(native_runtime, tmp_path):
    manager, _, session, _, starts, _ = native_runtime
    await submit_client_message(manager, session.id, "Create native history", [], "before", wiki_mode=None)
    await wait_dispatch_idle(manager, session.id)
    stopped = asyncio.Event()

    async def stop_unstarted(event):
        if event.get("type") == "dequeued" and event.get("session_id") == session.id:
            await manager.interrupt(session.id)
            stopped.set()

    manager.on_broadcast("stop-unstarted-compact", stop_unstarted)
    try:
        accepted = await manager.start_compact(session.id, client_request_id="unstarted")
        await asyncio.wait_for(stopped.wait(), 3)
        await wait_dispatch_idle(manager, session.id)
        assert (await accepted.wait()).state == "withdrawn"
        status = await get_submission_status(manager, session.id, "unstarted", user_id=session.user_id)
        assert status["execution"]["state"] == "withdrawn" and not status["execution"]["recoverable_input"]
        await manager.resume_dispatch(session.id)
        await wait_dispatch_idle(manager, session.id)
        assert len(starts) == 1 and not (tmp_path / "native-compacts.jsonl").exists()
    finally:
        manager.remove_broadcast("stop-unstarted-compact")


@pytest.mark.parametrize("phase", ["pending", "executing", "done"])
async def test_restart_keeps_original_compact_fact_without_replay(native_runtime, tmp_path, phase):
    manager, database, session, mode, _, _ = native_runtime
    await submit_client_message(manager, session.id, "Create native history", [], "before", wiki_mode=None)
    await wait_dispatch_idle(manager, session.id)
    mode[0] = "compact_ack_only" if phase == "executing" else "compact_ack_first"
    captured = asyncio.Event()
    copy = tmp_path / "restart.db"

    async def backup_pending(event):
        if event.get("type") == "dequeued" and event.get("session_id") == session.id:
            with closing(sqlite3.connect(tmp_path / "product.db")) as source, closing(sqlite3.connect(copy)) as target:
                source.backup(target)
            captured.set()

    if phase == "pending":
        manager.on_broadcast("snapshot-unclaimed-compact", backup_pending)
    await manager.start_compact(session.id, client_request_id="original")
    if phase == "pending":
        await asyncio.wait_for(captured.wait(), 2)
        manager.remove_broadcast("snapshot-unclaimed-compact")
    elif phase == "executing":
        await until(lambda: (tmp_path / "native-compacts.jsonl").exists())
        # The native execution is still open; preserve its actual crash state.
        with closing(sqlite3.connect(tmp_path / "product.db")) as source, closing(sqlite3.connect(copy)) as target:
            source.backup(target)
    else:
        await wait_dispatch_idle(manager, session.id)
        with closing(sqlite3.connect(tmp_path / "product.db")) as source, closing(sqlite3.connect(copy)) as target:
            source.backup(target)
    await manager.interrupt(session.id)
    await wait_dispatch_idle(manager, session.id)
    from server.database import Database
    restored_db = Database(str(copy))
    await restored_db.initialize()
    restored = SessionManager()
    try:
        await restored.initialize(restored_db, previous_writers_retired=True)
        status = await get_submission_status(restored, session.id, "original", user_id=session.user_id)
        assert status["execution"]["state"] == {"pending": "withdrawn", "executing": "unknown", "done": "done"}[phase]
        assert not status["execution"]["recoverable_input"]
        assert restored.dispatch_snapshot(session.id)["items"] == []
        with sqlite3.connect(copy) as reader:
            assert verify_main(reader) == 9
    finally:
        await restored.close_session_owners()
        await restored_db.close()


@pytest.mark.parametrize("failure", ["missing_history", "possible_send_store"])
async def test_write_before_failure_keeps_old_sample_and_has_no_native_effect(native_runtime, tmp_path, monkeypatch, failure):
    manager, database, session, mode, starts, _ = native_runtime
    await submit_client_message(manager, session.id, "Create native history", [], "before", wiki_mode=None)
    await wait_dispatch_idle(manager, session.id)
    mode[0] = "compact_ack_first"
    if failure == "missing_history":
        (tmp_path / "native-history.jsonl").unlink()
    else:
        record = database.record_native_input

        async def fail_before_send(session_id, request_id, evidence, **kwargs):
            if evidence.kind == "compact" and evidence.state == "unknown":
                raise OSError("Cannot commit possible-send")
            await record(session_id, request_id, evidence, **kwargs)

        monkeypatch.setattr(database, "record_native_input", fail_before_send)
    await manager.start_compact(session.id, client_request_id="prewrite")
    await wait_dispatch_idle(manager, session.id)
    status = await get_submission_status(manager, session.id, "prewrite", user_id=session.user_id)
    assert status["execution"]["state"] == "withdrawn"
    assert status["outcome"]["status"] == "failed" and status["outcome"]["compact"]["effect"] == "not_applied"
    assert not status["execution"]["recoverable_input"]
    assert len(starts) == 2 and not (tmp_path / "native-compacts.jsonl").exists()


@pytest.mark.parametrize("finish", ["success", "failure", "stop_tail"])
async def test_effect_survives_failure_or_stop_and_app_only_observes_business_work(native_runtime, tmp_path, finish):
    from server.applications import ApplicationManager

    manager, database, session, mode, _, events = native_runtime
    await submit_client_message(manager, session.id, "Create native history", [], "before", wiki_mode=None)
    await wait_dispatch_idle(manager, session.id)
    await database.save_application(app_id="original-app", name="App", app_dir=str(tmp_path), entrypoint="index.html",
        session_id=session.id, agent_id=session.agent_id, user_id=session.user_id, status="ready", created_at="now", updated_at="now")
    apps = ApplicationManager()
    apps.bind(manager, database)
    original_app = await database.get_application("original-app")
    mode[0] = "compact_stop_tail" if finish == "stop_tail" else "compact_control"
    receipt = await manager.start_compact(session.id, client_request_id="compact-original")
    await until(lambda: any(event["type"] == "context_state" and event.get("context_state", {}).get("activity") == "compacting" for event in events))
    assert (await database.get_application("original-app"))["status"] == "ready"
    boundary = {"method": "item/completed", "params": {"threadId": ROOT, "turnId": "compact-turn",
        "item": {"id": "compact-item", "type": "contextCompaction"}}}
    terminal = {"method": "turn/completed", "params": {"threadId": ROOT,
        "turn": {"id": "compact-turn", "status": "failed" if finish == "failure" else "completed"}}}
    if finish == "failure":
        terminal["params"]["turn"]["error"] = {"codexErrorInfo": "serverOverloaded", "message": "Native failed after installing history"}
    if finish == "stop_tail":
        (tmp_path / "native-stop-tail.json").write_text(json.dumps([boundary, terminal]))
        await manager.interrupt(session.id)
    else:
        inject(tmp_path, boundary, terminal)
    try:
        completion = await asyncio.wait_for(receipt.wait(), 4)
        assert completion.operation == "compact" and completion.recording_state == "committed"
        assert completion.outcome.status == {"success": "success", "failure": "failed", "stop_tail": "interrupted"}[finish]
        assert completion.outcome.compact.effect == "applied" and completion.outcome.final_answer is None
        assert completion.outcome.compact.item_ids == ["compact-item"]
        assert completion.outcome.recovery == {"transient": 0, "idle": 0, "stale_session": 0, "premature_exit": 0}
        assert (await database.read_session_result(session.id, receipt.request_id)).reply_text() == ""
        await wait_dispatch_idle(manager, session.id)
        assert await database.get_application("original-app") == original_app
    finally:
        apps.shutdown()


async def test_storage_verifier_rejects_withdrawn_compact_that_already_executed(native_runtime, tmp_path):
    manager, _, session, mode, _, _ = native_runtime
    await submit_client_message(manager, session.id, "Create native history", [], "before", wiki_mode=None)
    await wait_dispatch_idle(manager, session.id)
    mode[0] = "compact_ack_first"
    await manager.start_compact(session.id, client_request_id="original")
    await wait_dispatch_idle(manager, session.id)
    with sqlite3.connect(tmp_path / "product.db") as reader:
        reader.execute("UPDATE client_submissions SET execution_state='withdrawn' WHERE client_id='original'")
        reader.commit()
        with pytest.raises(RuntimeError, match="native input evidence"):
            verify_main(reader)


async def test_ordinary_input_during_compact_queues_instead_of_steering(native_runtime, tmp_path):
    manager, _, session, mode, _, events = native_runtime
    await submit_client_message(manager, session.id, "Create native history", [], "before", wiki_mode=None)
    await wait_dispatch_idle(manager, session.id)
    mode[0] = "compact_control"
    receipt = await manager.start_compact(session.id, client_request_id="compact-original")
    await until(lambda: any(event["type"] == "context_state" and event.get("context_state", {}).get("activity") == "compacting" for event in events))
    queued = await submit_client_message(manager, session.id, "Continue after compact", [], "after", wiki_mode=None)
    assert queued["accepted"] and queued["disposition"] != "steered"
    assert manager.dispatch_snapshot(session.id)["items"][0]["request_id"] == queued["request_id"]
    mode[0] = "normal"
    inject(tmp_path, {"method": "item/completed", "params": {"threadId": ROOT, "turnId": "compact-turn",
        "item": {"id": "compact-item", "type": "contextCompaction"}}},
        {"method": "turn/completed", "params": {"threadId": ROOT, "turn": {"id": "compact-turn", "status": "completed"}}})
    assert (await asyncio.wait_for(receipt.wait(), 3)).outcome.status == "success"
    await wait_dispatch_idle(manager, session.id)
    assert not (tmp_path / "native-steers.jsonl").exists()
    status = await get_submission_status(manager, session.id, "after", user_id=session.user_id)
    assert status["outcome"]["status"] == "success"


async def test_compact_terminal_record_failure_stays_in_original_owner_failure_boundary(native_runtime, monkeypatch):
    manager, database, session, mode, _, _ = native_runtime
    await submit_client_message(manager, session.id, "Create native history", [], "before", wiki_mode=None)
    await wait_dispatch_idle(manager, session.id)
    original = database.record_terminal

    async def fail_compact(**kwargs):
        if kwargs["content"]["operation"] == "compact":
            raise OSError("Compact terminal could not commit")
        return await original(**kwargs)

    monkeypatch.setattr(database, "record_terminal", fail_compact)
    mode[0] = "compact_ack_first"
    receipt = await manager.start_compact(session.id, client_request_id="compact-recording")
    completion = await asyncio.wait_for(receipt.wait(), 4)
    assert completion.operation == "compact" and completion.recording_state != "committed"
    assert manager.session_owner_failed(session.id)
    assert not (await get_submission_status(manager, session.id, "compact-recording", user_id=session.user_id)).get("outcome")
    monkeypatch.setattr(database, "record_terminal", original)
    assert await manager.delete_session(session.id)


async def test_compact_authorization_failure_before_business_write_proves_no_effect(native_runtime, tmp_path, monkeypatch):
    from server.harness import HarnessRun

    manager, database, session, mode, _, _ = native_runtime
    await submit_client_message(manager, session.id, "Create native history", [], "before", wiki_mode=None)
    await wait_dispatch_idle(manager, session.id)
    mode[0] = "compact_ack_first"
    authorize = HarnessRun.authorize_input

    async def revoke_at_write(run):
        if getattr(run, "operation", None) == "compact":
            row = await database.get_client_submission(session.id, "prewrite-revoked")
            if row["native_inputs"][-1]["state"] == "unknown":
                raise ValueError("Original write authorization was revoked")
        await authorize(run)

    monkeypatch.setattr(HarnessRun, "authorize_input", revoke_at_write)
    await manager.start_compact(session.id, client_request_id="prewrite-revoked")
    await wait_dispatch_idle(manager, session.id)
    status = await get_submission_status(manager, session.id, "prewrite-revoked", user_id=session.user_id)
    assert status["outcome"]["compact"]["effect"] == "not_applied"
    assert status["execution"]["state"] == "withdrawn"
    assert not (tmp_path / "native-compacts.jsonl").exists()


async def test_lost_ack_cannot_interrupt_a_proven_root_after_the_rpc_timeout(native_runtime, tmp_path):
    manager, _, session, mode, _, _ = native_runtime
    await submit_client_message(manager, session.id, "Create native history", [], "before", wiki_mode=None)
    await wait_dispatch_idle(manager, session.id)
    mode[0] = "compact_root_only"
    receipt = await manager.start_compact(session.id, client_request_id="slow-compact")
    async with asyncio.timeout(3):
        while True:
            status = await get_submission_status(manager, session.id, "slow-compact", user_id=session.user_id)
            if status["execution"]["native_inputs"] and status["execution"]["native_inputs"][-1]["turn_id"]:
                break
            await asyncio.sleep(0)
    # The fixture's real RPC deadline is 0.5 s. A native root, already proven,
    # keeps running under the existing turn deadlines even when its ACK is lost.
    await asyncio.sleep(0.7)
    status = await get_submission_status(manager, session.id, "slow-compact", user_id=session.user_id)
    assert status["execution"]["state"] == "running" and "outcome" not in status
    inject(tmp_path, {"method": "item/completed", "params": {"threadId": ROOT, "turnId": "compact-turn",
        "item": {"id": "compact-item", "type": "contextCompaction"}}},
        {"method": "turn/completed", "params": {"threadId": ROOT, "turn": {"id": "compact-turn", "status": "completed"}}})
    assert (await asyncio.wait_for(receipt.wait(), 3)).outcome.status == "success"
    status = await get_submission_status(manager, session.id, "slow-compact", user_id=session.user_id)
    assert not status["execution"]["native_inputs"][-1]["acknowledged"]


@pytest.mark.parametrize("deadline", ["idle", "overall"])
@pytest.mark.parametrize("native_status", ["completed", "interrupted"])
async def test_late_compact_terminal_keeps_expired_budget_and_applied_effect(native_runtime, tmp_path, monkeypatch, deadline, native_status):
    from server.config import settings

    manager, database, session, mode, _, events = native_runtime
    await submit_client_message(manager, session.id, "Create native history", [], "before", wiki_mode=None)
    await wait_dispatch_idle(manager, session.id)
    mode[0] = "compact_control"
    receipt = await manager.start_compact(session.id, client_request_id="late-compact")
    await until(lambda: any(event["type"] == "context_state" and event.get("context_state", {}).get("activity") == "compacting" for event in events))
    monkeypatch.setattr(settings, "turn_idle_timeout_seconds", .3 if deadline == "idle" else 0)
    monkeypatch.setattr(settings, "turn_max_seconds", .3 if deadline == "overall" else 0)
    monkeypatch.setattr(settings, "turn_idle_max_retries", 2)
    entered, release = asyncio.Event(), asyncio.Event()
    update = database.update_session_field

    async def hold_boundary(session_id, **fields):
        state = json.loads(fields["context_state"]) if fields.get("context_state") else {}
        if (state.get("invalidation") or {}).get("reason") == "compacted":
            entered.set()
            await release.wait()
        return await update(session_id, **fields)

    monkeypatch.setattr(database, "update_session_field", hold_boundary)
    try:
        inject(tmp_path, {"method": "item/completed", "params": {"threadId": ROOT, "turnId": "compact-turn",
            "item": {"id": "compact-item", "type": "contextCompaction"}}})
        await asyncio.wait_for(entered.wait(), 3)
        await asyncio.sleep(.35)
        inject(tmp_path, {"method": "turn/completed", "params": {"threadId": ROOT,
            "turn": {"id": "compact-turn", "status": native_status}}})
        run = session._run.backend
        await until(lambda: not run.question_admission_open)
    finally:
        release.set()
    completion = await asyncio.wait_for(receipt.wait(), 4)
    assert completion.outcome.status == "failed" and completion.outcome.code == "turn_timeout"
    assert completion.outcome.compact.effect == "applied"
    assert not any(completion.outcome.recovery.values())
    assert len((tmp_path / "native-compacts.jsonl").read_text().splitlines()) == 1
