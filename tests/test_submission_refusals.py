"""Expected refusal codes are independent of exception prose."""

import asyncio
import hashlib
import json
from pathlib import Path

import pytest

from server import deps
from server.attachments import save_upload
from server.service_health import ServiceHealth, ServiceUnavailable
from server.sessions.client_submissions import submission_status, submit_client_message
from server.sessions.owner_runtime import OwnerRuntime
from server.sessions.runtime_types import QuotaExceeded
from server.submission_models import InputRecoveryUnavailable, SubmissionConflict
from tests.accounts import create_account
from tests.session_api import session_client, session_socket
from tests.session_runtime import wait_dispatch_idle
from tests.test_submission_recovery import runtime as runtime
from tests.websocket_client import connection, receipt_frame


@pytest.mark.parametrize("subagents", [
    r'[{"name":"helper","extension":[{"value":"\ud800"}]}]',
    r'[{"name":"helper","extension":{"\ud800":true}}]',
    '[{"name":"helper","extension":[1e400]}]',
])
async def test_corrupt_subagent_extension_is_refused_before_admitting_a_request(runtime, subagents):
    manager, db, session, _, starts, _ = runtime
    async with db.transaction(write=True) as tx:
        await tx.execute("UPDATE agents SET subagents = ? WHERE id = ?", (subagents, session.agent_id))

    async with session_client(manager, session) as http:
        response = await http.post(f"/api/sessions/{session.id}/submissions", json={
            "client_request_id": "bad-subagent-extension", "content": "Refuse before input is admitted",
        })
    assert response.status_code == 200
    refusal = response.json()
    assert not refusal["accepted"]
    assert session.agent_id in refusal["message"] and "subagents" in refusal["message"]
    assert await submission_status(manager, session.id, "bad-subagent-extension") is None
    assert starts.empty()


@pytest.mark.parametrize(("fields", "status", "ws_code"), [
    ({"content": "Valid body", "extra": "Unknown input"}, 422, "admission_refused"),
    ({"content": " \t\n"}, 422, "admission_refused"),
    ({"content": "Valid body", "wiki_mode": "invalid"}, 422, "admission_refused"),
    ({"content": "Valid body", "attachment_ids": "invalid"}, 422, "attachment_unavailable"),
    ({"content": "Valid body", "attachment_ids": [4]}, 422, "attachment_unavailable"),
    ({"content": "Valid body", "attachment_ids": ["000000000000"]}, 200, "attachment_unavailable"),
    ({"content": "Valid body", "recovery_of_client_id": ""}, 422, "invalid_recovery_identity"),
])
async def test_invalid_messages_are_refused_by_all_submission_transports(runtime, fields, status, ws_code):
    manager, db, session, _, starts, _ = runtime
    endpoint = f"/api/sessions/{session.id}/submissions"
    typed_fields = {"attachment_ids": [], **fields}
    recovery = {"recovery_of_client_id": typed_fields.pop("recovery_of_client_id")} if "recovery_of_client_id" in fields else {}
    shapes = (fields, {"operation": {"kind": "message", **typed_fields}, **recovery})

    async with session_client(manager, session) as http, session_socket(manager, session) as send:
        for index, shape in enumerate(shapes):
            client_id = f"invalid-input-{index}"
            payload = {"client_request_id": client_id, **shape}
            response = await http.post(endpoint, json=payload)
            assert response.status_code == status
            if status == 200:
                assert response.json()["accepted"] is False and response.json()["code"] == ws_code
            reply = await send({"session_id": session.id, **payload})
            assert reply["accepted"] is False and reply["code"] == ws_code
            assert reply["client_request_id"] == client_id and reply["session_id"] == session.id
            assert await db.get_client_submission(session.id, client_id) is None
    assert starts.empty()


