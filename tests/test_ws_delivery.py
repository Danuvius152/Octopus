"""A browser's transport must not own runtime progress or authority."""

from __future__ import annotations

import asyncio
import json
import uuid

import pytest
from fastapi import FastAPI

from server import deps
from server.config import settings
from server.database import Database
from server.deps import get_session_manager
from server.sessions import SessionManager
from tests.accounts import create_account
from tests.fake_run import FakeRunBase
from tests.harness_profiles import process_profile
from tests.websocket_client import connection, frame, receipt_frame


def hold_model_runs(manager, monkeypatch):
    """Only the model transport is held; request/attempt/control remain real."""
    from server.harness import HarnessEvent

    starts = asyncio.Queue()

    class Run(FakeRunBase):
        def __init__(self):
            self.stopped = asyncio.Event()

        def is_alive(self):
            return not self.stopped.is_set()

        async def start(self, prompt, working_dir, resume_id=None, credential=None):
            self.prompt = prompt
            starts.put_nowait(self)

        async def stream(self):
            yield HarnessEvent(type="text", content="Holding the original request.")
            await self.stopped.wait()

        async def stop(self):
            self.stopped.set()

        async def interrupt(self):
            await self.stop()

    monkeypatch.setattr(manager, "_make_run", lambda *args, **kwargs: Run())
    return starts


async def start_held_request(manager, session_id, prompt, **kwargs):
    request_id = uuid.uuid4().hex
    key = f"ws-attempt-{request_id}"
    scope = asyncio.get_running_loop().create_future()

    async def observe(event):
        if (event.get("type") == "attempt_started" and event.get("session_id") == session_id
                and event.get("request_id") == request_id and not scope.done()):
            scope.set_result(event)

    manager.on_broadcast(key, observe)
    try:
        receipt = await manager.start_message(session_id, prompt, request_id=request_id, **kwargs)
        return receipt, await asyncio.wait_for(scope, 2)
    finally:
        manager.remove_broadcast(key)


@pytest.fixture
async def runtime(tmp_path):
    db = Database(":memory:")
    await db.initialize()
    _, bearer = await create_account(db, username="transport-owner", extra_roots=[tmp_path])
    manager = SessionManager()
    await manager.initialize(db)
    try:
        yield manager, db, bearer
    finally:
        await manager.stop_all_held_processes()
        await db.close()


