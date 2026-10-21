"""A normal branch CAS conflict cannot abandon captured cancellation cleanup."""

from __future__ import annotations

import asyncio
import json
import os
import shlex
import sys

import pytest

from server.agent_manager import AgentManager
from server.delegations import DelegationError
from server.mcp_identity import scope_is_live, work_is_open
from tests.test_delegation_restart_security import wait_live_scope
from tests.test_delegation_work import work_runtime as work_runtime


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


async def _background(manager, rec, context, path):
    token = next(entry.credential for entry in context.mcp_servers if entry.key == "bg")
    code = f"import os,time;from pathlib import Path;Path({str(path)!r}).write_text(str(os.getpid()));time.sleep(30)"
    response = await manager.work_test_client.post(
        f"/api/sessions/{rec.delegation_id}/bg-tasks",
        headers={"Authorization": f"Bearer {token}"},
        json={"command": "exec " + shlex.join([sys.executable, "-u", "-c", code]), "idle_timeout_seconds": 0},
    )
    assert response.status_code == 201, response.text
    async with asyncio.timeout(5):
        while not path.exists():
            await asyncio.sleep(.01)
    return response.json()["id"], int(path.read_text())


@pytest.mark.parametrize("conflict_stage", ["state", "terminal"])
async def test_descendant_cas_refusal_finishes_captured_cancellation_before_returning(work_runtime, monkeypatch, tmp_path, conflict_stage):
    manager, delegations, parent, starts = work_runtime
    agents = AgentManager(manager.db)
    for name in ("Changed helper", "Later helper", "Replacement"):
        await agents.create_agent(name=name, backend="codex")
    outer = await delegations.start_delegation(
        parent_session_id=parent.id, agent_name="Worker", request="held outer review",
    )
    outer_context = await asyncio.wait_for(starts.get(), 5)
    outer_scope = await wait_live_scope(outer_context)
    conflicted = await delegations.start_delegation(
        parent_session_id=outer.delegation_id, agent_name="Changed helper", request="held changed helper",
        scope=outer_scope,
    )
    conflicted_context = await asyncio.wait_for(starts.get(), 5)
    await wait_live_scope(conflicted_context)
    later = await delegations.start_delegation(
        parent_session_id=outer.delegation_id, agent_name="Later helper", request="held later helper",
        scope=outer_scope,
    )
    later_context = await asyncio.wait_for(starts.get(), 5)
    await wait_live_scope(later_context)
    independent = await delegations.start_delegation(
        parent_session_id=parent.id, agent_name="Worker", request="held independent new work",
    )
    independent_context = await asyncio.wait_for(starts.get(), 5)
    independent_scope = await wait_live_scope(independent_context)
    outer_bg, outer_pid = await _background(manager, outer, outer_context, tmp_path / "outer.pid")
    later_bg, later_pid = await _background(manager, later, later_context, tmp_path / "later.pid")
    independent_bg, independent_pid = await _background(manager, independent, independent_context, tmp_path / "independent.pid")
    scope = {"parent_session_id": outer.delegation_id, "user_id": None}
    original_conflicted = await manager.db.load_delegation_state(conflicted.delegation_id, **scope)
    replacement = next(row for row in await manager.db.load_agents() if row["name"] == "Replacement")
    if conflict_stage == "state":
        await manager.db.update_session_field(conflicted.delegation_id, agent_id=replacement["id"])
    save = manager.db.compare_and_set_delegation_state
    finishing, release = asyncio.Event(), asyncio.Event()

    async def pause_final_outer_record(*args, **kwargs):
        proposed = json.loads(args[2])
        if args[0] == outer.delegation_id and proposed["terminal_disposition"] is not None:
            finishing.set()
            await release.wait()
        changed = await save(*args, **kwargs)
        if (conflict_stage == "terminal" and changed and args[0] == conflicted.delegation_id
                and proposed["state"] == "cancelled" and proposed["terminal_disposition"] is None):
            await manager.db.update_session_field(conflicted.delegation_id, agent_id=replacement["id"])
        return changed

    monkeypatch.setattr(manager.db, "compare_and_set_delegation_state", pause_final_outer_record)
    cancelling = asyncio.create_task(delegations.cancel_delegation(outer.delegation_id, work_id=outer.work_id))
    confirming = None
    try:
        async with asyncio.timeout(5):
            while not finishing.is_set() and not cancelling.done():
                await asyncio.sleep(.01)
        # These are the captured real OS processes. No DB read competes with
        # the final command-recording gate while checking resource cleanup.
        async with asyncio.timeout(2):
            while _alive(outer_pid) or _alive(later_pid):
                await asyncio.sleep(.01)
        assert finishing.is_set() and not cancelling.done()
        assert outer.state == later.state == "cancelled"
        assert conflicted.state == ("running" if conflict_stage == "state" else "cancelled")
        assert all(not work_is_open(rec.work_id) for rec in (outer, conflicted, later))
        assert independent.state == "running" and work_is_open(independent.work_id)
        assert _alive(independent_pid)
        assert scope_is_live(independent_scope)
        confirming = asyncio.create_task(delegations.confirm_recording())
        await asyncio.sleep(0)
        assert not confirming.done()
        release.set()
        with pytest.raises(DelegationError) as refused:
            await cancelling
        assert refused.value.status_code == 409 and "relationship changed" in str(refused.value)
        await asyncio.wait_for(confirming, 5)
        assert json.loads(await manager.db.load_delegation_state(
            outer.delegation_id, parent_session_id=parent.id, user_id=None,
        ))["state"] == "cancelled"
        stored_conflicted = await manager.db.load_delegation_state(conflicted.delegation_id, **scope)
        if conflict_stage == "state":
            assert stored_conflicted == original_conflicted
        else:
            assert json.loads(stored_conflicted)["state"] == "cancelled"
            assert json.loads(stored_conflicted)["work_id"] == conflicted.work_id
            assert json.loads(stored_conflicted)["terminal_disposition"] is None
        assert json.loads(await manager.db.load_delegation_state(later.delegation_id, **scope))["state"] == "cancelled"
        async with asyncio.timeout(5):
            while any([(await manager.work_test_bg.get_task(task_id)).status == "running" for task_id in (outer_bg, later_bg)]):
                await asyncio.sleep(.01)
        assert (await manager.work_test_bg.get_task(outer_bg)).status == "cancelled"
        assert (await manager.work_test_bg.get_task(later_bg)).status == "cancelled"
        assert (await manager.work_test_bg.get_task(independent_bg)).status == "running"
        assert _alive(independent_pid)
        assert len(manager.dispatch_snapshot(parent.id)["items"]) == 1
    finally:
        release.set()
        await asyncio.gather(cancelling, *([confirming] if confirming is not None else []), return_exceptions=True)