@pytest.mark.parametrize(("fields", "ws_code"), [
    (r'{"content":"\ud800"}', "admission_refused"),
    (r'{"operation":{"kind":"message","content":"\ud800","attachment_ids":[]}}', "admission_refused"),
    (r'{"attachment_ids":["\ud800"]}', "attachment_unavailable"),
    (r'{"operation":{"kind":"message","content":"","attachment_ids":["\ud800"]}}', "attachment_unavailable"),
    (r'{"content":"restore","recovery_of_client_id":"\ud800"}', "invalid_recovery_identity"),
    (r'{"content":"query","wiki_mode":"\ud800"}', "admission_refused"),
    pytest.param(r'{"content":"query","extra":1e400}', "admission_refused", id="non_finite_extra"),
    pytest.param(r'{"content":"\ud800","extra":1e400}', "admission_refused", id="non_finite_extra_and_surrogate"),
    (r'{"content":"query","\ud800":42}', "admission_refused"),
    (r'{"operation":{"kind":"\ud800"}}', "admission_refused"),
])
async def test_invalid_submission_json_has_serializable_refusals_and_keeps_socket_open(runtime, fields, ws_code):
    manager, db, session, _, starts, _ = runtime
    endpoint = f"/api/sessions/{session.id}/submissions"
    payload = '{"client_request_id":"invalid-json-input",' + fields[1:]
    envelope = json.dumps({"type": "send_message", "session_id": session.id})
    bearer = await deps.get_user_manager().issue_token(session.user_id)
    async with session_client(manager, session) as http, connection(manager, bearer) as socket:
        response = await http.post(endpoint, content=payload, headers={"Content-Type": "application/json"})
        assert response.status_code == 422 and response.json()["detail"]
        for error in response.json()["detail"]:
            assert set(error) == {"loc", "msg", "type"}
            json.dumps(error, ensure_ascii=False, allow_nan=False).encode()
        await socket[0].put({"type": "websocket.receive", "text": envelope[:-1] + "," + payload[1:]})
        reply = await receipt_frame(socket[1], "submission")
        assert reply["accepted"] is False and reply["code"] == ws_code
        assert reply["client_request_id"] == "invalid-json-input" and reply["session_id"] == session.id
        assert await db.get_client_submission(session.id, "invalid-json-input") is None
        assert starts.empty()

        await socket[0].put({"type": "websocket.receive", "text": json.dumps({
            "type": "send_message", "session_id": session.id, "client_request_id": "valid-unicode", "content": "Valid 🙌 中文",
        })})
        followup = await receipt_frame(socket[1], "submission")
        assert followup["accepted"] is True and followup["input"]["content"] == "Valid 🙌 中文"
        await wait_dispatch_idle(manager, session.id)


async def test_submission_does_not_echo_unencodable_socket_identities(runtime):
    manager, db, session, _, starts, _ = runtime
    bearer = await deps.get_user_manager().issue_token(session.user_id)
    async with connection(manager, bearer) as socket:
        async def send(**identity):
            await socket[0].put({"type": "websocket.receive", "text": json.dumps({
                "type": "send_message", "content": "Original text", **identity,
            })})

        await send(session_id=session.id, client_request_id="\ud800")
        error = await receipt_frame(socket[1], "error")
        assert error["message"] == "Invalid client request identity" and "client_request_id" not in error
        await send(session_id="\ud800", client_request_id="valid-identity")
        refusal = await receipt_frame(socket[1], "submission")
        assert refusal["accepted"] is False and refusal["client_request_id"] == "valid-identity"
        assert refusal["session_id"] is None
        assert await db.get_client_submission(session.id, "valid-identity") is None
        assert starts.empty()

        await send(session_id=session.id, client_request_id="valid-identity")
        assert (await receipt_frame(socket[1], "submission"))["accepted"] is True
        await wait_dispatch_idle(manager, session.id)


@pytest.mark.parametrize(("operation", "flat_fields"), [
    ({"kind": "message", "content": "Typed", "attachment_ids": []}, {"content": "Flat"}),
    ({"kind": "compact"}, {"attachment_ids": []}),
    ({"kind": "compact", "attachment_ids": [4]}, {}),
])
async def test_typed_submission_rejects_fields_outside_its_shape(runtime, operation, flat_fields):
    manager, db, session, _, starts, _ = runtime
    endpoint = f"/api/sessions/{session.id}/submissions"
    payload = {"client_request_id": "mixed-input", "operation": operation, **flat_fields}
    async with session_client(manager, session) as http, session_socket(manager, session) as send:
        assert (await http.post(endpoint, json=payload)).status_code == 422
        reply = await send({"session_id": session.id, **payload})

    assert reply["accepted"] is False and reply["code"] == "admission_refused"
    assert reply["client_request_id"] == "mixed-input"
    assert await db.get_client_submission(session.id, "mixed-input") is None
    assert starts.empty()


