"""Current Wiki controls preserve accepted identities and recovery ceilings."""

import asyncio
import json

import pytest

from server.harness import HarnessEvent, get_harness
from server.knowledge import admit, decode_context, encode
from server.sessions.client_submissions import input_digest
from tests import wiki_api as api
from tests.fake_run import FakeRunBase
from tests.test_ws_delivery import connection, frame, hold_model_runs, receipt_frame
from tests.test_ws_delivery import runtime as runtime

client = api.client


async def test_compact_without_native_history_is_refused_without_message_or_wiki_work(client, tmp_path):
    manager = api.session_manager
    agent = await manager.db.get_system_agent()
    session = await manager.create_session(agent["id"], "No native history", str(tmp_path), backend="codex")
    response = await client.post(f"/api/sessions/{session.id}/submissions", json={
        "client_request_id": "unsupported-compact", "operation": {"kind": "compact"},
    })
    assert response.status_code == 200
    assert response.json()["accepted"] is False and response.json()["code"] == "compact_unavailable"
    assert await manager.db.get_client_submission(session.id, "unsupported-compact") is None
    assert manager.dispatch_snapshot(session.id)["items"] == []
    assert await manager.db.load_messages(session.id) == []


async def accepted_read_input(manager, session, *, damage=None):
    """An accepted current read input keeps its original payload and digest."""
    context = admit(session_id=session.id, request_id="old-request", user_id=session.user_id,
                    prompt="Original read-only request", selected="read")
    if damage == "missing":
        knowledge = None
    elif damage == "foreign":
        from dataclasses import replace
        knowledge = encode(replace(context, origin=replace(context.origin, user_id="foreign-account")))
    elif damage == "request":
        from dataclasses import replace
        knowledge = encode(replace(context, origin=replace(context.origin, request_id="another-request")))
    else:
        knowledge = encode(context)
    payload = {"content": "Original read-only request", "attachment_ids": [], "attachments": [],
               "wiki_mode": "read", "knowledge": knowledge, "trusted_request": context.trusted_request,
               "source": "human", "delivery_id": None, "work_id": "old-work", "order": 1,
               "question_session_id": None, "question_id": None, "priority": False}
    await manager.db.admit_client_submission(
        session.id, "old-client", input_digest(payload), "old-request", payload=payload,
        receipt={"accepted": True, "disposition": "accepted"},
    )
    await manager.db.complete_client_submission(
        session.id, "old-request", owner="old-request", state="withdrawn", reason="withdrawn",
    )
    async with manager.db.transaction(write=False) as tx:
        return await tx.fetch_one("SELECT payload,payload_sha256,receipt,request_id FROM client_submissions "
                                  "WHERE session_id=? AND client_id='old-client'", (session.id,))


@pytest.mark.parametrize(("content", "selected"), [
    ("A new ordinary question", None), ("/wiki maintain Reorganize everything", None),
    ("Reorganize everything", "maintain"),
])
async def test_restoring_current_read_input_keeps_its_ceiling_and_original_identity(
    client, tmp_path, monkeypatch, content, selected,
):
    manager = api.session_manager
    vault = tmp_path / "vault"
    vault.mkdir()
    await api.configure(client, vault)
    agent = await manager.db.get_system_agent()
    session = await manager.create_session(agent["id"], "Restore original", str(tmp_path), backend="codex")
    original = await accepted_read_input(manager, session)
    observed = []

    class Run(FakeRunBase):
        async def start(self, *args, **kwargs):
            observed.append(session._run.knowledge_context)

        async def stream(self):
            yield HarnessEvent(type="result", content="Read-only offline answer", is_error=False)

        async def stop(self):
            pass

        async def interrupt(self):
            pass

    monkeypatch.setattr(get_harness("codex"), "create_run", lambda config: Run())
    try:
        before = manager.dispatch_snapshot(session.id)
        confirmed = await client.get(f"/api/sessions/{session.id}/submissions/old-client")
        assert confirmed.status_code == 200, confirmed.text
        assert confirmed.json()["request_id"] == "old-request"
        assert manager.dispatch_snapshot(session.id) == before
        assert observed == []
        retry = await client.post(f"/api/sessions/{session.id}/submissions", json={
            "client_request_id": "old-client", "content": "Original read-only request", "wiki_mode": "read",
        })
        assert retry.json()["request_id"] == "old-request"
        conflict = await client.post(f"/api/sessions/{session.id}/submissions", json={
            "client_request_id": "old-client", "content": "Original read-only request", "wiki_mode": "maintain",
        })
        assert conflict.json()["code"] == "submission_conflict"
        assert manager.dispatch_snapshot(session.id) == before and observed == []
        request = {"client_request_id": "restored-client", "content": content,
                   "recovery_of_client_id": "old-client"}
        if selected is not None:
            request["wiki_mode"] = selected
        response = await client.post(f"/api/sessions/{session.id}/submissions", json=request)
        assert response.status_code == 200, response.text
        saved = response.json()
        assert saved["accepted"] is True
        assert saved["request_id"] != "old-request"
        await manager.await_session_execution(session.id, timeout=3)
        assert len(observed) == 1
        context = observed[0]
        assert context.mode == "read"
        assert context.maintenance.may_submit is False
        assert context.origin.session_id == session.id
        assert context.origin.request_id == saved["request_id"]
        assert context.origin.user_id == session.user_id
        assert context.trusted_request == content
        row = await manager.db.get_client_submission(session.id, "restored-client")
        assert row["recovery_of"] == "old-client"
        assert row["payload"]["work_id"] != "old-work"
        assert decode_context(row["payload"]["knowledge"]) == context
        duplicate = await client.post(f"/api/sessions/{session.id}/submissions", json=request)
        assert duplicate.json()["request_id"] == saved["request_id"]
        assert len(observed) == 1
        async with manager.db.transaction(write=False) as tx:
            assert await tx.fetch_one("SELECT payload,payload_sha256,receipt,request_id FROM client_submissions "
                                      "WHERE session_id=? AND client_id='old-client'", (session.id,)) == original
        assert confirmed.json()["input"]["wiki_mode"] == "read"
        assert json.loads(original[0])["wiki_mode"] == "read"
    finally:
        await manager.stop_all_held_processes()


