"""Delegated work cancellation through public APIs and scripted local CLIs."""

from __future__ import annotations

import asyncio
import json
import sys

import httpx
import pytest
from fastapi import FastAPI

from server.agent_manager import AgentManager
from server.bg_tasks import BgTaskManager
from server.database import Database
from server.delegations import DelegationError, DelegationManager
from server.deps import get_session_manager
from server.harness import get_harness
from server.mcp_identity import mint, verify
from server.sessions import SessionManager
from server.sessions.runtime_types import SessionMailboxFull
from tests.accounts import create_account
from tests.harness_profiles import process_profile, session_turn_argv
from tests.test_delegation_restart_security import wait_live_scope


@pytest.fixture
async def work_runtime(monkeypatch, tmp_path, short_process_shutdown):
    database = Database(":memory:")
    await database.initialize()
    owner, token = await create_account(database, extra_roots=[tmp_path])
    manager = SessionManager()
    await manager.initialize(database)
    agents = AgentManager(database)
    root = await database.get_system_agent()
    await agents.create_agent(name="Worker", backend="codex", user_id=owner["id"])
    parent = await manager.create_session(root["id"], "Caller", working_dir=str(tmp_path))
    await manager.interrupt(parent.id)  # inspect automatic replies without driving a model
    delegations = DelegationManager()
    delegations.bind(manager, database)
    bg = BgTaskManager()
    bg.bind(database, manager.deliver_bg_result, manager._broadcast)
    from server.routers import bg_tasks as bg_router
    from server.routers import delegations as delegation_router
    monkeypatch.setattr(bg_router, "bg_task_manager", bg)
    monkeypatch.setattr(delegation_router, "delegation_manager", delegations)
    monkeypatch.setattr("server.delegations.bg_task_manager", bg, raising=False)
    app = FastAPI()
    app.include_router(bg_router.router)
    app.include_router(delegation_router.router)
    app.dependency_overrides[get_session_manager] = lambda: manager
    manager.work_test_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test",
        headers={"Authorization": "Bearer " + token},
    )
    manager.work_test_app = app
    manager.work_test_bg = bg
    starts = asyncio.Queue()

    def argv(context):
        starts.put_nowait(context)
        # Select the original round, since prompts also carry ancestor context.
        scope = next((verify(entry.credential) for entry in context.mcp_servers if entry.credential), None)
        round_ = delegations.get_delegation(scope.session_id) if scope else None
        request = round_.request if round_ is not None and round_.work_id == scope.work_id else context.prompt
        code = "import json,time\n"
        code += "print(json.dumps({'type':'thread.started','thread_id':'scripted'}),flush=True)\n"
        if "controlled" in request:
            code += f"from pathlib import Path\nwhile not Path({str(tmp_path / 'release')!r}).exists(): time.sleep(.01)\n"
        elif "held" in request:
            code += "time.sleep(30)\n"
        code += "print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':'reply'}}),flush=True)\n"
        code += "print(json.dumps({'type':'turn.completed','usage':{}}),flush=True)\n"
        return [sys.executable, "-u", "-c", code], {"cwd": context.working_dir}

    harness = get_harness("codex")
    monkeypatch.setattr(harness, "profile", process_profile(harness.profile, build_turn_argv=session_turn_argv(harness.profile, argv), prepare_resume=None, activity_observer=None))
    try:
        yield manager, delegations, parent, starts
    finally:
        await bg.shutdown()
        await manager.work_test_client.aclose()
        delegations.shutdown()
        for session in list(manager.sessions.values()):
            await manager.interrupt(session.id)
            await manager.await_session_execution(session.id, timeout=5)
        await manager.stop_all_held_processes()
        await database.close()


async def wait_state(manager, delegation_id, state):
    async with asyncio.timeout(5):
        while manager.get_delegation(delegation_id).state != state:
            await asyncio.sleep(0.01)


async def assert_no_queued_messages(manager, session_id):
    if manager.get_session(session_id) is not None:
        assert manager.dispatch_snapshot(session_id)["items"] == []
    else:
        assert next(row for row in await manager.db.load_sessions(include_archived=True)
                    if row["id"] == session_id)["archived"]


@pytest.mark.parametrize("child_outcome", ["surviving", "deleted", "follow-up", "parent-read-failed"])
async def test_parent_delete_releases_retired_work_while_child_notice_is_pending(
    work_runtime, monkeypatch, tmp_path, child_outcome,
):
    from server.routers.sessions import router

    manager, delegations, parent, starts = work_runtime
    monkeypatch.setattr("server.delegations.delegation_manager", delegations)
    manager.work_test_app.include_router(router)
    rec = await delegations.start_delegation(
        parent_session_id=parent.id, agent_name="Worker", request="controlled original work",
    )
    child_id = rec.delegation_id
    await wait_live_scope(await asyncio.wait_for(starts.get(), 3))
    submit, lookup, retain = manager.start_message, manager.source_receipt, manager.retain_result_notice
    writing, release = asyncio.Event(), asyncio.Event()
    reading, release_read, waiting_for_parent = asyncio.Event(), asyncio.Event(), asyncio.Event()
    confirm = delegations.confirm_recording
    submissions = []
    notices = []
    reads = []
    original_notice_error = RuntimeError("Original child notice write failed")
    parent_error = RuntimeError("Original question receipt read failed")
    fail_read = False

    async def observe_parent_wait(*, return_session_id=None):
        if return_session_id == parent.id:
            waiting_for_parent.set()
        return await confirm(return_session_id=return_session_id)

    async def lose_confirmation(session_id, prompt, **kwargs):
        receipt = await submit(session_id, prompt, **kwargs)
        if kwargs.get("delivery_id", "").startswith("agent-question:"):
            submissions.append(receipt.request_id)
            raise ConnectionError("Original question confirmation lost")
        return receipt

    async def withhold_confirmation(source, delivery_id, **kwargs):
        if delivery_id.startswith("agent-question:"):
            reads.append(delivery_id)
            if fail_read:
                reading.set()
                await release_read.wait()
                raise parent_error
            if release_read.is_set():
                return await lookup(source, delivery_id, **kwargs)
            return None
        return await lookup(source, delivery_id, **kwargs)

    async def hold_child_notice(session_id, delivery_id, content):
        if session_id == child_id and delivery_id.startswith("agent-question:"):
            notices.append(delivery_id)
            if len(notices) == 1:
                writing.set()
                raise original_notice_error
            writing.set()
            await release.wait()
            if child_outcome == "follow-up":
                raise SessionMailboxFull("Original child notice had no capacity")
        return await retain(session_id, delivery_id, content)

    monkeypatch.setattr(manager, "start_message", lose_confirmation)
    monkeypatch.setattr(manager, "source_receipt", withhold_confirmation)
    monkeypatch.setattr(manager, "retain_result_notice", hold_child_notice)
    monkeypatch.setattr(delegations, "confirm_recording", observe_parent_wait)
    question = [{"question": "Where should the original work go?"}]
    question_id = await manager.create_pending_question(child_id, question)
    event = {
        "type": "question_request", "session_id": child_id, "work_id": rec.work_id,
        "question_id": question_id, "questions": question,
    }
    client = manager.work_test_client
    broadcasting = draining = deleting = None
    try:
        await asyncio.wait_for(writing.wait(), 3)
        with pytest.raises(RuntimeError, match="cleanup failed"):
            await delegations.confirm_recording()
        assert await manager.answer_question(child_id, question_id, [{"text": "Original destination"}])
        (tmp_path / "release").touch()
        await wait_state(delegations, child_id, "completed")
        await manager.await_session_execution(child_id, timeout=3)

        initial_reads = len(reads)
        await manager._broadcast({**event, "work_id": "unrelated-work"})
        assert len(reads) == initial_reads
        writing.clear()
        fail_read = child_outcome == "parent-read-failed"
        broadcasting = asyncio.create_task(manager._broadcast(event))
        if fail_read:
            await asyncio.wait_for(reading.wait(), 3)
            deleting = asyncio.create_task(client.delete(f"/api/sessions/{parent.id}"))
            await asyncio.wait_for(waiting_for_parent.wait(), 3)
            release_read.set()
            with pytest.raises(RuntimeError, match="Original question receipt read failed") as failure:
                await asyncio.wait_for(deleting, 3)
            assert failure.value is parent_error
            fail_read = False
        await asyncio.wait_for(writing.wait(), 3)
        assert len(reads) > initial_reads
        reads_before_duplicate = len(reads)
        await manager._broadcast(event)
        assert len(reads) == reads_before_duplicate

        if child_outcome == "follow-up":
            renewed = await delegations.follow_up_delegation(
                parent_session_id=parent.id, delegation_id=child_id, request="held next work",
            )
            assert renewed.work_id != rec.work_id
            await manager._broadcast(event)
            assert len(reads) == reads_before_duplicate
        else:
            response = await asyncio.wait_for(client.delete(f"/api/sessions/{parent.id}"), 3)
            assert response.status_code == 204
            assert await manager.db.get_session_binding(parent.id) is None
        if child_outcome == "deleted":
            response = await asyncio.wait_for(client.delete(f"/api/sessions/{child_id}"), 3)
            assert response.status_code == 204
            assert await manager.db.get_session_binding(child_id) is None
        draining = asyncio.create_task(delegations.confirm_recording())
        _, unfinished = await asyncio.wait({draining}, timeout=0.05)
        assert unfinished, "An admitted child notice command remains owned during full drain"
        release.set()
        if child_outcome == "follow-up":
            with pytest.raises(SessionMailboxFull, match="Original child notice had no capacity") as broadcast_failure:
                await asyncio.wait_for(broadcasting, 3)
            with pytest.raises(RuntimeError, match="Delegation command cleanup failed") as drain_failure:
                await asyncio.wait_for(draining, 3)
            assert drain_failure.value.__cause__ is broadcast_failure.value
            await delegations.confirm_recording()
        elif child_outcome == "parent-read-failed":
            with pytest.raises(RuntimeError, match="Original question receipt read failed") as broadcast_failure:
                await asyncio.wait_for(broadcasting, 3)
            with pytest.raises(RuntimeError, match="Delegation command cleanup failed") as drain_failure:
                await asyncio.wait_for(draining, 3)
            assert drain_failure.value.__cause__ is broadcast_failure.value is parent_error
            await delegations.confirm_recording()
        else:
            await asyncio.wait_for(broadcasting, 3)
            await asyncio.wait_for(draining, 3)
        assert len(submissions) == 1 and notices == [f"agent-question:{rec.work_id}:{question_id}"] * 2
        if child_outcome in {"surviving", "parent-read-failed"}:
            rows = await manager.db.load_messages(child_id)
            retained, = (row for row in rows if row["type"] == "delivery_notice")
            assert question_id in retained["content"] and "Return admission unconfirmed" in retained["content"]
    finally:
        release.set()
        release_read.set()
        (tmp_path / "release").touch()
        await asyncio.gather(*(task for task in (broadcasting, draining, deleting) if task is not None), return_exceptions=True)
        if broadcasting is None:
            for task in tuple(delegations._commands):
                if task.done() and not task.cancelled() and task.exception() is original_notice_error:
                    delegations._forget_command(task)


