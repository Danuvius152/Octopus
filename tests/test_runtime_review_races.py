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
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from server import deps, mcp_http
from server.agent_manager import AgentManager
from server.bg_tasks import BgTaskManager
from server.config import settings
from server.database import Database
from server.delegations import DelegationError, DelegationManager
from server.deps import get_session_manager
from server.harness import get_harness
from server.mcp_identity import mint, register_run, revoke_run, scope_is_live, verify
from server.routers.sessions import router as sessions_router
from server.routers.ws import router as ws_router
from server.sessions import SessionManager
from server.sessions import turns as turn_module
from server.sessions.submission import ReturnRefused
from server.users import UserManager
from tests.accounts import create_account


async def session_client(manager, session):
    app = FastAPI()
    app.include_router(sessions_router)
    app.dependency_overrides[get_session_manager] = lambda: manager
    bearer = await deps.get_user_manager().issue_token(session.user_id)
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test", headers={"Authorization": f"Bearer {bearer}"},
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


def gate_background_spawn(monkeypatch):
    """Hold the OS handle after spawn; ownership was already committed."""
    from server import bg_tasks

    entered, release = asyncio.Event(), asyncio.Event()
    spawn = asyncio.create_subprocess_exec

    async def delayed(*args, **kwargs):
        proc = await spawn(*args, **kwargs)
        entered.set()
        await release.wait()
        return proc

    monkeypatch.setattr(bg_tasks.asyncio, "create_subprocess_exec", delayed)
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
        return ReturnRefused("source_withdrawn")

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
async def runtime(monkeypatch, tmp_path, short_process_shutdown):
    monkeypatch.setattr(settings, "users_root", str(tmp_path / "users"))
    monkeypatch.setattr(settings, "agents_dir", str(tmp_path / "legacy-agents"))
    database = Database(":memory:")
    await database.initialize()
    owner, _ = await create_account(database, extra_roots=[tmp_path])
    manager = SessionManager()
    await manager.initialize(database)
    agents = AgentManager(database)
    agent = await database.get_system_agent()
    await agents.create_agent(name="Review worker", backend="codex", user_id=owner["id"])
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
            await manager.await_session_execution(session.id, timeout=5)
        delegations.shutdown()
        await manager.stop_all_held_processes()
        await database.close()


async def wait_dispatch_idle(manager, session_id):
    async with asyncio.timeout(5):
        while (snapshot := manager.dispatch_snapshot(session_id))["active_request_id"] is not None or snapshot["items"]:
            await asyncio.sleep(0.01)


@pytest.mark.parametrize("diagnostic,code,preparations", [
    ("503 service unavailable", None, 2),
    ("Your authentication token has expired", "auth_expired", 1),
    ("controller protocol identity mismatch", "preparation_failed", 1),
])
async def test_preparation_failures_use_bounded_classification(runtime, monkeypatch, diagnostic, code, preparations):
    manager, _, session, _, _, events = runtime
    seed = await manager.start_message(session.id, "Create the original history before testing resume preparation")
    assert (await asyncio.wait_for(seed.wait(), 5)).outcome.status == "success"
    await wait_dispatch_idle(manager, session.id)
    events.clear()
    calls = []
    async def prepare(*args):
        calls.append(args)
        if len(calls) == 1:
            raise RuntimeError(diagnostic)
    harness = get_harness("codex")
    monkeypatch.setattr(harness, "profile", replace(harness.profile, prepare_resume=prepare))
    monkeypatch.setattr(manager, "_TRANSIENT_RETRY_BASE_DELAY", 0)
    await manager.start_message(session.id, "test preparation")
    await wait_dispatch_idle(manager, session.id)
    outcomes = [event for event in events if event["type"] == "result"]
    assert len(outcomes) == 1
    assert outcomes[0]["content"]["code"] == code
    assert outcomes[0]["content"]["status"] == ("success" if code is None else "failed")
    assert len(calls) == preparations


async def test_corrupt_protocol_reports_incomplete_success_without_reexecution(runtime, monkeypatch):
    manager, database, session, _, starts, events = runtime
    def argv(context):
        starts.put_nowait(context)
        return [sys.executable, "-u", "-c", "print('{broken');print('{\"type\":\"item.completed\",\"item\":{\"type\":\"agent_message\",\"text\":\"Executed once\"}}');print('{\"type\":\"turn.completed\",\"usage\":{}}')"], {"cwd": context.working_dir}
    harness = get_harness("codex")
    monkeypatch.setattr(harness, "profile", replace(harness.profile, build_turn_argv=argv))
    await manager.start_message(session.id, "execute once")
    await wait_dispatch_idle(manager, session.id)
    outcomes = [event["content"] for event in events if event["type"] == "result"]
    assert len(outcomes) == 1 and outcomes[0]["status"] == "success"
    assert outcomes[0]["recording_integrity"] == "incomplete"
    assert outcomes[0]["recording_issues"]["invalid_json"] == 1
    assert starts.qsize() == 1
    stored = await database.load_messages(session.id)
    assert stored[-1]["content"]["recording_integrity"] == "incomplete"
    location = stored[-1]["content"]["recording_locations"][0]
    assert location["kind"] == "invalid_json" and location["line"] == 1
    assert set(location) == {"attempt_id", "kind", "line", "characters", "sha256"}


