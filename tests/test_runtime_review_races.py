"""Public runtime race regressions found by the integration review."""

from __future__ import annotations

import asyncio
import json
import os
import shlex
import signal
import sys
import time
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from server import deps, mcp_http
from server.agent_manager import AgentManager
from server.bg_tasks import BgTaskManager
from server.bootstrap import bootstrap_first_account
from server.config import settings
from server.database import Database
from server.delegations import DelegationError, DelegationManager
from server.deps import get_session_manager, scope_user_id
from server.harness import get_harness
from server.mcp_identity import mint, scope_is_live, verify
from server.routers import auth as auth_router
from server.routers.sessions import router as sessions_router
from server.routers.ws import router as ws_router
from server.sessions import SessionManager
from server.sessions import turns as turn_module
from server.sessions.base import PendingApproval
from server.users import UserManager


def session_client(manager, session):
    app = FastAPI()
    app.include_router(sessions_router)
    app.dependency_overrides[get_session_manager] = lambda: manager
    app.dependency_overrides[scope_user_id] = lambda: session.user_id
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test", headers={"Authorization": f"Bearer {settings.auth_token}"},
    )


def gate_sql(monkeypatch, database, predicate):
    entered, release = asyncio.Event(), asyncio.Event()
    execute = database.conn._execute

    async def delayed(function, *args, **kwargs):
        if args and isinstance(args[0], str) and predicate(args[0]) and not entered.is_set():
            entered.set()
            await release.wait()
        return await execute(function, *args, **kwargs)

    monkeypatch.setattr(database.conn, "_execute", delayed)
    return entered, release


async def wait_until(predicate, timeout=5):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.01)


def process_alive(pid):
    try:
        state = __import__("pathlib").Path(f"/proc/{pid}/stat").read_text().split()[2]
        return state != "Z"
    except (FileNotFoundError, ProcessLookupError):
        return False


@pytest.fixture
async def background(runtime):
    session_manager, database, session, _, _, _ = runtime
    manager, delivered, broadcasts = BgTaskManager(), [], []

    async def deliver(record):
        delivered.append(record)

    async def broadcast(event):
        broadcasts.append(event)
        await session_manager._broadcast(event)

    manager.bind(database, deliver, broadcast)
    await manager.start()
    try:
        yield manager, session, delivered, broadcasts
    finally:
        await manager.shutdown()


@pytest.fixture
async def runtime(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "users_root", str(tmp_path / "users"))
    monkeypatch.setattr(settings, "agents_dir", str(tmp_path / "legacy-agents"))
    database = Database(":memory:")
    await database.initialize()
    manager = SessionManager()
    await manager.initialize(database)
    agents = AgentManager(database)
    agent = await database.get_system_agent()
    await agents.create_agent(name="Review worker", backend="codex")
    parent = await manager.create_session(agent["id"], "Review caller", str(tmp_path), backend="codex")
    await manager.interrupt(parent.id)
    delegations = DelegationManager()
    delegations.bind(manager, database)
    starts = asyncio.Queue()
    events = []

    async def receive(event):
        events.append(event)

    manager.on_broadcast("review-events", receive)

    def argv(context):
        starts.put_nowait(context)
        code = "import json,time\n"
        code += "print(json.dumps({'type':'thread.started','thread_id':'review-thread'}),flush=True)\n"
        code += "print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':'Reply.'}}),flush=True)\n"
        if "held" in context.prompt:
            code += "time.sleep(30)\n"
        code += "print(json.dumps({'type':'turn.completed','usage':{}}),flush=True)\n"
        return [sys.executable, "-u", "-c", code], {"cwd": context.working_dir}

    harness = get_harness("codex")
    monkeypatch.setattr(harness, "profile", replace(harness.profile, build_turn_argv=argv, prepare_resume=None))
    try:
        yield manager, database, parent, delegations, starts, events
    finally:
        for session in list(manager.sessions.values()):
            await manager.interrupt(session.id)
            if session._active_task:
                await asyncio.wait_for(session._active_task, 5)
        delegations.shutdown()
        await manager.stop_all_held_processes()
        await database.close()


async def wait_dispatch_idle(manager, session_id):
    async with asyncio.timeout(5):
        while (snapshot := manager.dispatch_snapshot(session_id))["active_request_id"] is not None or snapshot["items"]:
            await asyncio.sleep(0.01)


async def begin_ownerless_run(runtime):
    manager, _, session, _, starts, events = runtime
    await manager.start_message(session.id, "held before account")
    context = await asyncio.wait_for(starts.get(), 5)
    await wait_until(lambda: any(event["type"] == "assistant_text" for event in events))
    token = next(entry.credential for entry in context.mcp_servers if entry.key == "ask")
    return token, verify(token)


