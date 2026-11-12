"""Delegation results finish rounds without retiring their conversations."""

from __future__ import annotations

import asyncio
import shlex

import pytest

from server.agent_manager import AgentManager
from server.delegations import DelegationError
from server.mcp_identity import work_is_open
from tests.test_delegation_restart import runtime as runtime
from tests.test_delegation_restart import wait_finished
from tests.test_delegation_restart_security import wait_live_scope
from tests.test_delegation_work import wait_state
from tests.test_delegation_work import work_runtime as work_runtime


async def test_completed_conversation_accepts_human_follow_up_before_and_after_restart(runtime):
    boot, starts = runtime
    first_boot = await boot()
    rec = await first_boot.delegations.start_delegation(
        parent_session_id=first_boot.parent_id, agent_name="Worker", request="review first",
    )
    await asyncio.wait_for(starts.get(), 5)
    await wait_finished(first_boot.manager, rec)
    await first_boot.delegations.confirm_recording()
    await first_boot.manager.await_session_execution(rec.delegation_id, timeout=5)
    assert rec.state == "completed"
    assert first_boot.manager.get_session(rec.delegation_id) is not None
    original_returns = first_boot.manager.dispatch_snapshot(first_boot.parent_id)["items"]
    assert len(original_returns) == 1

    human = await first_boot.manager.start_message(rec.delegation_id, "human follow-up")
    context = await asyncio.wait_for(starts.get(), 5)
    assert human.accepted and context.resume_id == "restart-thread"
    completion = await asyncio.wait_for(human.wait(), 5)
    assert completion.input_request_id == human.request_id
    await first_boot.delegations.confirm_recording()
    assert completion.outcome.status == "success"
    assert first_boot.manager.dispatch_snapshot(first_boot.parent_id)["items"] == original_returns
    assert first_boot.delegations.get_delegation(rec.delegation_id) is rec
    assert rec.state == "completed"

    await first_boot.close()
    second_boot = await boot()
    assert starts.empty()
    child = second_boot.manager.get_session(rec.delegation_id)
    assert child is not None and child.origin == "delegation"
    assert child.parent_session_id == first_boot.parent_id
    assert second_boot.manager.dispatch_snapshot(second_boot.parent_id)["items"] == []
    human = await second_boot.manager.start_message(child.id, "human after restart")
    context = await asyncio.wait_for(starts.get(), 5)
    assert human.accepted and context.resume_id == "restart-thread"
    assert (await asyncio.wait_for(human.wait(), 5)).outcome.status == "success"
    await second_boot.delegations.confirm_recording()
    assert second_boot.manager.dispatch_snapshot(second_boot.parent_id)["items"] == []
    restored = second_boot.delegations.get_delegation(child.id)
    assert restored.work_id == rec.work_id and restored.state == "completed"


async def test_failed_setup_still_archives_the_unadmitted_child(runtime, monkeypatch):
    boot, _ = runtime
    current = await boot()
    save = current.database.compare_and_set_delegation_state
    refused = False

    async def archive_target_after_registration(*args, **kwargs):
        nonlocal refused
        saved = await save(*args, **kwargs)
        if not refused:
            refused = True
            await AgentManager(current.database).archive_agent(kwargs["target_agent_id"])
        return saved

    monkeypatch.setattr(current.database, "compare_and_set_delegation_state", archive_target_after_registration)
    with pytest.raises(DelegationError, match="Agent not found or archived"):
        await current.delegations.start_delegation(
            parent_session_id=current.parent_id, agent_name="Worker", request="never admitted",
        )
    rec = current.delegations.list_delegations(current.parent_id)[0]
    assert rec.state == "cancelled"
    assert rec.error == "Delegation setup ended before child request admission"
    assert current.manager.get_session(rec.delegation_id) is None
    archived = await current.manager.load_archived_session_detail(rec.delegation_id)
    assert archived is not None
    assert archived.messages == []


async def test_explicit_archive_stays_read_only_after_restart_until_parent_follow_up(runtime):
    boot, starts = runtime
    current = await boot()
    rec = await current.delegations.start_delegation(
        parent_session_id=current.parent_id, agent_name="Worker", request="review first",
    )
    await asyncio.wait_for(starts.get(), 5)
    await wait_finished(current.manager, rec)
    await current.delegations.confirm_recording()
    assert current.manager.get_session(rec.delegation_id) is not None
    await current.manager.archive_session(rec.delegation_id, replace=False)
    await current.close()

    restored = await boot()
    assert restored.manager.get_session(rec.delegation_id) is None
    archived = await restored.manager.load_archived_session_detail(rec.delegation_id)
    assert archived is not None and archived.archived
    assert starts.empty()
    follow_up = await restored.delegations.follow_up_delegation(
        parent_session_id=restored.parent_id, delegation_id=rec.delegation_id,
        request="explicit next round",
    )
    context = await asyncio.wait_for(starts.get(), 5)
    assert context.resume_id == "restart-thread"
    assert follow_up.work_id != rec.work_id
    assert restored.manager.get_session(rec.delegation_id) is not None
    await wait_finished(restored.manager, follow_up)
    await restored.delegations.confirm_recording()
    assert follow_up.state == "completed"
    assert restored.manager.get_session(rec.delegation_id) is not None


async def test_background_return_after_human_follow_up_keeps_original_work_and_does_not_relay_twice(
    work_runtime, tmp_path,
):
    manager, delegations, parent, starts = work_runtime
    rec = await delegations.start_delegation(
        parent_session_id=parent.id, agent_name="Worker", request="controlled round",
    )
    context = await asyncio.wait_for(starts.get(), 5)
    await wait_live_scope(context)
    token = next(entry.credential for entry in context.mcp_servers if entry.key == "bg")
    release_bg = tmp_path / "release-bg"
    response = await manager.work_test_client.post(
        f"/api/sessions/{rec.delegation_id}/bg-tasks",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "command": f"while [ ! -f {shlex.quote(str(release_bg))} ]; do sleep 0.01; done; echo 'late work result'",
            "idle_timeout_seconds": 0,
        },
    )
    assert response.status_code == 201
    (tmp_path / "release").touch()
    await wait_state(delegations, rec.delegation_id, "completed")
    await delegations.confirm_recording()
    await manager.await_session_execution(rec.delegation_id, timeout=5)
    original_return = manager.dispatch_snapshot(parent.id)["items"]
    assert len(original_return) == 1
    assert manager.get_session(rec.delegation_id) is not None
    assert work_is_open(rec.work_id)

    human = await manager.start_message(rec.delegation_id, "human follow-up")
    await asyncio.wait_for(starts.get(), 5)
    assert (await asyncio.wait_for(human.wait(), 5)).outcome.status == "success"
    assert work_is_open(rec.work_id)
    release_bg.touch()
    returned = await asyncio.wait_for(starts.get(), 5)
    assert "late work result" in returned.prompt
    await manager.await_session_execution(rec.delegation_id, timeout=5)
    await delegations.confirm_recording()
    assert manager.get_session(rec.delegation_id) is not None
    assert manager.dispatch_snapshot(parent.id)["items"] == original_return
    assert delegations.get_delegation(rec.delegation_id) is rec and rec.state == "completed"
    assert work_is_open(rec.work_id)