@pytest.mark.parametrize("diagnostic,expected,spawns", [
    ("Your authentication token has expired", "auth_expired", 1),
    ("503 service unavailable", "transient_exhausted", 3),
    ("insufficient quota", None, 1),
])
async def test_long_execution_diagnostics_classify_tail_without_exposing_stderr(runtime, monkeypatch, diagnostic, expected, spawns):
    manager, _, session, _, starts, events = runtime
    secret = "secret-material-keep-private"
    def argv(context):
        starts.put_nowait(context)
        stderr = "access_token=" + secret + "\n" + "diagnostic " * 1000 + diagnostic
        code = (f"import sys;sys.stderr.write({stderr!r});"
                "print('{\"type\":\"thread.started\",\"thread_id\":\"diagnostic-thread\"}');"
                "print('{\"type\":\"turn.failed\",\"error\":{\"message\":\"execution failed\"}}')")
        return [sys.executable, "-u", "-c", code], {"cwd": context.working_dir}
    harness = get_harness("codex")
    monkeypatch.setattr(harness, "profile", replace(harness.profile, build_turn_argv=argv, prepare_resume=None))
    monkeypatch.setattr(manager, "_TRANSIENT_RETRY_BASE_DELAY", 0)
    receipt = await manager.start_message(session.id, "classify full evidence")
    completion = await asyncio.wait_for(receipt.wait(), 5)
    assert completion.outcome.status == "failed"
    assert completion.outcome.code == expected
    assert starts.qsize() == spawns
    assert secret not in json.dumps(events)
    assert len(completion.outcome.message or "") <= 2001


async def test_first_app_timeout_recovers_late_completed_attempt_without_resubmission(runtime, monkeypatch, tmp_path):
    from server import app_agent as app_module
    from server.app_agent import AppAgentError, AppAgentManager
    from server.applications import ApplicationManager

    manager, db, _, _, starts, _ = runtime
    monkeypatch.setattr(settings, "applications_dir", str(tmp_path / "apps"))
    monkeypatch.setattr(app_module, "ASK_TIMEOUT_SECONDS", .05)
    monkeypatch.setattr(manager, "_TRANSIENT_RETRY_BASE_DELAY", 0)
    apps = ApplicationManager()
    apps.bind(session_mgr=manager, db=db)
    app_agent = AppAgentManager()
    app_agent.bind(manager, db, apps)
    worker = next(a for a in await db.load_agents() if a["name"] == "Review worker")
    await db.save_application(app_id="timeout-app", name="Recovery app", description="Recover original requests",
        agent_id=worker["id"], session_id=None, app_dir=str(tmp_path / "apps" / "recovery"),
        entrypoint="index.html", status="ready", created_at="now", updated_at="now", user_id=worker["user_id"])
    row = await db.get_application("timeout-app")
    release = tmp_path / "release-app-turn"
    def argv(context):
        starts.put_nowait(context)
        code = "import json,time,os\n"
        code += "print(json.dumps({'type':'thread.started','thread_id':'app-thread'}),flush=True)\n"
        if starts.qsize() == 1:
            code += "print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':'obsolete'}}),flush=True)\n"
            code += "print(json.dumps({'type':'turn.failed','error':{'message':'503 service unavailable'}}),flush=True)\n"
        else:
            code += f"while not os.path.exists({str(release)!r}): time.sleep(.01)\n"
            code += "for text in ['answer one','answer two']: print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':text}}),flush=True)\n"
            code += "print(json.dumps({'type':'turn.completed','usage':{}}),flush=True)\n"
        return [sys.executable, "-u", "-c", code], {"cwd": context.working_dir}
    harness = get_harness("codex")
    monkeypatch.setattr(harness, "profile", replace(harness.profile, build_turn_argv=argv))
    try:
        with pytest.raises(AppAgentError) as exc:
            await app_agent.ask(row, message="first request")
        recovery = exc.value.recovery
        sid, rid = recovery["conversation_id"], recovery["request_id"]
        assert (await app_agent.get_request_status(row["id"], sid, rid))["state"] in {"running", "queued"}
        release.touch()
        await wait_dispatch_idle(manager, sid)
        status = await app_agent.get_request_status(row["id"], sid, rid)
        assert status["state"] == "completed"
        assert status["reply"] == "answer one\n\nanswer two"
        assert starts.qsize() == 2
        assert len(app_agent.list_conversations(row["id"])) == 1
    finally:
        release.touch()
        app_agent.shutdown()
        apps.shutdown()