async def connect_ws(manager, token):
    app = FastAPI()
    app.include_router(ws_router)
    app.dependency_overrides[get_session_manager] = lambda: manager
    incoming, outgoing = asyncio.Queue(), asyncio.Queue()
    await incoming.put({"type": "websocket.connect"})
    scope = {"type": "websocket", "asgi": {"version": "3.0"}, "scheme": "ws", "path": "/ws", "raw_path": b"/ws", "query_string": f"token={token}".encode(), "headers": [], "client": ("127.0.0.1", 1), "server": ("127.0.0.1", 8000), "subprotocols": []}
    task = asyncio.create_task(app(scope, incoming.get, outgoing.put))
    assert (await asyncio.wait_for(outgoing.get(), 5))["type"] == "websocket.accept"
    return incoming, outgoing, task


async def disconnect_ws(incoming, task):
    if not task.done():
        await incoming.put({"type": "websocket.disconnect", "code": 1000})
    await asyncio.wait_for(task, 5)


@pytest.mark.parametrize("legacy", [False, True])
async def test_ownerless_scope_is_rejected_by_rest_ws_and_direct_mcp_after_first_account(runtime, monkeypatch, legacy):
    manager, database, session, _, _, _ = runtime
    users = UserManager(database)
    monkeypatch.setattr(deps, "_user_manager", users)
    token, _ = await begin_ownerless_run(runtime)
    if legacy:
        token = mint(session.id)
    app = FastAPI()
    app.include_router(sessions_router)
    app.dependency_overrides[get_session_manager] = lambda: manager
    servers = {"ask": mcp_http.build_servers(fresh=True)["ask"]}
    mcp_http.mount_all(app, servers)
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json, text/event-stream"}
    initialize = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "scope-review", "version": "1"}}}
    async with mcp_http.lifespan(app, servers), httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8000", headers=headers) as client:
        assert (await client.get("/api/sessions")).status_code == 200
        assert await deps.scope_user_id_for(token) == (True, None)
        assert (await client.post("/mcp/ask/mcp", json=initialize)).status_code == 200
        await users.create_user(username="first", password="password1")
        await users.create_user(username="second", password="password2")
        assert (await client.get("/api/sessions")).status_code == 401
        assert await deps.scope_user_id_for(token) == (False, None)
        assert (await client.post("/mcp/ask/mcp", json=initialize)).status_code == 401


async def test_bootstrap_retires_actual_ownerless_run_and_next_request_uses_new_owner(runtime, monkeypatch):
    manager, database, session, _, starts, events = runtime
    users = UserManager(database)
    monkeypatch.setattr(deps, "_user_manager", users)
    _, scope = await begin_ownerless_run(runtime)
    assert scope_is_live(scope)
    summary = await bootstrap_first_account(database, username="first", password="password1", session_manager=manager)
    assert not scope_is_live(scope)
    assert session.user_id == summary["user_id"]
    await wait_dispatch_idle(manager, session.id)
    before = len(events)
    await manager.start_message(session.id, "Continue after claim")
    context = await asyncio.wait_for(starts.get(), 5)
    next_token = next(entry.credential for entry in context.mcp_servers if entry.key == "ask")
    assert verify(next_token).user_id == summary["user_id"]
    await wait_dispatch_idle(manager, session.id)
    outcomes = [event["content"] for event in events[before:] if event["type"] == "result"]
    assert len(outcomes) == 1 and outcomes[0]["status"] == "success"


async def test_bootstrap_retires_held_process_and_resumes_transcript_under_new_owner(runtime, monkeypatch, tmp_path):
    manager, database, parent, _, starts, _ = runtime
    monkeypatch.setattr(deps, "_user_manager", UserManager(database))
    pid_file = tmp_path / "held-before-adoption.pid"

    def argv(context):
        starts.put_nowait(context)
        code = f"import json,os,sys; open({str(pid_file)!r},'w').write(str(os.getpid()))\n"
        code += "for line in sys.stdin:\n"
        code += " print(json.dumps({'type':'system','subtype':'init','session_id':'held-thread'}),flush=True)\n"
        code += " print(json.dumps({'type':'result','subtype':'success','session_id':'held-thread','result':'Done.','is_error':False}),flush=True)\n"
        return [sys.executable, "-u", "-c", code], {"cwd": context.working_dir}

    harness = get_harness("claude-code")
    monkeypatch.setattr(harness, "profile", replace(harness.profile, build_turn_argv=argv, prepare_resume=None))
    held = await manager.create_session(parent.agent_id, "Held scope", str(tmp_path), backend="claude-code")
    await manager.start_message(held.id, "Finish initial request")
    before = await asyncio.wait_for(starts.get(), 5)
    await wait_dispatch_idle(manager, held.id)
    old_scope = verify(next(entry.credential for entry in before.mcp_servers if entry.key == "ask"))
    old_pid = int(pid_file.read_text())
    assert scope_is_live(old_scope) and process_alive(old_pid)
    summary = await bootstrap_first_account(database, username="first", password="password1", session_manager=manager)
    assert not scope_is_live(old_scope) and not process_alive(old_pid)
    await manager.start_message(held.id, "Continue the existing transcript")
    after = await asyncio.wait_for(starts.get(), 5)
    new_scope = verify(next(entry.credential for entry in after.mcp_servers if entry.key == "ask"))
    assert new_scope.user_id == summary["user_id"]
    assert after.resume_id == "held-thread"
    await wait_dispatch_idle(manager, held.id)


