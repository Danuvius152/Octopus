"""Delegation state recording preserves cancellation through lost acknowledgements."""
from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
from dataclasses import replace

import pytest

from server.agent_manager import AgentManager
from server.delegations import DelegationError
from server.harness import get_harness
from tests.test_delegation_restart import runtime as runtime
from tests.test_delegation_restart import wait_finished
from tests.test_delegation_restart_security import completed_before_restart


async def test_cancelled_tentative_round_can_be_confirmed_after_storage_recovery(runtime, monkeypatch):
    boot, starts = runtime
    current, original = await completed_before_restart(boot, starts)
    save = current.database.compare_and_set_delegation_state
    read = current.database.load_delegation_state
    committed = asyncio.Event()
    lose_acknowledgement = asyncio.Event()
    verification_unavailable = True
    previous_snapshot = None

    async def commit_then_lose_acknowledgement(*args, **kwargs):
        nonlocal previous_snapshot
        if json.loads(args[2])["state"] == "running":
            previous_snapshot = args[1]
            assert await save(*args, **kwargs)
            committed.set()
            await lose_acknowledgement.wait()
            raise OSError("synthetic acknowledgement lost after commit")
        return await save(*args, **kwargs)

    async def unavailable_verification(*args, **kwargs):
        if verification_unavailable:
            raise OSError("synthetic verification outage")
        return await read(*args, **kwargs)

    monkeypatch.setattr(current.database, "compare_and_set_delegation_state", commit_then_lose_acknowledgement)
    monkeypatch.setattr(current.database, "load_delegation_state", unavailable_verification)
    following = asyncio.create_task(current.delegations.follow_up_delegation(
        parent_session_id=current.parent_id, delegation_id=original.delegation_id,
        request="held review never admitted",
    ))
    cancellation = None
    confirmed = False
    try:
        await asyncio.wait_for(committed.wait(), 5)
        cancellation = asyncio.create_task(current.delegations.cancel_delegation(original.delegation_id))
        async with asyncio.timeout(5):
            while current.delegations.get_delegation(original.delegation_id).state != "cancelled":
                await asyncio.sleep(.01)
        lose_acknowledgement.set()
        failures = await asyncio.gather(following, cancellation, return_exceptions=True)
        assert all(isinstance(error, DelegationError) and error.status_code == 503 for error in failures)
        assert starts.empty()
        assert current.manager.dispatch_snapshot(current.parent_id)["items"] == []
        verification_unavailable = False
        recovered = await current.delegations.cancel_delegation(original.delegation_id)
        confirmed = True
        assert recovered.state == "cancelled"
        assert recovered.to_public_dict()["recording_state"] == "committed"
        assert starts.empty()
        await current.delegations.cancel_delegation(original.delegation_id)
        assert len(current.manager.dispatch_snapshot(current.parent_id)["items"]) == 1
        await current.delegations.confirm_recording()
    finally:
        verification_unavailable = False
        lose_acknowledgement.set()
        tasks = [following] + ([cancellation] if cancellation is not None else [])
        await asyncio.gather(*tasks, return_exceptions=True)
        if not confirmed and previous_snapshot is not None:
            # Restore this test's known acknowledged receipt so fixture teardown
            # can confirm the selected cancellation without abandoning SQLite.
            rows = await current.database.load_delegation_states()
            stored = next(row["delegation_state"] for row in rows if row["id"] == original.delegation_id)
            owner = await current.database.get_session_owner(current.parent_id)
            assert await save(
                original.delegation_id, stored, previous_snapshot,
                parent_session_id=current.parent_id, user_id=owner,
            )
            await current.delegations.cancel_delegation(original.delegation_id)


async def test_closed_admission_cancellation_is_not_rewritten_when_its_save_fails(runtime, monkeypatch):
    boot, starts = runtime
    current = await boot()
    save = current.database.compare_and_set_delegation_state
    unavailable = True

    async def unavailable_cancellation_save(*args, **kwargs):
        if unavailable and json.loads(args[2])["state"] == "cancelled":
            raise OSError("synthetic cancelled outcome storage outage")
        return await save(*args, **kwargs)

    monkeypatch.setattr(current.database, "compare_and_set_delegation_state", unavailable_cancellation_save)
    current.manager.close_admission()
    try:
        with pytest.raises(DelegationError) as rejected:
            await current.delegations.start_delegation(
                parent_session_id=current.parent_id, agent_name="Worker", request="closed review",
            )
        selected = current.delegations.list_delegations(current.parent_id)[0]
        assert rejected.value.status_code == 503
        assert selected.state == "cancelled"
        assert selected.to_public_dict()["recording_state"] == "unconfirmed"
        assert starts.empty()
        assert current.manager.dispatch_snapshot(current.parent_id)["items"] == []
    finally:
        unavailable = False
        records = current.delegations.list_delegations(current.parent_id)
        if records:
            await current.delegations.cancel_delegation(records[0].delegation_id)


