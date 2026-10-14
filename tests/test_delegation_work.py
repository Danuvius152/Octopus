"""Delegated work cancellation through public APIs and scripted local CLIs."""

from __future__ import annotations

import asyncio
import sys
from dataclasses import replace

import httpx
import pytest
from fastapi import FastAPI

from server.agent_manager import AgentManager
from server.bg_tasks import BgTaskManager
from server.database import Database
from server.delegations import DelegationError, DelegationManager
from server.deps import get_session_manager
from server.harness import get_harness
from server.mcp_identity import verify
from server.sessions import SessionManager


@pytest.fixture
async def work_runtime(monkeypatch, tmp_path):
    database = Database(":memory:")
    await database.initialize()
    manager = SessionManager()
    await manager.initialize(database)
    agents = AgentManager(database)
    root = await database.get_system_agent()
    await agents.create_agent(name="Worker", backend="codex")
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
    )
    manager.work_test_bg = bg
    starts = asyncio.Queue()

    def argv(context):
        starts.put_nowait(context)
        code = "import json,time\n"
        code += "print(json.dumps({'type':'thread.started','thread_id':'scripted'}),flush=True)\n"
        if "controlled" in context.prompt:
            code += f"from pathlib import Path\nwhile not Path({str(tmp_path / 'release')!r}).exists(): time.sleep(.01)\n"
        elif "held" in context.prompt:
            code += "time.sleep(30)\n"
        code += "print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':'reply'}}),flush=True)\n"
        code += "print(json.dumps({'type':'turn.completed','usage':{}}),flush=True)\n"
        return [sys.executable, "-u", "-c", code], {"cwd": context.working_dir}

    harness = get_harness("codex")
    monkeypatch.setattr(harness, "profile", replace(harness.profile, build_turn_argv=argv))
    try:
        yield manager, delegations, parent, starts
    finally:
        await bg.shutdown()
        await manager.work_test_client.aclose()
        delegations.shutdown()
        for session in list(manager.sessions.values()):
            await manager.interrupt(session.id)
            if session._active_task:
                await asyncio.wait_for(session._active_task, 5)
        await manager.stop_all_held_processes()
        await database.close()


async def wait_state(manager, delegation_id, state):
    async with asyncio.timeout(5):
        while manager.get_delegation(delegation_id).state != state:
            await asyncio.sleep(0.01)


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


async def test_cancel_queued_round_preserves_independent_human_request(work_runtime):
    manager, delegations, parent, starts = work_runtime
    first = await delegations.start_delegation(
        parent_session_id=parent.id, agent_name="Worker", request="first round",
    )
    await starts.get()
    await wait_state(delegations, first.delegation_id, "completed")
    child = manager.get_session(first.delegation_id)
    if child._active_task:
        await asyncio.wait_for(child._active_task, 5)
    await manager.start_message(child.id, "held independent human")
    assert "held independent human" in (await starts.get()).prompt
    second = await delegations.follow_up_delegation(
        parent_session_id=parent.id, delegation_id=child.id, request="held queued delegation",
    )
    await delegations.cancel_delegation(child.id, work_id=second.work_id)
    await asyncio.sleep(0.05)
    assert manager.get_session(child.id).status.value == "running"
    assert manager.dispatch_snapshot(child.id)["items"] == []


async def test_real_run_bearer_identifies_work_and_stop_revokes_it(work_runtime):
    from server.auth import _allowed

    manager, delegations, parent, starts = work_runtime
    rec = await delegations.start_delegation(
        parent_session_id=parent.id, agent_name="Worker", request="held work",
    )
    context = await starts.get()
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
    await asyncio.wait_for(spawned.wait(), 3)
    await delegations.cancel_delegation(rec.delegation_id, work_id=rec.work_id)
    release.set()
    response = await creating
    assert response.status_code == 201
    async with asyncio.timeout(6):
        while (await manager.work_test_bg.get_task(response.json()["id"])).status == "running":
            await asyncio.sleep(0.01)
    task = await manager.work_test_bg.get_task(response.json()["id"])
    assert task.status == "cancelled" and task.stop_reason == "work_cancelled"
    assert manager.dispatch_snapshot(rec.delegation_id)["items"] == []


async def test_ordinary_stop_keeps_bg_running_and_pauses_its_return(work_runtime):
    manager, delegations, parent, starts = work_runtime
    rec = await delegations.start_delegation(
        parent_session_id=parent.id, agent_name="Worker", request="held work",
    )
    context = await starts.get()
    token = next(entry.credential for entry in context.mcp_servers if entry.key == "bg")
    response = await manager.work_test_client.post(
        f"/api/sessions/{rec.delegation_id}/bg-tasks",
        headers={"Authorization": f"Bearer {token}"},
        json={"command": "sleep .2; echo finished", "idle_timeout_seconds": 0},
    )
    await manager.interrupt(rec.delegation_id)
    async with asyncio.timeout(5):
        while (await manager.work_test_bg.get_task(response.json()["id"])).status == "running":
            await asyncio.sleep(0.01)
    task = await manager.work_test_bg.get_task(response.json()["id"])
    assert task.status == "completed" and task.stdout.strip() == "finished"
    queued = manager.dispatch_snapshot(rec.delegation_id)
    assert queued["paused"] and [item["source"] for item in queued["items"]] == ["bg"]