async def test_account_transition_does_not_fan_out_unadopted_question_answer(runtime, monkeypatch):
    manager, database, session, _, _, _ = runtime
    users = UserManager(database)
    monkeypatch.setattr(deps, "_user_manager", users)
    question = await manager.create_pending_question(session.id, [{"question": "Private choice", "options": [{"label": "A"}]}])
    # The answer has already been recorded when account adoption crosses its
    # publication. Do not submit a new synchronous write while a connection
    # operation is deliberately gated: writes are now serialized.
    publication_entered, publication_release = asyncio.Event(), asyncio.Event()

    async def hold_publication(event):
        if event["type"] == "question_answer":
            publication_entered.set()
            await publication_release.wait()

    manager.on_broadcast("hold-answer-publication", hold_publication)
    answer = asyncio.create_task(manager.answer_question(session.id, question, [{"question": "Private choice", "answer": "synthetic private answer"}]))
    await asyncio.wait_for(publication_entered.wait(), 5)
    entered, release = gate_sql(monkeypatch, database, lambda sql: sql.startswith("UPDATE sessions SET user_id"))
    adoption = asyncio.create_task(bootstrap_first_account(database, username="first", password="password1", session_manager=manager))
    await asyncio.wait_for(entered.wait(), 5)
    assert session.user_id is None and await deps.accounts_exist()
    foreign = asyncio.create_task(users.create_user(username="second", password="password2"))
    foreign_events = []

    async def receive(event):
        foreign_events.append(event)

    manager.on_broadcast("foreign-during-adoption", receive, "unrelated-account")
    try:
        publication_release.set()
        assert await asyncio.wait_for(answer, 5)
        assert foreign_events == []
    finally:
        publication_release.set()
        release.set()
        await adoption
        await foreign


async def test_archived_background_result_keeps_adopted_owner(background, runtime, monkeypatch, tmp_path):
    bg, session, delivered, _ = background
    manager, database, _, _, _, _ = runtime
    users = UserManager(database)
    monkeypatch.setattr(deps, "_user_manager", users)
    gate = tmp_path / "complete-bg"
    code = f"import pathlib,time; gate=pathlib.Path({str(gate)!r});\nwhile not gate.exists(): time.sleep(.01)\nprint('synthetic private output')"
    record = await bg.start_task(session_id=session.id, command=shlex.join([sys.executable, "-c", code]), working_dir=str(tmp_path), idle_timeout_seconds=0)
    summary = await bootstrap_first_account(database, username="first", password="password1", session_manager=manager)
    foreign = await users.create_user(username="second", password="password2")
    owner_events, foreign_events = [], []

    async def owner_receive(event):
        owner_events.append(event)

    async def foreign_receive(event):
        foreign_events.append(event)

    manager.on_broadcast("bg-owner", owner_receive, summary["user_id"])
    manager.on_broadcast("bg-foreign", foreign_receive, foreign["id"])
    await manager.archive_session(session.id, replace=False)
    gate.touch()
    await wait_until(lambda: bool(delivered))
    assert foreign_events == []
    assert [event["type"] for event in owner_events] == ["session_archived", "bg_completed"]
    assert owner_events[-1]["task_id"] == record.id


@pytest.mark.parametrize("boundary", ["incoming", "broadcast", "rotation_notice"])
async def test_connected_ownerless_websocket_retires_on_account_transition(runtime, monkeypatch, boundary):
    manager, database, session, _, _, _ = runtime
    users = UserManager(database)
    monkeypatch.setattr(deps, "_user_manager", users)
    token = settings.auth_token
    incoming, outgoing, task = await connect_ws(manager, token)
    try:
        account = await users.create_user(username="first", password="password1")
        if boundary == "incoming":
            await incoming.put({"type": "websocket.receive", "text": "bad json"})
        elif boundary == "broadcast":
            await manager._broadcast({"type": "assistant_text", "session_id": session.id, "content": "synthetic private output"}, user_id=account["id"])
        else:
            await manager._broadcast({"type": "auth_token_rotated", "token": settings.auth_token})
        frame = await asyncio.wait_for(outgoing.get(), 5)
        assert frame["type"] == "websocket.close" and frame["code"] == 4001
    finally:
        await disconnect_ws(incoming, task)


