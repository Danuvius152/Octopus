"""Restart recovery distinguishes a relay wait from a completed delegation."""
from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import replace

import pytest

from server.agent_manager import AgentManager
from server.harness import get_harness
from server.mcp_identity import scope_is_live, verify
from tests.test_delegation_restart import runtime as runtime


@pytest.fixture
def gated_cli(runtime, tmp_path, monkeypatch):
    _, starts = runtime
    release = tmp_path / "release-outer"

    def argv(context):
        starts.put_nowait(context)
        code = "import json,time\nfrom pathlib import Path\n"
        code += "print(json.dumps({'type':'thread.started','thread_id':'nested-thread'}),flush=True)\n"
        if "outer gate" in context.prompt:
            code += "print(json.dumps({'type':'item.completed','item':{'id':'item_0','type':'agent_message','text':'waiting for helper'}}),flush=True)\n"
            code += f"while not Path({str(release)!r}).exists(): time.sleep(.01)\n"
        elif "held" in context.prompt:
            code += "print(json.dumps({'type':'item.completed','item':{'id':'item_0','type':'agent_message','text':'helper running'}}),flush=True)\n"
            code += "time.sleep(60)\n"
        else:
            code += "print(json.dumps({'type':'item.completed','item':{'id':'item_0','type':'agent_message','text':'review answer'}}),flush=True)\n"
        code += "print(json.dumps({'type':'turn.completed','usage':{}}),flush=True)\n"
        return [sys.executable, "-u", "-c", code], {"cwd": context.working_dir}

    harness = get_harness("codex")
    monkeypatch.setattr(harness, "profile", replace(
        harness.profile, build_turn_argv=argv, prepare_resume=None, activity_observer=None,
    ))
    return release


async def test_nested_first_turn_success_is_interrupted_when_its_relay_never_finished(
    runtime, gated_cli,
):
    boot, starts = runtime
    first_boot = await boot()
    await AgentManager(first_boot.database).create_agent(name="Grandchild", backend="codex")
    outer_ready, child_ready = asyncio.Event(), asyncio.Event()

    async def observe(event):
        if event.get("type") != "assistant_text":
            return
        if event.get("content") == "waiting for helper":
            outer_ready.set()
        if event.get("content") == "helper running":
            child_ready.set()

    first_boot.manager.on_broadcast("nested-runtime-observer", observe)
    outer = await first_boot.delegations.start_delegation(
        parent_session_id=first_boot.parent_id, agent_name="Worker", request="outer gate review",
    )
    await asyncio.wait_for(starts.get(), 5)
    await asyncio.wait_for(outer_ready.wait(), 5)
    descendant = await first_boot.delegations.start_delegation(
        parent_session_id=outer.delegation_id, agent_name="Grandchild", request="held helper review",
    )
    await asyncio.wait_for(starts.get(), 5)
    await asyncio.wait_for(child_ready.wait(), 5)
    gated_cli.touch()
    async with asyncio.timeout(5):
        while first_boot.manager.has_pending_or_active_work(outer.delegation_id, outer.work_id):
            await asyncio.sleep(.01)
    assert outer.state == "running" and descendant.state == "running"
    messages = await first_boot.database.load_messages(outer.delegation_id)
    results = [row["content"] for row in messages if row["type"] == "result"]
    assert len(results) == 1 and results[0]["status"] == "success"
    assert first_boot.manager.dispatch_snapshot(first_boot.parent_id)["items"] == []
    await first_boot.close()
    current = await boot()
    recovered_outer = current.delegations.get_delegation(outer.delegation_id)
    recovered_child = current.delegations.get_delegation(descendant.delegation_id)
    assert recovered_outer is not None and recovered_outer.state == "interrupted"
    assert recovered_child is not None and recovered_child.state == "interrupted"
    assert recovered_outer.work_id == outer.work_id
    assert recovered_child.work_id == descendant.work_id
    assert recovered_outer.to_public_dict()["recording_state"] == "committed"
    assert starts.empty()
    assert current.manager.dispatch_snapshot(current.parent_id)["items"] == []
    following = await current.delegations.follow_up_delegation(
        parent_session_id=current.parent_id, delegation_id=outer.delegation_id,
        request="held explicit relay retry",
    )
    context = await asyncio.wait_for(starts.get(), 5)
    assert following.work_id != outer.work_id
    assert context.resume_id == "nested-thread"
    assert recovered_child.state == "interrupted"


async def test_database_close_confirms_terminal_writer_created_during_its_own_drain(
    runtime, gated_cli, monkeypatch,
):
    boot, starts = runtime
    current = await boot()
    ready = asyncio.Event()

    async def observe(event):
        if event.get("type") == "assistant_text" and event.get("content") == "helper running":
            ready.set()

    current.manager.on_broadcast("shutdown-native-ready", observe)
    rec = await current.delegations.start_delegation(
        parent_session_id=current.parent_id, agent_name="Worker", request="held shutdown review",
    )
    context = await asyncio.wait_for(starts.get(), 5)
    await asyncio.wait_for(ready.wait(), 5)
    token = next(entry.credential for entry in context.mcp_servers if entry.key == "bg")
    scope = verify(token)
    assert scope is not None and scope_is_live(scope)
    write = current.database.compare_and_set_delegation_state
    fault = True

    async def unavailable_terminal_write(*args, **kwargs):
        if fault and json.loads(args[2])["state"] == "interrupted":
            raise OSError("synthetic terminal persistence outage during close")
        return await write(*args, **kwargs)

    monkeypatch.setattr(current.database, "compare_and_set_delegation_state", unavailable_terminal_write)
    try:
        # Keep the delegation observer bound: retiring the native turn creates
        # its final receipt writer inside the DB's own teardown hooks.
        with pytest.raises(RuntimeError, match="kept open"):
            await current.database.close()
        assert rec.state == "interrupted"
        assert rec.to_public_dict()["recording_state"] == "unconfirmed"
        assert not scope_is_live(scope)
        rows = await current.database.load_sessions(include_archived=True)
        assert any(row["id"] == current.parent_id for row in rows)
        messages = await current.database.load_messages(rec.delegation_id)
        results = [row["content"] for row in messages if row["type"] == "result"]
        assert len(results) == 1
        assert results[0]["status"] == "interrupted"
        assert results[0]["code"] == "service_shutdown"
        assert current.manager.dispatch_snapshot(current.parent_id)["items"] == []
        rejected = await current.manager.start_message(
            current.parent_id, "late automatic return", source="delegation", delivery_id="late-shutdown-return",
        )
        assert rejected.disposition == "closed"
        assert starts.empty()
    finally:
        fault = False
        # The public continuation first verifies the original selected receipt;
        # admission is sealed, so this explicit request starts no native turn.
        await current.delegations.follow_up_delegation(
            parent_session_id=current.parent_id, delegation_id=rec.delegation_id,
            request="explicit verification after storage recovery",
        )
        await current.database.close()
    assert rec.state == "interrupted"
    assert rec.to_public_dict()["recording_state"] == "committed"
    assert starts.empty()
