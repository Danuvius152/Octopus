"""An unattended child's original question is solved once by its real parent input."""

from __future__ import annotations

import asyncio
import sys

import pytest

from server.harness import get_harness
from server.mcp_identity import mint, verify
from server.routers.questions import router
from tests.harness_profiles import process_profile
from tests.test_delegation_restart_security import wait_live_scope
from tests.test_delegation_work import work_runtime as work_runtime


@pytest.mark.parametrize("solvable", [True, False])
async def test_unattended_parent_resolves_from_existing_facts_or_releases_original_child_wait(work_runtime, tmp_path, monkeypatch, solvable):
    manager, delegations, parent, starts = work_runtime
    parent = await manager.create_session(parent.agent_id, "Unattended parent", parent.working_dir, backend="codex")
    manager.work_test_app.include_router(router)
    release_parent, release_solver = tmp_path / "parent-done", tmp_path / "solver-done"

    def argv(context):
        starts.put_nowait(context)
        scope = next(verify(entry.credential) for entry in context.mcp_servers if entry.key == "bg")
        child = scope.session_id != parent.id
        solver = not child and "[agent-question:" in context.prompt
        code = "import json,time\nfrom pathlib import Path\n"
        code += "print(json.dumps({'type':'thread.started','thread_id':'policy-thread'}),flush=True)\n"
        if child:
            code += "time.sleep(30)\n"
        else:
            gate = release_solver if solver else release_parent
            if not solver or solvable:
                code += f"while not Path({str(gate)!r}).exists(): time.sleep(.01)\n"
            code += "print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':'Partial work complete; missing decision remains.'}}),flush=True)\n"
            code += "print(json.dumps({'type':'turn.completed','usage':{}}),flush=True)\n"
        return [sys.executable, "-u", "-c", code], {"cwd": context.working_dir}

    harness = get_harness("codex")
    monkeypatch.setattr(harness, "profile", process_profile(harness.profile, build_turn_argv=argv))
    await manager.start_message(parent.id, "Scheduled parent", source="schedule")
    await manager.resume_dispatch(parent.id)
    caller = await wait_live_scope(await asyncio.wait_for(starts.get(), 3))
    rec = await delegations.start_delegation(parent_session_id=parent.id, agent_name="Worker", request="Child needs a destination", scope=caller)
    child_scope = await wait_live_scope(await asyncio.wait_for(starts.get(), 3))
    client = manager.work_test_client
    child_bearer = mint(child_scope.session_id, user_id=child_scope.user_id, run_lease_id=child_scope.run_lease_id, work_id=child_scope.work_id)
    created = await client.post(f"/api/sessions/{rec.delegation_id}/questions", headers={"Authorization": f"Bearer {child_bearer}"},
        json={"questions": [{"question": "Which authorized destination?"}]})
    assert created.status_code == 201
    qid = created.json()["question_id"]
    child = manager.get_session(rec.delegation_id)
    pending, = manager.get_pending_questions(child.id)
    assert pending.answer_target == "parent" and pending.interaction_policy == "unattended"
    async with asyncio.timeout(3):
        while pending.parent_receipt is None:
            await asyncio.sleep(.01)
    release_parent.touch()
    solver_context = await asyncio.wait_for(starts.get(), 3)
    solver_scope = await wait_live_scope(solver_context)
    assert "无人值守" in solver_context.turn_instructions
    if solvable:
        token = mint(parent.id, user_id=parent.user_id, run_lease_id=solver_scope.run_lease_id, work_id=solver_scope.work_id)
        answered = await client.post(f"/api/sessions/{parent.id}/delegations/{child.id}/answer",
            headers={"Authorization": f"Bearer {token}"}, json={"question_id": qid, "answers": [{"text": "Use the destination already authorized"}]})
        assert answered.status_code == 200
        release_solver.touch()
    async with asyncio.timeout(3):
        while manager.has_pending_question(child.id, qid):
            await asyncio.sleep(.01)
    result = await client.get(f"/api/sessions/{child.id}/questions/{qid}/answer?structured=true")
    assert result.status_code == 200
    resolution = result.json()["resolution"]
    assert resolution["disposition"] == ("answered" if solvable else "closed")
    if not solvable:
        assert result.json()["reason"] == "needs_input" and resolution["answers"] is None
    else:
        assert resolution["source"]["parent_session_id"] == parent.id
    rows = await manager.db.load_messages(child.id)
    assert len([row for row in rows if row["type"] in {"question_answer", "question_closed"}]) == 1


