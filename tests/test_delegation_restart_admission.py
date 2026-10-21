"""Setup ownership and confirmed business refusal before child admission."""

from __future__ import annotations

import asyncio
import json

import pytest

from server.agent_manager import AgentManager
from server.delegations import DelegationError
from server.mcp_identity import revoke_run, verify
from tests.test_delegation_restart import runtime as runtime
from tests.test_delegation_restart import wait_finished
from tests.test_delegation_restart_nested import gated_cli as gated_cli
from tests.test_delegation_restart_security import wait_live_scope


async def test_cancelled_followup_waiter_keeps_new_round_after_restart(runtime, monkeypatch):
    boot, starts = runtime
    current = await boot()
    original = await current.delegations.start_delegation(
        parent_session_id=current.parent_id, agent_name="Worker", request="first review",
    )
    await asyncio.wait_for(starts.get(), 5)
    await wait_finished(current.manager, original)
    get_agent = current.database.get_agent
    waiting, release = asyncio.Event(), asyncio.Event()

    async def pause_before_admission(*args, **kwargs):
        waiting.set()
        await release.wait()
        return await get_agent(*args, **kwargs)

    monkeypatch.setattr(current.database, "get_agent", pause_before_admission)
    following = asyncio.create_task(current.delegations.follow_up_delegation(
        parent_session_id=current.parent_id, delegation_id=original.delegation_id,
        request="second owned review",
    ))
    try:
        await asyncio.wait_for(waiting.wait(), 5)
        following.cancel()
        with pytest.raises(asyncio.CancelledError):
            await following
    finally:
        release.set()
    await current.delegations.confirm_recording()
    await asyncio.wait_for(starts.get(), 5)
    new = current.delegations.get_delegation(original.delegation_id)
    await wait_finished(current.manager, new)
    await current.close()
    restarted = await boot()
    restored = restarted.delegations.get_delegation(original.delegation_id)
    assert restored.work_id == new.work_id != original.work_id
    assert restored.state == "completed" and restored.request == "second owned review"
    assert starts.empty()


async def test_repeated_cancel_of_ask_waiter_keeps_one_owned_round(runtime, monkeypatch):
    boot, starts = runtime
    current = await boot()
    save = current.database.compare_and_set_delegation_state
    waiting, release = asyncio.Event(), asyncio.Event()

    async def pause_after_save(*args, **kwargs):
        saved = await save(*args, **kwargs)
        if json.loads(args[2])["state"] == "running":
            waiting.set()
            await release.wait()
        return saved

    monkeypatch.setattr(current.database, "compare_and_set_delegation_state", pause_after_save)
    setup = asyncio.create_task(current.delegations.start_delegation(
        parent_session_id=current.parent_id, agent_name="Worker", request="first owned review",
    ))
    try:
        await asyncio.wait_for(waiting.wait(), 5)
        setup.cancel()
        await asyncio.sleep(0)
        setup.cancel()
        with pytest.raises(asyncio.CancelledError):
            await setup
    finally:
        release.set()
    await current.delegations.confirm_recording()
    await asyncio.wait_for(starts.get(), 5)
    rec = current.delegations.list_delegations(current.parent_id)[0]
    await wait_finished(current.manager, rec)
    assert rec.state == "completed" and rec.to_public_dict()["recording_state"] == "committed"
    assert starts.empty()
    await current.close()
    restarted = await boot()
    restored = restarted.delegations.get_delegation(rec.delegation_id)
    assert restored.work_id == rec.work_id and restored.state == "completed"
    assert starts.empty()


async def test_nested_caller_refusal_rolls_back_only_unadmitted_child(runtime, gated_cli, monkeypatch):
    boot, starts = runtime
    current = await boot()
    await AgentManager(current.database).create_agent(name="Leaf", backend="codex")
    outer = await current.delegations.start_delegation(
        parent_session_id=current.parent_id, agent_name="Worker", request="outer gate review",
    )
    context = await asyncio.wait_for(starts.get(), 5)
    await wait_live_scope(context)
    scope = verify(next(entry.credential for entry in context.mcp_servers if entry.key == "bg"))
    save = current.database.compare_and_set_delegation_state
    entered, release = asyncio.Event(), asyncio.Event()

    async def pause_child_save(*args, **kwargs):
        saved = await save(*args, **kwargs)
        if args[0] != outer.delegation_id and json.loads(args[2])["state"] == "running":
            entered.set()
            await release.wait()
        return saved

    monkeypatch.setattr(current.database, "compare_and_set_delegation_state", pause_child_save)
    nested = asyncio.create_task(current.delegations.start_delegation(
        parent_session_id=outer.delegation_id, agent_name="Leaf", request="never admitted helper",
        scope=scope,
    ))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        revoke_run(scope.run_lease_id)
    finally:
        release.set()
    with pytest.raises(DelegationError) as refused:
        await nested
    assert refused.value.status_code == 409
    inner = current.delegations.list_delegations(outer.delegation_id)[0]
    assert inner.state == "cancelled" and starts.empty()
    gated_cli.touch()
    await wait_finished(current.manager, outer)
    assert outer.state == "completed" and starts.empty()
    await current.delegations.confirm_recording()