@pytest.mark.parametrize("revoke", [False, True])
async def test_connected_legacy_websocket_receives_actual_token_rotation_handoff(runtime, monkeypatch, tmp_path, revoke):
    manager, database, _, _, _, _ = runtime
    monkeypatch.setattr(settings, "auth_token", settings.auth_token)
    monkeypatch.setattr(deps, "_user_manager", UserManager(database))
    monkeypatch.setattr(auth_router, "_db", database)
    env = tmp_path / "rotation.env"
    env.write_text(f"OCTOPUS_AUTH_TOKEN={settings.auth_token}\n")
    monkeypatch.setenv("OCTOPUS_ENV_FILE", str(env))
    old, replacement = settings.auth_token, "review-rotated-token-0123456789"
    incoming, outgoing, task = await connect_ws(manager, old)
    app = FastAPI()
    app.include_router(auth_router.router)
    app.dependency_overrides[get_session_manager] = lambda: manager
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post("/api/auth/rotate", headers={"Authorization": f"Bearer {old}"}, json={"new_token": replacement, "revoke_other_clients": revoke})
        assert response.status_code == 200
        frame = await asyncio.wait_for(outgoing.get(), 5)
        assert frame["type"] == "websocket.send"
        assert json.loads(frame["text"]) == {"type": "auth_token_rotated", "token": None if revoke else replacement}
        await incoming.put({"type": "websocket.receive", "text": "bad json"})
        next_frame = await asyncio.wait_for(outgoing.get(), 5)
        if revoke:
            assert next_frame["type"] == "websocket.close" and next_frame["code"] == 4001
        else:
            assert json.loads(next_frame["text"])["message"] == "Invalid JSON"
    finally:
        await disconnect_ws(incoming, task)


async def test_outbound_retirement_during_incoming_request_is_normal_disconnect(runtime, monkeypatch, caplog):
    manager, database, session, _, _, _ = runtime
    users = UserManager(database)
    monkeypatch.setattr(deps, "_user_manager", users)
    claimed = False

    async def create_account_during_delivery(event):
        nonlocal claimed
        if event["type"] == "dispatch_state" and not claimed:
            claimed = True
            await users.create_user(username="first", password="password1")

    manager.on_broadcast("claim-during-ws-request", create_account_during_delivery)
    incoming, outgoing, task = await connect_ws(manager, settings.auth_token)
    try:
        await incoming.put({"type": "websocket.receive", "text": json.dumps({"type": "send_message", "session_id": session.id, "content": "A pre-claim request"})})
        frame = await asyncio.wait_for(outgoing.get(), 5)
        assert frame["type"] == "websocket.close" and frame["code"] == 4001
        await asyncio.wait_for(task, 5)
        assert not [record for record in caplog.records if record.name == "server.routers.ws" and record.levelno >= 40]
    finally:
        await disconnect_ws(incoming, task)


async def test_unretired_websocket_transport_error_remains_logged(runtime, caplog):
    manager, _, _, _, _, _ = runtime
    incoming, _, task = await connect_ws(manager, settings.auth_token)
    await incoming.put({"type": "unexpected-transport-frame"})
    await asyncio.wait_for(task, 5)
    errors = [record for record in caplog.records if record.name == "server.routers.ws" and record.levelno >= 40]
    assert len(errors) == 1 and errors[0].message == "WebSocket error"


@pytest.mark.parametrize("operation", ["answer_question", "approve_tool", "deny_tool"])
async def test_websocket_cannot_resolve_another_accounts_question_or_approval(runtime, monkeypatch, operation):
    manager, database, session, _, _, _ = runtime
    users = UserManager(database)
    monkeypatch.setattr(deps, "_user_manager", users)
    await bootstrap_first_account(database, username="first", password="password1", session_manager=manager)
    foreign = await users.create_user(username="second", password="password2")
    token = await users.issue_token(foreign["id"])
    question = await manager.create_pending_question(session.id, [{"question": "Private choice", "options": [{"label": "A"}]}])
    future = asyncio.get_running_loop().create_future()
    session._pending_approvals["private-approval"] = PendingApproval("Bash", {}, "private-approval", future)
    incoming, outgoing, task = await connect_ws(manager, token)
    try:
        payload = {"type": operation, "session_id": session.id, "question_id": question, "tool_use_id": "private-approval", "answers": [{"question": "Private choice", "selected": ["A"]}]}
        await incoming.put({"type": "websocket.receive", "text": json.dumps(payload)})
        await incoming.put({"type": "websocket.receive", "text": "bad json"})
        async with asyncio.timeout(5):
            while True:
                response = await outgoing.get()
                if response["type"] == "websocket.send" and json.loads(response["text"]).get("message") == "Invalid JSON":
                    break
        assert manager.has_pending_question(session.id, question)
        assert not future.done()
    finally:
        await disconnect_ws(incoming, task)


async def test_resume_response_is_404_if_session_deleted_during_publication(runtime):
    manager, _, session, _, _, _ = runtime
    entered, release = asyncio.Event(), asyncio.Event()

    async def slow_resume(event):
        if event["type"] == "dispatch_state" and not event["dispatch_state"]["paused"]:
            entered.set()
            await release.wait()

    manager.on_broadcast("slow-resume", slow_resume)
    async with session_client(manager, session) as client:
        request = asyncio.create_task(client.post(f"/api/sessions/{session.id}/dispatch/resume"))
        await asyncio.wait_for(entered.wait(), 5)
        await manager.delete_session(session.id)
        release.set()
        response = await asyncio.wait_for(request, 5)
    assert response.status_code == 404