async def connect_ws(manager, token, *, authorized=True):
    app = FastAPI()
    app.include_router(ws_router)
    app.dependency_overrides[get_session_manager] = lambda: manager
    incoming, outgoing = asyncio.Queue(), asyncio.Queue()
    await incoming.put({"type": "websocket.connect"})
    scope = {"type": "websocket", "asgi": {"version": "3.0"}, "scheme": "ws", "path": "/ws", "raw_path": b"/ws", "query_string": f"token={token}".encode(), "headers": [], "client": ("127.0.0.1", 1), "server": ("127.0.0.1", 8000), "subprotocols": []}
    task = asyncio.create_task(app(scope, incoming.get, outgoing.put))
    first = await asyncio.wait_for(outgoing.get(), 5)
    assert first["type"] == "websocket.accept"
    if not authorized:
        refused = await asyncio.wait_for(outgoing.get(), 5)
        assert refused["type"] == "websocket.close" and refused["code"] == 4001
    return incoming, outgoing, task


async def disconnect_ws(incoming, task):
    if not task.done():
        await incoming.put({"type": "websocket.disconnect", "code": 1000})
    await asyncio.wait_for(task, 5)


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("accounts_exist", [False, True])
async def test_ownerless_scope_is_rejected_before_and_after_setup(tmp_path, monkeypatch, legacy, accounts_exist):
    database = Database(":memory:")
    await database.initialize()
    users = UserManager(database)
    monkeypatch.setattr(deps, "_user_manager", users)
    manager = SessionManager()
    await manager.initialize(database)
    if accounts_exist:
        await users.create_user(username="first", password="password1")
    lease = None if legacy else "retained-ownerless-lease"
    if lease:
        register_run(lease, "retained-ownerless-session", None, None)
    token = mint("retained-ownerless-session", run_lease_id=lease)
    app = FastAPI()
    app.include_router(sessions_router)
    app.dependency_overrides[get_session_manager] = lambda: manager
    servers = {"ask": mcp_http.build_servers(fresh=True)["ask"]}
    mcp_http.mount_all(app, servers)
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json, text/event-stream"}
    initialize = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "scope-review", "version": "1"}}}
    try:
        async with mcp_http.lifespan(app, servers), httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8000", headers=headers) as client:
            assert (await client.get("/api/sessions")).status_code == 401
            assert await deps.scope_user_id_for(token) == (False, None)
            assert (await client.post("/mcp/ask/mcp", json=initialize)).status_code == 401
            incoming, _, socket = await connect_ws(manager, token, authorized=False)
            await disconnect_ws(incoming, socket)
            assert await database.load_sessions() == []

            owned = await users.create_user(username="owned", password="password2")
            owned_scope = mint("owned-diagnostic-session", user_id=owned["id"])
            client.headers["Authorization"] = "Bearer " + owned_scope
            assert (await client.get("/api/sessions")).status_code == 200
            assert (await client.post("/mcp/ask/mcp", json=initialize)).status_code == 200
            bearer = await users.issue_token(owned["id"])
            incoming, _, socket = await connect_ws(manager, bearer)
            await disconnect_ws(incoming, socket)
    finally:
        revoke_run(lease)
        await database.close()