async def test_parent_delete_waits_for_original_resources_while_child_owner_is_busy(work_runtime, monkeypatch):
    from server.routers.sessions import router
    from server.sessions.owner_questions import CaptureQuestion

    manager, delegations, parent, starts = work_runtime
    monkeypatch.setattr("server.delegations.delegation_manager", delegations)
    manager.work_test_app.include_router(router)
    rec = await delegations.start_delegation(
        parent_session_id=parent.id, agent_name="Worker", request="held original work",
    )
    await wait_live_scope(await asyncio.wait_for(starts.get(), 3))
    child = manager.get_session(rec.delegation_id)
    owner = manager._question_owner(child, internal=True)
    async with asyncio.timeout(3):
        while child._run.backend is None or child._run.backend._process is None:
            await asyncio.sleep(0.01)
    backend = child._run.backend
    process = backend._process
    assert process is not None and process.returncode is None
    handling, release, retiring = asyncio.Event(), asyncio.Event(), asyncio.Event()
    stopping, release_stop = asyncio.Event(), asyncio.Event()
    handle, close_work = owner.questions.handle, manager.close_work
    stop = backend.stop

    async def hold_child_control(command):
        if isinstance(command, CaptureQuestion):
            handling.set()
            await release.wait()
        return await handle(command)

    async def observe_retirement(work_id):
        if work_id == rec.work_id:
            retiring.set()
        return await close_work(work_id)

    async def hold_process_exit(*args, **kwargs):
        stopping.set()
        await release_stop.wait()
        return await stop(*args, **kwargs)

    monkeypatch.setattr(owner.questions, "handle", hold_child_control)
    monkeypatch.setattr(manager, "close_work", observe_retirement)
    monkeypatch.setattr(backend, "stop", hold_process_exit)
    control = asyncio.create_task(owner.call_question(CaptureQuestion("absent")))
    deleting = None
    try:
        await asyncio.wait_for(handling.wait(), 3)
        deleting = asyncio.create_task(manager.work_test_client.delete(f"/api/sessions/{parent.id}"))
        await asyncio.wait_for(retiring.wait(), 3)
        _, unfinished = await asyncio.wait({deleting}, timeout=0.05)
        assert unfinished and process.returncode is None
        assert await manager.db.get_session_binding(parent.id) is not None
        release.set()
        assert await asyncio.wait_for(control, 3) is None
        await asyncio.wait_for(stopping.wait(), 3)
        _, unfinished = await asyncio.wait({deleting}, timeout=0.05)
        assert unfinished and process.returncode is None
        assert await manager.db.get_session_binding(parent.id) is not None
        release_stop.set()
        response = await asyncio.wait_for(asyncio.shield(deleting), 3)
        assert response.status_code == 204 and process.returncode is not None
        assert await manager.db.get_session_binding(parent.id) is None
    finally:
        release.set()
        release_stop.set()
        await asyncio.gather(control, *([deleting] if deleting is not None else []), return_exceptions=True)


@pytest.mark.parametrize("follow_up", [False, True], ids=["initial-refusal-archive", "follow-up-restore"])
async def test_child_lifecycle_subscriber_can_delete_the_original_parent(work_runtime, monkeypatch, follow_up):
    from server.mcp_identity import close_work
    from server.routers import sessions as session_routes

    manager, delegations, parent, starts = work_runtime
    manager.work_test_app.include_router(session_routes.router)
    monkeypatch.setattr("server.delegations.delegation_manager", delegations)
    submit = manager.start_message
    child_id = None
    deleted = asyncio.get_running_loop().create_future()

    async def refuse_initial(sid, *args, **kwargs):
        nonlocal child_id
        if sid != parent.id and kwargs.get("source") == "delegation":
            child_id = sid
            close_work(kwargs["work_id"])
        return await submit(sid, *args, **kwargs)

    async def delete_parent(event):
        if ((event["type"] == "session_unarchived" and follow_up and event["session_id"] == child_id)
                or (event["type"] == "session_archived" and not follow_up and event["old_session_id"] == child_id)):
            try:
                async with asyncio.timeout(3):
                    response = await manager.work_test_client.delete(f"/api/sessions/{parent.id}")
                deleted.set_result(response.status_code)
            except Exception as error:
                deleted.set_exception(error)

    if follow_up:
        previous = await delegations.start_delegation(parent_session_id=parent.id, agent_name="Worker", request="First round")
        child_id = previous.delegation_id
        await wait_state(delegations, child_id, "completed")
        await delegations.confirm_recording()
        await manager.archive_session(child_id, replace=False)
        await asyncio.wait_for(starts.get(), 3)
    else:
        monkeypatch.setattr(manager, "start_message", refuse_initial)
    manager.on_broadcast("delete parent during child lifecycle", delete_parent, required=True)
    try:
        if follow_up:
            with pytest.raises(DelegationError, match="closed"):
                await asyncio.wait_for(delegations.follow_up_delegation(
                    parent_session_id=parent.id, delegation_id=child_id, request="Follow-up during restoration",
                ), 5)
        else:
            rec = await asyncio.wait_for(delegations.start_delegation(
                parent_session_id=parent.id, agent_name="Worker", request="Refuse the first input",
            ), 5)
            assert rec.state == "cancelled"
        assert await asyncio.wait_for(deleted, 5) == 204
        assert await manager.db.get_session_binding(parent.id) is None
        assert (await manager.db.get_session_binding(child_id))["archived"]
        assert starts.empty()
    finally:
        manager.remove_broadcast("delete parent during child lifecycle")


@pytest.mark.parametrize("ack_lost", [False, True], ids=["before-commit", "after-commit"])
@pytest.mark.parametrize("follow_up", [False, True], ids=["initial", "follow-up"])
async def test_delete_settles_a_refused_rounds_original_receipt(work_runtime, monkeypatch, ack_lost, follow_up):
    from server.mcp_identity import close_work
    from server.routers.sessions import router as sessions_router

    manager, delegations, parent, _ = work_runtime
    db = manager.db
    manager.work_test_app.include_router(sessions_router)
    client = manager.work_test_client
    url = f"/api/sessions/{parent.id}/delegations"
    payload = {"agent_name": "Worker", "request": "Refuse the child input"}
    monkeypatch.setattr("server.delegations.delegation_manager", delegations)
    if follow_up:
        first = await client.post(url, json={"agent_name": "Worker", "request": "First round"})
        assert first.status_code == 201
        child_id = first.json()["delegation_id"]
        await wait_state(delegations, child_id, "completed")
        await delegations.confirm_recording()
        url += f"/{child_id}/follow-up"
        payload = {"request": "Refuse the follow-up input"}
    compare, submit = db.compare_and_set_delegation_state, manager.start_message
    writes, admissions = [], []

    async def refuse_child(sid, *args, **kwargs):
        if sid != parent.id and kwargs.get("source") == "delegation":
            close_work(kwargs["work_id"])
        if sid == parent.id and kwargs.get("source") == "delegation":
            admissions.append(kwargs["delivery_id"])
        return await submit(sid, *args, **kwargs)

    async def fail_receipt_once(child_id, expected, snapshot, **kwargs):
        if json.loads(snapshot)["terminal_disposition"] == "accepted":
            writes.append((child_id, expected, snapshot))
            if len(writes) == 1:
                if ack_lost:
                    assert await compare(child_id, expected, snapshot, **kwargs)
                raise OSError("Original receipt write interrupted")
        return await compare(child_id, expected, snapshot, **kwargs)

    monkeypatch.setattr(manager, "start_message", refuse_child)
    monkeypatch.setattr(db, "compare_and_set_delegation_state", fail_receipt_once)
    with pytest.raises(OSError, match="Original receipt write interrupted"):
        await client.post(url, json=payload)
    rec = delegations.get_delegation(writes[0][0])
    try:
        receipt = await manager.source_receipt("delegation", f"agent-terminal:{rec.work_id}", session_id=parent.id)
        assert receipt is not None and receipt[1].accepted
        assert (await asyncio.wait_for(client.delete(f"/api/sessions/{parent.id}"), 4)).status_code == 204
        assert len(writes) == 2 and writes[0] == writes[1]
        assert admissions == [f"agent-terminal:{rec.work_id}"]
        await delegations.confirm_recording()
    finally:
        if len(writes) == 1:
            await delegations._command(delegations._inject_terminal(rec))


