"""Complete delegation commands survive callers that stop waiting."""

from __future__ import annotations

import asyncio
import json

import pytest

from server.agent_manager import AgentManager
from server.delegations import DelegationError, DelegationManager
from tests.test_delegation_restart import runtime as runtime
from tests.test_delegation_restart import wait_finished
from tests.test_delegation_restart_security import (
    completed_before_restart,
    wait_live_scope,
)
from tests.test_delegation_work import work_runtime as work_runtime


@pytest.mark.parametrize("command", ["ask", "followup", "cancel"])
async def test_unbound_manager_refuses_public_command_without_acquiring_work(command):
    delegations = DelegationManager()
    with pytest.raises(DelegationError) as rejected:
        if command == "ask":
            await delegations.start_delegation(parent_session_id="parent", agent_name="Worker", request="review")
        elif command == "followup":
            await delegations.follow_up_delegation(parent_session_id="parent", delegation_id="child", request="review")
        else:
            await delegations.cancel_delegation("child")
    assert rejected.value.status_code == 500


async def test_cancelled_ask_waiter_keeps_owned_setup_and_starts_one_child(runtime, monkeypatch):
    boot, starts = runtime
    current = await boot()
    save = current.database.compare_and_set_delegation_state
    entered, release = asyncio.Event(), asyncio.Event()

    async def paused_save(*args, **kwargs):
        if json.loads(args[2])["state"] == "running":
            entered.set()
            await release.wait()
        return await save(*args, **kwargs)

    monkeypatch.setattr(current.database, "compare_and_set_delegation_state", paused_save)
    asking = asyncio.create_task(current.delegations.start_delegation(
        parent_session_id=current.parent_id, agent_name="Worker", request="first owned review",
    ))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        # No live round exists until the complete first CAS has committed.
        assert current.delegations.list_delegations(current.parent_id) == []
        asking.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asking
    finally:
        release.set()
    await current.delegations.confirm_recording()
    await asyncio.wait_for(starts.get(), 5)
    rec = current.delegations.list_delegations(current.parent_id)[0]
    await wait_finished(current.manager, rec)
    assert rec.state == "completed"
    assert starts.empty()


async def test_cancelled_followup_waiter_still_admits_the_new_round(runtime, monkeypatch):
    boot, starts = runtime
    current, original = await completed_before_restart(boot, starts)
    get_agent = current.database.get_agent
    entered, release = asyncio.Event(), asyncio.Event()

    async def pause_before_admission(*args, **kwargs):
        entered.set()
        await release.wait()
        return await get_agent(*args, **kwargs)

    monkeypatch.setattr(current.database, "get_agent", pause_before_admission)
    following = asyncio.create_task(current.delegations.follow_up_delegation(
        parent_session_id=current.parent_id, delegation_id=original.delegation_id,
        request="second owned review",
    ))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        following.cancel()
        with pytest.raises(asyncio.CancelledError):
            await following
    finally:
        release.set()
    await current.delegations.confirm_recording()
    await asyncio.wait_for(starts.get(), 5)
    new = current.delegations.get_delegation(original.delegation_id)
    await wait_finished(current.manager, new)
    assert new.work_id != original.work_id
    assert new.request == "second owned review" and new.state == "completed"
    assert starts.empty()


async def test_terminal_cas_keeps_list_running_until_commit_acknowledged(runtime, monkeypatch):
    boot, starts = runtime
    current = await boot()
    save = current.database.compare_and_set_delegation_state
    entered, release = asyncio.Event(), asyncio.Event()

    async def pause_before_terminal_commit(*args, **kwargs):
        if json.loads(args[2])["state"] == "completed":
            entered.set()
            await release.wait()
        return await save(*args, **kwargs)

    monkeypatch.setattr(current.database, "compare_and_set_delegation_state", pause_before_terminal_commit)
    rec = await current.delegations.start_delegation(
        parent_session_id=current.parent_id, agent_name="Worker", request="review result publication",
    )
    await asyncio.wait_for(starts.get(), 5)
    try:
        await asyncio.wait_for(entered.wait(), 5)
        assert current.delegations.list_delegations(current.parent_id)[0].state == "running"
        assert current.manager.dispatch_snapshot(current.parent_id)["items"] == []
    finally:
        release.set()
    await wait_finished(current.manager, rec)
    await current.delegations.confirm_recording()
    assert rec.state == "completed"
    assert len(current.manager.dispatch_snapshot(current.parent_id)["items"]) == 1