async def test_closing_old_work_retires_its_held_backend_while_new_input_is_claiming(runtime, monkeypatch, tmp_path):
    from server.sessions.client_submissions import (
        submission_status,
        submit_client_message,
    )

    manager, database, parent, _, starts, _ = runtime
    pid_file = tmp_path / "held-old-work.pid"

    def argv(context):
        starts.put_nowait(context)
        code = f"import json,os,sys; open({str(pid_file)!r},'w').write(str(os.getpid()))\n"
        code += "for line in sys.stdin:\n"
        code += " print(json.dumps({'type':'system','subtype':'init','session_id':'held-work-thread'}),flush=True)\n"
        code += " print(json.dumps({'type':'result','subtype':'success','session_id':'held-work-thread','is_error':False}),flush=True)\n"
        return [sys.executable, "-u", "-c", code], {"cwd": context.working_dir}

    harness = get_harness("claude-code")
    monkeypatch.setattr(harness, "profile", replace(harness.profile, build_turn_argv=argv, prepare_resume=None))
    session = await manager.create_session(parent.agent_id, "Held old work", str(tmp_path), backend="claude-code")
    old_work = f"held-work:{session.id}"
    await manager.start_message(session.id, "Finish the original work", work_id=old_work)
    original_context = await asyncio.wait_for(starts.get(), 5)
    old_scope = verify(next(entry.credential for entry in original_context.mcp_servers if entry.key == "ask"))
    await wait_dispatch_idle(manager, session.id)
    old_pid = int(pid_file.read_text())
    backend = session._run.backend
    assert backend is not None and backend.work_id == old_work and process_alive(old_pid)
    assert scope_is_live(old_scope) and old_scope.user_id == session.user_id
    stopped = asyncio.Event()
    stop = backend.stop

    async def observed_stop():
        await stop()
        stopped.set()

    monkeypatch.setattr(backend, "stop", observed_stop)
    entered, release = gate_sql(
        monkeypatch, database, lambda sql: sql.startswith("UPDATE client_submissions SET execution_state='running'"),
    )
    submitting = asyncio.create_task(submit_client_message(
        manager, session.id, "Continue with independent human input", [], "next-held-work", wiki_mode=None,
    ))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        assert starts.empty(), "the next input has not reached model creation"
        affected = [await manager.close_work(old_work)]
        await manager.finish_work_cancellation(affected)
        await asyncio.wait_for(stopped.wait(), 2)
        assert not process_alive(old_pid)
        assert not scope_is_live(old_scope)
        assert starts.empty(), "retiring the old backend must not dispatch the blocked input"
    finally:
        release.set()
        result = await asyncio.wait_for(submitting, 5)
    assert result["accepted"]
    context = await asyncio.wait_for(starts.get(), 5)
    scope = verify(next(entry.credential for entry in context.mcp_servers if entry.key == "ask"))
    assert scope.work_id is None and scope.user_id == session.user_id and context.resume_id == "held-work-thread"
    await wait_dispatch_idle(manager, session.id)
    assert int(pid_file.read_text()) != old_pid
    status = await submission_status(manager, session.id, "next-held-work")
    assert status is not None and status["execution"]["state"] == "done"




async def test_wiki_held_process_reuses_transport_with_fresh_turn_authority(runtime, monkeypatch, tmp_path):
    from server.wiki.store import WikiError
    from server.wiki.tasks import tasks

    manager, _, parent, _, starts, events = runtime
    release = tmp_path / "release-wiki-turn"
    def argv(context):
        starts.put_nowait(context)
        code = "import json,sys,time,os\n"
        code += "for line in sys.stdin:\n"
        code += " print(json.dumps({'type':'assistant','message':{'content':[{'type':'text','text':'working'}]}}),flush=True)\n"
        code += f" while not os.path.exists({str(release)!r}): time.sleep(.01)\n"
        code += " print(json.dumps({'type':'result','subtype':'success','session_id':'wiki-held-thread','is_error':False}),flush=True)\n"
        return [sys.executable, "-u", "-c", code], {"cwd": context.working_dir}
    harness = get_harness("claude-code")
    monkeypatch.setattr(harness, "profile", replace(harness.profile, build_turn_argv=argv, prepare_resume=None))
    session = await manager.create_session(parent.agent_id, "Wiki held", str(tmp_path), backend="claude-code")
    first = await manager.start_message(session.id, "first", wiki_mode="read")
    context = await asyncio.wait_for(starts.get(), 5)
    scope = verify(next(entry.credential for entry in context.mcp_servers if entry.key == "wiki"))
    await wait_until(lambda: any(e.get("type") == "assistant_text" and e.get("session_id") == session.id for e in events))
    old_task = tasks.require(scope)
    assert old_task.request_id == first.request_id
    release.touch()
    assert (await asyncio.wait_for(first.wait(), 5)).state == "completed"
    await wait_dispatch_idle(manager, session.id)
    with pytest.raises(WikiError):
        tasks.require(scope)
    release.unlink()
    events.clear()
    second = await manager.start_message(session.id, "second", wiki_mode="read")
    await wait_until(lambda: any(e.get("type") == "assistant_text" and e.get("session_id") == session.id for e in events))
    assert starts.empty(), "a changed Wiki task must not respawn the held process"
    assert tasks.require(scope).request_id == second.request_id
    assert tasks.resolve(scope) is not old_task
    release.touch()
    assert (await asyncio.wait_for(second.wait(), 5)).state == "completed"
    await wait_dispatch_idle(manager, session.id)
    with pytest.raises(WikiError):
        tasks.require(scope)


