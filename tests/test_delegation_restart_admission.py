"""Unadmitted delegation setup cannot replace a confirmed reusable round."""
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


async def test_cancelled_followup_before_admission_restores_previous_round_after_restart(
    runtime, monkeypatch,
):
    boot, starts = runtime
    current = await boot()
    original = await current.delegations.start_delegation(
        parent_session_id=current.parent_id, agent_name="Worker", request="first review",
    )
    await asyncio.wait_for(starts.get(), 5)
    await wait_finished(current.manager, original)
    get_agent = current.database.get_agent
    waiting = asyncio.Event()

    async def pause_before_admission(*args, **kwargs):
        waiting.set()
        await asyncio.Event().wait()
        return await get_agent(*args, **kwargs)

    monkeypatch.setattr(current.database, "get_agent", pause_before_admission)
    followup = asyncio.create_task(current.delegations.follow_up_delegation(
        parent_session_id=current.parent_id, delegation_id=original.delegation_id,
        request="never admitted review",
    ))
    try:
        await asyncio.wait_for(waiting.wait(), 5)
        followup.cancel()
        with pytest.raises(asyncio.CancelledError):
            await followup
    finally:
        monkeypatch.setattr(current.database, "get_agent", get_agent)
        if not followup.done():
            followup.cancel()
            await asyncio.gather(followup, return_exceptions=True)
    assert starts.empty()
    await current.close()
    restarted = await boot()
    restored = restarted.delegations.get_delegation(original.delegation_id)
    assert restored.work_id == original.work_id
    assert restored.state == "completed"
    assert restored.request == "first review"
    assert starts.empty()


async def test_cancelled_initial_setup_finishes_its_writer_without_a_phantom_run(
    runtime, monkeypatch,
):
    boot, starts = runtime
    current = await boot()
    save = current.database.compare_and_set_delegation_state
    waiting = asyncio.Event()
    release = asyncio.Event()

    async def pause_after_save(*args, **kwargs):
        saved = await save(*args, **kwargs)
        if json.loads(args[2])["state"] == "running":
            waiting.set()
            await release.wait()
        return saved

    monkeypatch.setattr(current.database, "compare_and_set_delegation_state", pause_after_save)
    setup = asyncio.create_task(current.delegations.start_delegation(
        parent_session_id=current.parent_id, agent_name="Worker", request="never launched first review",
    ))
    try:
        await asyncio.wait_for(waiting.wait(), 5)
        setup.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await setup
    finally:
        release.set()
        if not setup.done():
            setup.cancel()
            await asyncio.gather(setup, return_exceptions=True)
    rec = current.delegations.list_delegations(current.parent_id)[0]
    assert rec.state == "cancelled"
    assert rec.to_public_dict()["recording_state"] == "committed"
    assert not current.manager.has_pending_or_active_work(rec.delegation_id, rec.work_id)
    assert starts.empty()
    await current.close()
    restarted = await boot()
    restored = restarted.delegations.get_delegation(rec.delegation_id)
    assert restored.state == "cancelled"
    assert starts.empty()


async def test_nested_setup_save_failure_does_not_leave_parent_waiting_for_unstarted_child(
    runtime, gated_cli, monkeypatch,
):
    boot, starts = runtime
    current = await boot()
    await AgentManager(current.database).create_agent(name="Leaf", backend="codex")
    outer = await current.delegations.start_delegation(
        parent_session_id=current.parent_id, agent_name="Worker", request="outer gate review",
    )
    outer_context = await asyncio.wait_for(starts.get(), 5)
    save = current.database.compare_and_set_delegation_state
    fail = True

    async def fail_relay_save_once(*args, **kwargs):
        nonlocal fail
        if fail and args[0] == outer.delegation_id and json.loads(args[2])["awaiting_sub_relay"]:
            fail = False
            raise OSError("synthetic nested setup save failure")
        return await save(*args, **kwargs)

    monkeypatch.setattr(current.database, "compare_and_set_delegation_state", fail_relay_save_once)
    with pytest.raises(DelegationError, match="saving failed"):
        await current.delegations.start_delegation(
            parent_session_id=outer.delegation_id, agent_name="Leaf", request="never admitted leaf review",
            scope=verify(next(entry.credential for entry in outer_context.mcp_servers if entry.key == "bg")),
        )
    inner = current.delegations.list_delegations(outer.delegation_id)[0]
    assert inner.state == "cancelled"
    assert starts.empty()
    gated_cli.touch()
    await wait_finished(current.manager, outer)
    assert outer.state == "completed"
    assert starts.empty()