async def test_delete_closes_every_delegated_lease_before_waiting_on_cleanup(work_runtime, monkeypatch):
    from server.bg_tasks import BgTaskError
    from server.mcp_identity import work_is_open
    from server.research.manager import ResearchError, ResearchManager

    manager, delegations, parent, starts = work_runtime
    monkeypatch.setattr("server.delegations.delegation_manager", delegations)
    research = ResearchManager()
    research.bind(manager, manager.db)
    await AgentManager(manager.db).create_agent(name="Leaf", backend="codex", user_id=parent.user_id)
    first = await delegations.start_delegation(parent_session_id=parent.id, agent_name="Worker", request="held first")
    await wait_live_scope(await starts.get())
    second = await delegations.start_delegation(parent_session_id=parent.id, agent_name="Worker", request="held second")
    scope = await wait_live_scope(await starts.get())
    leaf = await delegations.start_delegation(parent_session_id=second.delegation_id, agent_name="Leaf", request="held leaf", scope=scope)
    await wait_live_scope(await starts.get())
    close, close_work = manager._close_original_execution, manager.close_work
    closing, release = asyncio.Event(), asyncio.Event()
    failed = False

    async def hold_owner(owner, command):
        if owner.session is parent:
            closing.set()
            await release.wait()
        return await close(owner, command)

    async def first_cleanup(work_id):
        nonlocal failed
        if work_id == first.work_id and not failed:
            failed = True
            raise OSError("Original first round cleanup failed")
        return await close_work(work_id)

    monkeypatch.setattr(manager, "_close_original_execution", hold_owner)
    monkeypatch.setattr(manager, "close_work", first_cleanup)
    deleting = asyncio.create_task(manager.delete_session(parent.id))
    try:
        await asyncio.wait_for(closing.wait(), 3)
        assert all(not work_is_open(rec.work_id) for rec in (first, second, leaf))
        with pytest.raises(BgTaskError):
            await manager.work_test_bg.start_task(session_id=second.delegation_id, command="true", working_dir=parent.working_dir, scope=scope)
        with pytest.raises(ResearchError) as refused:
            await research.start(second.delegation_id, "Late delegated research", scope=scope)
        assert refused.value.status_code == 409
        with pytest.raises(DelegationError) as refused_delegation:
            await delegations.start_delegation(parent_session_id=second.delegation_id, agent_name="Leaf", request="Late child", scope=scope)
        assert refused_delegation.value.status_code == 409
        release.set()
        with pytest.raises(OSError, match="Original first round cleanup failed"):
            await asyncio.wait_for(deleting, 4)
        assert await manager.delete_session(parent.id)
    finally:
        release.set()
        await asyncio.gather(deleting, return_exceptions=True)
        await research.shutdown()


@pytest.mark.parametrize("notice_fails", [False, True])
async def test_delete_captures_a_delegation_commit_already_in_flight(work_runtime, monkeypatch, notice_fails):
    from server.mcp_identity import work_is_open

    manager, delegations, parent, starts = work_runtime
    monkeypatch.setattr("server.delegations.delegation_manager", delegations)
    committed, release = asyncio.Event(), asyncio.Event()
    compare = manager.db.compare_and_set_delegation_state
    leases_at_compensation = []
    archived = asyncio.Event()

    async def observe_archive(event):
        if event["type"] == "session_archived":
            archived.set()
            if notice_fails:
                raise OSError("Detached child archive notification failed")

    async def hold_first_commit(child_id, expected, snapshot, **kwargs):
        state = json.loads(snapshot)
        if expected is None:
            saved = await compare(child_id, expected, snapshot, **kwargs)
            committed.set()
            await release.wait()
            return saved
        if state["state"] == "cancelled":
            leases_at_compensation.append(work_is_open(state["work_id"]))
        return await compare(child_id, expected, snapshot, **kwargs)

    monkeypatch.setattr(manager.db, "compare_and_set_delegation_state", hold_first_commit)
    manager.on_broadcast("detached child archive", observe_archive, required=True)
    starting = asyncio.create_task(delegations.start_delegation(parent_session_id=parent.id, agent_name="Worker", request="Late commit"))
    deleting = None
    try:
        await asyncio.wait_for(committed.wait(), 3)
        deleting = asyncio.create_task(manager.delete_session(parent.id))
        async with asyncio.timeout(3):
            while not manager.session_deleting(parent.id):
                await asyncio.sleep(0)
        release.set()
        with pytest.raises(DelegationError, match="closed"):
            await starting
        assert await asyncio.wait_for(deleting, 3)
        await asyncio.wait_for(archived.wait(), 3)
        assert leases_at_compensation and not any(leases_at_compensation)
        assert starts.empty()
    finally:
        release.set()
        manager.remove_broadcast("detached child archive")
        await asyncio.gather(*(task for task in (starting, deleting) if task is not None), return_exceptions=True)


@pytest.mark.parametrize("control_fails", [False, True], ids=["blocked", "write-fails"])
async def test_parent_delete_joins_the_childs_public_research_control(work_runtime, monkeypatch, control_fails):
    from server.research import manager as research_module
    from server.research.manager import ResearchManager
    from server.routers import research as research_routes
    from server.routers import sessions as session_routes

    manager, delegations, parent, starts = work_runtime
    db = manager.db
    research = ResearchManager()
    research.bind(manager, db)
    manager.work_test_app.include_router(research_routes.router)
    manager.work_test_app.include_router(session_routes.router)
    monkeypatch.setattr("server.delegations.delegation_manager", delegations)
    monkeypatch.setattr("server.delegations.research_manager", research)
    monkeypatch.setattr(research_routes, "research_manager", research)
    entered, stopping, control_sql, release = (asyncio.Event() for _ in range(4))
    confirm = db.confirm_research_outcome

    async def pipeline(*args, **kwargs):
        entered.set()
        try:
            await asyncio.Future()
        finally:
            stopping.set()

    async def hold_control(*args, **kwargs):
        if not control_sql.is_set():
            control_sql.set()
            await release.wait()
            if control_fails:
                raise OSError("Child cancellation write interrupted")
        return await confirm(*args, **kwargs)

    monkeypatch.setattr(research_module, "run_research", pipeline)
    child = await delegations.start_delegation(parent_session_id=parent.id, agent_name="Worker", request="held child Research")
    scope = await wait_live_scope(await starts.get())
    job = await research.start(child.delegation_id, "Control belongs to the original work", scope=scope)
    await asyncio.wait_for(entered.wait(), 3)
    monkeypatch.setattr(db, "confirm_research_outcome", hold_control)
    path = f"/api/sessions/{child.delegation_id}/research/{job['id']}/cancel"
    client = manager.work_test_client
    cancelling = asyncio.create_task(client.post(path))
    deleting = None
    try:
        await asyncio.wait_for(control_sql.wait(), 3)
        deleting = asyncio.create_task(client.delete(f"/api/sessions/{parent.id}"))
        await asyncio.wait_for(stopping.wait(), 3)
        assert not deleting.done() and await db.get_session_binding(parent.id) is not None
        release.set()
        if control_fails:
            for task in (cancelling, deleting):
                with pytest.raises(OSError, match="Child cancellation write interrupted"):
                    await asyncio.wait_for(task, 3)
            assert await db.get_session_binding(parent.id) is not None
            assert (await client.delete(f"/api/sessions/{parent.id}")).status_code == 204
        else:
            assert (await asyncio.wait_for(cancelling, 3)).status_code == 200
            assert (await asyncio.wait_for(deleting, 3)).status_code == 204
        assert (await db.get_research_job(job["id"]))["status"] == "cancelled"
        assert (await client.post(path)).status_code == 409
        assert not research.drain_blockers()
    finally:
        release.set()
        await asyncio.gather(*(task for task in (cancelling, deleting) if task is not None), return_exceptions=True)
        await research.shutdown()


@pytest.mark.parametrize("delegated", [False, True], ids=["session", "child-work"])
async def test_parent_delete_prevents_research_leaf_spawn_during_authorization(work_runtime, monkeypatch, delegated):
    from dataclasses import replace

    from server.research.manager import ResearchManager
    from server.routers.sessions import router as sessions_router

    manager, delegations, parent, starts = work_runtime
    research = ResearchManager()
    db = manager.db
    research.bind(manager, db)
    monkeypatch.setattr("server.research.research_manager", research)
    monkeypatch.setattr("server.delegations.research_manager", research)
    monkeypatch.setattr("server.delegations.delegation_manager", delegations)
    manager.work_test_app.include_router(sessions_router)
    source, scope = parent, None
    if delegated:
        child = await delegations.start_delegation(parent_session_id=parent.id, agent_name="Worker", request="held Research parent")
        scope = await wait_live_scope(await starts.get())
        source = manager.get_session(child.delegation_id)

    preparing, authorizing, resume_leaf = asyncio.Event(), asyncio.Event(), asyncio.Event()
    claimed, resume_delete = asyncio.Event(), asyncio.Event()
    launch = asyncio.get_running_loop().create_future()
    authorize, close, spawn = db.require_run_authority, manager._close_original_execution, asyncio.create_subprocess_exec
    harness = get_harness(source.backend)

    def leaf_argv(ctx):
        preparing.set()
        return [sys.executable, "-c", "import time; time.sleep(10)"], {"cwd": ctx.working_dir}

    async def hold_leaf(*args, **kwargs):
        await authorize(*args, **kwargs)
        if preparing.is_set():
            authorizing.set()
            await resume_leaf.wait()

    async def hold_owner(*args, **kwargs):
        claimed.set()
        await resume_delete.wait()
        return await close(*args, **kwargs)

    async def observe_spawn(*args, **kwargs):
        if preparing.is_set() and not launch.done():
            launch.set_result(args)
        return await spawn(*args, **kwargs)

    monkeypatch.setattr(harness, "profile", replace(harness.profile, build_oneshot_argv=leaf_argv))
    monkeypatch.setattr(db, "require_run_authority", hold_leaf)
    monkeypatch.setattr(manager, "_close_original_execution", hold_owner)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", observe_spawn)
    job = await research.start(source.id, "A leaf awaiting authorization", scope=scope)
    pipeline = research._task(job["id"])
    deleting = None
    try:
        await asyncio.wait_for(authorizing.wait(), 3)
        deleting = asyncio.create_task(manager.work_test_client.delete(f"/api/sessions/{parent.id}"))
        await asyncio.wait_for(claimed.wait(), 3)
        resume_leaf.set()
        done, _ = await asyncio.wait({pipeline, launch}, timeout=3, return_when=asyncio.FIRST_COMPLETED)
        assert launch not in done and pipeline in done
        assert (await db.get_research_job(job["id"]))["status"] == "cancelled"
    finally:
        resume_leaf.set()
        resume_delete.set()
        if deleting is not None:
            assert (await asyncio.wait_for(deleting, 5)).status_code == 204
        await research.shutdown()