async def test_unattended_child_closes_when_parent_return_admission_is_busy(work_runtime, monkeypatch):
    manager, delegations, original_parent, starts = work_runtime
    parent = await manager.create_session(original_parent.agent_id, "Busy parent", original_parent.working_dir, backend="codex")
    manager.work_test_app.include_router(router)
    await manager.start_message(parent.id, "held scheduled parent", source="schedule")
    caller = await wait_live_scope(await asyncio.wait_for(starts.get(), 3))
    rec = await delegations.start_delegation(parent_session_id=parent.id, agent_name="Worker", request="held child", scope=caller)
    child_scope = await wait_live_scope(await asyncio.wait_for(starts.get(), 3))
    entered, release = asyncio.Event(), asyncio.Event()
    update = manager.db.update_session_field
    async def hold_archive(session_id, **fields):
        await update(session_id, **fields)
        if session_id == parent.id and fields.get("archived"):
            entered.set()
            await release.wait()
    monkeypatch.setattr(manager.db, "update_session_field", hold_archive)
    archiving = asyncio.create_task(manager.archive_session(parent.id, replace=False))
    try:
        await asyncio.wait_for(entered.wait(), 3)
        token = mint(child_scope.session_id, user_id=child_scope.user_id, run_lease_id=child_scope.run_lease_id, work_id=child_scope.work_id)
        created = await manager.work_test_client.post(f"/api/sessions/{rec.delegation_id}/questions",
            headers={"Authorization": f"Bearer {token}"}, json={"questions": [{"question": "Missing parent decision?"}]})
        assert created.status_code == 201
        qid = created.json()["question_id"]
        async with asyncio.timeout(1.5):
            while manager.has_pending_question(rec.delegation_id, qid):
                await asyncio.sleep(.01)
        result = await manager.work_test_client.get(f"/api/sessions/{rec.delegation_id}/questions/{qid}/answer?structured=true")
        assert result.status_code == 200 and result.json()["reason"] == "needs_input"
        assert not release.is_set()
    finally:
        release.set()
        await asyncio.gather(archiving, return_exceptions=True)