async def test_blocked_question_answer_publication_stays_with_its_original_account(runtime, monkeypatch):
    manager, database, session, _, _, _ = runtime
    users = UserManager(database)
    monkeypatch.setattr(deps, "_user_manager", users)
    foreign = await users.create_user(username="second", password="password2")
    # Hold delivery after the answer commits, then verify the original fact
    # and both accounts' actual subscriptions while the prefix is blocked.
    publication_entered, publication_release = asyncio.Event(), asyncio.Event()

    async def hold_publication(event):
        if event["type"] == "question_answer":
            publication_entered.set()
            await publication_release.wait()

    owner_events, foreign_events = [], []
    creation_delivered, answer_delivered = asyncio.Event(), asyncio.Event()

    async def receive(event):
        owner_events.append(event)
        if event["type"] == "question_request":
            creation_delivered.set()

    async def observe_answer_prefix(event):
        if event["type"] == "question_answer":
            answer_delivered.set()

    async def foreign_receive(event):
        foreign_events.append(event)

    manager.on_broadcast("hold-answer-publication", hold_publication)
    manager.on_broadcast("original-question-owner", receive, session.user_id)
    manager.on_broadcast("foreign-during-answer", foreign_receive, foreign["id"])
    manager.on_broadcast("original-answer-prefix", observe_answer_prefix, session.user_id)
    question = await manager.create_pending_question(session.id, [{"question": "Private choice", "options": [{"label": "A"}]}])
    await asyncio.wait_for(creation_delivered.wait(), 2)
    assert [event["type"] for event in owner_events] == ["question_request"]
    assert foreign_events == []
    answer = asyncio.create_task(manager.answer_question(session.id, question, [{"question": "Private choice", "text": "synthetic private answer"}]))
    await asyncio.wait_for(publication_entered.wait(), 5)
    try:
        messages = await database.load_messages(session.id)
        answers = [row for row in messages if row["type"] == "question_answer"]
        assert len(answers) == 1 and "synthetic private answer" in answers[0]["content"]
        assert not answer_delivered.is_set() and foreign_events == []
        publication_release.set()
        assert await asyncio.wait_for(answer, 5)
        await asyncio.wait_for(answer_delivered.wait(), 2)
        assert [event["type"] for event in owner_events] == ["question_request", "question_answer"]
        assert foreign_events == []
    finally:
        publication_release.set()
        await asyncio.gather(answer, return_exceptions=True)


async def test_archived_background_result_keeps_its_original_account(background, runtime, monkeypatch, tmp_path):
    bg, session, delivered, _ = background
    manager, database, _, _, _, _ = runtime
    users = UserManager(database)
    monkeypatch.setattr(deps, "_user_manager", users)
    gate = tmp_path / "complete-bg"
    code = f"import pathlib,time; gate=pathlib.Path({str(gate)!r});\nwhile not gate.exists(): time.sleep(.01)\nprint('synthetic private output')"
    record = await bg.start_task(session_id=session.id, command=shlex.join([sys.executable, "-c", code]), working_dir=str(tmp_path), idle_timeout_seconds=0)
    settlement = bg._running[record.id].task
    foreign = await users.create_user(username="second", password="password2")
    owner_events, foreign_events = [], []

    async def owner_receive(event):
        owner_events.append(event)

    async def foreign_receive(event):
        foreign_events.append(event)

    manager.on_broadcast("bg-owner", owner_receive, session.user_id)
    manager.on_broadcast("bg-foreign", foreign_receive, foreign["id"])
    await manager.archive_session(session.id, replace=False)
    gate.touch()
    await asyncio.wait_for(asyncio.shield(settlement), 5)
    assert [item.id for item in delivered] == [record.id]
    assert foreign_events == []
    assert [event["type"] for event in owner_events if event["type"] != "dispatch_state"] == [
        "session_archived", "bg_completed", "bg_delivery",
    ]
    assert owner_events[-1]["task_id"] == record.id
    assert owner_events[-1]["session_id"] == session.id and owner_events[-1]["delivery_disposition"] == "stopped"


@pytest.mark.parametrize("boundary", ["incoming", "broadcast", "unscoped_notice"])
@pytest.mark.parametrize("revocation", ["bearer", "account"])
async def test_connected_account_websocket_retires_when_authority_is_revoked(runtime, monkeypatch, boundary, revocation):
    manager, database, session, _, _, _ = runtime
    users = UserManager(database)
    monkeypatch.setattr(deps, "_user_manager", users)
    token = await users.issue_token(session.user_id)
    incoming, outgoing, task = await connect_ws(manager, token)
    try:
        if revocation == "account":
            await users.set_disabled(session.user_id, True)
        else:
            await users.revoke_token(token)
        if boundary == "incoming":
            await incoming.put({"type": "websocket.receive", "text": "bad json"})
        elif boundary == "broadcast":
            await manager._broadcast({"type": "assistant_text", "session_id": session.id, "content": "synthetic private output"}, user_id=session.user_id)
        else:
            await manager._broadcast({"type": "schedules_changed"})
        frame = await asyncio.wait_for(outgoing.get(), 5)
        assert frame["type"] == "websocket.close" and frame["code"] == 4001
    finally:
        await disconnect_ws(incoming, task)