async def test_child_owner_refuses_a_research_return_when_parent_closes_during_preparation(work_runtime, monkeypatch):
    from server.research import manager as research_module
    from server.research.manager import ResearchManager
    from server.research.orchestrator import ResearchReport
    from server.sessions import owner_runtime

    manager, delegations, parent, starts = work_runtime
    research = ResearchManager()
    research.bind(manager, manager.db)
    monkeypatch.setattr("server.delegations.delegation_manager", delegations)
    monkeypatch.setattr("server.delegations.research_manager", research)
    child = await delegations.start_delegation(parent_session_id=parent.id, agent_name="Worker", request="held child")
    scope = await wait_live_scope(await starts.get())
    preparing, release = asyncio.Event(), asyncio.Event()
    check = owner_runtime.require_retired_writers

    async def hold_preparation(original_manager, sid):
        await check(original_manager, sid)
        if sid == child.delegation_id:
            preparing.set()
            await release.wait()

    async def report(question, **kwargs):
        return ResearchReport(question=question, report="Saved report", findings=[], sources=[], angles=[], claims_examined=0, cost=0)

    monkeypatch.setattr(owner_runtime, "require_retired_writers", hold_preparation)
    monkeypatch.setattr(research_module, "run_research", report)
    job = await research.start(child.delegation_id, "Return through the surviving child Owner", scope=scope)
    deleting = None
    try:
        await asyncio.wait_for(preparing.wait(), 3)
        deleting = asyncio.create_task(manager.delete_session(parent.id))
        async with asyncio.timeout(3):
            while not manager.session_deleting(parent.id):
                await asyncio.sleep(0)
        release.set()
        assert await asyncio.wait_for(deleting, 3)
        assert (await manager.db.get_research_job(job["id"]))["injection_status"] == "failed"
        assert await manager.db.get_source_submission("research", f"research:{job['id']}", session_id=child.delegation_id) is None
    finally:
        release.set()
        if deleting is not None:
            await asyncio.gather(deleting, return_exceptions=True)
        await research.shutdown()


async def test_parent_delete_confirms_the_original_committed_round_cancellation(work_runtime, monkeypatch):
    manager, delegations, parent, starts = work_runtime
    db = manager.db
    monkeypatch.setattr("server.delegations.delegation_manager", delegations)
    child = await delegations.start_delegation(parent_session_id=parent.id, agent_name="Worker", request="held child")
    await wait_live_scope(await starts.get())
    compare, attempts = db.compare_and_set_delegation_state, []

    async def lose_first_ack(child_id, expected, snapshot, **kwargs):
        saved = await compare(child_id, expected, snapshot, **kwargs)
        state = json.loads(snapshot)
        if state["state"] == "cancelled" and state["terminal_disposition"] is None:
            attempts.append((expected, snapshot, saved))
            if len(attempts) == 1:
                raise OSError("Lost original round cancellation acknowledgement")
        return saved

    monkeypatch.setattr(db, "compare_and_set_delegation_state", lose_first_ack)
    with pytest.raises(OSError, match="Lost original round cancellation acknowledgement"):
        await delegations.cancel_delegation(child.delegation_id, reason="User stopped this exact round")
    assert await asyncio.wait_for(manager.delete_session(parent.id), 4)
    assert len(attempts) == 2 and attempts[0][:2] == attempts[1][:2]
    assert [attempt[2] for attempt in attempts] == [True, False]
    assert child.state == "cancelled" and child.error == "User stopped this exact round"


async def test_parent_delete_retries_the_original_delegated_research_recording(work_runtime, monkeypatch):
    from server.research import manager as research_module
    from server.research.manager import ResearchManager

    manager, delegations, parent, starts = work_runtime
    db = manager.db
    research = ResearchManager()
    research.bind(manager, db)
    monkeypatch.setattr("server.delegations.delegation_manager", delegations)
    monkeypatch.setattr("server.delegations.research_manager", research)
    monkeypatch.setattr("server.research.research_manager", research)
    entered, retired = asyncio.Event(), asyncio.Event()
    confirm = db.confirm_research_outcome
    attempts = []

    async def pipeline(*args, **kwargs):
        entered.set()
        try:
            await asyncio.Future()
        finally:
            retired.set()

    async def fail_once(job_id, status, **kwargs):
        if status == "cancelled":
            attempts.append(job_id)
            if len(attempts) == 1:
                raise OSError("Transient delegated Research recording failure")
        return await confirm(job_id, status, **kwargs)

    monkeypatch.setattr(research_module, "run_research", pipeline)
    monkeypatch.setattr(db, "confirm_research_outcome", fail_once)
    rec = await delegations.start_delegation(parent_session_id=parent.id, agent_name="Worker", request="held research owner")
    scope = await wait_live_scope(await asyncio.wait_for(starts.get(), 3))
    try:
        job = await research.start(rec.delegation_id, "Original delegated research", scope=scope)
        await asyncio.wait_for(entered.wait(), 3)
        with pytest.raises(OSError, match="Transient delegated Research recording failure"):
            await asyncio.wait_for(manager.delete_session(parent.id), 5)
        assert retired.is_set() and manager.get_session(parent.id) is parent
        assert (await db.get_research_job(job["id"]))["status"] == "running"
        assert await asyncio.wait_for(manager.delete_session(parent.id), 5)
        assert attempts == [job["id"], job["id"]]
        assert (await db.get_research_job(job["id"]))["status"] == "cancelled"
        await delegations.confirm_recording()
    finally:
        await research.shutdown()


@pytest.mark.parametrize("ack_lost", [False, True], ids=["before-commit", "after-commit"])
async def test_parent_delete_finishes_recovered_child_research_delivery(work_runtime, monkeypatch, ack_lost):
    from server.research import manager as research_module
    from server.research.manager import ResearchManager
    from server.research.orchestrator import ResearchReport

    manager, delegations, parent, starts = work_runtime
    db = manager.db
    research = ResearchManager()
    research.bind(manager, db)
    monkeypatch.setattr("server.delegations.delegation_manager", delegations)
    monkeypatch.setattr("server.delegations.research_manager", research)
    confirm = db.confirm_research_outcome
    attempts, returns = [], []
    admit = manager.admit_return

    async def pipeline(question, **kwargs):
        return ResearchReport(question=question, report="Selected report",
                              findings=[], sources=[], angles=[], claims_examined=0, cost=0)

    async def fail_once(job_id, status, **kwargs):
        attempts.append((job_id, status, kwargs))
        if len(attempts) == 1:
            if ack_lost:
                await confirm(job_id, status, **kwargs)
            raise OSError("Transient completed Research recording error")
        return await confirm(job_id, status, **kwargs)

    async def observe_return(target, **kwargs):
        if target.source == "research":
            returns.append(target.delivery_id)
        return await admit(target, **kwargs)

    monkeypatch.setattr(research_module, "run_research", pipeline)
    monkeypatch.setattr(db, "confirm_research_outcome", fail_once)
    monkeypatch.setattr(manager, "admit_return", observe_return)
    rec = await delegations.start_delegation(parent_session_id=parent.id, agent_name="Worker", request="held Research owner")
    scope = await wait_live_scope(await asyncio.wait_for(starts.get(), 3))
    job = await research.start(rec.delegation_id, "Original completed report", scope=scope)
    with pytest.raises(OSError, match="Transient completed Research recording error"):
        await research.wait_for_idle()
    assert await asyncio.wait_for(manager.delete_session(parent.id), 4)
    await research.wait_for_idle()
    assert await db.get_session_binding(rec.delegation_id) is not None
    row = await db.get_research_job(job["id"])
    assert len(attempts) == 2 and attempts[0] == attempts[1]
    assert row["status"] == "completed" and row["injection_status"] == "failed"
    assert returns == [f"research:{job['id']}"]
    assert await manager.source_receipt("research", returns[0], session_id=rec.delegation_id) is None
    await research.shutdown()


@pytest.mark.parametrize("write_fails", [False, True])
async def test_parent_delete_settles_the_accepted_child_research_receipt(work_runtime, monkeypatch, write_fails):
    from server.research import manager as research_module
    from server.research.manager import ResearchManager
    from server.research.orchestrator import ResearchReport
    from server.routers import sessions as session_routes

    manager, delegations, parent, starts = work_runtime
    db = manager.db
    manager.work_test_app.include_router(session_routes.router)
    research = ResearchManager()
    research.bind(manager, db)
    monkeypatch.setattr("server.delegations.delegation_manager", delegations)
    monkeypatch.setattr("server.delegations.research_manager", research)
    queued, recording, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    advance, delete = db.advance_research_delivery, db.delete_session
    writes = []

    async def pipeline(question, **kwargs):
        return ResearchReport(question=question, report="Selected report",
                              findings=[], sources=[], angles=[], claims_examined=0, cost=0)

    async def observe_delivery(event):
        if event["type"] == "research_delivery" and event["delivery_receipt"]["stage"] == "queued":
            queued.set()

    async def hold_recording(job_id, request_id, stage, **kwargs):
        if stage in {"failed", "unknown", "recording_unconfirmed"}:
            writes.append((job_id, request_id, stage))
            recording.set()
            await release.wait()
            if write_fails and len(writes) == 1:
                raise OSError("Transient accepted Research receipt write failure")
        return await advance(job_id, request_id, stage, **kwargs)

    async def delete_after_receipt(session_id):
        if session_id == parent.id:
            row = await db.get_research_job(job["id"])
            assert row["delivery_receipt"]["stage"] == "failed"
        await delete(session_id)

    async def delete_parent():
        response = await manager.work_test_client.delete(f"/api/sessions/{parent.id}")
        assert response.status_code == 204, response.text

    monkeypatch.setattr(research_module, "run_research", pipeline)
    monkeypatch.setattr(db, "advance_research_delivery", hold_recording)
    monkeypatch.setattr(db, "delete_session", delete_after_receipt)
    rec = await delegations.start_delegation(parent_session_id=parent.id, agent_name="Worker", request="held Research owner")
    scope = await wait_live_scope(await asyncio.wait_for(starts.get(), 3))
    manager.on_broadcast("observe original Research delivery", observe_delivery)
    deletion = None
    try:
        job = await research.start(rec.delegation_id, "Original Research receipt", scope=scope)
        await asyncio.wait_for(queued.wait(), 3)
        original = await manager.source_receipt("research", f"research:{job['id']}", session_id=rec.delegation_id)
        assert original is not None and original[1].accepted
        if write_fails:
            release.set()
            assert await manager.cancel_request(rec.delegation_id, original[1].request_id)
            with pytest.raises(OSError, match="Transient accepted Research receipt write failure"):
                await asyncio.wait_for(research.wait_for_idle(), 3)
            row = await db.get_research_job(job["id"])
            assert row["delivery_receipt"]["stage"] == "queued"
            await asyncio.wait_for(delete_parent(), 3)
        else:
            deletion = asyncio.create_task(delete_parent())
            await asyncio.wait_for(recording.wait(), 3)
            _, pending = await asyncio.wait({deletion}, timeout=0.1)
            assert pending and await db.get_session_binding(parent.id) is not None
            release.set()
            await asyncio.wait_for(deletion, 3)
        assert writes == [(job["id"], original[1].request_id, "failed")] * (2 if write_fails else 1)
        assert await db.get_session_binding(rec.delegation_id) is not None
        await research.shutdown()
    finally:
        release.set()
        manager.remove_broadcast("observe original Research delivery")
        if deletion is not None:
            await asyncio.gather(deletion, return_exceptions=True)