@pytest.mark.parametrize("kind", ["text", "tool_use", "tool_result", "delta"])
async def test_large_committed_answer_has_browser_preview_and_full_http_detail(runtime, monkeypatch, tmp_path, kind):
    import io
    import urllib.request

    from httpx import ASGITransport, AsyncClient

    from server.app_agent import AppAgentManager
    from server.applications import ApplicationManager
    from server.cli import build_parser, do_pull
    from server.harness import HarnessEvent
    from server.main import app as http_app

    manager, db, bearer = runtime
    agent = await db.get_system_agent()
    session = await manager.create_session(agent["id"], "Large answer", str(tmp_path))
    answer = '中文\\"\n' * 180_000 + "Original answer end."
    internal = []
    wire_kind = "assistant_text" if kind in {"text", "delta"} else kind
    body_field = "input" if kind == "tool_use" else "output" if kind == "tool_result" else "content"

    class Run(FakeRunBase):
        async def start(self, *args, **kwargs):
            pass

        async def stream(self):
            if kind == "delta":
                yield HarnessEvent(type="text_delta", content=answer)
            yield HarnessEvent(type="text" if kind == "delta" else kind, content=answer if kind != "tool_use" else None,
                tool_name="LargeTool" if kind not in {"text", "delta"} else None, tool_use_id="original-tool" if kind not in {"text", "delta"} else None,
                tool_input={"payload": answer} if kind == "tool_use" else None)
            yield HarnessEvent(type="result", session_id="large-answer-native")

        async def stop(self):
            pass

        async def interrupt(self):
            await self.stop()

    monkeypatch.setattr(manager, "_make_run", lambda *args, **kwargs: Run())

    async def observe(event):
        if event.get("type") == wire_kind:
            internal.append(event)

    manager.on_broadcast("full-answer-consumer", observe)
    apps, app_agent = ApplicationManager(), AppAgentManager()
    apps.bind(session_mgr=manager, db=db)
    app_agent.bind(session_mgr=manager, db=db, app_mgr=apps)
    app_dir = tmp_path / "reader-app"
    app_dir.mkdir()
    await db.save_application(app_id="large-reader", name="Large reader", agent_id=session.agent_id,
        app_dir=str(app_dir), entrypoint="index.html", status="ready", created_at="now", updated_at="now", user_id=session.user_id)
    http_app.dependency_overrides[get_session_manager] = lambda: manager
    try:
        async with connection(manager, bearer) as browser:
            if kind == "text":
                app_row = await db.get_application("large-reader")
                turn = await app_agent.begin_turn(app_row, message="Produce the complete answer")
                session = manager.get_session(turn.session_id)
                assert session is not None
                stream = [event async for event in app_agent.stream_turn(turn)]
                assert [event["text"] for event in stream if event["type"] == "message"] == [answer]
                assert next(event for event in stream if event["type"] == "done")["reply"] == answer
                saved = await app_agent.get_request_status("large-reader", session.id, turn.request_id)
                assert saved["reply"] == answer
            else:
                receipt = await manager.start_message(session.id, "Produce the complete answer")
                result = await asyncio.wait_for(receipt.wait(), 5)
                assert result.outcome.status == "success"
            while True:
                wire = await asyncio.wait_for(browser[1].get(), 2)
                assert wire["type"] == "websocket.send", wire
                assert len(wire["text"].encode()) <= 256 * 1024
                event = json.loads(wire["text"])
                if event["type"] == "assistant_delta":
                    assert "detail_ref" not in event
                if event["type"] == wire_kind:
                    break
            assert event["body_state"] == "preview"
            expected = {"payload": answer} if kind == "tool_use" else answer
            assert event[body_field] and event[body_field] != expected
            assert internal[0][body_field] == expected and "body_state" not in internal[0]
            async with AsyncClient(transport=ASGITransport(app=http_app), base_url="http://test",
                                   headers={"Authorization": f"Bearer {bearer}"}) as client:
                detail = await client.get(event["detail_ref"])
                original = detail.json()
                assert detail.status_code == 200
                assert original["tool_input" if kind == "tool_use" else "content"] == expected
                assert "no-store" in detail.headers["cache-control"]
                complete = await client.get(f"/api/sessions/{session.id}")
                full = next(m for m in complete.json()["messages"] if m["seq"] == event["seq"])
                assert full == original
                display = await client.get(f"/api/sessions/{session.id}?view=browser")
                shown = next(m for m in display.json()["messages"] if m["seq"] == event["seq"])
                assert len(json.dumps(shown, ensure_ascii=False, separators=(",", ":")).encode()) <= 64 * 1024
                assert shown["body_state"] == "preview" and shown["detail_ref"] == event["detail_ref"]
                exported = await client.get(f"/api/sessions/{session.id}/message-export")
                assert next(m for m in exported.json()["messages"] if m["seq"] == event["seq"]) == original
                if kind == "text":
                    loop = asyncio.get_running_loop()

                    def urlopen(request, **kwargs):
                        response = asyncio.run_coroutine_threadsafe(client.get(request.full_url,
                            headers=dict(request.header_items())), loop).result(timeout=5)
                        response.raise_for_status()
                        return io.BytesIO(response.content)

                    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
                    output = tmp_path / "complete-pull"
                    args = build_parser().parse_args(["pull", session.id, "--server", "http://test", f"--token={bearer}", "--project-dir", str(output)])
                    await asyncio.to_thread(do_pull, args)
                    native = [json.loads(line) for line in (output / "large-answer-native.jsonl").read_text().splitlines()]
                    assert next(row for row in native if row["type"] == "assistant")["message"]["content"][0]["text"] == answer
    finally:
        app_agent.shutdown()
        apps.shutdown()
        manager.remove_broadcast("full-answer-consumer")
        http_app.dependency_overrides.pop(get_session_manager, None)