async def test_outbound_retirement_during_incoming_request_is_normal_disconnect(runtime, monkeypatch, caplog):
    manager, database, session, _, _, _ = runtime
    users = UserManager(database)
    monkeypatch.setattr(deps, "_user_manager", users)
    token = await users.issue_token(session.user_id)
    revoked = False

    async def revoke_bearer_during_delivery(event):
        nonlocal revoked
        if event["type"] == "dispatch_state" and not revoked:
            revoked = True
            await users.revoke_token(token)

    manager.on_broadcast("revoke-during-ws-request", revoke_bearer_during_delivery)
    incoming, outgoing, task = await connect_ws(manager, token)
    try:
        await incoming.put({"type": "websocket.receive", "text": json.dumps({
            "type": "send_message", "session_id": session.id, "content": "A request crossing bearer revocation",
            "client_request_id": "revoke-during-request",
        })})
        frame = await asyncio.wait_for(outgoing.get(), 5)
        assert frame["type"] == "websocket.close" and frame["code"] == 4001
        await asyncio.wait_for(task, 5)
        assert not [record for record in caplog.records if record.name == "server.routers.ws" and record.levelno >= 40]
    finally:
        await disconnect_ws(incoming, task)


async def test_unretired_websocket_transport_error_remains_logged(runtime, caplog):
    manager, _, session, _, _, _ = runtime
    token = await deps.get_user_manager().issue_token(session.user_id)
    incoming, _, task = await connect_ws(manager, token)
    await incoming.put({"type": "unexpected-transport-frame"})
    await asyncio.wait_for(task, 5)
    errors = [record for record in caplog.records if record.name == "server.routers.ws" and record.levelno >= 40]
    assert len(errors) == 1 and errors[0].message == "WebSocket error"


async def test_websocket_cannot_resolve_another_accounts_question(runtime, monkeypatch):
    manager, database, session, _, _, _ = runtime
    users = UserManager(database)
    monkeypatch.setattr(deps, "_user_manager", users)
    foreign = await users.create_user(username="second", password="password2")
    token = await users.issue_token(foreign["id"])
    question = await manager.create_pending_question(session.id, [{"question": "Private choice", "options": [{"label": "A"}]}])
    incoming, outgoing, task = await connect_ws(manager, token)
    try:
        payload = {"type": "answer_question", "session_id": session.id, "question_id": question,
                   "answers": [{"question": "Private choice", "selected": ["A"]}],
                   "client_command_id": "foreign-answer", "request_id": None, "attempt_id": None}
        await incoming.put({"type": "websocket.receive", "text": json.dumps(payload)})
        await incoming.put({"type": "websocket.receive", "text": "bad json"})
        async with asyncio.timeout(5):
            while True:
                response = await outgoing.get()
                if response["type"] == "websocket.send" and json.loads(response["text"]).get("message") == "Invalid JSON":
                    break
        assert manager.has_pending_question(session.id, question)
    finally:
        await disconnect_ws(incoming, task)


async def test_resume_reply_does_not_wait_for_publication_and_later_delete_is_visible(runtime):
    manager, _, session, _, _, _ = runtime
    entered, release = asyncio.Event(), asyncio.Event()

    async def slow_resume(event):
        if event["type"] == "dispatch_state" and not event["dispatch_state"]["paused"]:
            entered.set()
            await release.wait()

    manager.on_broadcast("slow-resume", slow_resume)
    async with await session_client(manager, session) as client:
        request = asyncio.create_task(client.post(f"/api/sessions/{session.id}/dispatch/resume"))
        try:
            await asyncio.wait_for(entered.wait(), 5)
            response = await asyncio.wait_for(request, 2)
            assert response.status_code == 200
            assert not release.is_set()
            await manager.delete_session(session.id)
            assert (await client.get(f"/api/sessions/{session.id}")).status_code == 404
        finally:
            release.set()
            await asyncio.gather(request, return_exceptions=True)
    await asyncio.wait_for(manager.close_session_owners(), 2)


async def test_detail_response_is_404_if_session_deleted_during_history_read(runtime, monkeypatch):
    manager, database, session, _, _, _ = runtime
    entered, release = asyncio.Event(), asyncio.Event()
    load_messages = database.load_messages

    async def delayed_history(*args, **kwargs):
        rows = await load_messages(*args, **kwargs)
        if not entered.is_set():
            # The real SQL read has completed. Hold its response while deletion
            # preserves attachments and commits, without holding the DB writer.
            entered.set()
            await release.wait()
        return rows

    monkeypatch.setattr(database, "load_messages", delayed_history)
    async with await session_client(manager, session) as client:
        request = asyncio.create_task(client.get(f"/api/sessions/{session.id}"))
        await asyncio.wait_for(entered.wait(), 5)
        # A completed deletion must revoke this already loaded response too.
        deletion = asyncio.create_task(manager.delete_session(session.id))
        try:
            assert await asyncio.wait_for(deletion, 5)
            assert manager.get_session(session.id) is None
        finally:
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
    submission = asyncio.create_task(manager.start_message(
        session.id, "late background result", source="bg", delivery_id="bg:late",
    ))
    try:
        # Admission uses the same serialized DB as archive. Both operations
        # must be allowed to finish before checking the queued-return result.
        await asyncio.sleep(0)
        assert not submission.done()
        release.set()
        assert await asyncio.wait_for(archive, 5) is False
        assert (await asyncio.wait_for(submission, 5)).accepted
    finally:
        release.set()
        await asyncio.gather(archive, submission, return_exceptions=True)
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


