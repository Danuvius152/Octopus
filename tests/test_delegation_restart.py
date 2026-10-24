"""Delegation continuation across real database close/reopen and fresh managers."""
from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import dataclass, replace
from pathlib import Path

import pytest

from server import deps
from server.agent_manager import AgentManager
from server.database import Database
from server.delegations import DelegationManager
from server.harness import get_harness
from server.sessions import SessionManager
from server.users import UserManager
from tests.accounts import create_account


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
async def runtime(tmp_path, monkeypatch, short_process_shutdown):
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
        harness.profile, build_turn_argv=argv, prepare_resume=None, activity_observer=None,
    ))

    async def boot(path=None):
        database_path = Path(path or tmp_path / "restart.db")
        fresh = not database_path.exists()
        database = Database(str(database_path))
        await database.initialize()
        if fresh:
            await create_account(database, extra_roots=[tmp_path])
        else:
            deps.set_user_manager(UserManager(database))
        manager = SessionManager()
        await manager.initialize(database)
        parents = [row for row in await database.load_sessions() if row["origin"] == "user"]
        if parents:
            parent_id = parents[0]["id"]
        else:
            root = await database.get_system_agent()
            assert root is not None, "The restart fixture requires an existing owned installation"
            parent = await manager.create_session(root["id"], "Caller", str(tmp_path), backend="codex")
            parent_id = parent.id
            await AgentManager(database).create_agent(name="Worker", backend="codex", user_id=root["user_id"])
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
    original_accounts = await first_boot.database.list_users()
    assert len(original_accounts) == 1 and original_accounts[0]["dek_wrapped"]
    await first_boot.close()
    second_boot = await boot()
    assert await second_boot.database.list_users() == original_accounts
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
    # Completion can still be recording its relay receipt. Corrupt a settled
    # snapshot, rather than racing a legitimate compare-and-set writer.
    await current.delegations.confirm_recording()
    before = await current.database.load_delegation_state(
        rec.delegation_id, parent_session_id=current.parent_id, user_id=current.manager.get_session(current.parent_id).user_id,
    )
    broken = json.loads(before)
    broken["awaiting_sub_relay"] = "not a boolean"
    assert await current.database.compare_and_set_delegation_state(
        rec.delegation_id, before, json.dumps(broken),
        target_agent_id=rec.target_agent_id, parent_session_id=current.parent_id, user_id=current.manager.get_session(current.parent_id).user_id,
    )
    await current.close()
    with pytest.raises(RuntimeError, match="Invalid persisted delegation state"):
        await boot()
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