async def test_committed_nested_failure_is_preserved_when_relay_snapshot_save_failed(
    runtime, tmp_path, monkeypatch,
):
    boot, starts = runtime
    release = tmp_path / "release-failing-outer"

    def argv(context):
        starts.put_nowait(context)
        code = "import json,time\nfrom pathlib import Path\n"
        code += "print(json.dumps({'type':'thread.started','thread_id':'failing-nested-thread'}),flush=True)\n"
        if "failing outer gate" in context.prompt:
            code += "print(json.dumps({'type':'item.completed','item':{'id':'item_0','type':'agent_message','text':'waiting for helper'}}),flush=True)\n"
            code += f"while not Path({str(release)!r}).exists(): time.sleep(.01)\n"
            code += "print(json.dumps({'type':'turn.failed','error':{'message':'authentication failed'}}),flush=True)\n"
        else:
            code += "print(json.dumps({'type':'item.completed','item':{'id':'item_0','type':'agent_message','text':'helper running'}}),flush=True)\n"
            code += "time.sleep(60)\n"
        return [sys.executable, "-u", "-c", code], {"cwd": context.working_dir}

    harness = get_harness("codex")
    monkeypatch.setattr(harness, "profile", replace(harness.profile, build_turn_argv=argv))
    current = await boot()
    await AgentManager(current.database).create_agent(name="Leaf", backend="codex")
    outer_ready, leaf_ready = asyncio.Event(), asyncio.Event()

    async def observe(event):
        if event.get("type") == "assistant_text":
            if event.get("content") == "waiting for helper":
                outer_ready.set()
            if event.get("content") == "helper running":
                leaf_ready.set()

    current.manager.on_broadcast("nested-failure-ready", observe)
    outer = await current.delegations.start_delegation(
        parent_session_id=current.parent_id, agent_name="Worker", request="failing outer gate review",
    )
    await asyncio.wait_for(starts.get(), 5)
    await asyncio.wait_for(outer_ready.wait(), 5)
    await current.delegations.start_delegation(
        parent_session_id=outer.delegation_id, agent_name="Leaf", request="held helper",
    )
    await asyncio.wait_for(starts.get(), 5)
    await asyncio.wait_for(leaf_ready.wait(), 5)
    save = current.database.compare_and_set_delegation_state
    unavailable = True

    async def unavailable_outer_failure(*args, **kwargs):
        if unavailable and args[0] == outer.delegation_id and json.loads(args[2])["state"] == "failed":
            raise OSError("synthetic selected nested failure snapshot outage")
        return await save(*args, **kwargs)

    monkeypatch.setattr(current.database, "compare_and_set_delegation_state", unavailable_outer_failure)
    checkpoint = tmp_path / "nested-failure-checkpoint.db"
    try:
        release.touch()
        await wait_finished(current.manager, outer)
        assert outer.state == "failed"
        assert outer.to_public_dict()["recording_state"] == "unconfirmed"
        messages = await current.database.load_messages(outer.delegation_id)
        results = [row["content"] for row in messages if row["type"] == "result"]
        assert len(results) == 1 and results[0]["status"] == "failed"
        assert current.manager.dispatch_snapshot(current.parent_id)["items"] == []

        def snapshot():
            with sqlite3.connect(tmp_path / "restart.db") as source, sqlite3.connect(checkpoint) as target:
                source.backup(target)

        await asyncio.to_thread(snapshot)
    finally:
        unavailable = False
        current.manager.close_admission()
        await current.delegations.follow_up_delegation(
            parent_session_id=current.parent_id, delegation_id=outer.delegation_id,
            request="explicit verification after snapshot recovery",
        )
        await current.close()
    restarted = await boot(checkpoint)
    recovered = restarted.delegations.get_delegation(outer.delegation_id)
    assert recovered.state == "failed"
    assert recovered.to_public_dict()["recording_state"] == "committed"
    assert starts.empty()
    assert restarted.manager.dispatch_snapshot(restarted.parent_id)["items"] == []
