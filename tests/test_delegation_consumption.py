"""Every owned reply must finish its actual parent carrier before final delivery."""

import asyncio

import pytest

from server.agent_manager import AgentManager
from server.delegations import DelegationError
from server.mcp_identity import revoke_run
from tests.test_delegation_notification_failure import (
    notification_runtime as notification_runtime,
)
from tests.test_delegation_notification_failure import (
    release_model,
    wait_round_finished,
)
from tests.test_delegation_text_deletion import observe_terminal_deliveries


async def starts(runtime, session_id, count):
    async with asyncio.timeout(3):
        while True:
            observed = [scope for scope, _ in runtime.starts if scope.session_id == session_id]
            if len(observed) >= count and (runtime.root / f"{observed[count - 1].run_lease_id}.input").exists():
                return
            await asyncio.sleep(.005)


@pytest.mark.parametrize("reuse_leaf", [False, True])
@pytest.mark.parametrize("child_count", [2, 3])
async def test_fast_owned_replies_wait_for_each_exact_parent_input(notification_runtime, monkeypatch, reuse_leaf, child_count):
    runtime = notification_runtime
    runtime.controls.enabled = False
    deliveries = observe_terminal_deliveries(runtime, monkeypatch)
    for name in ("Leaf A", "Leaf B", "Leaf C"):
        await AgentManager(runtime.manager.db).create_agent(name=name, backend="codex", user_id=runtime.parent.user_id)
    outer = await runtime.delegations.start_delegation(parent_session_id=runtime.parent.id,
        agent_name="Notification worker", request="Use two owned replies before replying")
    await starts(runtime, outer.delegation_id, 1)
    caller = next(scope for scope, _ in runtime.starts if scope.session_id == outer.delegation_id)
    first = await runtime.delegations.start_delegation(parent_session_id=outer.delegation_id,
        agent_name="Leaf A", request="First owned reply", scope=caller)
    await release_model(runtime, first)
    await wait_round_finished(runtime, first)
    previous = first
    for index in range(1, child_count):
        if reuse_leaf:
            following = await runtime.delegations.follow_up_delegation(parent_session_id=outer.delegation_id,
                delegation_id=first.delegation_id, request=f"Owned reply {index}", scope=caller)
            assert previous.work_id != following.work_id
        else:
            following = await runtime.delegations.start_delegation(parent_session_id=outer.delegation_id,
                agent_name=f"Leaf {chr(65 + index)}", request=f"Owned reply {index}", scope=caller)
        await release_model(runtime, following)
        await wait_round_finished(runtime, following)
        previous = following
    await release_model(runtime, outer)
    # A success processing A cannot consume the already queued return B,
    # even when both children ended before the initiating turn ended.
    for index in range(child_count):
        await starts(runtime, outer.delegation_id, index + 2)
        await runtime.delegations.confirm_recording()
        assert outer.state == "running"
        assert not [delivery for delivery in deliveries if delivery[2]["delivery_id"] == f"agent-terminal:{outer.work_id}"]
        await release_model(runtime, outer)
    await wait_round_finished(runtime, outer)
    assert outer.state == "completed"
    relayed = [delivery for delivery in deliveries if delivery[2]["delivery_id"] == f"agent-terminal:{outer.work_id}"]
    assert len(relayed) == 1
    parent_inputs = [receipt for sid, work, receipt in runtime.receipts if sid == outer.delegation_id and work == outer.work_id]
    assert len(parent_inputs) == child_count + 1
    last = await parent_inputs[-1].wait()
    assert last.request_id == parent_inputs[-1].request_id and last.outcome.status == "success"