async def test_parent_delete_resumes_the_accepted_nested_delegation_write(work_runtime, monkeypatch):
    from server.routers import sessions as session_routes

    manager, delegations, parent, starts = work_runtime
    db = manager.db
    manager.work_test_app.include_router(session_routes.router)
    monkeypatch.setattr("server.delegations.delegation_manager", delegations)
    await AgentManager(db).create_agent(name="Nested Worker", backend="codex", user_id=parent.user_id)
    child = await delegations.start_delegation(parent_session_id=parent.id, agent_name="Worker", request="held child")
    scope = await wait_live_scope(await asyncio.wait_for(starts.get(), 3))
    compare, admit = db.compare_and_set_delegation_state, manager.admit_return
    writes, admissions = [], []
    failed = asyncio.Event()

    async def fail_once(*args, **kwargs):
        state = json.loads(args[2])
        if args[0] != child.delegation_id and state["terminal_disposition"] == "accepted":
            writes.append(state["work_id"])
            if len(writes) == 1:
                failed.set()
                raise OSError("Transient nested delegation receipt write failure")
        return await compare(*args, **kwargs)

    async def observe_admission(target, **kwargs):
        if target.source == "delegation" and target.session_id == child.delegation_id:
            admissions.append(target.delivery_id)
        return await admit(target, **kwargs)

    monkeypatch.setattr(db, "compare_and_set_delegation_state", fail_once)
    monkeypatch.setattr(manager, "admit_return", observe_admission)
    nested = await delegations.start_delegation(
        parent_session_id=child.delegation_id, agent_name="Nested Worker", request="Complete a nested reply", scope=scope,
    )
    await asyncio.wait_for(failed.wait(), 3)
    with pytest.raises(RuntimeError, match="Delegation command cleanup failed"):
        await delegations.confirm_recording()
    original = await manager.source_receipt("delegation", f"agent-terminal:{nested.work_id}", session_id=child.delegation_id)
    assert original is not None and original[1].accepted
    try:
        response = await asyncio.wait_for(manager.work_test_client.delete(f"/api/sessions/{parent.id}"), 4)
        assert response.status_code == 204, response.text
        stored = json.loads(await db.load_delegation_state(
            nested.delegation_id, parent_session_id=child.delegation_id, user_id=parent.user_id,
        ))
        assert stored["state"] == "completed" and stored["terminal_disposition"] == "accepted"
        assert writes == [nested.work_id, nested.work_id]
        assert admissions == [f"agent-terminal:{nested.work_id}"]
        await delegations.confirm_recording()
    finally:
        if writes == [nested.work_id]:
            await delegations._command(delegations._inject_terminal(nested))


@pytest.mark.parametrize("prior_cancel", ["publishing", "failed"])
async def test_parent_delete_joins_original_nested_delegation_cancellation(work_runtime, monkeypatch, prior_cancel):
    from server.routers import sessions as session_routes

    manager, delegations, parent, starts = work_runtime
    db = manager.db
    manager.work_test_app.include_router(session_routes.router)
    monkeypatch.setattr("server.delegations.delegation_manager", delegations)
    await AgentManager(db).create_agent(name="Nested Worker", backend="codex", user_id=parent.user_id)
    child = await delegations.start_delegation(parent_session_id=parent.id, agent_name="Worker", request="held child")
    scope = await wait_live_scope(await asyncio.wait_for(starts.get(), 3))
    nested = await delegations.start_delegation(
        parent_session_id=child.delegation_id, agent_name="Nested Worker", request="held nested", scope=scope,
    )
    await wait_live_scope(await asyncio.wait_for(starts.get(), 3))

    async def delete_parent():
        response = await asyncio.wait_for(manager.work_test_client.delete(f"/api/sessions/{parent.id}"), 3)
        assert response.status_code == 204, response.text

    if prior_cancel == "failed":
        cancel = manager.work_test_bg.cancel_work
        attempts = []

        async def fail_once(work_id):
            if work_id == nested.work_id:
                attempts.append(work_id)
                if len(attempts) == 1:
                    raise OSError("Transient nested cancellation failure")
            await cancel(work_id)

        monkeypatch.setattr(manager.work_test_bg, "cancel_work", fail_once)
        with pytest.raises(OSError, match="Transient nested cancellation failure"):
            await delegations.cancel_delegation(nested.delegation_id, work_id=nested.work_id)
        await delete_parent()
        assert attempts == [nested.work_id, nested.work_id]
    else:
        deleted = asyncio.get_running_loop().create_future()

        async def subscriber(event):
            if event["type"] == "delegation_delivery" and event["session_id"] == child.delegation_id:
                try:
                    await delete_parent()
                except Exception as error:
                    deleted.set_exception(error)
                else:
                    deleted.set_result(None)

        manager.on_broadcast("delete from nested delegation delivery", subscriber)
        try:
            await asyncio.wait_for(delegations.cancel_delegation(child.delegation_id), 5)
            await asyncio.wait_for(deleted, 4)
        finally:
            manager.remove_broadcast("delete from nested delegation delivery")

    await delegations.confirm_recording()
    assert await db.get_session_binding(parent.id) is None
    assert await db.get_session_binding(child.delegation_id) is not None
    assert nested.state == "cancelled"
    assert not manager.work_has_resources(child.work_id) and not manager.work_has_resources(nested.work_id)


@pytest.mark.parametrize("delay", [False, True])
async def test_parent_delete_owns_retirement_when_delegation_subscriber_fails(work_runtime, monkeypatch, caplog, delay):
    from server.routers import sessions as session_routes

    manager, delegations, parent, starts = work_runtime
    manager.work_test_app.include_router(session_routes.router)
    monkeypatch.setattr("server.delegations.delegation_manager", delegations)
    raised, release = asyncio.Event(), asyncio.Event()
    secret = "private-test-value"
    monkeypatch.setattr("server.config.settings.auth_token", secret)

    async def subscriber(event):
        if event["type"] == "delegation_delivery" and event["session_id"] == parent.id:
            if delay:
                await release.wait()
            raised.set()
            try:
                raise OSError("Original subscriber error")
            except OSError as cause:
                raise ValueError(f"Required delegation notification failed ({secret})") from cause

    manager.on_broadcast("required failing delegation delivery", subscriber, required=True)
    try:
        rec = await delegations.start_delegation(parent_session_id=parent.id, agent_name="Worker", request="held child")
        await wait_live_scope(await asyncio.wait_for(starts.get(), 3))
        response = await asyncio.wait_for(manager.work_test_client.delete(f"/api/sessions/{parent.id}"), 3)
        assert response.status_code == 204, response.text
        release.set()
        await asyncio.wait_for(raised.wait(), 3)
        await delegations.confirm_recording()
        assert "OSError: Original subscriber error" in caplog.text
        assert "ValueError: Required delegation notification failed ([redacted])" in caplog.text
        assert secret not in caplog.text
        assert rec.to_public_dict()["terminal_disposition"] == "closed"
        assert await manager.db.get_session_binding(parent.id) is None
    finally:
        release.set()
        manager.remove_broadcast("required failing delegation delivery")


async def test_application_delete_retires_direct_and_completed_delegated_writers_only(work_runtime, monkeypatch, tmp_path):
    import shlex

    from server import bg_tasks
    from server import delegations as delegation_module
    from server.applications import ApplicationManager
    from server.routers import applications as app_routes

    manager, delegations, parent, starts = work_runtime
    db, background = manager.db, manager.work_test_bg
    monkeypatch.setattr(bg_tasks, "bg_task_manager", background)
    monkeypatch.setattr(delegation_module, "delegation_manager", delegations)
    apps = ApplicationManager()
    apps.bind(manager, db)
    monkeypatch.setattr(app_routes, "_manager", apps)
    manager.work_test_app.include_router(app_routes.router)
    agent = await AgentManager(db).create_agent(name="Application builder", backend="codex", user_id=parent.user_id)
    app = await apps.create_application(name="Writer ownership", description="A small page", agent_id=agent["id"])
    await asyncio.wait_for(starts.get(), 3)
    sid = app["session_id"]
    await manager.await_session_execution(sid, timeout=5)
    direct = await manager.start_message(sid, "held direct development chat")
    await asyncio.wait_for(starts.get(), 3)
    rec = await delegations.start_delegation(parent_session_id=sid, agent_name="Worker", request="controlled delegated writer")
    context = await asyncio.wait_for(starts.get(), 3)
    await wait_live_scope(context)
    credential = next(entry.credential for entry in context.mcp_servers if entry.key == "bg")
    release = tmp_path / "release-writers"

    def command(name):
        return f"while ! test -f {shlex.quote(str(release))}; do sleep .01; done; touch {shlex.quote(str(tmp_path / name))}"

    own = await background.start_task(session_id=sid, working_dir=app["app_dir"], command=command("late-direct"))
    response = await manager.work_test_client.post(f"/api/sessions/{rec.delegation_id}/bg-tasks",
        headers={"Authorization": f"Bearer {credential}"}, json={"command": command("late-delegated")})
    assert response.status_code == 201, response.text
    child_bg = response.json()["id"]
    (tmp_path / "release").write_text("finish delegated round")
    await wait_state(delegations, rec.delegation_id, "completed")
    await delegations.confirm_recording()
    independent = await background.start_task(session_id=rec.delegation_id, working_dir=app["app_dir"], command="sleep 120")
    from server.app_backends import backend_supervisor

    stopping, finish_stop = asyncio.Event(), asyncio.Event()
    original_stop = backend_supervisor.stop

    async def stop(app_id):
        stopping.set()
        await finish_stop.wait()
        await original_stop(app_id)

    monkeypatch.setattr(backend_supervisor, "stop", stop)
    deletion = None
    try:
        deletion = asyncio.create_task(manager.work_test_client.delete(f"/api/applications/{app['id']}"))
        await asyncio.wait_for(stopping.wait(), 3)
        refused_bg = await manager.work_test_client.post(f"/api/sessions/{sid}/bg-tasks", json={"command": "touch forbidden"})
        refused_delegation = await manager.work_test_client.post(f"/api/sessions/{sid}/delegations", json={"agent_name": "Worker", "request": "new work"})
        assert refused_bg.status_code == 400 and "closed" in refused_bg.text, refused_bg.text
        assert refused_delegation.status_code == 409 and "deletion" in refused_delegation.text, refused_delegation.text
        finish_stop.set()
        deleted = await asyncio.wait_for(deletion, 3)
        assert deleted.status_code == 204, deleted.text
        assert (await direct.wait()).outcome.status == "interrupted"
        assert (await background.get_task(own.id)).status == "cancelled"
        assert (await background.get_task(child_bg)).status == "cancelled"
        assert (await background.get_task(independent.id)).status == "running"
        assert rec.state == "completed"
        release.write_text("original writers must already be retired")
        assert not (tmp_path / "late-direct").exists()
        assert not (tmp_path / "late-delegated").exists()
        assert await db.get_application(app["id"]) is None
        assert manager.get_session(sid) is not None
    finally:
        finish_stop.set()
        if deletion is not None:
            await asyncio.gather(deletion, return_exceptions=True)
        apps.shutdown()