async def test_unready_ws_input_is_unaccepted_and_same_identity_can_be_retried_after_recovery(runtime, monkeypatch, tmp_path):
    manager, db, bearer = runtime
    agent = await db.get_system_agent()
    session = await manager.create_session(agent["id"], "Recovery barrier", str(tmp_path))
    starts = hold_model_runs(manager, monkeypatch)
    entered, release = asyncio.Event(), asyncio.Event()
    load = db.load_sessions
    async def held_load(*args, **kwargs):
        rows = await load(*args, **kwargs)
        entered.set()
        await release.wait()
        return rows
    monkeypatch.setattr(db, "load_sessions", held_load)
    payload = {"type": "send_message", "session_id": session.id, "content": "Retain my identity", "client_request_id": "retry-after-recovery"}
    async with connection(manager, bearer) as browser:
        initializing = asyncio.create_task(manager.initialize(db))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            await browser[0].put({"type": "websocket.receive", "text": json.dumps(payload)})
            refused = await receipt_frame(browser[1], "submission")
            assert refused["accepted"] is False and refused["code"] == "service_not_ready"
            assert "state" not in refused and starts.empty()
            assert await db.get_client_submission(session.id, payload["client_request_id"]) is None
        finally:
            release.set()
            await initializing
        await browser[0].put({"type": "websocket.receive", "text": json.dumps(payload)})
        accepted = await receipt_frame(browser[1], "submission")
        assert accepted["accepted"] is True
        await asyncio.wait_for(starts.get(), 2)
        assert starts.empty()
        assert (await db.get_client_submission(session.id, payload["client_request_id"]))["request_id"] == accepted["request_id"]
        await manager.interrupt(session.id)


async def test_submission_identity_survives_ack_loss_reconnect_and_manager_restart(runtime, monkeypatch, tmp_path):
    manager, db, bearer = runtime
    agent = await db.get_system_agent()
    session = await manager.create_session(agent["id"], "deduplicated", str(tmp_path), backend="codex")
    starts = hold_model_runs(manager, monkeypatch)
    # A separate read-scoped request cannot take this browser input as a steer.
    blocker, _scope = await start_held_request(manager, session.id, "The original running task", wiki_mode="read")
    original_run = await asyncio.wait_for(starts.get(), 2)
    payload = {"type": "send_message", "session_id": session.id, "content": "execute once", "client_request_id": "same-input"}
    async def submit(owner, data):
        async with connection(owner, bearer) as client:
            await client[0].put({"type": "websocket.receive", "text": json.dumps(data)})
            return await receipt_frame(client[1], "submission")
    first = await submit(manager, payload)
    second = await submit(manager, payload)
    assert first["accepted"] and second["accepted"]
    assert first["request_id"] == second["request_id"]
    queued = manager.dispatch_snapshot(session.id)["items"]
    assert len(queued) == 1 and queued[0]["request_id"] == first["request_id"]
    assert queued[0]["summary"] == "execute once"
    conflict = await submit(manager, {**payload, "content": "different operation"})
    assert conflict["accepted"] is False and conflict["code"] == "submission_conflict"
    await manager.interrupt(session.id)
    assert (await asyncio.wait_for(blocker.wait(), 2)).outcome.status == "interrupted"
    await manager.drain_turns()
    assert original_run.stopped.is_set() and starts.empty()
    restarted = SessionManager()
    await restarted.initialize(db)
    third = await submit(restarted, payload)
    assert third["request_id"] == first["request_id"] and third["accepted"]
    assert restarted.dispatch_snapshot(session.id)["items"] == []
    assert starts.empty()


async def test_control_receipts_bind_stop_and_reject_a_new_attempt(runtime, monkeypatch, tmp_path):
    manager, db, bearer = runtime
    agent = await db.get_system_agent()
    session = await manager.create_session(agent["id"], "scoped Stop", str(tmp_path), backend="codex")
    starts = hold_model_runs(manager, monkeypatch)
    original, scope = await start_held_request(manager, session.id, "first")
    first_run = await asyncio.wait_for(starts.get(), 2)
    successor = None
    async with connection(manager, bearer) as client:
        async def send(data):
            await client[0].put({"type": "websocket.receive", "text": json.dumps(data)})
            return await receipt_frame(client[1], "command_receipt")
        try:
            payload = {"type": "interrupt", "session_id": session.id, "client_command_id": "stop-1",
                       "request_id": original.request_id, "attempt_id": scope["attempt_id"],
                       "generation": manager.dispatch_snapshot(session.id)["generation"]}
            first = await send(payload)
            assert first["accepted"]
            assert (await asyncio.wait_for(original.wait(), 2)).outcome.status == "interrupted"
            successor, next_scope = await start_held_request(manager, session.id, "next")
            next_run = await asyncio.wait_for(starts.get(), 2)
            assert next_scope["attempt_id"] != scope["attempt_id"]
            assert await send(payload) == first  # replays only the original Stop receipt
            stale = await send({**payload, "client_command_id": "late-stop"})
            assert stale["accepted"] is False and stale["code"] == "stale_command"
            assert first_run.stopped.is_set() and not next_run.stopped.is_set()
            assert manager.dispatch_snapshot(session.id)["active_request_id"] == successor.request_id
            assert starts.empty()
        finally:
            await manager.interrupt(session.id)
            if successor is not None:
                assert (await asyncio.wait_for(successor.wait(), 2)).outcome.status == "interrupted"
            await manager.await_session_execution(session.id, timeout=2)


