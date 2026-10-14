"""Delegation continuation across real database close/reopen and fresh managers."""
from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
from dataclasses import dataclass, replace

import pytest

from server.agent_manager import AgentManager
from server.database import Database
from server.delegations import DelegationManager
from server.harness import get_harness
from server.sessions import SessionManager


@dataclass
class Boot:
    database: Database
    manager: SessionManager
    delegations: DelegationManager
    parent_id: str

    async def close(self):
        self.delegations.shutdown()
        await self.database.close()


@pytest.fixture
async def runtime(tmp_path, monkeypatch):
    starts = asyncio.Queue()
    boots = []

    def argv(context):
        starts.put_nowait(context)
        code = "import json,time\n"
        code += "print(json.dumps({'type':'thread.started','thread_id':'restart-thread'}),flush=True)\n"
        if "held" in context.prompt:
            code += "time.sleep(60)\n"
        code += "print(json.dumps({'type':'item.completed','item':{'id':'item_0','type':'agent_message','text':'review answer'}}),flush=True)\n"
        if "fail authentication" in context.prompt:
            code += "print(json.dumps({'type':'turn.failed','error':{'message':'authentication failed'}}),flush=True)\n"
        else:
            code += "print(json.dumps({'type':'turn.completed','usage':{}}),flush=True)\n"
        return [sys.executable, "-u", "-c", code], {"cwd": context.working_dir}

    harness = get_harness("codex")
    monkeypatch.setattr(harness, "profile", replace(
        harness.profile, build_turn_argv=argv, activity_observer=None,
    ))

    async def boot(path=None):
        database = Database(str(path or tmp_path / "restart.db"))
        await database.initialize()
        manager = SessionManager()
        await manager.initialize(database)
        parents = [row for row in await database.load_sessions() if row["origin"] == "user"]
        if parents:
            parent_id = parents[0]["id"]
        else:
            root = await database.get_system_agent()
            parent = await manager.create_session(root["id"], "Caller", str(tmp_path), backend="codex")
            parent_id = parent.id
            await AgentManager(database).create_agent(name="Worker", backend="codex")
        await manager.interrupt(parent_id)
        delegations = DelegationManager()
        delegations.bind(manager, database)
        current = Boot(database, manager, delegations, parent_id)
        boots.append(current)
        await delegations.restore()
        return current

    try:
        yield boot, starts
    finally:
        for current in reversed(boots):
            await current.close()


async def wait_finished(manager, delegation):
    async with asyncio.timeout(5):
        while delegation.state == "running" or manager.has_pending_or_active_work(
            delegation.delegation_id, delegation.work_id,
        ):
            await asyncio.sleep(.01)


async def test_completed_delegation_can_follow_up_after_restart_without_replaying_result(runtime):
    boot, starts = runtime
    first_boot = await boot()
    first = await first_boot.delegations.start_delegation(
        parent_session_id=first_boot.parent_id, agent_name="Worker", request="review first",
    )
    await asyncio.wait_for(starts.get(), 5)
    await wait_finished(first_boot.manager, first)
    assert first.state == "completed"
    await first_boot.close()
    second_boot = await boot()
    assert starts.empty()
    assert second_boot.manager.dispatch_snapshot(second_boot.parent_id)["items"] == []
    second = await second_boot.delegations.follow_up_delegation(
        parent_session_id=second_boot.parent_id, delegation_id=first.delegation_id,
        request="held review again",
    )
    context = await asyncio.wait_for(starts.get(), 5)
    assert second.delegation_id == first.delegation_id
    assert second.work_id != first.work_id
    assert context.resume_id == "restart-thread"
    history = await second_boot.database.load_messages(first.delegation_id)
    assert any(row["content"] == "review answer" for row in history)
    assert second_boot.delegations.list_delegations(second_boot.parent_id)


async def test_malformed_future_snapshot_is_rejected_without_starting_work(runtime):
    boot, starts = runtime
    current = await boot()
    rec = await current.delegations.start_delegation(
        parent_session_id=current.parent_id, agent_name="Worker", request="review once",
    )
    await asyncio.wait_for(starts.get(), 5)
    await wait_finished(current.manager, rec)
    before = await current.database.load_delegation_state(
        rec.delegation_id, parent_session_id=current.parent_id, user_id=None,
    )
    broken = json.loads(before)
    broken["awaiting_sub_relay"] = "not a boolean"
    assert await current.database.compare_and_set_delegation_state(
        rec.delegation_id, before, json.dumps(broken),
        parent_session_id=current.parent_id, user_id=None,
    )
    await current.close()
    with pytest.raises(RuntimeError, match="Invalid persisted delegation state"):
        await boot()
    assert starts.empty()


