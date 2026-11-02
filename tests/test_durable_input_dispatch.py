"""Admission, dispatch and terminal settlement share one durable input."""

import asyncio

import pytest

from server.sessions.client_submissions import submission_status, submit_client_message
from tests.test_submission_recovery import runtime as runtime


async def test_http_reuses_the_same_durable_admission_as_socket_input(runtime):
    from tests.session_runtime import wait_dispatch_idle
    from tests.test_runtime_review_races import session_client

    manager, db, session, _, starts, _ = runtime
    async with await session_client(manager, session) as client:
        endpoint = f"/api/sessions/{session.id}/submissions"
        payload = {"client_request_id": "http-original", "content": "One HTTP input", "attachment_ids": []}
        first = await client.post(endpoint, json=payload)
        assert first.status_code == 200
        assert first.json()["accepted"]
        await wait_dispatch_idle(manager, session.id)
        repeated = await client.post(endpoint, json=payload)
        assert repeated.json()["request_id"] == first.json()["request_id"]
        assert repeated.json()["execution"]["state"] == "done"
        conflict = await client.post(endpoint, json={**payload, "content": "Changed"})
        assert conflict.json()["accepted"] is False and conflict.json()["code"] == "submission_conflict"
        assert starts.qsize() == 1


async def test_refused_attachment_can_be_fixed_and_submitted_with_the_same_identity(runtime):
    from server.attachments import save_upload

    manager, db, session, _, starts, _ = runtime
    file = save_upload(session.id, "required.txt", b"complete evidence", user_id=session.user_id)
    file.path.unlink()
    refused = await submit_client_message(manager, session.id, "Read all evidence", [file.id], "retry", wiki_mode=None)
    assert not refused["accepted"]
    assert await submission_status(manager, session.id, "retry") is None
    file.path.write_bytes(b"complete evidence")
    accepted = await submit_client_message(manager, session.id, "Read all evidence", [file.id], "retry", wiki_mode=None)
    assert accepted["accepted"]
    await manager.await_session_execution(session.id, timeout=5)
    assert starts.qsize() == 1


async def test_competing_contents_cannot_share_a_submission_identity(runtime):
    manager, db, session, _, starts, _ = runtime
    replies = await asyncio.gather(*(
        submit_client_message(manager, session.id, content, [], "one-key", wiki_mode=None)
        for content in ("First content", "Other content")
    ))
    assert sum(reply["accepted"] for reply in replies) == 1
    assert next(reply for reply in replies if not reply["accepted"])["code"] == "submission_conflict"
    await manager.await_session_execution(session.id, timeout=5)
    assert starts.qsize() == 1


@pytest.mark.parametrize("phase", ["admission", "claim"])
async def test_no_execution_precedes_the_input_commit(runtime, monkeypatch, phase):
    manager, db, session, _, starts, _ = runtime
    entered, release = asyncio.Event(), asyncio.Event()
    execute, commit = db.conn.execute, db.conn.commit
    target = "INSERT INTO client_submissions" if phase == "admission" else "UPDATE client_submissions SET execution_state='running'"
    selected = False

    async def observe_sql(sql, *args, **kwargs):
        nonlocal selected
        cursor = await execute(sql, *args, **kwargs)
        if sql.startswith(target):
            selected = True
        return cursor

    async def hold_commit():
        nonlocal selected
        if selected:
            selected = False
            entered.set()
            await release.wait()
        await commit()

    monkeypatch.setattr(db.conn, "execute", observe_sql)
    monkeypatch.setattr(db.conn, "commit", hold_commit)
    submitting = asyncio.create_task(submit_client_message(
        manager, session.id, "Commit before execution", [], "commit-barrier", wiki_mode=None,
    ))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        assert starts.empty()
        release.set()
        reply = await asyncio.wait_for(submitting, 5)
        assert reply["accepted"]
        await manager.await_session_execution(session.id, timeout=5)
        assert starts.qsize() == 1
    finally:
        release.set()
        await submitting


async def test_explicit_refusal_does_not_reserve_a_submission_identity(runtime):
    manager, db, session, _, starts, _ = runtime
    manager.close_admission()
    reply = await submit_client_message(
        manager, session.id, "Keep this draft", [], "retry-after-refusal", wiki_mode=None,
    )
    assert reply["accepted"] is False
    assert await submission_status(manager, session.id, "retry-after-refusal") is None
    assert starts.empty()


async def test_result_notification_observes_the_input_already_settled(runtime):
    manager, db, session, _, starts, _ = runtime
    observed = asyncio.get_running_loop().create_future()

    async def on_result(event):
        if event.get("type") == "result" and event.get("session_id") == session.id:
            observed.set_result(await db.get_client_submission(session.id, "one-input"))

    manager.on_broadcast("atomic-input-result", on_result)
    try:
        reply = await submit_client_message(
            manager, session.id, "Exactly one request", [], "one-input", wiki_mode=None,
        )
        row = await asyncio.wait_for(observed, 5)
        assert row["request_id"] == reply["request_id"]
        assert row["execution_state"] == "done"
        assert (await submission_status(manager, session.id, "one-input"))["execution"]["state"] == "done"
        repeated = await submit_client_message(
            manager, session.id, "Exactly one request", [], "one-input", wiki_mode=None,
        )
        assert repeated["request_id"] == reply["request_id"]
        assert starts.qsize() == 1
    finally:
        manager.remove_broadcast("atomic-input-result")