async def test_idle_stop_and_specific_question_commands_reject_stale_scope(runtime, tmp_path, monkeypatch):
    manager, db, bearer = runtime
    agent = await db.get_system_agent()
    session = await manager.create_session(agent["id"], "scoped controls", str(tmp_path), backend="codex")
    starts = hold_model_runs(manager, monkeypatch)
    active = None
    command_number = 0
    async with connection(manager, bearer) as client:
        async def send(operation, **fields):
            nonlocal command_number
            command_number += 1
            payload = {"type": operation, "session_id": session.id, "client_command_id": str(command_number),
                       "request_id": None, "attempt_id": None, **fields}
            await client[0].put({"type": "websocket.receive", "text": json.dumps(payload)})
            return await receipt_frame(client[1], "command_receipt")
        snapshot = manager.dispatch_snapshot(session.id)
        assert not (await send("interrupt", generation="retired", revision=snapshot["revision"]))["accepted"]
        assert not manager.dispatch_snapshot(session.id)["paused"]
        assert (await send("interrupt", generation=snapshot["generation"], revision=snapshot["revision"]))["accepted"]
        assert not (await send("interrupt", generation=snapshot["generation"], revision=snapshot["revision"]))["accepted"]
        try:
            active, scope = await start_held_request(manager, session.id, "The new question attempt")
            await asyncio.wait_for(starts.get(), 2)
            qid = await manager.create_pending_question(
                session.id, [{"question": "Which?", "options": [{"label": "A"}]}],
            )
            answers = [{"selected": ["A"]}]
            fields = {"request_id": active.request_id, "question_id": qid, "answers": answers}
            assert not (await send("answer_question", attempt_id="old-attempt", **fields))["accepted"]
            assert manager.has_pending_question(session.id, qid)
            assert (await send("answer_question", attempt_id=scope["attempt_id"], **fields))["accepted"]
            assert "A" in await manager.wait_for_question_answer(session.id, qid, timeout=.1)
        finally:
            await manager.interrupt(session.id)
            if active is not None:
                await asyncio.wait_for(active.wait(), 2)
            await manager.await_session_execution(session.id, timeout=2)


@pytest.mark.parametrize("mode", [None, "read", "maintain"])
async def test_agent_bearer_cannot_enter_human_websocket(runtime, monkeypatch, tmp_path, mode):
    from server.mcp_identity import mint, register_run, revoke_run
    from server.wiki.tasks import tasks

    manager, db, _bearer = runtime
    agent = await db.get_system_agent()
    session = await manager.create_session(agent["id"], "agent ws", str(tmp_path), backend="codex")
    grant = tasks.open(session, mode, "/wiki maintain Library/note.md") if mode else None
    lease = "ws-authority-test"
    register_run(lease, session.id, session.user_id, None)
    token = mint(session.id, user_id=session.user_id, wiki_task_id=grant, run_lease_id=lease)
    try:
        assert await deps.resolve_principal(token) is not None
        async with connection(manager, token) as client:
            closed = await asyncio.wait_for(client[1].get(), 1)
            assert closed["type"] == "websocket.close" and closed["code"] == 4001
            assert manager.dispatch_snapshot(session.id)["items"] == []
    finally:
        revoke_run(lease)
        if grant:
            tasks.close(grant)


async def test_closed_submission_is_rejected_with_client_identity(runtime, tmp_path):
    manager, db, bearer = runtime
    agent = await db.get_system_agent()
    session = await manager.create_session(agent["id"], "closed ws", str(tmp_path), backend="codex")
    manager.close_admission()
    async with connection(manager, bearer) as client:
        await client[0].put({"type": "websocket.receive", "text": json.dumps({
            "type": "send_message", "session_id": session.id, "content": "keep my draft",
            "client_request_id": "draft-1",
        })})
        rejected = await receipt_frame(client[1], "submission")
        assert rejected["type"] == "submission"
        assert rejected["accepted"] is False
        assert rejected["client_request_id"] == "draft-1"
        assert rejected["session_id"] == session.id
        assert "closed" in rejected["message"].lower()
        assert manager.dispatch_snapshot(session.id)["items"] == []
        assert not await db.load_messages(session.id)