async def test_follow_up_has_new_work_and_stale_cancel_cannot_stop_it(work_runtime):
    _, delegations, parent, starts = work_runtime
    first = await delegations.start_delegation(
        parent_session_id=parent.id, agent_name="Worker", request="first round",
    )
    await starts.get()
    await wait_state(delegations, first.delegation_id, "completed")
    original_work = first.to_public_dict()["work_id"]
    second = await delegations.follow_up_delegation(
        parent_session_id=parent.id, delegation_id=first.delegation_id, request="held next round",
    )
    await starts.get()
    assert second.work_id != original_work
    assert first.state == "completed" and first.work_id == original_work
    stale = await delegations.cancel_delegation(first.delegation_id, work_id=original_work)
    assert stale is first and stale.state == "completed"
    assert delegations.get_delegation(first.delegation_id).state == "running"
    await delegations.cancel_delegation(second.delegation_id, work_id=second.work_id)
    assert second.state == "cancelled"


async def test_follow_up_cancellation_waits_for_its_own_required_notification(work_runtime):
    manager, delegations, parent, starts = work_runtime
    first_entered, first_release = asyncio.Event(), asyncio.Event()
    second_entered, second_release = asyncio.Event(), asyncio.Event()
    failure = DelegationError("Required follow-up notification failed")
    cancelling = None

    async def subscriber(event):
        if event["type"] != "delegation_delivery" or event["session_id"] != parent.id:
            return
        if event["delegation"]["state"] == "completed":
            first_entered.set()
            await first_release.wait()
        else:
            assert event["delegation"]["state"] == "cancelled"
            second_entered.set()
            await second_release.wait()
            raise failure

    manager.on_broadcast("observe independent round notifications", subscriber, user_id=parent.user_id, required=True)
    try:
        first = await delegations.start_delegation(parent_session_id=parent.id, agent_name="Worker", request="first round")
        await asyncio.wait_for(starts.get(), 3)
        await asyncio.wait_for(first_entered.wait(), 3)
        second = await asyncio.wait_for(delegations.follow_up_delegation(
            parent_session_id=parent.id, delegation_id=first.delegation_id, request="held follow-up",
        ), 3)
        await wait_live_scope(await asyncio.wait_for(starts.get(), 3))
        assert second.work_id != first.work_id
        cancelling = asyncio.create_task(delegations.cancel_delegation(second.delegation_id, work_id=second.work_id))
        await asyncio.wait_for(second_entered.wait(), 3)
        assert not cancelling.done()
        second_release.set()
        with pytest.raises(DelegationError) as raised:
            await asyncio.wait_for(cancelling, 3)
        assert raised.value is failure
        assert first.state == "completed" and second.state == "cancelled"
        assert delegations.drain_blockers()
    finally:
        first_release.set()
        second_release.set()
        manager.remove_broadcast("observe independent round notifications")
        if cancelling is not None:
            await asyncio.gather(cancelling, return_exceptions=True)
        await delegations.confirm_recording()


async def test_cancel_queued_round_preserves_independent_human_request(work_runtime):
    manager, delegations, parent, starts = work_runtime
    first = await delegations.start_delegation(
        parent_session_id=parent.id, agent_name="Worker", request="first round",
    )
    await starts.get()
    await wait_state(delegations, first.delegation_id, "completed")
    await asyncio.wait_for(delegations.confirm_recording(), 5)
    child = manager.get_session(first.delegation_id)
    assert child is not None
    await manager.await_session_execution(child.id, timeout=5)
    human = await manager.start_message(child.id, "held independent human")
    assert human.accepted
    assert "held independent human" in (await starts.get()).prompt
    second = await delegations.follow_up_delegation(
        parent_session_id=parent.id, delegation_id=child.id, request="held queued delegation",
    )
    await delegations.cancel_delegation(child.id, work_id=second.work_id)
    await asyncio.wait_for(delegations.confirm_recording(), 5)
    assert manager.get_session(child.id).status.value == "running"
    snapshot = manager.dispatch_snapshot(child.id)
    assert snapshot["active_request_id"] == human.request_id and snapshot["items"] == []


async def test_real_run_bearer_identifies_work_and_stop_revokes_it(work_runtime):
    from server.auth import _allowed

    manager, delegations, parent, starts = work_runtime
    rec = await delegations.start_delegation(
        parent_session_id=parent.id, agent_name="Worker", request="held work",
    )
    context = await starts.get()
    await wait_live_scope(context)
    token = next(entry.credential for entry in context.mcp_servers if entry.key == "bg")
    scope = verify(token)
    assert scope.work_id == rec.work_id
    assert scope.run_lease_id
    assert await _allowed(token)
    await manager.interrupt(rec.delegation_id)
    assert not await _allowed(token)


async def test_explicit_cancel_stops_owned_bg_and_late_tool_call_is_rejected(work_runtime, tmp_path):
    manager, delegations, parent, starts = work_runtime
    rec = await delegations.start_delegation(
        parent_session_id=parent.id, agent_name="Worker", request="held work",
    )
    context = await starts.get()
    await wait_live_scope(context)
    token = next(entry.credential for entry in context.mcp_servers if entry.key == "bg")
    headers = {"Authorization": f"Bearer {token}"}
    path = f"/api/sessions/{rec.delegation_id}/bg-tasks"
    response = await manager.work_test_client.post(
        path, headers=headers,
        json={"command": "echo started; sleep 30", "idle_timeout_seconds": 0},
    )
    assert response.status_code == 201
    task = response.json()
    assert task["work_id"] == rec.work_id
    await delegations.cancel_delegation(rec.delegation_id, work_id=rec.work_id)
    async with asyncio.timeout(6):
        while (await manager.work_test_bg.get_task(task["id"])).status == "running":
            await asyncio.sleep(0.01)
    assert (await manager.work_test_bg.get_task(task["id"])).status == "cancelled"
    await manager.work_test_client.post(
        path, headers=headers, json={"command": f"touch {tmp_path}/late"},
    )
    assert not (tmp_path / "late").exists()


async def test_cancel_cleans_bg_whose_spawn_returns_after_work_closed(work_runtime, monkeypatch):
    manager, delegations, parent, starts = work_runtime
    rec = await delegations.start_delegation(
        parent_session_id=parent.id, agent_name="Worker", request="held work",
    )
    context = await starts.get()
    await wait_live_scope(context)
    token = next(entry.credential for entry in context.mcp_servers if entry.key == "bg")
    spawned, release = asyncio.Event(), asyncio.Event()
    original = asyncio.create_subprocess_exec

    async def delayed_spawn(*args, **kwargs):
        process = await original(*args, **kwargs)
        if args[0] == "/bin/sh":
            spawned.set()
            await release.wait()
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", delayed_spawn)
    creating = asyncio.create_task(manager.work_test_client.post(
        f"/api/sessions/{rec.delegation_id}/bg-tasks",
        headers={"Authorization": f"Bearer {token}"},
        json={"command": "echo started; sleep 30", "idle_timeout_seconds": 0},
    ))
    cancelling = None
    try:
        await asyncio.wait_for(spawned.wait(), 3)
        cancelling = asyncio.create_task(delegations.cancel_delegation(rec.delegation_id, work_id=rec.work_id))
        await wait_state(delegations, rec.delegation_id, "cancelled")
        # The original startup still owns its delayed process handle. Closing
        # work must wait for it rather than treating a cancellation as exit.
        assert not cancelling.done()
        release.set()
        await asyncio.wait_for(asyncio.shield(cancelling), 6)
        response = await creating
    finally:
        release.set()
        await asyncio.gather(creating, *([cancelling] if cancelling else []), return_exceptions=True)
    assert response.status_code == 201
    async with asyncio.timeout(6):
        while (await manager.work_test_bg.get_task(response.json()["id"])).status == "running":
            await asyncio.sleep(0.01)
    task = await manager.work_test_bg.get_task(response.json()["id"])
    assert task.status == "cancelled" and task.stop_reason == "work_cancelled"
    await assert_no_queued_messages(manager, rec.delegation_id)
    messages = await manager.db.load_messages(rec.delegation_id)
    assert not [row for row in messages if (row.get("tool_input") or {}).get("request_context", {}).get("source") == "bg"]