async def test_detail_response_is_404_if_session_deleted_during_history_read(runtime, monkeypatch):
    manager, database, session, _, _, _ = runtime
    entered, release = gate_sql(monkeypatch, database, lambda sql: "FROM messages WHERE session_id" in sql)
    async with session_client(manager, session) as client:
        request = asyncio.create_task(client.get(f"/api/sessions/{session.id}"))
        await asyncio.wait_for(entered.wait(), 5)
        await manager.delete_session(session.id)
        release.set()
        response = await asyncio.wait_for(request, 5)
    assert response.status_code == 404


async def test_auto_archive_preserves_stop_and_new_return_during_database_write(runtime, monkeypatch):
    manager, database, parent, _, starts, events = runtime
    session = await manager.create_session(parent.agent_id, "Transient", parent.working_dir, origin="schedule", backend="codex")
    entered, release = gate_sql(monkeypatch, database, lambda sql: sql.startswith("UPDATE sessions SET archived"))
    archive = asyncio.create_task(manager.auto_archive_scheduled_session(session.id))
    await asyncio.wait_for(entered.wait(), 5)
    await manager.interrupt(session.id)
    await manager.start_message(session.id, "late background result", source="bg", delivery_id="bg:late")
    release.set()
    assert await asyncio.wait_for(archive, 5) is False
    assert manager.get_session(session.id) is session
    snapshot = manager.dispatch_snapshot(session.id)
    assert snapshot["paused"] is True
    assert [item["summary"] for item in snapshot["items"]] == ["late background result"]
    assert session.id in {row["id"] for row in await database.load_sessions()}
    assert starts.empty()
    assert not [event for event in events if event["type"] == "session_archived" and event["old_session_id"] == session.id]


@pytest.mark.parametrize("stop", ["timeout", "cancel"])
async def test_background_stops_surviving_group_after_parent_exits(background, tmp_path, stop):
    manager, session, delivered, _ = background
    child_pid = tmp_path / "descendant.pid"
    parent_pid = tmp_path / "parent.pid"
    child = f"import os,time; open({str(child_pid)!r},'w').write(str(os.getpid())); time.sleep(30)"
    parent = f"import os,subprocess,sys; open({str(parent_pid)!r},'w').write(str(os.getpid())); subprocess.Popen([sys.executable,'-c',{child!r}])"
    record = await manager.start_task(
        session_id=session.id, working_dir=str(tmp_path), command=shlex.join([sys.executable, "-c", parent]),
        timeout_seconds=1 if stop == "timeout" else 30, idle_timeout_seconds=0,
    )
    await wait_until(child_pid.exists)
    pid = int(child_pid.read_text())
    pgid = os.getpgid(pid)
    try:
        await wait_until(lambda: not process_alive(int(parent_pid.read_text())))
        await asyncio.sleep(0.05)
        if stop == "cancel":
            assert await manager.cancel_task(record.id) is True
        await wait_until(lambda: bool(delivered), timeout=12)
        assert delivered[0].status == ("failed" if stop == "timeout" else "cancelled")
        assert delivered[0].stop_reason == ("total_timeout" if stop == "timeout" else "user_cancel")
        assert not process_alive(pid)
    finally:
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            pass


async def test_background_total_limit_runs_while_database_registration_waits(background, runtime, monkeypatch, tmp_path):
    manager, session, delivered, _ = background
    _, database, _, _, _, _ = runtime
    entered, release = gate_sql(monkeypatch, database, lambda sql: sql.startswith("INSERT INTO bg_tasks"))
    pid_file = tmp_path / "registering.pid"
    code = f"import os,time; open({str(pid_file)!r},'w').write(str(os.getpid())); print('started',flush=True); time.sleep(30)"
    creation = asyncio.create_task(manager.start_task(
        session_id=session.id, working_dir=str(tmp_path), command=shlex.join([sys.executable, "-u", "-c", code]),
        timeout_seconds=1, idle_timeout_seconds=0,
    ))
    await asyncio.wait_for(entered.wait(), 5)
    await wait_until(pid_file.exists)
    pid = int(pid_file.read_text())
    pgid = os.getpgid(pid)
    try:
        await asyncio.sleep(1.2)
        assert not process_alive(pid)
        assert delivered == []
        release.set()
        record = await asyncio.wait_for(creation, 5)
        await wait_until(lambda: bool(delivered))
        assert delivered[0].id == record.id
        assert (delivered[0].status, delivered[0].stop_reason) == ("failed", "total_timeout")
    finally:
        release.set()
        await creation
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            pass


