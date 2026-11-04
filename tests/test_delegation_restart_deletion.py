"""Deleted delegation relationships do not masquerade as storage outages."""
from __future__ import annotations

import asyncio
import json

import pytest

from server.agent_manager import AgentManager
from server.delegations import DelegationError
from server.mcp_identity import verify
from tests.test_delegation_restart import runtime as runtime
from tests.test_delegation_restart import wait_finished
from tests.test_delegation_restart_nested import gated_cli as gated_cli
from tests.test_delegation_restart_security import (
    completed_before_restart,
    wait_live_scope,
)


@pytest.mark.parametrize(("deleted", "status"), [("parent", 404), ("child", 409)])
@pytest.mark.parametrize("phase", ["before-cas", "after-cas", "before-input"])
async def test_relationship_deleted_during_followup_setup_is_reported_as_gone(
    runtime, monkeypatch, deleted, status, phase,
):
    boot, starts = runtime
    current, original = await completed_before_restart(boot, starts)
    assert current.manager.get_session(original.delegation_id) is not None
    save = current.database.compare_and_set_delegation_state
    saving = asyncio.Event()
    release = asyncio.Event()

    async def gated_save(*args, **kwargs):
        if phase == "after-cas":
            result = await save(*args, **kwargs)
        if json.loads(args[2])["state"] == "running":
            saving.set()
            await release.wait()
        return result if phase == "after-cas" else await save(*args, **kwargs)

    if phase == "before-input":
        get_agent = current.database.get_agent

        async def gated_agent(*args, **kwargs):
            saving.set()
            await release.wait()
            return await get_agent(*args, **kwargs)

        monkeypatch.setattr(current.database, "get_agent", gated_agent)
    else:
        monkeypatch.setattr(current.database, "compare_and_set_delegation_state", gated_save)
    following = asyncio.create_task(current.delegations.follow_up_delegation(
        parent_session_id=current.parent_id, delegation_id=original.delegation_id,
        request="held review after setup",
    ))
    try:
        await asyncio.wait_for(saving.wait(), 5)
        deleted_id = current.parent_id if deleted == "parent" else original.delegation_id
        assert await current.manager.delete_session(deleted_id)
    finally:
        release.set()
        result = (await asyncio.gather(following, return_exceptions=True))[0]
    assert isinstance(result, DelegationError)
    assert result.status_code == status
    assert "gone" in str(result)
    assert starts.empty()
    await current.delegations.confirm_recording()
    assert not current.delegations.drain_blockers()
    assert not await current.manager.delete_session(deleted_id)
    assert not current.delegations.drain_blockers()


@pytest.mark.parametrize("changed", ["target", "parent", "origin"])
async def test_changed_child_relation_is_a_confirmed_refusal_even_with_the_same_snapshot(
    runtime, monkeypatch, changed,
):
    boot, starts = runtime
    current, original = await completed_before_restart(boot, starts)
    previous = await current.database.load_delegation_state(
        original.delegation_id, parent_session_id=current.parent_id, user_id=current.manager.get_session(current.parent_id).user_id,
    )
    save = current.database.compare_and_set_delegation_state
    entered, release = asyncio.Event(), asyncio.Event()

    async def pause_new_round(*args, **kwargs):
        if json.loads(args[2])["state"] == "running":
            entered.set()
            await release.wait()
        return await save(*args, **kwargs)

    monkeypatch.setattr(current.database, "compare_and_set_delegation_state", pause_new_round)
    following = asyncio.create_task(current.delegations.follow_up_delegation(
        parent_session_id=current.parent_id, delegation_id=original.delegation_id,
        request="held stale relationship review",
    ))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        if changed == "target":
            replacement = await AgentManager(current.database).create_agent(
                name="Replacement", backend="codex", user_id=current.manager.get_session(current.parent_id).user_id,
            )
            await current.database.update_session_field(original.delegation_id, agent_id=replacement["id"])
            # The read scope intentionally does not include a target argument:
            # an equal old snapshot cannot confirm authority for the old target.
            assert await current.database.load_delegation_state(
                original.delegation_id, parent_session_id=current.parent_id, user_id=current.manager.get_session(current.parent_id).user_id,
            ) == previous
        elif changed == "origin":
            await current.database.update_session_field(original.delegation_id, origin="user")
        else:
            parent = await current.manager.create_session(
                (await current.database.get_system_agent())["id"], "Other parent", backend="codex",
            )
            async with current.database.transaction(write=True) as tx:
                await tx.execute("UPDATE sessions SET parent_session_id = ? WHERE id = ?", (parent.id, original.delegation_id))
    finally:
        release.set()
    with pytest.raises(DelegationError) as refused:
        await following
    assert refused.value.status_code == 409
    current_round = current.delegations.get_delegation(original.delegation_id)
    assert current_round.work_id == original.work_id and current_round.state == "completed"
    assert starts.empty()
    await current.delegations.confirm_recording()


