"""Setup ownership and confirmed business refusal before child admission."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from server.agent_manager import AgentManager
from server.delegations import DelegationError
from server.mcp_identity import revoke_run, verify
from tests.test_delegation_restart import runtime as runtime
from tests.test_delegation_restart import wait_finished
from tests.test_delegation_restart_nested import gated_cli as gated_cli
from tests.test_delegation_restart_security import wait_live_scope


@pytest.mark.parametrize("legacy_loop_error", [False, True])
async def test_reference_diagnostics_keep_one_original_delegation(runtime, monkeypatch, legacy_loop_error):
    boot, starts = runtime
    current = await boot()
    root = Path(current.manager.get_session(current.parent_id).working_dir)
    loop = root / "loop-reference"
    loop.symlink_to("loop-target")
    (root / "loop-target").symlink_to(loop.name)
    (root / "real-reference").write_text("Actual reference content")
    resolve = Path.resolve

    def resolve_with_legacy_loop_error(path, *args, **kwargs):
        if path == loop:
            raise RuntimeError("Symlink loop from supported older Python")
        return resolve(path, *args, **kwargs)

    if legacy_loop_error:
        monkeypatch.setattr(Path, "resolve", resolve_with_legacy_loop_error)
    rec = await current.delegations.start_delegation(
        parent_session_id=current.parent_id, agent_name="Worker", request="Review references with distinct diagnostics",
        files=[loop.name, "missing-reference", "real-reference"],
    )
    context = await asyncio.wait_for(starts.get(), 5)
    assert "- loop-reference  (could not resolve)" in context.prompt
    assert f"- {root / 'missing-reference'}  (not found)" in context.prompt
    assert f"- {root / 'real-reference'}\n" in context.prompt + "\n"
    await wait_finished(current.manager, rec)
    await current.delegations.confirm_recording()
    assert rec.state == "completed" and not current.delegations.drain_blockers()
    rows = await current.database.load_delegation_states()
    assert len(rows) == 1 and rows[0]["id"] == rec.delegation_id
    assert json.loads(rows[0]["delegation_state"])["state"] == "completed"
    assert starts.empty()


@pytest.mark.parametrize("error_type", [ValueError, ConnectionError])
async def test_committed_child_admission_is_observed_without_a_memory_queue(runtime, monkeypatch, error_type):
    boot, starts = runtime
    current = await boot()
    database, manager, delegations = current.database, current.manager, current.delegations
    parent_id = current.parent_id
    admit = database.admit_client_submission
    attempts = []

    async def lose_acknowledgement(sid, client_id, digest, request_id, **kwargs):
        result = await admit(sid, client_id, digest, request_id, **kwargs)
        if sid != parent_id and kwargs["payload"]["source"] == "delegation":
            attempts.append((sid, request_id, kwargs["payload"]["work_id"]))
            raise error_type("Original commit acknowledged before the Owner could publish the queue")
        return result

    monkeypatch.setattr(database, "admit_client_submission", lose_acknowledgement)
    monkeypatch.setattr("server.delegations.delegation_manager", delegations)
    rec = await delegations.start_delegation(parent_session_id=parent_id, agent_name="Worker", request="Observe the original input")
    (child_id, request_id, work_id), = attempts
    assert (rec.delegation_id, rec.work_id) == (child_id, work_id)
    assert not manager.has_pending_or_active_work(child_id, work_id) and starts.empty()
    submission = await database.get_submission_for_request(child_id, request_id)
    assert submission["receipt"]["accepted"] and submission["execution_state"] == "pending"

    assert await manager.delete_session(child_id)
    await delegations.confirm_recording()
    assert rec.state == "interrupted"
    assert await database.get_session_binding(child_id) is None
    replies = manager.dispatch_snapshot(parent_id)["items"]
    assert len(replies) == 1 and "Child has no committed outcome" in replies[0]["summary"]
    reply = await manager.source_receipt("delegation", f"agent-terminal:{work_id}", session_id=parent_id)
    assert reply is not None and reply[1].accepted
    assert len(attempts) == 1 and starts.empty() and not delegations.drain_blockers()


async def test_confirmed_child_refusal_withdraws_the_published_setup(runtime, monkeypatch):
    boot, starts = runtime
    current = await boot()
    save = current.database.compare_and_set_delegation_state
    child_ids = []

    async def archive_target_after_publication(*args, **kwargs):
        saved = await save(*args, **kwargs)
        if saved and json.loads(args[2])["state"] == "running":
            child_ids.append(args[0])
            child = current.manager.get_session(args[0])
            await AgentManager(current.database).archive_agent(child.agent_id)
        return saved

    monkeypatch.setattr(current.database, "compare_and_set_delegation_state", archive_target_after_publication)
    with pytest.raises(DelegationError) as refused:
        await current.delegations.start_delegation(
            parent_session_id=current.parent_id, agent_name="Worker", request="Refuse before input admission",
        )
    assert refused.value.status_code == 409 and len(child_ids) == 1
    rec = current.delegations.get_delegation(child_ids[0])
    assert rec.state == "cancelled" and rec.finished_at is not None
    assert current.manager.get_session(rec.delegation_id) is None
    assert await current.database.load_messages(rec.delegation_id) == []
    assert current.manager.dispatch_snapshot(current.parent_id)["items"] == []
    rows = await current.database.load_delegation_states()
    assert len(rows) == 1 and json.loads(rows[0]["delegation_state"])["state"] == "cancelled"
    await current.delegations.confirm_recording()
    assert not current.delegations.drain_blockers() and starts.empty()


async def test_cancelled_followup_waiter_keeps_new_round_after_restart(runtime, monkeypatch):
    boot, starts = runtime
    current = await boot()
    original = await current.delegations.start_delegation(
        parent_session_id=current.parent_id, agent_name="Worker", request="first review",
    )
    await asyncio.wait_for(starts.get(), 5)
    await wait_finished(current.manager, original)
    get_identity = current.database.get_agent_identity
    waiting, release = asyncio.Event(), asyncio.Event()

    async def pause_before_admission(*args, **kwargs):
        waiting.set()
        await release.wait()
        return await get_identity(*args, **kwargs)

    monkeypatch.setattr(current.database, "get_agent_identity", pause_before_admission)
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
    await AgentManager(current.database).create_agent(
        name="Leaf", backend="codex", user_id=current.manager.get_session(current.parent_id).user_id,
    )
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