async def test_background_total_limit_runs_while_started_publication_waits(background, runtime, monkeypatch, tmp_path):
    manager, session, delivered, _ = background
    entered, release = asyncio.Event(), asyncio.Event()
    broadcast = manager._broadcast_cb

    async def held_started(event):
        if event["type"] == "bg_started":
            entered.set()
            await release.wait()
        await broadcast(event)

    monkeypatch.setattr(manager, "_broadcast_cb", held_started)
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


async def test_cancelled_background_registration_retains_confirmed_row_without_starting_a_process(background, runtime, monkeypatch, tmp_path):
    manager, session, delivered, broadcasts = background
    _, database, _, _, _, _ = runtime
    entered, release = gate_sql(monkeypatch, database, lambda sql: sql.startswith("INSERT INTO bg_tasks"))
    pid_file = tmp_path / "aborted-registration.pid"
    code = f"import os,time; open({str(pid_file)!r},'w').write(str(os.getpid())); time.sleep(30)"
    creation = asyncio.create_task(manager.start_task(
        session_id=session.id, working_dir=str(tmp_path), command=shlex.join([sys.executable, "-c", code]),
        idle_timeout_seconds=0,
    ))
    await asyncio.wait_for(entered.wait(), 5)
    assert not pid_file.exists(), "uncommitted launch started an OS process"
    creation.cancel()
    try:
        # Cancelling its waiter does not cancel the captured SQL operation.
        # Let the real driver finish before expecting its caller to unwind.
        await asyncio.sleep(0)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(creation, 8)
    finally:
        release.set()
        await asyncio.gather(creation, return_exceptions=True)
    assert not pid_file.exists()
    registered = await manager.list_tasks(session.id)
    assert len(registered) == 1
    assert registered[0].session_id == session.id
    assert registered[0].command == shlex.join([sys.executable, "-c", code])
    assert registered[0].status == "interrupted" and registered[0].stop_reason == "startup_failed"
    assert registered[0].exit_code is None
    assert registered[0].stdout == registered[0].stderr == ""
    assert delivered == broadcasts == []


async def test_stop_during_running_publication_retires_resources_before_delivering_its_prefix(runtime, monkeypatch):
    from server.harness.run import HarnessRun

    manager, database, session, _, starts, events = runtime
    entered, release = asyncio.Event(), asyncio.Event()
    resources = []
    spawn = HarnessRun.start

    async def capture_original_start(run, *args, **kwargs):
        resources.append(run)
        return await spawn(run, *args, **kwargs)

    monkeypatch.setattr(HarnessRun, "start", capture_original_start)

    async def slow_client(event):
        if event["type"] == "status" and event.get("status") == "running":
            entered.set()
            await release.wait()

    manager.on_broadcast("slow-running-client", slow_client)
    receipt = await manager.start_message(session.id, "held request")
    try:
        await asyncio.wait_for(entered.wait(), 5)
        await asyncio.wait_for(manager.interrupt(session.id), 2)
        await manager.await_session_execution(session.id, timeout=5)
        assert manager.get_session(session.id).status.value == "idle"
        assert manager.dispatch_snapshot(session.id)["paused"] is True
        assert all(not run.is_alive() for run in resources)
        assert starts.qsize() <= 1
        history = await database.load_messages(session.id)
        assert [row["content"] for row in history if row["role"] == "user"] == ["held request"]
        terminals = [row for row in history if row["type"] == "result"]
        assert len(terminals) == 1 and terminals[0]["content"]["status"] == "interrupted"
        assert not [event for event in events if event["type"] == "result"]
    finally:
        release.set()
    completion = await asyncio.wait_for(receipt.wait(), 5)
    assert completion.recording_state == "committed" and completion.outcome.status == "interrupted"
    await wait_dispatch_idle(manager, session.id)
    assert starts.qsize() <= 1 and all(not run.is_alive() for run in resources)
    outcomes = [event for event in events if event["type"] == "result"]
    assert len(outcomes) == 1
    assert outcomes[0]["content"]["status"] == "interrupted"