async def test_ordinary_stop_keeps_bg_running_and_pauses_its_return(work_runtime):
    manager, delegations, parent, starts = work_runtime
    rec = await delegations.start_delegation(
        parent_session_id=parent.id, agent_name="Worker", request="held work",
    )
    context = await starts.get()
    await wait_live_scope(context)
    token = next(entry.credential for entry in context.mcp_servers if entry.key == "bg")
    completed, release, returned = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def hold_completed_publication(event):
        if event.get("session_id") == rec.delegation_id and event["type"] == "bg_completed":
            completed.set()
            await release.wait()
        if (event.get("session_id") == rec.delegation_id and event["type"] == "dispatch_state"
                and any(item["source"] == "bg" for item in event["dispatch_state"]["items"])):
            returned.set()

    manager.on_broadcast("hold-stopped-bg-return", hold_completed_publication)
    response = await manager.work_test_client.post(
        f"/api/sessions/{rec.delegation_id}/bg-tasks",
        headers={"Authorization": f"Bearer {token}"},
        json={"command": "sleep .2; echo finished", "idle_timeout_seconds": 0},
    )
    try:
        await manager.interrupt(rec.delegation_id)
        await asyncio.wait_for(completed.wait(), 5)
        task = await manager.work_test_bg.get_task(response.json()["id"])
        assert task.status == "completed" and task.stdout.strip() == "finished"
        assert manager.dispatch_snapshot(rec.delegation_id)["items"] == []
        release.set()
        await asyncio.wait_for(returned.wait(), 5)
        queued = manager.dispatch_snapshot(rec.delegation_id)
        assert queued["paused"] and [item["source"] for item in queued["items"]] == ["bg"]
    finally:
        release.set()
        manager.remove_broadcast("hold-stopped-bg-return")


async def test_completed_old_round_can_cancel_residual_bg_without_touching_new_round(work_runtime, tmp_path):
    manager, delegations, parent, starts = work_runtime
    first = await delegations.start_delegation(
        parent_session_id=parent.id, agent_name="Worker", request="controlled round",
    )
    context = await starts.get()
    await wait_live_scope(context)
    token = next(entry.credential for entry in context.mcp_servers if entry.key == "bg")
    response = await manager.work_test_client.post(
        f"/api/sessions/{first.delegation_id}/bg-tasks",
        headers={"Authorization": f"Bearer {token}"},
        json={"command": "echo running; sleep 30", "idle_timeout_seconds": 0},
    )
    (tmp_path / "release").touch()
    await wait_state(delegations, first.delegation_id, "completed")
    second = await delegations.follow_up_delegation(
        parent_session_id=parent.id, delegation_id=first.delegation_id, request="held new round",
    )
    await starts.get()
    await delegations.cancel_delegation(first.delegation_id, work_id=first.work_id)
    async with asyncio.timeout(6):
        while (await manager.work_test_bg.get_task(response.json()["id"])).status == "running":
            await asyncio.sleep(0.01)
    assert (await manager.work_test_bg.get_task(response.json()["id"])).status == "cancelled"
    assert first.state == "completed" and second.state == "running"
    assert len(manager.dispatch_snapshot(parent.id)["items"]) == 1


async def test_nested_cancellation_closes_descendants_without_waking_closed_branch(work_runtime):
    manager, delegations, parent, starts = work_runtime
    await AgentManager(manager.db).create_agent(name="Grandchild", backend="codex", user_id=parent.user_id)
    child = await delegations.start_delegation(
        parent_session_id=parent.id, agent_name="Worker", request="held child",
    )
    child_context = await starts.get()
    await wait_live_scope(child_context)
    child_token = next(entry.credential for entry in child_context.mcp_servers if entry.key == "bg")
    grandchild = await delegations.start_delegation(
        parent_session_id=child.delegation_id, agent_name="Grandchild", request="held grandchild",
        scope=verify(child_token),
    )
    context = await starts.get()
    await wait_live_scope(context)
    token = next(entry.credential for entry in context.mcp_servers if entry.key == "bg")
    response = await manager.work_test_client.post(
        f"/api/sessions/{grandchild.delegation_id}/bg-tasks",
        headers={"Authorization": f"Bearer {token}"},
        json={"command": "echo running; sleep 30", "idle_timeout_seconds": 0},
    )
    await delegations.cancel_delegation(child.delegation_id, work_id=child.work_id)
    await delegations.cancel_delegation(child.delegation_id, work_id=child.work_id)
    async with asyncio.timeout(6):
        while (await manager.work_test_bg.get_task(response.json()["id"])).status == "running":
            await asyncio.sleep(0.01)
    assert grandchild.state == "cancelled"
    await assert_no_queued_messages(manager, child.delegation_id)
    assert len(manager.dispatch_snapshot(parent.id)["items"]) == 1


async def test_cancelling_parent_work_releases_the_nested_rounds_notification(work_runtime):
    manager, delegations, parent, starts = work_runtime
    await AgentManager(manager.db).create_agent(name="Nested Worker", backend="codex", user_id=parent.user_id)
    child = await delegations.start_delegation(parent_session_id=parent.id, agent_name="Worker", request="held child")
    scope = await wait_live_scope(await asyncio.wait_for(starts.get(), 3))
    nested = await delegations.start_delegation(
        parent_session_id=child.delegation_id, agent_name="Nested Worker", request="held nested", scope=scope,
    )
    await wait_live_scope(await asyncio.wait_for(starts.get(), 3))
    entered, release = asyncio.Event(), asyncio.Event()

    async def hold_notification(event):
        if event["type"] == "delegation_delivery" and event["delegation"]["work_id"] == nested.work_id:
            entered.set()
            await release.wait()

    manager.on_broadcast("hold nested cancellation", hold_notification, user_id=parent.user_id, required=True)
    cancelling = asyncio.create_task(delegations.cancel_delegation(nested.delegation_id, work_id=nested.work_id))
    retiring = draining = None
    try:
        await asyncio.wait_for(entered.wait(), 3)
        assert not cancelling.done(), "The original parent work is open and the send is required"
        retiring = asyncio.create_task(delegations.cancel_delegation(child.delegation_id, work_id=child.work_id))
        done, _ = await asyncio.wait({retiring, cancelling}, timeout=3)
        assert done == {retiring, cancelling}, "Closing the original parent work hands off the outstanding send"
        assert retiring.result() is child and cancelling.result() is nested
        assert not manager.work_has_resources(child.work_id) and not manager.work_has_resources(nested.work_id)
        draining = asyncio.create_task(delegations.confirm_recording())
        assert not (await asyncio.wait({draining}, timeout=.05))[0]
        release.set()
        await asyncio.wait_for(draining, 3)
    finally:
        release.set()
        manager.remove_broadcast("hold nested cancellation")
        await asyncio.gather(*(task for task in (cancelling, retiring, draining) if task is not None), return_exceptions=True)


async def test_held_claude_reuses_actual_lease_until_work_changes(work_runtime, monkeypatch):
    manager, _, _, _ = work_runtime
    root = await manager.db.get_system_agent()
    session = await manager.create_session(root["id"], "Reusable", backend="claude-code")
    contexts = []

    def argv(context):
        contexts.append(context)
        code = "import json,sys\n"
        code += "for line in sys.stdin:\n"
        code += " print(json.dumps({'type':'system','subtype':'init','session_id':'reusable'}),flush=True)\n"
        code += " print(json.dumps({'type':'result','subtype':'success','session_id':'reusable','result':'done','is_error':False}),flush=True)\n"
        return [sys.executable, "-u", "-c", code], {"cwd": context.working_dir}

    harness = get_harness("claude-code")
    monkeypatch.setattr(harness, "profile", process_profile(harness.profile, build_turn_argv=argv, prepare_resume=None, activity_observer=None))
    finished = asyncio.Queue()

    async def capture(event):
        if event["type"] == "result" and event["session_id"] == session.id:
            finished.put_nowait(event)

    manager.on_broadcast("reuse-result", capture)
    for prompt in ("first", "second"):
        await manager.start_message(session.id, prompt)
        assert not (await asyncio.wait_for(finished.get(), 5))["is_error"]
    assert len(contexts) == 1
    await wait_live_scope(contexts[0])
    first_token = next(entry.credential for entry in contexts[0].mcp_servers if entry.key == "bg")
    first_lease = verify(first_token).run_lease_id
    await manager.start_message(session.id, "new owner", source="delegation", work_id="separate-owner")
    await asyncio.wait_for(finished.get(), 5)
    assert len(contexts) == 2
    await wait_live_scope(contexts[1])
    second_token = next(entry.credential for entry in contexts[1].mcp_servers if entry.key == "bg")
    assert verify(second_token).work_id == "separate-owner"
    assert verify(second_token).run_lease_id != first_lease
    from server.auth import _allowed
    assert not await _allowed(first_token)


async def test_signed_work_survives_mcp_thread_and_late_loopback_is_refused(work_runtime, monkeypatch, tmp_path):
    import threading

    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    from server.config import settings
    from server.routers import bg_tasks as bg_router
    from tests.callback_api import start_callback_api

    manager, delegations, parent, starts = work_runtime
    api = await start_callback_api(
        bg_router.router, overrides={get_session_manager: lambda: manager},
        readiness_token=mint("callback-api-readiness", user_id=parent.user_id),
    )
    monkeypatch.setattr(settings, "port", api.port)
    entered, release = threading.Event(), threading.Event()
    original = httpx.post

    def blocked_post(url, **kwargs):
        if "late-mcp" in kwargs.get("json", {}).get("command", ""):
            entered.set()
            assert release.wait(5), "local test barrier was not released"
        return original(url, **kwargs)

    monkeypatch.setattr(httpx, "post", blocked_post)
    cancellation = None
    closed = asyncio.Event()
    close_work = manager.close_work

    async def observe_close(work_id):
        retirement = await close_work(work_id)
        closed.set()
        return retirement

    monkeypatch.setattr(manager, "close_work", observe_close)
    try:
        rec = await delegations.start_delegation(
            parent_session_id=parent.id, agent_name="Worker", request="held work",
        )
        context = await starts.get()
        await wait_live_scope(context)
        token = next(entry.credential for entry in context.mcp_servers if entry.key == "bg")
        async with httpx.AsyncClient(headers={"Authorization": f"Bearer {token}"}, timeout=10) as client:
            async with streamable_http_client(f"http://127.0.0.1:{api.port}/mcp/bg/mcp", http_client=client) as (read, write, _):
                async with ClientSession(read, write) as mcp:
                    await mcp.initialize()
                    receipt = await mcp.call_tool("run", {"command": "echo owned; sleep 30", "idle_timeout_seconds": 0})
                    assert "Started bg task" in receipt.content[0].text
                    tasks = await manager.work_test_bg.list_tasks(rec.delegation_id)
                    assert len(tasks) == 1 and tasks[0].work_id == rec.work_id
                    late = asyncio.create_task(mcp.call_tool("run", {"command": f"touch {tmp_path}/late-mcp"}))
                    assert await asyncio.to_thread(entered.wait, 3)
                    cancellation = asyncio.create_task(delegations.cancel_delegation(rec.delegation_id, work_id=rec.work_id))
                    # The original work scope is now revoked. Release the
                    # loopback thread before waiting for process retirement;
                    # cleanup and the thread no longer share a five-second race.
                    await asyncio.wait_for(closed.wait(), 3)
                    release.set()
                    result = await asyncio.wait_for(late, 5)
                    assert await asyncio.wait_for(cancellation, 5) is rec
                    assert "Error" in result.content[0].text
                    assert not (tmp_path / "late-mcp").exists()
                    assert len(await manager.work_test_bg.list_tasks(rec.delegation_id)) == 1
    finally:
        release.set()
        try:
            if cancellation is not None:
                await asyncio.wait_for(asyncio.shield(cancellation), 5)
        finally:
            await api.stop()