async def test_cancelled_background_registration_reaps_process_without_terminal_receipt(background, runtime, monkeypatch, tmp_path):
    manager, session, delivered, broadcasts = background
    _, database, _, _, _, _ = runtime
    entered, _ = gate_sql(monkeypatch, database, lambda sql: sql.startswith("INSERT INTO bg_tasks"))
    pid_file = tmp_path / "aborted-registration.pid"
    code = f"import os,time; open({str(pid_file)!r},'w').write(str(os.getpid())); time.sleep(30)"
    creation = asyncio.create_task(manager.start_task(
        session_id=session.id, working_dir=str(tmp_path), command=shlex.join([sys.executable, "-c", code]),
        idle_timeout_seconds=0,
    ))
    await asyncio.wait_for(entered.wait(), 5)
    await wait_until(pid_file.exists)
    pid = int(pid_file.read_text())
    creation.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(creation, 8)
    assert not process_alive(pid)
    assert await manager.list_tasks(session.id) == []
    assert delivered == broadcasts == []


async def test_stop_during_running_publication_releases_status_as_well_as_dispatch(runtime):
    manager, _, session, _, starts, events = runtime
    entered, release = asyncio.Event(), asyncio.Event()

    async def slow_client(event):
        if event["type"] == "status" and event.get("status") == "running":
            entered.set()
            await release.wait()

    manager.on_broadcast("slow-running-client", slow_client)
    await manager.start_message(session.id, "request")
    await asyncio.wait_for(entered.wait(), 5)
    await manager.interrupt(session.id)
    await wait_dispatch_idle(manager, session.id)
    assert manager.get_session(session.id).status.value == "idle"
    assert manager.dispatch_snapshot(session.id)["paused"] is True
    assert starts.empty()
    outcomes = [event for event in events if event["type"] == "result"]
    assert len(outcomes) == 1
    assert outcomes[0]["content"]["status"] == "interrupted"


async def test_stop_of_unstarted_delegation_preserves_round_until_resume(runtime):
    manager, _, parent, delegations, starts, events = runtime
    stopped = asyncio.Event()

    async def stop_on_claim(event):
        if event["type"] == "dequeued" and event["session_id"] != parent.id and not stopped.is_set():
            await manager.interrupt(event["session_id"])
            stopped.set()

    manager.on_broadcast("stop-delegation-claim", stop_on_claim)
    round_ = await delegations.start_delegation(
        parent_session_id=parent.id, agent_name="Review worker", request="Review once",
    )
    await asyncio.wait_for(stopped.wait(), 5)
    assert round_.state == "running"
    assert manager.dispatch_snapshot(parent.id)["items"] == []
    assert manager.dispatch_snapshot(round_.delegation_id)["paused"] is True
    assert len(manager.dispatch_snapshot(round_.delegation_id)["items"]) == 1
    assert starts.empty()
    assert not [event for event in events if event["session_id"] == round_.delegation_id and event["type"] in {"error", "result"}]
    await manager.resume_dispatch(round_.delegation_id)
    await asyncio.wait_for(starts.get(), 5)
    async with asyncio.timeout(5):
        while round_.state == "running":
            await asyncio.sleep(0.01)
    assert round_.state == "completed"
    assert starts.empty()
    replies = manager.dispatch_snapshot(parent.id)["items"]
    assert len(replies) == 1
    assert replies[0]["summary"].startswith("[agent-reply:")


async def test_queued_return_keeps_its_owner_when_deleted_during_dispatch_publication(runtime):
    manager, database, _, _, _, _ = runtime
    owner = await database.create_user(
        user_id="review-owner", username="review-owner", password_hash="unused",
        created_at=datetime.now(UTC).isoformat(),
    )
    agent = await AgentManager(database).create_agent(name="Owned queue", user_id=owner["id"])
    session = await manager.create_session(agent["id"], "Private pending reply", None, backend="codex")
    await manager.interrupt(session.id)
    entered, release = asyncio.Event(), asyncio.Event()
    owned, foreign = [], []
    published = asyncio.Event()

    async def slow_client(event):
        if event["type"] == "dispatch_state" and event["session_id"] == session.id and event["dispatch_state"]["items"]:
            entered.set()
            await release.wait()

    async def owned_client(event):
        owned.append(event)
        if event["type"] == "queued":
            published.set()

    async def foreign_client(event):
        foreign.append(event)

    manager.on_broadcast("slow-private-dispatch", slow_client, user_id=owner["id"])
    manager.on_broadcast("owned-private-dispatch", owned_client, user_id=owner["id"])
    manager.on_broadcast("foreign-private-dispatch", foreign_client, user_id="other-account")
    queuing = asyncio.create_task(manager.start_message(session.id, "Private background reply", source="bg"))
    await asyncio.wait_for(entered.wait(), 5)
    assert await manager.delete_session(session.id)
    release.set()
    await queuing
    await asyncio.wait_for(published.wait(), 5)
    assert not [event for event in foreign if event.get("session_id") == session.id and event["type"] in {"dispatch_state", "queued", "dequeued"}]
    assert len([event for event in owned if event["type"] == "queued"]) == 1