@pytest.mark.parametrize("first", ["seal", "child"])
async def test_child_admission_and_parent_seal_have_one_order(notification_runtime, monkeypatch, first):
    runtime = notification_runtime
    runtime.controls.enabled = False
    await AgentManager(runtime.manager.db).create_agent(name="Race leaf", backend="codex", user_id=runtime.parent.user_id)
    outer = await runtime.delegations.start_delegation(parent_session_id=runtime.parent.id,
        agent_name="Notification worker", request="Finish while a child is admitted")
    await starts(runtime, outer.delegation_id, 1)
    caller = next(scope for scope, _ in runtime.starts if scope.session_id == outer.delegation_id)
    entered, release = asyncio.Event(), asyncio.Event()
    if first == "seal":
        original = runtime.delegations._commit_admission

        async def delayed(previous, proposed, user_id):
            if proposed.parent_work_id == outer.work_id:
                entered.set()
                await release.wait()
            return await original(previous, proposed, user_id)

        monkeypatch.setattr(runtime.delegations, "_commit_admission", delayed)
    else:
        original = runtime.manager.start_message

        async def delayed(session_id, content, **kwargs):
            session = runtime.manager.get_session(session_id)
            if session is not None and session.parent_session_id == outer.delegation_id:
                # Responsibility is registered and the caller checked. A CAS
                # barrier alone cannot keep the caller's run lease alive.
                entered.set()
                await release.wait()
            return await original(session_id, content, **kwargs)

        monkeypatch.setattr(runtime.manager, "start_message", delayed)
    child = asyncio.create_task(runtime.delegations.start_delegation(parent_session_id=outer.delegation_id,
        agent_name="Race leaf", request="Competing owned reply", scope=caller))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        await release_model(runtime, outer)
        if first == "seal":
            async with asyncio.timeout(2):
                while outer.state == "running":
                    await asyncio.sleep(.005)
            release.set()
            with pytest.raises(DelegationError, match="closed"):
                await child
            sessions = await runtime.manager.db.load_sessions(user_id=runtime.parent.user_id, include_archived=True)
            refused = [row for row in sessions if row["parent_session_id"] == outer.delegation_id]
            assert len(refused) == 1 and refused[0]["archived"]
            assert not await runtime.manager.db.session_has_inputs(refused[0]["id"])
            assert not outer._child_work_ids
        else:
            async with asyncio.timeout(2):
                while outer._candidate_request_id is None:
                    await asyncio.sleep(.005)
            assert outer.state == "running" and len(outer._child_work_ids) == 1
            release.set()
            leaf = await child
            await runtime.manager.await_session_execution(outer.delegation_id)
            await runtime.delegations.confirm_recording()
            assert outer.state == "running" and outer._child_work_ids == {leaf.work_id}
            await release_model(runtime, leaf)
            await wait_round_finished(runtime, leaf)
            await starts(runtime, outer.delegation_id, 2)
            await release_model(runtime, outer)
            await wait_round_finished(runtime, outer)
            assert outer.state == "completed" and not outer._child_work_ids
    finally:
        release.set()
        await asyncio.gather(child, return_exceptions=True)


@pytest.mark.parametrize("setups", [1, 2])
async def test_withdrawing_unadmitted_child_rechecks_the_already_successful_parent(notification_runtime, monkeypatch, setups):
    runtime = notification_runtime
    runtime.controls.enabled = False
    await AgentManager(runtime.manager.db).create_agent(name="Refused leaf", backend="codex", user_id=runtime.parent.user_id)
    deliveries = observe_terminal_deliveries(runtime, monkeypatch)
    outer = await runtime.delegations.start_delegation(parent_session_id=runtime.parent.id,
        agent_name="Notification worker", request="Complete before child setup is refused")
    await starts(runtime, outer.delegation_id, 1)
    caller = next(scope for scope, _ in runtime.starts if scope.session_id == outer.delegation_id)
    entered, release = asyncio.Event(), asyncio.Event()
    commit = runtime.delegations._commit_admission
    second_waiting, second_go = asyncio.Event(), asyncio.Event()
    admissions = 0

    async def delayed(previous, proposed, user_id):
        nonlocal admissions
        if proposed.parent_work_id == outer.work_id:
            admissions += 1
            if admissions == 2:
                second_waiting.set()
                await second_go.wait()
        saved = await commit(previous, proposed, user_id)
        if proposed.parent_work_id == outer.work_id:
            entered.set()
            await release.wait()
        return saved

    monkeypatch.setattr(runtime.delegations, "_commit_admission", delayed)
    child = asyncio.create_task(runtime.delegations.start_delegation(parent_session_id=outer.delegation_id,
        agent_name="Refused leaf", request="Never admitted", scope=caller))
    later = None
    try:
        await asyncio.wait_for(entered.wait(), 2)
        if setups == 2:
            later = asyncio.create_task(runtime.delegations.start_delegation(parent_session_id=outer.delegation_id,
                agent_name="Refused leaf", request="Also never admitted", scope=caller))
            await asyncio.wait_for(second_waiting.wait(), 2)
        await release_model(runtime, outer)
        async with asyncio.timeout(2):
            while outer._candidate_request_id is None:
                await asyncio.sleep(.005)
        assert outer.state == "running" and len(outer._child_work_ids) == 1
        if setups == 2:
            entered.clear()
            second_go.set()
            await asyncio.wait_for(entered.wait(), 2)
            assert len(outer._child_work_ids) == 2
        revoke_run(caller.run_lease_id)
        release.set()
        with pytest.raises(DelegationError):
            await child
        if later is not None:
            with pytest.raises(DelegationError):
                await later
        await runtime.delegations.confirm_recording()
        assert outer.state == "completed" and not outer._child_work_ids
        assert len([delivery for delivery in deliveries if delivery[2]["delivery_id"] == f"agent-terminal:{outer.work_id}"]) == 1
    finally:
        second_go.set()
        release.set()
        await asyncio.gather(*[task for task in (child, later) if task is not None], return_exceptions=True)