@pytest.mark.parametrize("damage", ["missing", "foreign", "request"])
async def test_unverifiable_original_context_cannot_be_recovered(client, tmp_path, monkeypatch, damage):
    manager = api.session_manager
    agent = await manager.db.get_system_agent()
    session = await manager.create_session(agent["id"], "Retain unavailable input", str(tmp_path), backend="codex")
    original = await accepted_read_input(manager, session, damage=damage)
    launches = []
    monkeypatch.setattr(get_harness("codex"), "create_run", lambda config: launches.append(config))
    before = manager.dispatch_snapshot(session.id)
    response = await client.post(f"/api/sessions/{session.id}/submissions", json={
        "client_request_id": "unverifiable-recovery", "content": "/wiki maintain New work",
        "recovery_of_client_id": "old-client",
    })
    assert response.status_code == 200, response.text
    assert response.json()["accepted"] is False
    assert response.json()["code"] == "recovery_unavailable"
    assert await manager.db.get_client_submission(session.id, "unverifiable-recovery") is None
    assert manager.dispatch_snapshot(session.id) == before
    assert launches == []
    async with manager.db.transaction(write=False) as tx:
        assert await tx.fetch_one("SELECT payload,payload_sha256,receipt,request_id FROM client_submissions "
                                  "WHERE session_id=? AND client_id='old-client'", (session.id,)) == original


async def test_concurrent_recovery_of_read_only_input_has_one_new_owner(client, tmp_path, monkeypatch):
    manager = api.session_manager
    agent = await manager.db.get_system_agent()
    session = await manager.create_session(agent["id"], "One recovery", str(tmp_path), backend="codex")
    await accepted_read_input(manager, session)
    contexts = []
    entered, release = asyncio.Event(), asyncio.Event()

    class Run(FakeRunBase):
        async def start(self, *args, **kwargs):
            contexts.append(session._run.knowledge_context)
            entered.set()

        async def stream(self):
            await release.wait()
            yield HarnessEvent(type="result", content="One offline answer", is_error=False)

        async def stop(self):
            release.set()

        async def interrupt(self):
            release.set()

    monkeypatch.setattr(get_harness("codex"), "create_run", lambda config: Run())
    try:
        responses = await asyncio.gather(*(client.post(f"/api/sessions/{session.id}/submissions", json={
            "client_request_id": client_id, "content": "/wiki maintain New request",
            "wiki_mode": "maintain", "recovery_of_client_id": "old-client",
        }) for client_id in ("restore-a", "restore-b")))
        accepted = [response.json() for response in responses if response.json()["accepted"]]
        assert len(accepted) == 1
        await asyncio.wait_for(entered.wait(), 3)
        assert len(contexts) == 1 and contexts[0].mode == "read"
        assert contexts[0].maintenance.may_submit is False
        old = await manager.db.get_client_submission(session.id, "old-client")
        assert old["recovered_by"] == accepted[0]["client_request_id"]
        assert old["payload"]["operation"]["wiki_mode"] == "read"
    finally:
        release.set()
        await manager.stop_all_held_processes()


async def test_new_http_off_input_is_refused_without_admission_or_execution(client, tmp_path, monkeypatch):
    manager = api.session_manager
    agent = await manager.db.get_system_agent()
    session = await manager.create_session(agent["id"], "Current modes", str(tmp_path), backend="codex")
    launches = []

    class Run(FakeRunBase):
        async def start(self, *args, **kwargs):
            pass

        async def stream(self):
            yield HarnessEvent(type="result", content="Offline controlled result", is_error=False)

        async def stop(self):
            pass

        async def interrupt(self):
            pass

    def create(config):
        launches.append(config)
        return Run()

    monkeypatch.setattr(get_harness("codex"), "create_run", create)
    before = manager.dispatch_snapshot(session.id)
    try:
        response = await client.post(f"/api/sessions/{session.id}/submissions", json={
            "client_request_id": "new-off-refused", "content": "Answer from our sources", "wiki_mode": "off"})
        assert response.status_code == 422, response.text
        assert await manager.db.get_client_submission(session.id, "new-off-refused") is None
        assert manager.dispatch_snapshot(session.id) == before
        assert launches == []
    finally:
        await manager.stop_all_held_processes()


