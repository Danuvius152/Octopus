"""Deleted delegation relationships do not masquerade as storage outages."""
from __future__ import annotations

import asyncio
import json

import pytest

from server.agent_manager import AgentManager
from server.delegations import DelegationError
from tests.test_delegation_restart import runtime as runtime
from tests.test_delegation_restart_security import completed_before_restart


@pytest.mark.parametrize(("deleted", "status"), [("parent", 404), ("child", 409)])
async def test_relationship_deleted_during_followup_setup_is_reported_as_gone(
    runtime, monkeypatch, deleted, status,
):
    boot, starts = runtime
    current, original = await completed_before_restart(boot, starts)
    assert current.manager.get_session(original.delegation_id) is not None
    save = current.database.compare_and_set_delegation_state
    saving = asyncio.Event()
    release = asyncio.Event()

    async def gated_save(*args, **kwargs):
        if json.loads(args[2])["state"] == "running":
            saving.set()
            await release.wait()
        return await save(*args, **kwargs)

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