async def test_overall_cap_expires_in_retry_backoff_before_another_cli_can_start(runtime, monkeypatch):
    manager, _, session, _, starts, events = runtime
    monkeypatch.setattr(settings, "turn_idle_timeout_seconds", 0)
    monkeypatch.setattr(settings, "turn_max_seconds", 0.1)
    invocations = []

    def argv(context):
        invocations.append(context)
        if len(invocations) == 1:
            payload = {"type": "turn.failed", "error": {"message": "Model overloaded", "codex_error_info": "server_overloaded"}}
        else:
            payload = {"type": "turn.completed", "usage": {}}
        return [sys.executable, "-u", "-c", f"import json; print(json.dumps({payload!r}), flush=True)"], {"cwd": context.working_dir}

    harness = get_harness("codex")
    monkeypatch.setattr(harness, "profile", replace(harness.profile, build_turn_argv=argv, prepare_resume=None))
    began = time.monotonic()
    await manager.start_message(session.id, "Capacity request")
    await wait_dispatch_idle(manager, session.id)
    assert time.monotonic() - began < 0.8
    assert len(invocations) == 1
    assert starts.empty()
    outcomes = [event for event in events if event["type"] == "result"]
    assert len(outcomes) == 1
    assert outcomes[0]["content"]["status"] == "failed"
    assert outcomes[0]["code"] == "turn_timeout"
    assert "maximum duration" in outcomes[0]["content"]["message"]


async def test_follow_up_of_deleted_child_does_not_replace_its_finished_round(runtime):
    manager, _, parent, delegations, starts, _ = runtime
    original = await delegations.start_delegation(
        parent_session_id=parent.id, agent_name="Review worker", request="Initial review",
    )
    await asyncio.wait_for(starts.get(), 5)
    async with asyncio.timeout(5):
        while original.state == "running":
            await asyncio.sleep(0.01)
    assert original.state == "completed"
    if manager.get_session(original.delegation_id) is None:
        await manager.unarchive_session(original.delegation_id)
    else:
        await wait_dispatch_idle(manager, original.delegation_id)
    assert await manager.delete_session(original.delegation_id)
    with pytest.raises(DelegationError, match="is gone") as rejected:
        await delegations.follow_up_delegation(
            parent_session_id=parent.id, delegation_id=original.delegation_id, request="Another review",
        )
    assert rejected.value.status_code == 409
    assert delegations.get_delegation(original.delegation_id) is original
    assert not delegations.has_active_delegation_for_parent(parent.id)
    assert len(delegations.list_delegations(parent.id)) == 1


async def completed_delegation(runtime):
    manager, _, parent, delegations, starts, _ = runtime
    original = await delegations.start_delegation(
        parent_session_id=parent.id, agent_name="Review worker", request="Initial review",
    )
    await asyncio.wait_for(starts.get(), 5)
    async with asyncio.timeout(5):
        while original.state == "running":
            await asyncio.sleep(0.01)
    if manager.get_session(original.delegation_id) is not None:
        await wait_dispatch_idle(manager, original.delegation_id)
        await manager.archive_session(original.delegation_id, replace=False)
    return original


async def test_cancelled_follow_up_setup_restores_original_finished_round(runtime):
    manager, _, parent, delegations, starts, _ = runtime
    original = await completed_delegation(runtime)
    entered, release = asyncio.Event(), asyncio.Event()

    async def slow_client(event):
        if event["type"] == "session_unarchived" and event["session_id"] == original.delegation_id:
            entered.set()
            await release.wait()

    manager.on_broadcast("slow-reopen", slow_client)
    following = asyncio.create_task(delegations.follow_up_delegation(
        parent_session_id=parent.id, delegation_id=original.delegation_id, request="Cancelled setup",
    ))
    await asyncio.wait_for(entered.wait(), 5)
    following.cancel()
    with pytest.raises(asyncio.CancelledError):
        await following
    assert delegations.get_delegation(original.delegation_id) is original
    assert not delegations.has_active_delegation_for_parent(parent.id)
    assert starts.empty()
    assert len(manager.dispatch_snapshot(parent.id)["items"]) == 1


async def test_cancelled_reopen_cannot_overwrite_a_newer_follow_up(runtime):
    manager, _, parent, delegations, starts, _ = runtime
    original = await completed_delegation(runtime)
    entered, release = asyncio.Event(), asyncio.Event()

    async def slow_client(event):
        if event["type"] == "session_unarchived" and event["session_id"] == original.delegation_id and not entered.is_set():
            entered.set()
            await release.wait()

    manager.on_broadcast("slow-first-reopen", slow_client)
    following = asyncio.create_task(delegations.follow_up_delegation(
        parent_session_id=parent.id, delegation_id=original.delegation_id, request="Cancelled round",
    ))
    await asyncio.wait_for(entered.wait(), 5)
    cancelled = delegations.get_delegation(original.delegation_id)
    await delegations.cancel_delegation(cancelled.delegation_id, work_id=cancelled.work_id)
    fresh = await delegations.follow_up_delegation(
        parent_session_id=parent.id, delegation_id=original.delegation_id, request="Independent new review",
    )
    await asyncio.wait_for(starts.get(), 5)
    release.set()
    assert await following is cancelled
    assert cancelled.state == "cancelled"
    assert delegations.get_delegation(original.delegation_id) is fresh
    assert fresh.work_id not in {original.work_id, cancelled.work_id}