async def test_cancelled_cancel_waiter_still_stops_owned_bg_and_relays_once(work_runtime, monkeypatch):
    manager, delegations, parent, starts = work_runtime
    rec = await delegations.start_delegation(
        parent_session_id=parent.id, agent_name="Worker", request="held cancel review",
    )
    context = await asyncio.wait_for(starts.get(), 5)
    await wait_live_scope(context)
    token = next(item.credential for item in context.mcp_servers if item.key == "bg")
    response = await manager.work_test_client.post(
        f"/api/sessions/{rec.delegation_id}/bg-tasks",
        headers={"Authorization": f"Bearer {token}"},
        json={"command": "echo owned; sleep 30", "idle_timeout_seconds": 0},
    )
    assert response.status_code == 201, response.text
    task_id = response.json()["id"]
    save = manager.db.compare_and_set_delegation_state
    entered, release = asyncio.Event(), asyncio.Event()

    async def pause_cancel_commit(*args, **kwargs):
        if json.loads(args[2])["state"] == "cancelled":
            entered.set()
            await release.wait()
        return await save(*args, **kwargs)

    monkeypatch.setattr(manager.db, "compare_and_set_delegation_state", pause_cancel_commit)
    cancelling = asyncio.create_task(delegations.cancel_delegation(rec.delegation_id))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        assert delegations.list_delegations(parent.id)[0].state == "running"
        cancelling.cancel()
        with pytest.raises(asyncio.CancelledError):
            await cancelling
    finally:
        release.set()
    await delegations.confirm_recording()
    async with asyncio.timeout(5):
        while (await manager.work_test_bg.get_task(task_id)).status == "running":
            await asyncio.sleep(.01)
    stopped = await manager.work_test_bg.get_task(task_id)
    assert stopped.status == "cancelled" and stopped.stop_reason == "work_cancelled"
    assert rec.state == "cancelled"
    assert len(manager.dispatch_snapshot(parent.id)["items"]) == 1
    await delegations.cancel_delegation(rec.delegation_id)
    assert len(manager.dispatch_snapshot(parent.id)["items"]) == 1


async def test_delegation_answers_oldest_pending_form_after_an_earlier_form_was_answered(work_runtime):
    manager, delegations, parent, starts = work_runtime
    rec = await delegations.start_delegation(
        parent_session_id=parent.id, agent_name="Worker", request="held question review",
    )
    await wait_live_scope(await asyncio.wait_for(starts.get(), 5))
    question = [{"question": "Review?", "options": [{"label": "Yes"}]}]
    first = await manager.create_pending_question(rec.delegation_id, question)
    second = await manager.create_pending_question(rec.delegation_id, question)
    assert await manager.answer_question(rec.delegation_id, first, [{"selected": ["Yes"]}])
    answered = await delegations.answer_pending_question(rec.delegation_id, "Yes")
    assert answered["question_id"] == second
    assert await manager.wait_for_question_answer(rec.delegation_id, first, timeout=.01) == "Q: Review?\nA: Yes"
    assert await manager.wait_for_question_answer(rec.delegation_id, second, timeout=.01) == "Q: Review?\nA: Yes"
    assert manager.get_pending_questions(rec.delegation_id) == ()


async def test_followup_rolls_back_confirmed_round_when_real_session_refuses_archived_target(runtime, monkeypatch):
    boot, starts = runtime
    current, original = await completed_before_restart(boot, starts)
    assert current.manager.get_session(original.delegation_id) is not None
    confirmed = current.delegations.get_delegation(original.delegation_id)
    previous = confirmed.to_public_dict()
    snapshot = await current.database.load_delegation_state(
        original.delegation_id, parent_session_id=current.parent_id, user_id=current.manager.get_session(current.parent_id).user_id,
    )
    messages = await current.database.load_messages(original.delegation_id)
    agents = AgentManager(current.database)
    await agents.archive_agent(original.target_agent_id)
    save = current.database.compare_and_set_delegation_state
    committed_rounds = []

    async def record_confirmed_round(*args, **kwargs):
        changed = await save(*args, **kwargs)
        proposed = json.loads(args[2])
        if changed and proposed["state"] == "running":
            committed_rounds.append(proposed["work_id"])
        return changed

    monkeypatch.setattr(current.database, "compare_and_set_delegation_state", record_confirmed_round)
    with pytest.raises(DelegationError) as refused:
        await current.delegations.follow_up_delegation(
            parent_session_id=current.parent_id, delegation_id=original.delegation_id,
            request="review while the target is archived",
        )
    assert refused.value.status_code == 409
    assert len(committed_rounds) == 1 and committed_rounds[0] != original.work_id
    assert current.delegations.get_delegation(original.delegation_id).to_public_dict() == previous
    assert await current.database.load_delegation_state(
        original.delegation_id, parent_session_id=current.parent_id, user_id=current.manager.get_session(current.parent_id).user_id,
    ) == snapshot
    assert await current.database.get_latest_delegation_request(
        original.delegation_id, committed_rounds[0], parent_session_id=current.parent_id, user_id=current.manager.get_session(current.parent_id).user_id,
    ) is None
    assert await current.database.load_messages(original.delegation_id) == messages
    assert current.manager.dispatch_snapshot(original.delegation_id)["items"] == []
    assert starts.empty()
    await current.delegations.confirm_recording()

    await agents.unarchive_agent(original.target_agent_id)
    following = await current.delegations.follow_up_delegation(
        parent_session_id=current.parent_id, delegation_id=original.delegation_id,
        request="review after restoring the target",
    )
    await asyncio.wait_for(starts.get(), 5)
    await wait_finished(current.manager, following)
    assert following.state == "completed"
    assert following.work_id not in {original.work_id, committed_rounds[0]}
    assert starts.empty()
    await current.delegations.confirm_recording()