@pytest.mark.parametrize("stage", ["queued", "claimed", "preparing"])
async def test_stopping_parent_ends_an_already_accepted_unattended_question_opportunity(work_runtime, tmp_path, monkeypatch, stage):
    manager, delegations, original_parent, starts = work_runtime
    parent = await manager.create_session(original_parent.agent_id, "Stopped parent", original_parent.working_dir, backend="codex")
    manager.work_test_app.include_router(router)
    await manager.start_message(parent.id, f"{'held' if stage == 'queued' else 'controlled'} scheduled parent", source="schedule")
    caller = await wait_live_scope(await asyncio.wait_for(starts.get(), 3))
    rec = await delegations.start_delegation(parent_session_id=parent.id, agent_name="Worker", request="held child", scope=caller)
    child_scope = await wait_live_scope(await asyncio.wait_for(starts.get(), 3))
    token = mint(child_scope.session_id, user_id=child_scope.user_id, run_lease_id=child_scope.run_lease_id, work_id=child_scope.work_id)
    created = await manager.work_test_client.post(f"/api/sessions/{rec.delegation_id}/questions",
        headers={"Authorization": f"Bearer {token}"}, json={"questions": [{"question": "Destination after parent Stop?"}]})
    assert created.status_code == 201
    qid = created.json()["question_id"]
    pending, = manager.get_pending_questions(rec.delegation_id)
    async with asyncio.timeout(3):
        while pending.parent_receipt is None:
            await asyncio.sleep(.01)
    receipt = pending.parent_receipt
    assert receipt.accepted and not receipt._completion.done()
    assert receipt.request_id in {item["request_id"] for item in manager.dispatch_snapshot(parent.id)["items"]}
    release = asyncio.Event()
    if stage != "queued":
        entered = asyncio.Event()
        if stage == "claimed":
            async def hold_claim(event):
                if event.get("type") == "dequeued" and event.get("request_id") == receipt.request_id:
                    entered.set()
                    await release.wait()
            manager.on_broadcast("solver-claim-barrier", hold_claim)
        else:
            runtime = manager._session_owners[id(parent)].runtime
            prepare = runtime._prepare_input
            async def hold_solver(configured, request):
                if request.question_id == qid:
                    entered.set()
                    await release.wait()
                return await prepare(configured, request)
            monkeypatch.setattr(runtime, "_prepare_input", hold_solver)
        (tmp_path / "release").touch()
        await asyncio.wait_for(entered.wait(), 3)
    try:
        await manager.interrupt(parent.id)
    finally:
        release.set()
    async with asyncio.timeout(1.5):
        while manager.has_pending_question(rec.delegation_id, qid):
            await asyncio.sleep(.01)
    snapshot = manager.dispatch_snapshot(parent.id)
    assert snapshot["paused"] and receipt._completion.done()
    assert receipt.request_id not in {item["request_id"] for item in snapshot["items"]}
    result = await manager.work_test_client.get(f"/api/sessions/{rec.delegation_id}/questions/{qid}/answer?structured=true")
    assert result.status_code == 200 and result.json()["reason"] == "needs_input"
    assert result.json()["resolution"]["answers"] is None
    assert starts.empty(), "No solver turn may start while the parent is stopped"
    await manager.await_session_execution(parent.id, timeout=3)
    await manager.resume_dispatch(parent.id)
    completion = await asyncio.wait_for(receipt.wait(), 3)
    assert starts.empty()
    if stage == "preparing":
        assert completion.outcome.status == "interrupted"
    else:
        assert completion.state == "expired"


async def test_unattended_child_does_not_queue_a_solver_in_an_already_stopped_parent(work_runtime):
    manager, delegations, original_parent, starts = work_runtime
    parent = await manager.create_session(original_parent.agent_id, "Already stopped parent", original_parent.working_dir, backend="codex")
    manager.work_test_app.include_router(router)
    await manager.start_message(parent.id, "held scheduled parent", source="schedule")
    caller = await wait_live_scope(await asyncio.wait_for(starts.get(), 3))
    rec = await delegations.start_delegation(parent_session_id=parent.id, agent_name="Worker", request="held child", scope=caller)
    child_scope = await wait_live_scope(await asyncio.wait_for(starts.get(), 3))
    await manager.interrupt(parent.id)
    token = mint(child_scope.session_id, user_id=child_scope.user_id, run_lease_id=child_scope.run_lease_id, work_id=child_scope.work_id)
    created = await manager.work_test_client.post(f"/api/sessions/{rec.delegation_id}/questions",
        headers={"Authorization": f"Bearer {token}"}, json={"questions": [{"question": "Parent was already stopped?"}]})
    assert created.status_code == 201
    qid = created.json()["question_id"]
    async with asyncio.timeout(1.5):
        while manager.has_pending_question(rec.delegation_id, qid):
            await asyncio.sleep(.01)
    snapshot = manager.dispatch_snapshot(parent.id)
    assert snapshot["paused"] and not snapshot["items"] and starts.empty()
    result = await manager.work_test_client.get(f"/api/sessions/{rec.delegation_id}/questions/{qid}/answer?structured=true")
    assert result.status_code == 200 and result.json()["reason"] == "needs_input"