@pytest.mark.parametrize("selected", ["off", "unknown", 7, []])
async def test_invalid_websocket_mode_keeps_the_socket_and_admits_nothing(runtime, tmp_path, selected):
    manager, db, bearer = runtime
    agent = await db.get_system_agent()
    session = await manager.create_session(agent["id"], "Refuse invalid mode", str(tmp_path), backend="codex")
    before = manager.dispatch_snapshot(session.id)
    async with connection(manager, bearer) as browser:
        await browser[0].put({"type": "websocket.receive", "text": json.dumps({
            "type": "send_message", "session_id": session.id, "content": "Must not be admitted",
            "client_request_id": "invalid-mode", "wiki_mode": selected,
        })})
        refused = await receipt_frame(browser[1], "submission")
        assert refused["accepted"] is False
        assert "mode" in refused["message"].lower()
        assert await db.get_client_submission(session.id, "invalid-mode") is None
        assert manager.dispatch_snapshot(session.id) == before
        await browser[0].put({"type": "websocket.receive", "text": "invalid json"})
        assert (await frame(browser[1]))["message"] == "Invalid JSON"


@pytest.mark.parametrize(("content", "selected"), [
    ("A restored ordinary question", None), ("/wiki maintain Reorganize the vault", None),
    ("Reorganize the vault", "maintain"),
])
async def test_websocket_restoration_keeps_the_original_read_ceiling(
    runtime, tmp_path, monkeypatch, content, selected,
):
    manager, db, bearer = runtime
    agent = await db.get_system_agent()
    session = await manager.create_session(agent["id"], "Restore via WebSocket", str(tmp_path), backend="codex")
    original = await accepted_read_input(manager, session)
    starts = hold_model_runs(manager, monkeypatch)
    async with connection(manager, bearer) as browser:
        payload = {"type": "send_message", "session_id": session.id,
                   "content": content, "client_request_id": "ws-recovery", "recovery_of_client_id": "old-client"}
        if selected is not None:
            payload["wiki_mode"] = selected
        await browser[0].put({"type": "websocket.receive", "text": json.dumps(payload)})
        accepted = await receipt_frame(browser[1], "submission")
        assert accepted["accepted"] is True
        await asyncio.wait_for(starts.get(), 3)
        context = session._run.knowledge_context
        assert context.mode == session._run.wiki_mode == "read"
        assert context.maintenance.may_submit is False
        assert context.origin.request_id == accepted["request_id"] != "old-request"
        assert context.origin.session_id == session.id and context.origin.user_id == session.user_id
        assert context.trusted_request == content
        async with db.transaction(write=False) as tx:
            assert await tx.fetch_one("SELECT payload,payload_sha256,receipt,request_id FROM client_submissions "
                                      "WHERE session_id=? AND client_id='old-client'", (session.id,)) == original
        row = await db.get_client_submission(session.id, "ws-recovery")
        assert row["recovery_of"] == "old-client"


@pytest.mark.parametrize(("content", "original_control", "expected"), [
    ("An ordinary question", None, "auto"), ("/wiki maintain Library/One.md", "maintain", "maintain"),
])
async def test_explicit_auto_preserves_public_command_interpretation_and_retry_identity(
    client, tmp_path, monkeypatch, content, original_control, expected,
):
    manager = api.session_manager
    vault = tmp_path / "vault"
    vault.mkdir()
    await api.configure(client, vault)
    agent = await manager.db.get_system_agent()
    session = await manager.create_session(agent["id"], "Keep auto interpretation", str(tmp_path), backend="codex")
    starts = hold_model_runs(manager, monkeypatch)
    body = {"client_request_id": "same-auto", "content": content, "wiki_mode": "auto"}
    try:
        submitted = await client.post(f"/api/sessions/{session.id}/submissions", json=body)
        assert submitted.status_code == 200 and submitted.json()["accepted"], submitted.text
        await asyncio.wait_for(starts.get(), 3)
        saved = await manager.db.get_client_submission(session.id, "same-auto")
        assert saved["payload"]["operation"]["wiki_mode"] == original_control
        assert decode_context(saved["payload"]["knowledge"]).mode == expected
        repeated = await client.post(f"/api/sessions/{session.id}/submissions", json=body)
        assert repeated.json()["request_id"] == submitted.json()["request_id"]
        assert starts.empty()
    finally:
        await manager.stop_all_held_processes()