@pytest.mark.parametrize("deleted", ["parent", "child"])
@pytest.mark.parametrize("lost_receipt", [False, True])
async def test_deletion_after_input_acceptance_keeps_original_round_responsibility(runtime, monkeypatch, deleted, lost_receipt):
    boot, starts = runtime
    current, original = await completed_before_restart(boot, starts)
    submit = current.manager.start_message
    accepted, release = asyncio.Event(), asyncio.Event()
    receipts = []

    async def held_receipt(*args, **kwargs):
        receipt = await submit(*args, **kwargs)
        receipts.append(receipt)
        accepted.set()
        await release.wait()
        if lost_receipt:
            raise ValueError("Original acceptance receipt unavailable")
        return receipt

    monkeypatch.setattr(current.manager, "start_message", held_receipt)
    following = asyncio.create_task(current.delegations.follow_up_delegation(
        parent_session_id=current.parent_id, delegation_id=original.delegation_id,
        request="held accepted review",
    ))
    try:
        await asyncio.wait_for(accepted.wait(), 5)
        await asyncio.wait_for(starts.get(), 5)
        rec = current.delegations.get_delegation(original.delegation_id)
        assert receipts[0].accepted and rec.work_id != original.work_id
        deleted_id = current.parent_id if deleted == "parent" else original.delegation_id
        assert await current.manager.delete_session(deleted_id)
        assert current.delegations.get_delegation(original.delegation_id) is rec
        assert current.delegations.drain_blockers()
    finally:
        release.set()
    if lost_receipt:
        with pytest.raises(DelegationError, match="receipt unavailable"):
            await following
    else:
        assert await following is rec
    assert current.delegations.get_delegation(original.delegation_id) is rec
    with pytest.raises(DelegationError):
        await current.delegations.cancel_delegation(rec.delegation_id, work_id=rec.work_id)
    assert current.delegations.drain_blockers(), "A missing row cannot rewrite accepted or unknown input as never submitted"
    await current.delegations.confirm_recording()


async def test_deleted_unadmitted_followup_withdraws_only_its_original_parent_work_responsibility(runtime, gated_cli, monkeypatch):
    boot, starts = runtime
    current = await boot()
    await AgentManager(current.database).create_agent(
        name="Leaf", backend="codex", user_id=current.manager.get_session(current.parent_id).user_id,
    )
    outer = await current.delegations.start_delegation(
        parent_session_id=current.parent_id, agent_name="Worker", request="outer gate review",
    )
    context = await asyncio.wait_for(starts.get(), 5)
    await wait_live_scope(context)
    scope = verify(next(entry.credential for entry in context.mcp_servers if entry.key == "bg"))
    child = await current.delegations.start_delegation(
        parent_session_id=outer.delegation_id, agent_name="Leaf", request="first nested review", scope=scope,
    )
    await asyncio.wait_for(starts.get(), 5)
    await wait_finished(current.manager, child)
    save = current.database.compare_and_set_delegation_state
    saved, release = asyncio.Event(), asyncio.Event()
    setup_work = None

    async def after_cas(*args, **kwargs):
        nonlocal setup_work
        result = await save(*args, **kwargs)
        snapshot = json.loads(args[2])
        if args[0] == child.delegation_id and snapshot["state"] == "running":
            setup_work = snapshot["work_id"]
            saved.set()
            await release.wait()
        return result

    monkeypatch.setattr(current.database, "compare_and_set_delegation_state", after_cas)
    following = asyncio.create_task(current.delegations.follow_up_delegation(
        parent_session_id=outer.delegation_id, delegation_id=child.delegation_id,
        request="unadmitted nested continuation", scope=scope,
    ))
    try:
        await asyncio.wait_for(saved.wait(), 5)
        assert await current.manager.delete_session(child.delegation_id)
    finally:
        release.set()
    with pytest.raises(DelegationError):
        await following
    parent = await current.database.load_delegation_state(
        outer.delegation_id, parent_session_id=current.parent_id,
        user_id=current.manager.get_session(current.parent_id).user_id,
    )
    assert setup_work not in json.loads(parent)["child_work_ids"]
    assert setup_work not in {row.work_id for row in current.delegations.list_delegations(outer.delegation_id)}
    assert starts.empty()
    gated_cli.touch()
    await wait_finished(current.manager, outer)
    await current.delegations.confirm_recording()
    assert not current.delegations.drain_blockers()


async def test_changed_relationship_after_cas_does_not_use_deleted_setup_exit(runtime, monkeypatch):
    boot, starts = runtime
    current, original = await completed_before_restart(boot, starts)
    save = current.database.compare_and_set_delegation_state
    saved, release = asyncio.Event(), asyncio.Event()

    async def after_cas(*args, **kwargs):
        result = await save(*args, **kwargs)
        if json.loads(args[2])["state"] == "running":
            saved.set()
            await release.wait()
        return result

    monkeypatch.setattr(current.database, "compare_and_set_delegation_state", after_cas)
    following = asyncio.create_task(current.delegations.follow_up_delegation(
        parent_session_id=current.parent_id, delegation_id=original.delegation_id,
        request="held changed-relation review",
    ))
    try:
        await asyncio.wait_for(saved.wait(), 5)
        await current.database.update_session_field(original.delegation_id, origin="user")
        # Force a real pre-admission refusal after the relationship changes.
        await AgentManager(current.database).archive_agent(original.target_agent_id)
    finally:
        release.set()
    with pytest.raises(DelegationError, match="relationship changed"):
        await following
    rec = current.delegations.get_delegation(original.delegation_id)
    assert rec.work_id != original.work_id and rec.state == "running"
    assert current.delegations.drain_blockers()
    assert starts.empty()
    await current.delegations.confirm_recording()