async def test_unconfirmed_followup_rollback_retains_new_receipt_and_original_cancellation(
    runtime, monkeypatch,
):
    boot, starts = runtime
    current = await boot()
    original = await current.delegations.start_delegation(
        parent_session_id=current.parent_id, agent_name="Worker", request="first confirmed review",
    )
    await asyncio.wait_for(starts.get(), 5)
    await wait_finished(current.manager, original)
    get_agent = current.database.get_agent
    save = current.database.compare_and_set_delegation_state
    waiting = asyncio.Event()

    async def pause_before_admission(*args, **kwargs):
        waiting.set()
        await asyncio.Event().wait()
        return await get_agent(*args, **kwargs)

    async def fail_rollback(*args, **kwargs):
        if json.loads(args[2])["work_id"] == original.work_id:
            raise OSError("synthetic rollback storage failure")
        return await save(*args, **kwargs)

    monkeypatch.setattr(current.database, "get_agent", pause_before_admission)
    monkeypatch.setattr(current.database, "compare_and_set_delegation_state", fail_rollback)
    following = asyncio.create_task(current.delegations.follow_up_delegation(
        parent_session_id=current.parent_id, delegation_id=original.delegation_id,
        request="never admitted second review",
    ))
    try:
        await asyncio.wait_for(waiting.wait(), 5)
        following.cancel()
        with pytest.raises(asyncio.CancelledError):
            await following
        unconfirmed = current.delegations.get_delegation(original.delegation_id)
        assert unconfirmed.work_id != original.work_id
        assert unconfirmed.to_public_dict()["recording_state"] == "unconfirmed"
        with pytest.raises(RuntimeError, match="unconfirmed"):
            await current.database.close()
        assert starts.empty()
    finally:
        monkeypatch.setattr(current.database, "get_agent", get_agent)
        monkeypatch.setattr(current.database, "compare_and_set_delegation_state", save)
        if not following.done():
            following.cancel()
            await asyncio.gather(following, return_exceptions=True)
        current_round = current.delegations.get_delegation(original.delegation_id)
        if current_round.work_id != original.work_id:
            await current.delegations.cancel_delegation(
                current_round.delegation_id, work_id=current_round.work_id,
            )


async def test_repeated_cancel_does_not_abandon_initial_round_recording_owner(
    runtime, monkeypatch,
):
    boot, starts = runtime
    current = await boot()
    save = current.database.compare_and_set_delegation_state
    saving = asyncio.Event()
    release = asyncio.Event()

    async def pause_after_running_commit(*args, **kwargs):
        saved = await save(*args, **kwargs)
        if json.loads(args[2])["state"] == "running":
            saving.set()
            await release.wait()
        return saved

    monkeypatch.setattr(current.database, "compare_and_set_delegation_state", pause_after_running_commit)
    setup = asyncio.create_task(current.delegations.start_delegation(
        parent_session_id=current.parent_id, agent_name="Worker", request="never admitted repeated cancel",
    ))
    try:
        await asyncio.wait_for(saving.wait(), 5)
        setup.cancel()
        async with asyncio.timeout(5):
            while current.delegations.list_delegations(current.parent_id)[0].state == "running":
                await asyncio.sleep(.01)
        setup.cancel()
        with pytest.raises(asyncio.CancelledError):
            await setup
    finally:
        release.set()
        if not setup.done():
            setup.cancel()
            await asyncio.gather(setup, return_exceptions=True)
    await current.delegations.confirm_recording()
    rec = current.delegations.list_delegations(current.parent_id)[0]
    assert rec.state == "cancelled"
    assert rec.to_public_dict()["recording_state"] == "committed"
    assert starts.empty()
    await current.close()
    restarted = await boot()
    restored = restarted.delegations.get_delegation(rec.delegation_id)
    assert restored.state == "cancelled"
    assert starts.empty()