async def test_submission_command_field_does_not_consume_command_identity(runtime):
    manager, db, session, _, starts, _ = runtime
    bearer = await deps.get_user_manager().issue_token(session.user_id)
    command_fields = {"session_id": session.id, "client_command_id": "reused-control-id"}
    async with connection(manager, bearer) as socket:
        await socket[0].put({"type": "websocket.receive", "text": json.dumps({
            "type": "send_message", "client_request_id": "misplaced-control-id", "content": "Valid body", **command_fields,
        })})
        refusal = await receipt_frame(socket[1], "submission")
        assert refusal["accepted"] is False and refusal["client_request_id"] == "misplaced-control-id"
        snapshot = manager.dispatch_snapshot(session.id)
        await socket[0].put({"type": "websocket.receive", "text": json.dumps({
            "type": "interrupt", "request_id": None, "attempt_id": None, "generation": snapshot["generation"],
            "revision": snapshot["revision"], **command_fields,
        })})
        control = await receipt_frame(socket[1], "command_receipt")
        assert control["accepted"] is True
        assert control["client_command_id"] == "reused-control-id"

    assert await db.get_client_submission(session.id, "misplaced-control-id") is None
    assert starts.empty()


async def test_submission_transports_refuse_foreign_and_missing_sessions_identically(runtime, tmp_path):
    manager, db, session, _, starts, _ = runtime
    account, _ = await create_account(db, username="other-submitter", extra_roots=[tmp_path])
    agent = await db.get_system_agent(account["id"])
    foreign = await manager.create_session(agent["id"], "Another account", str(tmp_path), backend="codex")

    async with session_client(manager, session) as http, session_socket(manager, session) as send:
        for session_id in (foreign.id, "missing-session"):
            for fields in ({"content": "Unauthorized"}, {"operation": {
                "kind": "message", "content": "Unauthorized", "attachment_ids": [],
            }}):
                payload = {"client_request_id": "unauthorized-input", **fields}
                response = await http.post(f"/api/sessions/{session_id}/submissions", json=payload)
                assert response.status_code == 404 and response.json() == {"detail": "Session not found"}
                assert await send({"session_id": session_id, **payload}) == {
                    "type": "submission", "session_id": session_id, "client_request_id": "unauthorized-input",
                    "accepted": False, "message": "Session not found",
                }
    assert await db.get_client_submission(foreign.id, "unauthorized-input") is None
    assert await db.get_client_submission(session.id, "unauthorized-input") is None
    assert starts.empty()


@pytest.mark.parametrize("first_transport", ["http", "flat", "typed"])
async def test_transports_accept_same_message_text_attachments_and_wiki_control(runtime, first_transport):
    manager, db, session, _, starts, _ = runtime
    first = save_upload(session.id, "one.txt", b"one", user_id=session.user_id)
    second = save_upload(session.id, "two.txt", b"two", user_id=session.user_id)
    attachment_ids = [second.id, first.id]
    content = " \n/wiki maintain  保留原文\t "
    fields = {"content": content, "attachment_ids": attachment_ids, "wiki_mode": "auto"}
    endpoint = f"/api/sessions/{session.id}/submissions"

    async with session_client(manager, session) as http, session_socket(manager, session) as send:
        async def submit(transport):
            body = {"operation": {"kind": "message", **fields}} if transport == "typed" else fields
            payload = {"client_request_id": "same-input", **body}
            if transport == "http":
                response = await http.post(endpoint, json=payload)
                assert response.status_code == 200
                return response.json()
            return await send({"session_id": session.id, **payload})

        original = await submit(first_transport)
        assert original["accepted"] is True and original["client_request_id"] == "same-input"
        for transport in {"http", "flat", "typed"} - {first_transport}:
            duplicate = await submit(transport)
            assert duplicate["accepted"] is True and duplicate["request_id"] == original["request_id"]
        await wait_dispatch_idle(manager, session.id)
        status = (await http.get(f"{endpoint}/same-input")).json()
        assert status["request_id"] == original["request_id"]
        assert status["execution"]["state"] == "done" and status["outcome"]["status"] == "success"
        assert status["input"]["content"] == content
        assert status["input"]["attachment_ids"] == attachment_ids
        assert [item["id"] for item in status["input"]["attachments"]] == attachment_ids
        assert status["input"]["wiki_mode"] == "maintain"
        reordered = await send({"session_id": session.id, "client_request_id": "same-input", **fields,
                                "attachment_ids": list(reversed(attachment_ids))})
        assert reordered["accepted"] is False and reordered["code"] == "submission_conflict"

    row = await db.get_client_submission(session.id, "same-input")
    assert row["request_id"] == original["request_id"]
    assert row["payload"]["operation"] == {"kind": "message", **fields, "wiki_mode": "maintain"}
    canonical = f'{{"attachment_ids":["{second.id}","{first.id}"],"content":{json.dumps(content, ensure_ascii=False)},"wiki_mode":"maintain"}}'
    assert row["payload_sha256"] == hashlib.sha256(canonical.encode()).hexdigest()
    assert starts.qsize() == 1