async def test_blocked_asgi_send_does_not_hold_bus_or_other_browser(runtime):
    manager, _, bearer = runtime
    release = asyncio.Event()
    async with connection(manager, bearer, release=release) as slow:
        async with connection(manager, bearer) as healthy:
            event = {"type": "schedules_changed"}
            publish = asyncio.create_task(manager._broadcast(event))
            try:
                await asyncio.wait_for(slow[3].wait(), 1)
                done, _ = await asyncio.wait({publish}, timeout=0.1)
                assert publish in done, "blocked browser owns the runtime broadcast"
                assert await frame(healthy[1]) == event
            finally:
                release.set()
                await publish


async def test_direct_receipts_obey_single_frame_capacity(runtime):
    manager, _, bearer = runtime
    async with connection(manager, bearer) as browser:
        await browser[0].put({"type": "websocket.receive", "text": json.dumps({
            "type": "send_message", "session_id": "missing" * 50_000, "content": "input", "client_request_id": "original-request",
        })})
        wire = await asyncio.wait_for(browser[1].get(), 2)
        assert wire["type"] == "websocket.close" and wire["code"] == 1013


async def test_unrepresentable_native_identity_still_discloses_terminal_status(runtime, tmp_path):
    manager, db, bearer = runtime
    agent = await db.get_system_agent()
    session = await manager.create_session(agent["id"], "Terminal view", str(tmp_path))
    event = {"type": "result", "session_id": session.id, "seq": 0, "terminal": True, "is_error": True,
        "content": {"kind": "outcome", "operation": "message", "status": "failed", "request_id": "r", "recovery": {}},
        "runtime_metadata": {"request_id": "r", "attempt_id": "a", "native": {
            "thread_id": "native" * 20_000, "turn_id": "t", "root_thread_id": "r", "root_turn_id": "t"}}}
    async with connection(manager, bearer) as browser:
        await manager._broadcast(event)
        shown = await frame(browser[1])
        assert shown["type"] == "browser_unavailable" and "failed" in shown["message"]
        assert shown["detail_ref"] == f"/api/sessions/{session.id}/messages/0"


async def account(runtime, monkeypatch):
    from server.users import UserManager
    manager, db, _bearer = runtime
    users = UserManager(db)
    monkeypatch.setattr(deps, "_user_manager", users)
    user = await users.create_user(username="wsowner", password="password1")
    return users, user, await users.issue_token(user["id"])


@pytest.mark.parametrize("resource", ["missing", "deleted", "other-owner"])
async def test_resource_rejection_preserves_account_socket(runtime, monkeypatch, tmp_path, resource):
    manager, db, _bearer = runtime
    users, user, token = await account(runtime, monkeypatch)
    agent = await db.get_system_agent()
    sid = "nonexistent"
    if resource != "missing":
        from server.agent_manager import AgentManager

        owner = user["id"]
        if resource == "other-owner":
            owner = (await users.create_user(username="other", password="password2"))["id"]
        agent = await AgentManager(db).create_agent(name="owned-agent", user_id=owner)
        session = await manager.create_session(agent["id"], "private", None)
        sid = session.id
        if resource == "deleted":
            await manager.delete_session(sid)
    async with connection(manager, token) as client:
        await client[0].put({"type": "websocket.receive", "text": json.dumps({
            "type": "send_message", "session_id": sid, "content": "draft",
            "client_request_id": "stable-id",
        })})
        assert await frame(client[1]) == {
            "type": "submission", "session_id": sid, "client_request_id": "stable-id",
            "accepted": False, "message": "Session not found",
        }
        await client[0].put({"type": "websocket.receive", "text": "invalid json"})
        assert (await frame(client[1]))["message"] == "Invalid JSON"
        assert (await users.resolve_token(token))["id"] == user["id"]
        await users.revoke_token(token)
        await client[0].put({"type": "websocket.receive", "text": "invalid json"})
        closed = await asyncio.wait_for(client[1].get(), 1)
        assert closed["type"] == "websocket.close" and closed["code"] == 4001