async def test_follow_up_returns_before_blocked_notification_and_preserves_accepted_work(runtime):
    manager, _, parent, delegations, starts, _ = runtime
    original = await completed_delegation(runtime)
    entered, release = asyncio.Event(), asyncio.Event()

    async def slow_client(event):
        if event["type"] == "dispatch_state" and event["session_id"] == original.delegation_id and event["dispatch_state"]["items"] and not entered.is_set():
            entered.set()
            await release.wait()

    manager.on_broadcast("slow-follow-up-receipt", slow_client)
    following = asyncio.create_task(delegations.follow_up_delegation(
        parent_session_id=parent.id, delegation_id=original.delegation_id, request="held accepted review",
    ))
    await asyncio.wait_for(entered.wait(), 5)
    await asyncio.wait_for(starts.get(), 5)
    accepted = delegations.get_delegation(original.delegation_id)
    assert await following is accepted
    assert following.cancel() is False
    assert accepted.state == "running"
    assert delegations.get_delegation(original.delegation_id) is accepted
    assert manager.has_pending_or_active_work(original.delegation_id, accepted.work_id)
    release.set()
    await delegations.cancel_delegation(accepted.delegation_id, work_id=accepted.work_id)


async def test_overall_timeout_during_process_start_reports_timeout_not_user_stop(runtime, monkeypatch):
    manager, _, session, _, _, events = runtime
    monkeypatch.setattr(settings, "turn_idle_timeout_seconds", 0)
    monkeypatch.setattr(settings, "turn_max_seconds", 0.1)
    original_spawn = asyncio.create_subprocess_exec

    async def slow_spawn(*args, **kwargs):
        process = await original_spawn(*args, **kwargs)
        if args[0] == sys.executable:
            await asyncio.sleep(0.25)
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", slow_spawn)
    await manager.start_message(session.id, "held startup")
    await wait_dispatch_idle(manager, session.id)
    outcomes = [event for event in events if event["type"] == "result"]
    assert len(outcomes) == 1
    assert outcomes[0]["content"]["status"] == "failed"
    assert outcomes[0]["code"] == "turn_timeout"
    assert "maximum duration" in outcomes[0]["content"]["message"]


async def test_elapsed_cap_retires_a_reusable_run_before_its_success_can_be_held(runtime, monkeypatch):
    manager, database, parent, _, starts, events = runtime
    agent = await database.get_system_agent()
    session = await manager.create_session(agent["id"], "Held cap", parent.working_dir, backend="claude-code")
    monkeypatch.setattr(settings, "turn_idle_timeout_seconds", 0)
    monkeypatch.setattr(settings, "turn_max_seconds", 1)
    offset = [0.0]
    monkeypatch.setattr(turn_module, "time", SimpleNamespace(monotonic=lambda: time.monotonic() + offset[0]))

    async def advance_clock(event):
        if event["type"] == "assistant_text" and event["session_id"] == session.id:
            offset[0] = 2.0

    manager.on_broadcast("advance-overall-clock", advance_clock)

    def argv(context):
        starts.put_nowait(context)
        code = "import json,sys\n"
        code += "for line in sys.stdin:\n"
        code += " print(json.dumps({'type':'system','subtype':'init','session_id':'held-cap'}),flush=True)\n"
        code += " print(json.dumps({'type':'assistant','message':{'role':'assistant','content':[{'type':'text','text':'Ready.'}]}}),flush=True)\n"
        code += " print(json.dumps({'type':'result','subtype':'success','session_id':'held-cap','result':'Done.','is_error':False}),flush=True)\n"
        return [sys.executable, "-u", "-c", code], {"cwd": context.working_dir}

    harness = get_harness("claude-code")
    monkeypatch.setattr(harness, "profile", replace(harness.profile, build_turn_argv=argv, prepare_resume=None))
    await manager.start_message(session.id, "Complete at the deadline")
    context = await asyncio.wait_for(starts.get(), 5)
    await wait_dispatch_idle(manager, session.id)
    scope = verify(next(entry.credential for entry in context.mcp_servers if entry.key == "ask"))
    outcome = next(event for event in events if event["type"] == "result" and event["session_id"] == session.id)
    assert outcome["content"]["status"] == "failed"
    assert outcome["code"] == "turn_timeout"
    assert not scope_is_live(scope)