async def test_shutdown_retires_in_flight_background_startup(work_runtime, monkeypatch):
    from server import bg_tasks

    manager, _, parent, _ = work_runtime
    entered, release = asyncio.Event(), asyncio.Event()
    original = asyncio.create_subprocess_exec
    processes = []

    async def delayed_spawn(*args, **kwargs):
        if args[0] == "/bin/sh":
            entered.set()
            await release.wait()
        process = await original(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", delayed_spawn)
    creating = asyncio.create_task(manager.work_test_bg.start_task(
        session_id=parent.id, command="echo starting; sleep 30", working_dir=parent.working_dir,
    ))
    startup_bound = bg_tasks._STARTUP_DRAIN_SECONDS
    try:
        await asyncio.wait_for(entered.wait(), 3)
        monkeypatch.setattr(bg_tasks, "_STARTUP_DRAIN_SECONDS", 0.03)
        with pytest.raises(RuntimeError, match="shutdown incomplete"):
            await asyncio.wait_for(manager.work_test_bg.shutdown(), 0.3)
        with pytest.raises(bg_tasks.BgTaskError, match="shutting down"):
            await manager.work_test_bg.start_task(
                session_id=parent.id, command="echo too late", working_dir=parent.working_dir,
            )
        # Restore the process-reaping allowance before releasing the fault.
        monkeypatch.setattr(bg_tasks, "_STARTUP_DRAIN_SECONDS", startup_bound)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await creating
        await manager.work_test_bg.shutdown()
        assert len(processes) == 1 and processes[0].returncode is not None
        retained = await manager.work_test_bg.list_tasks(parent.id)
        assert len(retained) == 1 and retained[0].status == "interrupted"
        saved = await manager.db.get_bg_task(retained[0].id)
        assert saved["status"] == "interrupted" and saved["completed_at"] is not None
        assert saved["knowledge_context"] == retained[0].knowledge_context
        from server.knowledge import decode_owned

        assert decode_owned(saved["knowledge_context"]).return_binding.reference.session_id == parent.id
    finally:
        monkeypatch.setattr(bg_tasks, "_STARTUP_DRAIN_SECONDS", startup_bound)
        release.set()
        await asyncio.gather(creating, return_exceptions=True)
        await manager.work_test_bg.shutdown()


async def test_aborted_start_keeps_process_handle_for_cleanup(work_runtime, monkeypatch):
    manager, _, parent, _ = work_runtime
    spawned, release = asyncio.Event(), asyncio.Event()
    processes = []
    original = asyncio.create_subprocess_exec

    async def delayed_spawn(*args, **kwargs):
        process = await original(*args, **kwargs)
        processes.append(process)
        spawned.set()
        await release.wait()
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", delayed_spawn)
    creating = asyncio.create_task(manager.work_test_bg.start_task(
        session_id=parent.id, command="exec sleep 30", working_dir=parent.working_dir,
    ))
    await asyncio.wait_for(spawned.wait(), 3)
    creating.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await creating
    async with asyncio.timeout(3):
        while processes[0].returncode is None:
            await asyncio.sleep(0.01)
    async with asyncio.timeout(3):
        while (await manager.work_test_bg.list_tasks(parent.id))[0].status == "running":
            await asyncio.sleep(0.01)
    retained = await manager.work_test_bg.list_tasks(parent.id)
    assert len(retained) == 1 and retained[0].status == "failed"
    saved = await manager.db.get_bg_task(retained[0].id)
    assert saved["status"] == "failed" and saved["stop_reason"] == "startup_failed"
    assert saved["completed_at"] is not None
    assert saved["knowledge_context"] == retained[0].knowledge_context
    from server.knowledge import decode_owned

    assert decode_owned(saved["knowledge_context"]).return_binding.reference.session_id == parent.id


async def test_archived_completed_round_still_owns_background_process(work_runtime, tmp_path):
    manager, delegations, parent, starts = work_runtime
    rec = await delegations.start_delegation(
        parent_session_id=parent.id, agent_name="Worker", request="controlled round",
    )
    context = await starts.get()
    await wait_live_scope(context)
    token = next(entry.credential for entry in context.mcp_servers if entry.key == "bg")
    response = await manager.work_test_client.post(
        f"/api/sessions/{rec.delegation_id}/bg-tasks", headers={"Authorization": f"Bearer {token}"},
        json={"command": "echo running; sleep 30", "idle_timeout_seconds": 0},
    )
    (tmp_path / "release").touch()
    await wait_state(delegations, rec.delegation_id, "completed")
    await asyncio.wait_for(delegations.confirm_recording(), 5)
    await manager.await_session_execution(rec.delegation_id, timeout=5)
    assert manager.get_session(rec.delegation_id) is not None
    await manager.archive_session(rec.delegation_id, replace=False)
    assert manager.get_session(rec.delegation_id) is None
    stored = next(row for row in await manager.db.load_sessions(include_archived=True)
                  if row["id"] == rec.delegation_id)
    assert stored["archived"]
    await delegations.cancel_delegation(rec.delegation_id, work_id=rec.work_id)
    async with asyncio.timeout(5):
        while (await manager.work_test_bg.get_task(response.json()["id"])).status == "running":
            await asyncio.sleep(0.01)
    assert (await manager.work_test_bg.get_task(response.json()["id"])).status == "cancelled"
    assert rec.state == "completed" and manager.get_session(rec.delegation_id) is None


async def test_cancel_reaps_model_process_when_spawn_handle_returns_late(work_runtime, monkeypatch):
    manager, delegations, parent, _ = work_runtime
    spawned, release = asyncio.Event(), asyncio.Event()
    processes = []
    original = asyncio.create_subprocess_exec

    async def delayed_spawn(*args, **kwargs):
        process = await original(*args, **kwargs)
        if sys.executable in args:
            processes.append(process)
            spawned.set()
            await release.wait()
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", delayed_spawn)
    rec = await delegations.start_delegation(
        parent_session_id=parent.id, agent_name="Worker", request="held work",
    )
    await asyncio.wait_for(spawned.wait(), 3)
    cancellation = asyncio.create_task(delegations.cancel_delegation(rec.delegation_id, work_id=rec.work_id))
    try:
        await wait_state(delegations, rec.delegation_id, "cancelled")
        assert not cancellation.done(), "Cancellation still owns the outstanding OS spawn handle"
        release.set()
        assert await asyncio.wait_for(cancellation, 3) is rec
        async with asyncio.timeout(3):
            while processes[0].returncode is None:
                await asyncio.sleep(0.01)
    finally:
        release.set()
        await asyncio.gather(cancellation, return_exceptions=True)
        # This is the scripted process captured at the OS boundary, never a
        # real CLI. Retire it if a regression failed the behavioral assertion.
        if processes[0].returncode is None:
            processes[0].kill()
            await processes[0].wait()
    assert rec.state == "cancelled"


async def test_cancel_during_claim_prevents_owned_turn_from_starting(work_runtime):
    manager, delegations, parent, starts = work_runtime
    cancelled = asyncio.Event()
    executions = []

    async def cancel_claim(event):
        if event["type"] == "dequeued" and event["session_id"] != parent.id and not cancelled.is_set():
            rec = delegations.get_delegation(event["session_id"])
            child = manager.get_session(rec.delegation_id)
            executions.append(manager._session_owners[id(child)].runtime.current)
            await delegations.cancel_delegation(rec.delegation_id, work_id=rec.work_id)
            cancelled.set()

    manager.on_broadcast("cancel-claim", cancel_claim)
    rec = await delegations.start_delegation(
        parent_session_id=parent.id, agent_name="Worker", request="held work",
    )
    await asyncio.wait_for(cancelled.wait(), 3)
    execution, = executions
    await asyncio.wait_for(asyncio.shield(execution.retired), 3)
    await asyncio.wait_for(delegations.confirm_recording(), 5)
    assert starts.empty() and rec.state == "cancelled"
    await assert_no_queued_messages(manager, rec.delegation_id)


async def test_concurrent_follow_ups_accept_one_new_round(work_runtime):
    _, delegations, parent, starts = work_runtime
    first = await delegations.start_delegation(
        parent_session_id=parent.id, agent_name="Worker", request="first",
    )
    await starts.get()
    await wait_state(delegations, first.delegation_id, "completed")
    # A committed round can still own its original relay/archive command.
    # Exercise competing continuations after that original command retires.
    await asyncio.wait_for(delegations.confirm_recording(), 5)
    results = await asyncio.gather(*[
        delegations.follow_up_delegation(
            parent_session_id=parent.id, delegation_id=first.delegation_id, request="held next",
        ) for _ in range(2)
    ], return_exceptions=True)
    assert len([item for item in results if isinstance(item, DelegationError)]) == 1
    assert len([item for item in results if not isinstance(item, Exception)]) == 1
    accepted = next(item for item in results if not isinstance(item, Exception))
    assert accepted.state == "running" and accepted.work_id != first.work_id
    await asyncio.wait_for(starts.get(), 5)
    assert starts.empty()


async def test_signed_diagnostic_identity_without_a_run_cannot_create_model_work(work_runtime, tmp_path):
    from server.mcp_identity import mint

    manager, _, parent, _ = work_runtime
    response = await manager.work_test_client.post(
        f"/api/sessions/{parent.id}/bg-tasks",
        headers={"Authorization": f"Bearer {mint(parent.id, user_id=parent.user_id)}"},
        json={"command": f"touch {tmp_path}/unowned"},
    )
    assert response.status_code == 400
    assert not (tmp_path / "unowned").exists()