async def test_restart_confirms_interruption_when_storage_acknowledgement_is_lost(runtime, monkeypatch):
    boot, starts = runtime
    current = await boot()
    rec = await current.delegations.start_delegation(
        parent_session_id=current.parent_id, agent_name="Worker", request="held before restart",
    )
    await asyncio.wait_for(starts.get(), 5)
    await current.close()
    save = Database.compare_and_set_delegation_state

    async def lose_acknowledgement(database, *args, **kwargs):
        confirmed = await save(database, *args, **kwargs)
        if confirmed and json.loads(args[2])["state"] == "interrupted":
            raise OSError("synthetic lost restart acknowledgement")
        return confirmed

    monkeypatch.setattr(Database, "compare_and_set_delegation_state", lose_acknowledgement)
    restarted = await boot()
    recovered = restarted.delegations.get_delegation(rec.delegation_id)
    assert recovered.state == "interrupted"
    assert recovered.to_public_dict()["recording_state"] == "committed"
    assert starts.empty()


async def test_unfinished_round_is_interrupted_and_only_explicit_follow_up_starts_work(runtime):
    boot, starts = runtime
    first_boot = await boot()
    first = await first_boot.delegations.start_delegation(
        parent_session_id=first_boot.parent_id, agent_name="Worker", request="held review",
    )
    await asyncio.wait_for(starts.get(), 5)
    await first_boot.close()
    second_boot = await boot()
    recovered = second_boot.delegations.get_delegation(first.delegation_id)
    assert recovered.state == "interrupted"
    assert "service" in recovered.error
    assert starts.empty()
    assert second_boot.manager.dispatch_snapshot(second_boot.parent_id)["items"] == []
    follow_up = await second_boot.delegations.follow_up_delegation(
        parent_session_id=second_boot.parent_id, delegation_id=first.delegation_id,
        request="held examine partial history and continue",
    )
    await asyncio.wait_for(starts.get(), 5)
    assert follow_up.state == "running" and follow_up.work_id != first.work_id


async def test_restart_keeps_committed_success_when_round_snapshot_was_not_acknowledged(
    runtime, tmp_path, monkeypatch,
):
    boot, starts = runtime
    first_boot = await boot()
    original = first_boot.database.compare_and_set_delegation_state
    fail = True

    async def save(*args, **kwargs):
        if fail and json.loads(args[2])["state"] == "completed":
            raise OSError("synthetic round storage failure")
        return await original(*args, **kwargs)

    monkeypatch.setattr(first_boot.database, "compare_and_set_delegation_state", save)
    checkpoint = tmp_path / "checkpoint.db"
    try:
        first = await first_boot.delegations.start_delegation(
            parent_session_id=first_boot.parent_id, agent_name="Worker", request="complete before outage",
        )
        await asyncio.wait_for(starts.get(), 5)
        await wait_finished(first_boot.manager, first)
        assert first.state == "completed"
        assert first.to_public_dict()["recording_state"] == "unconfirmed"

        def snapshot():
            with sqlite3.connect(tmp_path / "restart.db") as source, sqlite3.connect(checkpoint) as target:
                source.backup(target)

        await asyncio.to_thread(snapshot)
    finally:
        fail = False
        # Confirm the selected round through the public continuation seam;
        # only this explicit new work runs in the original process.
        if first_boot.delegations.get_delegation(first.delegation_id) is not None:
            await first_boot.delegations.follow_up_delegation(
                parent_session_id=first_boot.parent_id, delegation_id=first.delegation_id,
                request="held cleanup",
            )
            await asyncio.wait_for(starts.get(), 5)
        await first_boot.close()
    restarted = await boot(checkpoint)
    recovered = restarted.delegations.get_delegation(first.delegation_id)
    assert recovered.state == "completed"
    assert starts.empty()
    assert restarted.manager.dispatch_snapshot(restarted.parent_id)["items"] == []


async def test_failed_snapshot_and_failed_verification_keep_database_open(runtime, monkeypatch):
    boot, starts = runtime
    current = await boot()
    save = current.database.compare_and_set_delegation_state
    read = current.database.load_delegation_state
    fault = True

    async def unavailable_save(*args, **kwargs):
        if fault and json.loads(args[2])["state"] == "completed":
            raise OSError("synthetic write outage")
        return await save(*args, **kwargs)

    async def unavailable_read(*args, **kwargs):
        if fault:
            raise OSError("synthetic verification outage")
        return await read(*args, **kwargs)

    monkeypatch.setattr(current.database, "compare_and_set_delegation_state", unavailable_save)
    monkeypatch.setattr(current.database, "load_delegation_state", unavailable_read)
    rec = await current.delegations.start_delegation(
        parent_session_id=current.parent_id, agent_name="Worker", request="complete during outage",
    )
    await asyncio.wait_for(starts.get(), 5)
    await wait_finished(current.manager, rec)
    failure = None
    try:
        await current.database.close()
    except RuntimeError as error:
        failure = error
    finally:
        fault = False
        if failure is not None:
            await current.delegations.follow_up_delegation(
                parent_session_id=current.parent_id, delegation_id=rec.delegation_id,
                request="explicit verification after recovery",
            )
    assert failure is not None, "unconfirmed state must not close its database"
    assert starts.empty()