@pytest.mark.parametrize("shape", [{}, {"operation": None}], ids=["omitted-operation", "null-operation"])
async def test_flat_websocket_attachment_only_message_may_omit_content(runtime, shape):
    manager, db, session, _, starts, _ = runtime
    attachment = save_upload(session.id, "only.txt", b"document", user_id=session.user_id)
    endpoint = f"/api/sessions/{session.id}/submissions"
    payload = {"client_request_id": "file-only", "attachment_ids": [attachment.id], **shape}
    async with session_client(manager, session) as http, session_socket(manager, session) as send:
        receipt = await send({"session_id": session.id, **payload})
        assert receipt["accepted"] is True and receipt["input"]["content"] == ""
        duplicate = (await http.post(endpoint, json=payload)).json()
        assert duplicate["accepted"] is True and duplicate["request_id"] == receipt["request_id"]
        await wait_dispatch_idle(manager, session.id)

    row = await db.get_client_submission(session.id, "file-only")
    assert row["request_id"] == receipt["request_id"]
    assert row["payload"]["operation"]["content"] == ""
    assert row["payload"]["operation"]["attachment_ids"] == [attachment.id]
    assert starts.qsize() == 1


async def test_attachment_unreadable_at_hash_is_refused_before_admission(runtime, monkeypatch):
    manager, db, session, _, starts, _ = runtime
    attachment = save_upload(session.id, "unreadable.txt", b"document", user_id=session.user_id)
    endpoint = f"/api/sessions/{session.id}/submissions"
    open_file = Path.open

    def deny_attachment_open(path, *args, **kwargs):
        if path == attachment.path:
            raise PermissionError("The attachment cannot be read")
        return open_file(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", deny_attachment_open)
    payload = {"client_request_id": "unreadable-file", "attachment_ids": [attachment.id]}
    async with session_client(manager, session) as http, session_socket(manager, session) as send:
        http_response = await http.post(endpoint, json=payload)
        assert http_response.status_code == 200
        flat = await send({"session_id": session.id, **payload})
        typed = await send({"session_id": session.id, "client_request_id": "unreadable-file", "operation": {
            "kind": "message", "content": "", "attachment_ids": [attachment.id],
        }})
        for refusal in (http_response.json(), flat, typed):
            assert refusal["accepted"] is False and refusal["code"] == "attachment_unavailable"
            assert "state" not in refusal and refusal["client_request_id"] == "unreadable-file"
    assert await db.get_client_submission(session.id, "unreadable-file") is None
    assert starts.empty()


@pytest.mark.parametrize(("first_transport", "fault"), [
    ("http", "decode"), ("http", "projection"), ("typed", "projection"),
])
async def test_committed_submission_with_unreadable_receipt_is_confirmed_by_original_identity(
    runtime, monkeypatch, first_transport, fault,
):
    manager, db, session, _, starts, _ = runtime
    file = save_upload(session.id, "keep.txt", b"document", user_id=session.user_id)
    message = {"content": "  Keep the original input 中文\n", "attachment_ids": [file.id]}
    payload = {"client_request_id": "lost-receipt", **message}
    endpoint = f"/api/sessions/{session.id}/submissions"
    read = db.get_client_submission
    lost = []

    async def lose_first_committed_read(session_id, client_id):
        row = await read(session_id, client_id)
        if client_id == "lost-receipt" and row is not None and not lost:
            lost.append(row["request_id"])
            if fault == "decode":
                raise ValueError("Receipt decoding failed after admission")
            row["payload"]["attachments"][0].pop("sha256")
        return row

    monkeypatch.setattr(db, "get_client_submission", lose_first_committed_read)
    async with session_client(manager, session) as http, session_socket(manager, session) as send:
        if first_transport == "http":
            response = await http.post(endpoint, json=payload)
            assert response.status_code == 200
            first = response.json()
        else:
            first = await send({"session_id": session.id, "client_request_id": "lost-receipt",
                                "operation": {"kind": "message", **message}})
        assert first["accepted"] is False and first["state"] == "unknown"
        assert first["code"] == "admission_unknown" and first["client_request_id"] == "lost-receipt"

        await wait_dispatch_idle(manager, session.id)
        result = (await http.get(f"{endpoint}/lost-receipt")).json()
        assert result["accepted"] is True and result["request_id"] == lost[0]
        assert result["execution"]["state"] == "done"
        assert result["outcome"]["status"] == "success"
        assert result["input"]["content"] == payload["content"]
        duplicate = await send({"session_id": session.id, **payload})
        assert duplicate["request_id"] == lost[0] and duplicate["accepted"] is True
    assert starts.qsize() == 1
    async with db.transaction(write=False) as tx:
        assert (await tx.fetch_one("SELECT COUNT(*) FROM client_submissions WHERE session_id=?", (session.id,)))[0] == 1


async def test_concurrent_duplicate_confirms_original_when_owner_receipt_read_fails(runtime, monkeypatch):
    manager, db, session, _, starts, _ = runtime
    endpoint = f"/api/sessions/{session.id}/submissions"
    payload = {"client_request_id": "concurrent-input", "content": "Process this once"}
    read = db.get_client_submission
    both_prechecked = asyncio.Event()
    prechecked = 0
    unobserved = []

    async def read_at_admission_boundary(session_id, client_id):
        nonlocal prechecked
        row = await read(session_id, client_id)
        if client_id != "concurrent-input":
            return row
        if asyncio.current_task().get_name() == f"session-owner-{session.id}":
            if row is not None and not unobserved:
                unobserved.append(row["request_id"])
                raise ValueError("The Owner could not decode the original receipt")
        elif row is None:
            prechecked += 1
            if prechecked == 2:
                both_prechecked.set()
            await asyncio.wait_for(both_prechecked.wait(), 2)
        return row

    monkeypatch.setattr(db, "get_client_submission", read_at_admission_boundary)
    async with session_client(manager, session) as http, session_socket(manager, session) as send:
        response, socket_receipt = await asyncio.gather(
            http.post(endpoint, json=payload), send({"session_id": session.id, **payload}),
        )
        assert response.status_code == 200 and prechecked == 2 and len(unobserved) == 1
        for receipt in (response.json(), socket_receipt):
            assert receipt["accepted"] is True and receipt["request_id"] == unobserved[0]
        await wait_dispatch_idle(manager, session.id)
    assert starts.qsize() == 1
    async with db.transaction(write=False) as tx:
        assert (await tx.fetch_one("SELECT COUNT(*) FROM client_submissions WHERE session_id=?", (session.id,)))[0] == 1


async def test_transports_return_the_same_validated_receipt_types(runtime, monkeypatch):
    manager, db, session, _, starts, _ = runtime
    endpoint = f"/api/sessions/{session.id}/submissions"
    payload = {"client_request_id": "legacy-boolean", "content": "Process this once"}
    read = db.get_client_submission

    async def read_snapshot_with_legacy_boolean(session_id, client_id):
        row = await read(session_id, client_id)
        if client_id == "legacy-boolean" and row is not None:
            row["receipt"]["accepted"] = "true"
        return row

    async with session_client(manager, session) as http, session_socket(manager, session) as send:
        first = (await http.post(endpoint, json=payload)).json()
        assert first["accepted"] is True
        await wait_dispatch_idle(manager, session.id)
        monkeypatch.setattr(db, "get_client_submission", read_snapshot_with_legacy_boolean)
        http_receipt = (await http.post(endpoint, json=payload)).json()
        ws_receipt = await send({"session_id": session.id, **payload})
        for receipt in (http_receipt, ws_receipt):
            assert receipt["accepted"] is True and receipt["request_id"] == first["request_id"]
            assert receipt["execution"]["state"] == "done"
    assert starts.qsize() == 1


async def test_untrusted_commit_inside_owner_stops_public_confirmation_and_replay(runtime, monkeypatch):
    manager, db, session, _, starts, _ = runtime
    payload = {"client_request_id": "untrusted-commit", "content": "Do not replay this input"}
    endpoint = f"/api/sessions/{session.id}/submissions"
    admit, commit = db.admit_client_submission, db.conn.commit
    statements, exit_codes = [], []
    healthy = db.health

    def request_exit(code):
        exit_codes.append(code)
        raise RuntimeError("Intercepted process exit")

    async def lose_commit_proof():
        await commit()
        raise OSError("Cannot acknowledge the SQL COMMIT")

    async def fail_inside_admission(*args, **kwargs):
        with monkeypatch.context() as fault:
            fault.setattr(db.conn, "commit", lose_commit_proof)
            return await admit(*args, **kwargs)

    await db.conn.set_trace_callback(statements.append)
    monkeypatch.setattr(db, "admit_client_submission", fail_inside_admission)
    db.health = ServiceHealth(exit_process=request_exit)
    try:
        async with session_client(manager, session) as http:
            with pytest.raises(ServiceUnavailable):
                await http.post(endpoint, json=payload)
            assert exit_codes == [70] and db.health.state == "fatal"
            assert manager.session_owner_failed(session.id)
            assert statements[-1] == "COMMIT"
            recorded = len(statements)
            with pytest.raises(ServiceUnavailable):
                await http.get(f"{endpoint}/untrusted-commit")
            with pytest.raises(ServiceUnavailable):
                await http.post(endpoint, json=payload)
            assert len(statements) == recorded
            assert starts.empty()
    finally:
        # The real process exits at COMMIT failure. Restore the test fixture's
        # separate health object so pytest can close the idle connection.
        db.health = healthy


async def test_owner_post_commit_failure_keeps_original_input_observable(runtime, monkeypatch):
    manager, db, session, _, starts, _ = runtime
    endpoint = f"/api/sessions/{session.id}/submissions"
    payload = {"client_request_id": "committed-before-owner-error", "content": "Keep this input"}

    async def fail_settlement(self, *, withdraw_returns):
        raise ValueError("Prior work could not settle")

    monkeypatch.setattr(OwnerRuntime, "_withdraw_batch_work", fail_settlement)
    async with session_client(manager, session) as http:
        refusal = (await http.post(endpoint, json=payload)).json()
        assert refusal["state"] == "unknown" and refusal["code"] == "admission_unknown"
        assert manager.session_owner_failed(session.id)
        observed = (await http.get(f"{endpoint}/{payload['client_request_id']}")).json()
        assert observed["accepted"] is True and observed["execution"]["state"] == "pending"
        assert starts.empty()

    row = await db.get_client_submission(session.id, payload["client_request_id"])
    assert row["request_id"] == observed["request_id"]


async def test_quota_refusal_has_its_own_code_and_does_not_reserve_the_input(runtime, monkeypatch):
    manager, db, session, _, starts, _ = runtime

    async def quota(*args, **kwargs):
        raise QuotaExceeded("Localized quota explanation")

    with monkeypatch.context() as refusal:
        refusal.setattr(manager, "start_message", quota)
        reply = await submit_client_message(manager, session.id, "Retry after quota", [], "quota", wiki_mode=None)
    assert reply["code"] == "quota_exceeded" and not reply["accepted"]
    assert await submission_status(manager, session.id, "quota") is None
    retried = await submit_client_message(manager, session.id, "Retry after quota", [], "quota", wiki_mode=None)
    assert retried["accepted"]
    await manager.await_session_execution(session.id, timeout=5)
    assert starts.qsize() == 1


@pytest.mark.parametrize("error,expected", [
    (SubmissionConflict("Another language and wording"), "submission_conflict"),
    (InputRecoveryUnavailable("Review the original input"), "recovery_unavailable"),
    (ValueError("This prose happens to contain different input"), "admission_refused"),
])
async def test_refusal_classification_does_not_parse_error_prose(runtime, monkeypatch, error, expected):
    manager, db, session, _, starts, _ = runtime

    async def refuse(*args, **kwargs):
        raise error

    monkeypatch.setattr(manager, "start_message", refuse)
    reply = await submit_client_message(manager, session.id, "Retain original", [], "refused", wiki_mode=None)
    assert not reply["accepted"] and reply["code"] == expected
    assert reply["message"] == str(error)
    assert await submission_status(manager, session.id, "refused") is None
    assert starts.empty()