async def test_outgoing_account_frame_uses_one_read_and_no_activity_write(runtime, monkeypatch):
    manager, db, _bearer = runtime
    _, _, token = await account(runtime, monkeypatch)
    async with connection(manager, token) as client:
        statements = []
        await db.conn.set_trace_callback(statements.append)
        await manager._broadcast({"type": "schedules_changed"})
        assert (await frame(client[1]))["type"] == "schedules_changed"
        assert len([s for s in statements if s.startswith("SELECT")]) == 1
        assert not [s for s in statements if s.startswith("UPDATE")]


async def test_incoming_account_activity_is_throttled_independently(runtime, monkeypatch):
    manager, db, _bearer = runtime
    _, _, token = await account(runtime, monkeypatch)
    async with connection(manager, token) as client:
        statements = []
        await db.conn.set_trace_callback(statements.append)
        for _ in range(3):
            await client[0].put({"type": "websocket.receive", "text": "invalid json"})
            assert (await frame(client[1]))["message"] == "Invalid JSON"
        updates = [s for s in statements if s.startswith("UPDATE auth_tokens")]
        assert len(updates) == 1


@pytest.mark.parametrize("withdraw", ["revoke", "disable"])
async def test_queued_account_frame_rechecks_live_authority(runtime, monkeypatch, withdraw):
    manager, _, _bearer = runtime
    users, user, token = await account(runtime, monkeypatch)
    release = asyncio.Event()
    async with connection(manager, token, release=release) as client:
        await manager._broadcast({"type": "schedules_changed", "marker": "in-flight"})
        await asyncio.wait_for(client[3].wait(), 1)
        await manager._broadcast({"type": "schedules_changed", "marker": "queued"})
        if withdraw == "revoke":
            await users.revoke_token(token)
        else:
            await users.set_disabled(user["id"], True)
        release.set()
        assert (await frame(client[1]))["marker"] == "in-flight"
        closed = await asyncio.wait_for(client[1].get(), 1)
        assert closed["type"] == "websocket.close" and closed["code"] == 4001
        await asyncio.wait_for(client[2], 1)
        assert client[1].empty()


@pytest.mark.parametrize("resource", ["deleted", "unknown", "foreign", "archived"])
async def test_queued_session_frame_does_not_revoke_valid_account(
    runtime, monkeypatch, tmp_path, caplog, resource,
):
    from server import ws_delivery
    from server.agent_manager import AgentManager
    from server.users import UserManager

    manager, db, bearer = runtime
    user_id = (await deps.resolve_principal(bearer)).user_id
    agent = await db.get_system_agent()
    if resource == "foreign":
        other = await UserManager(db).create_user(username="other-frame-owner", password="password2")
        agent = await AgentManager(db).create_agent(name="foreign", user_id=other["id"])
    session = await manager.create_session(agent["id"], "queued frame", None)
    if resource == "archived":
        await manager.archive_session(session.id)
    sid = "unknown-session" if resource == "unknown" else session.id
    monkeypatch.setattr(ws_delivery, "MAX_PENDING_FRAMES", 6)
    monkeypatch.setattr(ws_delivery, "MAX_PENDING_BYTES", 2048)
    release = asyncio.Event()
    async with connection(manager, bearer, release=release) as browser:
        await manager._broadcast({"type": "schedules_changed", "marker": "first"})
        await asyncio.wait_for(browser[3].wait(), 1)
        # Deliberately misroute a frame through the real bus: delivery must
        # enforce ownership independently of the producer's audience.
        await manager._broadcast({"type": "assistant_text", "session_id": sid, "content": "private"}, user_id=user_id)
        if resource == "deleted":
            assert await manager.delete_session(session.id)
        await manager._broadcast({"type": "schedules_changed", "marker": "after"})
        release.set()
        assert (await frame(browser[1]))["marker"] == "first"
        if resource == "archived":
            assert (await frame(browser[1]))["content"] == "private"
        while (event := await frame(browser[1])).get("type") == "session_deleted":
            pass
        assert event == {"type": "schedules_changed", "marker": "after"}
        # Repeated drops must return both budgets; the valid frame is a
        # barrier proving the previous drop finished before the next offer.
        for number in range(6):
            if resource != "archived":
                await manager._broadcast({"type": "assistant_text", "session_id": sid, "content": "x" * 400}, user_id=user_id)
            await manager._broadcast({"type": "schedules_changed", "marker": number})
            assert (await frame(browser[1]))["marker"] == number
        assert not browser[2].done()
    if resource == "foreign":
        assert "cross-account" in caplog.text