async def test_stop_of_unstarted_delegation_preserves_round_until_resume(runtime, monkeypatch):
    manager, database, parent, delegations, starts, events = runtime
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
    entered, release = asyncio.Event(), asyncio.Event()
    execute = database.conn._execute

    async def pause_parent_admission(function, *args, **kwargs):
        if (len(args) > 1 and isinstance(args[0], str)
                and "FROM agents a WHERE a.id = ?" in args[0]
                and args[1][0] == parent.agent_id and not entered.is_set()):
            entered.set()
            await release.wait()
        return await execute(function, *args, **kwargs)

    monkeypatch.setattr(database.conn, "_execute", pause_parent_admission)
    confirming = None
    try:
        await manager.resume_dispatch(round_.delegation_id)
        await asyncio.wait_for(starts.get(), 5)
        await asyncio.wait_for(entered.wait(), 5)
        assert round_.state == "completed"
        assert starts.empty()
        assert manager.dispatch_snapshot(parent.id)["items"] == []
        confirming = asyncio.create_task(delegations.confirm_recording())
        await asyncio.sleep(0)
        assert not confirming.done(), "the original relay command still owns admission"
    finally:
        release.set()
        if confirming is not None:
            await asyncio.wait_for(confirming, 5)
        else:
            await delegations.confirm_recording()
    replies = manager.dispatch_snapshot(parent.id)["items"]
    assert len(replies) == 1
    assert replies[0]["source"] == "delegation"
    assert replies[0]["summary"].startswith("[agent-reply:")
    assert starts.empty()


async def test_queued_return_keeps_its_owner_when_deleted_during_dispatch_publication(runtime):
    manager, database, _, _, _, _ = runtime
    owner = await deps.get_user_manager().create_user(username="review-owner", password="password2")
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
        code = ("import json;print('{\"type\":\"thread.started\",\"thread_id\":\"backoff-thread\"}',flush=True);"
                f"print(json.dumps({payload!r}), flush=True)")
        return [sys.executable, "-u", "-c", code], {"cwd": context.working_dir}

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
    # Wait for the original relay before explicitly archiving the retained child.
    await asyncio.wait_for(delegations.confirm_recording(), 5)
    if manager.get_session(original.delegation_id) is not None:
        await wait_dispatch_idle(manager, original.delegation_id)
        await manager.archive_session(original.delegation_id, replace=False)
    return original


async def test_cancelled_follow_up_waiter_keeps_the_owned_round_and_its_reply(runtime):
    manager, _, parent, delegations, starts, _ = runtime
    original = await completed_delegation(runtime)
    entered, release = asyncio.Event(), asyncio.Event()

    async def slow_client(event):
        if event["type"] == "session_unarchived" and event["session_id"] == original.delegation_id:
            entered.set()
            await release.wait()

    manager.on_broadcast("slow-reopen", slow_client)
    following = asyncio.create_task(delegations.follow_up_delegation(
        parent_session_id=parent.id, delegation_id=original.delegation_id, request="Accepted setup after waiter disconnect",
    ))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        following.cancel()
        with pytest.raises(asyncio.CancelledError):
            await following
        accepted = delegations.get_delegation(original.delegation_id)
        assert accepted is not original and accepted.work_id != original.work_id
        assert accepted.request == "Accepted setup after waiter disconnect"
        assert accepted.state == "running" and original.state == "completed"
        assert starts.empty(), "the held reopen has not admitted the child yet"
        release.set()
        await asyncio.wait_for(delegations.confirm_recording(), 5)
        await wait_until(lambda: len(manager.dispatch_snapshot(parent.id)["items"]) == 2)
        await manager.await_session_execution(accepted.delegation_id, timeout=5)
        await asyncio.wait_for(delegations.confirm_recording(), 5)
        assert delegations.get_delegation(original.delegation_id) is accepted
        assert accepted.state == "completed" and original.state == "completed"
        assert not manager.has_pending_or_active_work(accepted.delegation_id, accepted.work_id)
        saved = next(row for row in await manager.db.load_sessions(include_archived=True)
                     if row["id"] == accepted.delegation_id)
        assert not saved["archived"]
        assert not delegations.has_active_delegation_for_parent(parent.id)
        assert starts.qsize() == 1, "exactly one child round survives the cancelled waiter"
    finally:
        release.set()
        await asyncio.gather(following, return_exceptions=True)


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
    try:
        await asyncio.wait_for(entered.wait(), 5)
        accepted = delegations.get_delegation(original.delegation_id)
        assert await asyncio.wait_for(asyncio.shield(following), 2) is accepted
        assert following.cancel() is False
        assert accepted.state == "running"
        assert delegations.get_delegation(original.delegation_id) is accepted
        assert manager.has_pending_or_active_work(original.delegation_id, accepted.work_id)
        assert starts.empty(), "The original claim prefix has not reached its subscriber yet"
    finally:
        release.set()
        await asyncio.gather(following, return_exceptions=True)
    await asyncio.wait_for(starts.get(), 5)
    assert starts.empty()
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