async def test_completed_old_round_can_cancel_residual_bg_without_touching_new_round(work_runtime, tmp_path):
    manager, delegations, parent, starts = work_runtime
    first = await delegations.start_delegation(
        parent_session_id=parent.id, agent_name="Worker", request="controlled round",
    )
    context = await starts.get()
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
    await AgentManager(manager.db).create_agent(name="Grandchild", backend="codex")
    child = await delegations.start_delegation(
        parent_session_id=parent.id, agent_name="Worker", request="held child",
    )
    await starts.get()
    grandchild = await delegations.start_delegation(
        parent_session_id=child.delegation_id, agent_name="Grandchild", request="held grandchild",
    )
    context = await starts.get()
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
    assert manager.dispatch_snapshot(child.delegation_id)["items"] == []
    assert len(manager.dispatch_snapshot(parent.id)["items"]) == 1


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
    monkeypatch.setattr(harness, "profile", replace(harness.profile, build_turn_argv=argv))
    finished = asyncio.Queue()

    async def capture(event):
        if event["type"] == "result" and event["session_id"] == session.id:
            finished.put_nowait(event)

    manager.on_broadcast("reuse-result", capture)
    for prompt in ("first", "second"):
        await manager.start_message(session.id, prompt)
        assert not (await asyncio.wait_for(finished.get(), 5))["is_error"]
    assert len(contexts) == 1
    first_token = next(entry.credential for entry in contexts[0].mcp_servers if entry.key == "bg")
    first_lease = verify(first_token).run_lease_id
    await manager.start_message(session.id, "new owner", source="delegation", work_id="separate-owner")
    await asyncio.wait_for(finished.get(), 5)
    assert len(contexts) == 2
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
    try:
        rec = await delegations.start_delegation(
            parent_session_id=parent.id, agent_name="Worker", request="held work",
        )
        context = await starts.get()
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
                    await delegations.cancel_delegation(rec.delegation_id, work_id=rec.work_id)
                    release.set()
                    result = await asyncio.wait_for(late, 5)
                    assert "Error" in result.content[0].text
                    assert not (tmp_path / "late-mcp").exists()
                    assert len(await manager.work_test_bg.list_tasks(rec.delegation_id)) == 1
    finally:
        release.set()
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
        assert await manager.work_test_bg.list_tasks(parent.id) == []
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
    assert await manager.work_test_bg.list_tasks(parent.id) == []


async def test_archived_completed_round_still_owns_background_process(work_runtime, tmp_path):
    manager, delegations, parent, starts = work_runtime
    rec = await delegations.start_delegation(
        parent_session_id=parent.id, agent_name="Worker", request="controlled round",
    )
    context = await starts.get()
    token = next(entry.credential for entry in context.mcp_servers if entry.key == "bg")
    response = await manager.work_test_client.post(
        f"/api/sessions/{rec.delegation_id}/bg-tasks", headers={"Authorization": f"Bearer {token}"},
        json={"command": "echo running; sleep 30", "idle_timeout_seconds": 0},
    )
    (tmp_path / "release").touch()
    await wait_state(delegations, rec.delegation_id, "completed")
    child = manager.get_session(rec.delegation_id)
    if child._active_task:
        await asyncio.wait_for(child._active_task, 5)
    assert await manager.auto_archive_scheduled_session(rec.delegation_id)
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
    await delegations.cancel_delegation(rec.delegation_id, work_id=rec.work_id)
    release.set()
    try:
        async with asyncio.timeout(3):
            while processes[0].returncode is None:
                await asyncio.sleep(0.01)
    finally:
        # This is the scripted process captured at the OS boundary, never a
        # real CLI. Retire it if a regression failed the behavioral assertion.
        if processes[0].returncode is None:
            processes[0].kill()
            await processes[0].wait()
    assert rec.state == "cancelled"


async def test_cancel_during_claim_prevents_owned_turn_from_starting(work_runtime):
    manager, delegations, parent, starts = work_runtime
    cancelled = asyncio.Event()

    async def cancel_claim(event):
        if event["type"] == "dequeued" and event["session_id"] != parent.id and not cancelled.is_set():
            rec = delegations.get_delegation(event["session_id"])
            await delegations.cancel_delegation(rec.delegation_id, work_id=rec.work_id)
            cancelled.set()

    manager.on_broadcast("cancel-claim", cancel_claim)
    rec = await delegations.start_delegation(
        parent_session_id=parent.id, agent_name="Worker", request="held work",
    )
    await asyncio.wait_for(cancelled.wait(), 3)
    await asyncio.sleep(0.05)
    assert starts.empty() and rec.state == "cancelled"
    assert manager.dispatch_snapshot(rec.delegation_id)["items"] == []


async def test_concurrent_follow_ups_accept_one_new_round(work_runtime):
    _, delegations, parent, starts = work_runtime
    first = await delegations.start_delegation(
        parent_session_id=parent.id, agent_name="Worker", request="first",
    )
    await starts.get()
    await wait_state(delegations, first.delegation_id, "completed")
    results = await asyncio.gather(*[
        delegations.follow_up_delegation(
            parent_session_id=parent.id, delegation_id=first.delegation_id, request="held next",
        ) for _ in range(2)
    ], return_exceptions=True)
    assert len([item for item in results if isinstance(item, DelegationError)]) == 1
    assert len([item for item in results if not isinstance(item, Exception)]) == 1


async def test_signed_diagnostic_identity_cannot_create_unowned_model_work(work_runtime, tmp_path):
    from server.mcp_identity import mint

    manager, _, parent, _ = work_runtime
    response = await manager.work_test_client.post(
        f"/api/sessions/{parent.id}/bg-tasks",
        headers={"Authorization": f"Bearer {mint(parent.id)}"},
        json={"command": f"touch {tmp_path}/unowned"},
    )
    assert response.status_code == 400
    assert not (tmp_path / "unowned").exists()