@pytest.mark.parametrize("entry", ["rest", "internal", "archived", "failed"])
async def test_confirmed_deletion_notifies_both_browsers_after_original_retirement(
    runtime, monkeypatch, entry,
):
    import httpx

    from server.routers.sessions import router as session_router

    manager, db, bearer = runtime
    agent = await db.get_system_agent()
    session = await manager.create_session(agent["id"], "delete on two pages", None)
    if entry == "archived":
        await manager.archive_session(session.id)
    original = db.delete_session
    entered, release = asyncio.Event(), asyncio.Event()

    async def delete(sid):
        entered.set()
        await release.wait()
        if entry == "failed":
            raise RuntimeError("original deletion failed")
        await original(sid)

    monkeypatch.setattr(db, "delete_session", delete)
    app = FastAPI()
    app.include_router(session_router)
    app.dependency_overrides[get_session_manager] = lambda: manager
    async with connection(manager, bearer) as first, connection(manager, bearer) as second:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            async def remove():
                if entry == "rest":
                    response = await client.delete(f"/api/sessions/{session.id}", headers={"Authorization": f"Bearer {bearer}"})
                    assert response.status_code == 204
                else:
                    assert await manager.delete_session(session.id)

            deleting = asyncio.create_task(remove())
            async def assert_no_deletion():
                await manager._broadcast({"type": "schedules_changed", "marker": "barrier"})
                for browser in (first, second):
                    while (event := await frame(browser[1])).get("marker") != "barrier":
                        assert event["type"] != "session_deleted"
            try:
                await asyncio.wait_for(entered.wait(), 2)
                await assert_no_deletion()
                release.set()
                if entry == "failed":
                    with pytest.raises(RuntimeError, match="original deletion failed"):
                        await deleting
                    assert manager.get_session(session.id) is session
                    await assert_no_deletion()
                else:
                    await deleting
                    for browser in (first, second):
                        assert await receipt_frame(browser[1], "session_deleted") == {"type": "session_deleted", "session_id": session.id}
                        assert not browser[2].done()
                    assert manager.get_session(session.id) is None
                    assert await db.get_session_binding(session.id) is None
                    assert not await manager.delete_session(session.id)
                    await manager._broadcast({"type": "schedules_changed"})
                    for browser in (first, second):
                        assert await frame(browser[1]) == {"type": "schedules_changed"}
            finally:
                release.set()
                await asyncio.gather(deleting, return_exceptions=True)


@pytest.mark.parametrize("transition", ["archive", "delete-archived"])
async def test_frame_readability_and_account_share_the_final_read(runtime, monkeypatch, transition):
    manager, db, bearer = runtime
    agent = await db.get_system_agent()
    session = await manager.create_session(agent["id"], "ownership during authentication", None)
    if transition == "delete-archived":
        await manager.archive_session(session.id)
    entered, release = asyncio.Event(), asyncio.Event()
    read = db.get_token_user
    held = False

    async def read_after_transition(*args, **kwargs):
        nonlocal held
        if not held and asyncio.current_task().get_name().startswith("websocket-writer:"):
            held = True
            entered.set()
            await release.wait()
        return await read(*args, **kwargs)

    monkeypatch.setattr(db, "get_token_user", read_after_transition)
    async with connection(manager, bearer) as browser:
        await manager._broadcast({"type": "assistant_text", "session_id": session.id, "content": "original"}, user_id=session.user_id)
        try:
            await asyncio.wait_for(entered.wait(), 1)
            if transition == "archive":
                await manager.archive_session(session.id)
            else:
                # Archived sessions are removed through existing DB consumers.
                await db.delete_session(session.id)
            release.set()
            await manager._broadcast({"type": "schedules_changed"})
            seen = []
            while (event := await frame(browser[1]))["type"] != "schedules_changed":
                seen.append(event)
            assert any(event.get("content") == "original" for event in seen) == (transition == "archive")
            assert not browser[2].done()
        finally:
            release.set()


async def test_setup_proof_cannot_open_business_socket_before_or_after_setup(monkeypatch):
    from server.users import UserManager

    monkeypatch.setattr(settings, "auth_token", "ws-setup-proof-only")
    db = Database(":memory:")
    await db.initialize()
    users = UserManager(db)
    deps.set_user_manager(users)
    manager = SessionManager()
    await manager.initialize(db)
    try:
        for accounts_exist in (False, True):
            if accounts_exist:
                user = await users.setup(username="socket-owner", password="socket-account-password")
                bearer = await users.issue_token(user["id"])
                async with connection(manager, bearer) as account_client:
                    await manager._broadcast({"type": "schedules_changed"})
                    assert await frame(account_client[1]) == {"type": "schedules_changed"}
            async with connection(manager, settings.auth_token) as client:
                closed = await asyncio.wait_for(client[1].get(), 1)
                assert closed["type"] == "websocket.close" and closed["code"] == 4001
                assert client[1].empty()
            assert await db.load_sessions() == []
    finally:
        await manager.stop_all_held_processes()
        await db.close()


@pytest.mark.parametrize("limit", ["frames", "bytes"])
async def test_overflow_ends_connection_instead_of_silently_losing_control(runtime, monkeypatch, limit):
    from server import ws_delivery
    manager, _, bearer = runtime
    monkeypatch.setattr(ws_delivery, "MAX_PENDING_FRAMES", 2 if limit == "frames" else 100)
    monkeypatch.setattr(ws_delivery, "MAX_PENDING_BYTES", 160 if limit == "bytes" else 10000)
    release = asyncio.Event()
    async with connection(manager, bearer, release=release) as client:
        await manager._broadcast({"type": "text_delta", "content": "x" * 50})
        await asyncio.wait_for(client[3].wait(), 1)
        await manager._broadcast({"type": "question_request", "content": "y" * 50})
        await manager._broadcast({"type": "result", "content": "z" * 50})
        closed = await asyncio.wait_for(client[1].get(), 1)
        assert closed["type"] == "websocket.close" and closed["code"] == 1013
        await asyncio.wait_for(client[2], 1)
        assert client[1].empty()
        assert not ws_delivery._connections


async def test_send_timeout_cancels_transport_and_reconnects_instead_of_signout(runtime, monkeypatch):
    from server import ws_delivery
    manager, _, bearer = runtime
    monkeypatch.setattr(ws_delivery, "SEND_TIMEOUT_SECONDS", 0.03)
    release = asyncio.Event()
    async with connection(manager, bearer, release=release) as client:
        await manager._broadcast({"type": "schedules_changed"})
        await asyncio.wait_for(client[3].wait(), 1)
        closed = await asyncio.wait_for(client[1].get(), 1)
        assert closed["type"] == "websocket.close" and closed["code"] == 1013
        await asyncio.wait_for(client[2], 1)


async def test_slow_browser_does_not_delay_stop_or_the_committed_result(runtime, monkeypatch, tmp_path, short_process_shutdown):
    import sys

    from server.harness import get_harness

    manager, db, bearer = runtime
    agent = await db.get_system_agent()
    session = await manager.create_session(agent["id"], "network stop", str(tmp_path), backend="codex")

    def argv(context):
        code = (
            "import json,time\n"
            "print(json.dumps({'type':'thread.started','thread_id':'ws-stop-thread'}),flush=True)\n"
            "print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':'Working.'}}),flush=True)\n"
            "time.sleep(30)\n"
        )
        return [sys.executable, "-u", "-c", code], {"cwd": context.working_dir}

    harness = get_harness("codex")
    monkeypatch.setattr(harness, "profile", process_profile(harness.profile, build_turn_argv=argv, prepare_resume=None))
    release = asyncio.Event()
    async with connection(manager, bearer, release=release) as slow:
        async with connection(manager, bearer) as healthy:
            original = await asyncio.wait_for(manager.start_message(session.id, "hold this turn"), 1)
            await asyncio.wait_for(slow[3].wait(), 1)
            ready = await receipt_frame(healthy[1], "assistant_text")
            assert ready["content"] == "Working."
            assert await asyncio.wait_for(manager.interrupt(session.id), 1)
            completion = await asyncio.wait_for(original.wait(), 5)
            assert completion.outcome.status == "interrupted"
            await manager.await_session_execution(session.id, timeout=5)
            rows = await db.load_messages(session.id)
            results = [row for row in rows if row["type"] == "result"]
            assert len(results) == 1
            assert results[0]["content"]["status"] == "interrupted"
            async with asyncio.timeout(1):
                while (event := await frame(healthy[1]))["type"] != "result":
                    pass
            assert event["content"]["status"] == "interrupted"
            assert not release.is_set(), "Stop/result waited for the slow network gate"
