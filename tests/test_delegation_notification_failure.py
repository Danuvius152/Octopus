"""Delegated outcomes survive a failed original notification prefix."""

from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from server.agent_manager import AgentManager
from server.database import Database
from server.delegations import DelegationManager
from server.harness import get_harness
from server.harness.profile import StdinMode
from server.mcp_identity import verify
from server.sessions import SessionManager
from server.sessions.owner import SessionOwnerUnavailable


@pytest.fixture
async def notification_runtime(tmp_path, monkeypatch):
    database = Database(str(tmp_path / "delegations.sqlite3"))
    await database.initialize()
    manager = SessionManager()
    await manager.initialize(database)
    root = await database.get_system_agent()
    await AgentManager(database).create_agent(name="Notification worker", backend="codex")
    parent = await manager.create_session(root["id"], "Original caller", str(tmp_path), backend="codex")
    await manager.interrupt(parent.id)
    model = tmp_path / "notification_model.py"
    model.write_text(
        "import json,sys,time\nfrom pathlib import Path\n"
        "sid,gate=sys.argv[1],Path(sys.argv[2])\n"
        "gate.with_suffix('.input').write_text(sys.stdin.readline())\n"
        "print(json.dumps({'type':'thread.started','thread_id':sid}),flush=True)\n"
        "print(json.dumps({'type':'item.completed','item':{'id':'body','type':'agent_message','text':'Original body from '+sid}}),flush=True)\n"
        "while not gate.exists(): time.sleep(.005)\n"
        "print(json.dumps({'type':'turn.completed','usage':{'input_tokens':1,'output_tokens':1}}),flush=True)\n"
    )
    starts, processes, receipts, frames = [], [], [], []
    questioned = set()
    question_entered, release_question = asyncio.Event(), asyncio.Event()
    controls = SimpleNamespace(enabled=True)
    failure = asyncio.CancelledError("Original delegated question publication was cancelled")
    harness = get_harness("codex")

    def argv(context):
        scope = verify(next(entry.credential for entry in context.mcp_servers if entry.key == "bg"))
        assert scope is not None
        starts.append((scope, context))
        return [sys.executable, "-u", str(model), scope.session_id, str(tmp_path / f"{scope.run_lease_id}.result")], {"cwd": context.working_dir}

    monkeypatch.setattr(harness, "profile", replace(
        harness.profile, binary=sys.executable, prepare_resume=None, activity_observer=None,
        stdin_mode=StdinMode.STREAM_JSON, build_turn_argv=argv,
    ))
    spawn, submit = asyncio.create_subprocess_exec, manager.start_message

    async def observe_spawn(*args, **kwargs):
        process = await spawn(*args, **kwargs)
        if str(model) in args:
            processes.append(process)
        return process

    async def observe_submission(session_id, content, **kwargs):
        receipt = await submit(session_id, content, **kwargs)
        receipts.append((session_id, kwargs.get("work_id"), receipt))
        return receipt

    async def observe(event):
        frames.append(event)

    async def fail_original_question(event):
        sid = event.get("session_id")
        if sid == parent.id:
            return
        request_id = event.get("request_id")
        if controls.enabled and event.get("type") == "assistant_text" and request_id not in questioned:
            questioned.add(request_id)
            assert await manager.create_pending_question(sid, [{"question": "Original delegated question"}])
        if event.get("type") == "question_request" and request_id in questioned:
            question_entered.set()
            await release_question.wait()
            raise failure

    monkeypatch.setattr(asyncio, "create_subprocess_exec", observe_spawn)
    monkeypatch.setattr(manager, "start_message", observe_submission)
    manager.on_broadcast("observe-original-delegation", observe)
    manager.on_broadcast("fail-original-delegation-question", fail_original_question)
    delegations = DelegationManager()
    delegations.bind(manager, database)
    try:
        yield SimpleNamespace(
            manager=manager, delegations=delegations, parent=parent, starts=starts, receipts=receipts,
            frames=frames, entered=question_entered, release=release_question, controls=controls, root=tmp_path,
        )
    finally:
        release_question.set()
        for scope, _context in starts:
            (tmp_path / f"{scope.run_lease_id}.result").touch()
        try:
            await asyncio.wait_for(manager.drain_turns(timeout=3), 4)
            await manager.stop_all_held_processes()
            await delegations.confirm_recording()
        finally:
            delegations.shutdown()
            await database.close()
            for process in processes:
                if process.returncode is None:
                    process.kill()
                await asyncio.wait_for(process.wait(), 3)
            assert all(not Path(f"/proc/{process.pid}").exists() for process in processes)
            print("original-processes", [(process.pid, process.returncode) for process in processes])


async def release_model(runtime, rec):
    async with asyncio.timeout(2):
        while not any(scope.session_id == rec.delegation_id and scope.work_id == rec.work_id for scope, _ in runtime.starts):
            await asyncio.sleep(.005)
    scope = next(scope for scope, _ in reversed(runtime.starts)
                 if scope.session_id == rec.delegation_id and scope.work_id == rec.work_id)
    (runtime.root / f"{scope.run_lease_id}.result").touch()


async def unavailable_after_commit(runtime, rec):
    await asyncio.wait_for(runtime.entered.wait(), 2)
    original = next(receipt for sid, work, receipt in reversed(runtime.receipts)
                    if sid == rec.delegation_id and work == rec.work_id)
    waiting = asyncio.create_task(original.wait())
    try:
        await release_model(runtime, rec)
        async with asyncio.timeout(2):
            while await runtime.manager.db.get_committed_terminal(rec.delegation_id, original.request_id) is None:
                await asyncio.sleep(.01)
        assert not waiting.done() and rec.state == "running"
        assert not [event for event in runtime.frames if event.get("request_id") == original.request_id and event.get("type") == "result"]
        runtime.release.set()
        completion = await asyncio.wait_for(waiting, 2)
        assert completion.state == "completed" and completion.recording_state == "committed"
        assert completion.outcome.status == "success" and completion.notification_state == "failed"
        outcome = await runtime.manager.db.get_committed_terminal(rec.delegation_id, original.request_id)
        assert outcome["status"] == "success"
        print("original-proof", json.dumps({
            "request_id": original.request_id, "work_id": rec.work_id,
            "committed_outcome": outcome["status"], "completion": completion.state,
            "notification": completion.notification_state,
            "observed_terminal": any(event.get("request_id") == original.request_id and event.get("type") == "result" for event in runtime.frames),
            "delegation_state_before_drain": rec.state,
        }))
        return original
    finally:
        runtime.release.set()
        await asyncio.gather(waiting, return_exceptions=True)


async def wait_round_finished(runtime, rec):
    async with asyncio.timeout(2):
        while rec.state == "running" or runtime.manager.has_pending_or_active_work(rec.delegation_id, rec.work_id):
            await asyncio.sleep(.005)
    await asyncio.wait_for(runtime.delegations.confirm_recording(), 2)


async def test_committed_delegation_finishes_after_its_question_prefix_becomes_unavailable(
    notification_runtime,
):
    runtime = notification_runtime
    rec = await runtime.delegations.start_delegation(
        parent_session_id=runtime.parent.id, agent_name="Notification worker", request="Preserve this original delegated result",
    )
    await unavailable_after_commit(runtime, rec)
    await runtime.manager.await_session_execution(rec.delegation_id)
    await runtime.delegations.confirm_recording()
    assert rec.state == "completed"
    rows = await runtime.manager.db.load_messages(rec.delegation_id)
    assert [row["content"] for row in rows if row["role"] == "assistant" and row["type"] == "text"] == [
        f"Original body from {rec.delegation_id}",
    ]
    assert len([row for row in rows if row["type"] == "result"]) == 1
    replies = runtime.manager.dispatch_snapshot(runtime.parent.id)["items"]
    assert len(replies) == 1 and replies[0]["source"] == "delegation"
    assert f"Original body from {rec.delegation_id}" in replies[0]["summary"]
    assert len(runtime.starts) == 1


async def test_followup_finishes_its_own_round_after_a_failed_question_notification(notification_runtime):
    runtime = notification_runtime
    runtime.controls.enabled = False
    first = await runtime.delegations.start_delegation(
        parent_session_id=runtime.parent.id, agent_name="Notification worker", request="First successful round",
    )
    await release_model(runtime, first)
    await wait_round_finished(runtime, first)
    runtime.controls.enabled = True
    second = await runtime.delegations.follow_up_delegation(
        parent_session_id=runtime.parent.id, delegation_id=first.delegation_id, request="Second original round",
    )
    assert second is not first and second.work_id != first.work_id
    original = await unavailable_after_commit(runtime, second)
    await wait_round_finished(runtime, second)
    assert first.state == second.state == "completed"
    assert runtime.delegations.get_delegation(first.delegation_id) is second
    terminal = await runtime.manager.db.get_committed_terminal(second.delegation_id, original.request_id)
    assert terminal["status"] == "success"
    replies = runtime.manager.dispatch_snapshot(runtime.parent.id)["items"]
    assert len(replies) == 2 and len({item["request_id"] for item in replies}) == 2
    assert len(runtime.starts) == 2


async def test_old_unavailable_completion_cannot_finish_the_followup_round(notification_runtime, monkeypatch):
    runtime = notification_runtime
    first = await runtime.delegations.start_delegation(
        parent_session_id=runtime.parent.id, agent_name="Notification worker", request="Original cancelled round",
    )
    original = next(receipt for sid, work, receipt in runtime.receipts
                    if sid == first.delegation_id and work == first.work_id)
    read = runtime.manager.db.load_committed_request_messages
    entered, release = asyncio.Event(), asyncio.Event()

    async def hold_original_snapshot(session_id, request_id):
        if session_id == first.delegation_id and request_id == original.request_id:
            entered.set()
            await release.wait()
        return await read(session_id, request_id)

    monkeypatch.setattr(runtime.manager.db, "load_committed_request_messages", hold_original_snapshot)
    try:
        await unavailable_after_commit(runtime, first)
        await asyncio.wait_for(entered.wait(), 2)
        cancelled = await runtime.delegations.cancel_delegation(first.delegation_id, work_id=first.work_id)
        assert cancelled is first and first.state == "completed"
        runtime.controls.enabled = False
        second = await runtime.delegations.follow_up_delegation(
            parent_session_id=runtime.parent.id, delegation_id=first.delegation_id, request="Independent new round",
        )
        assert second.work_id != first.work_id and second.state == "running"
        release.set()
        await asyncio.wait_for(runtime.delegations.confirm_recording(), 2)
        assert runtime.delegations.get_delegation(second.delegation_id) is second
        assert second.state == "running" and first.state == "completed"
        assert len(runtime.manager.dispatch_snapshot(runtime.parent.id)["items"]) == 1
        await release_model(runtime, second)
        await wait_round_finished(runtime, second)
        assert second.state == "completed" and first.state == "completed"
        replies = runtime.manager.dispatch_snapshot(runtime.parent.id)["items"]
        assert len(replies) == 2 and len({item["request_id"] for item in replies}) == 2
        assert len(runtime.starts) == 2
    finally:
        release.set()


async def test_nested_round_waits_for_its_relay_after_both_original_prefixes_fail(notification_runtime):
    runtime = notification_runtime
    await AgentManager(runtime.manager.db).create_agent(name="Leaf worker", backend="codex")
    outer = await runtime.delegations.start_delegation(
        parent_session_id=runtime.parent.id, agent_name="Notification worker", request="Delegate then relay the answer",
    )
    await asyncio.wait_for(runtime.entered.wait(), 2)
    caller = next(scope for scope, _ in runtime.starts if scope.session_id == outer.delegation_id)
    leaf = await runtime.delegations.start_delegation(
        parent_session_id=outer.delegation_id, agent_name="Leaf worker", request="Original leaf result", scope=caller,
    )
    first = await unavailable_after_commit(runtime, outer)
    async with asyncio.timeout(2):
        while runtime.manager.has_pending_or_active_work(outer.delegation_id, outer.work_id):
            await asyncio.sleep(.005)
    await runtime.delegations.confirm_recording()
    assert outer.state == leaf.state == "running"
    assert runtime.manager.dispatch_snapshot(runtime.parent.id)["items"] == []
    assert (await runtime.manager.db.get_committed_terminal(outer.delegation_id, first.request_id))["status"] == "success"
    await release_model(runtime, leaf)
    await wait_round_finished(runtime, leaf)
    assert outer.state == "running" and runtime.manager.dispatch_snapshot(runtime.parent.id)["items"] == []
    async with asyncio.timeout(2):
        while sum(scope.session_id == outer.delegation_id for scope, _ in runtime.starts) != 2:
            await asyncio.sleep(.005)
    await release_model(runtime, outer)
    await wait_round_finished(runtime, outer)
    assert outer.state == leaf.state == "completed"
    replies = runtime.manager.dispatch_snapshot(runtime.parent.id)["items"]
    assert len(replies) == 1
    assert replies[0]["summary"].count(f"Original body from {outer.delegation_id}") == 1
    assert leaf.delegation_id not in replies[0]["summary"]
    assert len(runtime.starts) == 3
    for rec, expected_terminals in ((outer, 2), (leaf, 1)):
        rows = await runtime.manager.db.load_messages(rec.delegation_id)
        terminals = [row for row in rows if row["type"] == "result"]
        assert len(terminals) == expected_terminals and all(row["content"]["status"] == "success" for row in terminals)


async def test_database_close_joins_completion_created_round_commit(notification_runtime, monkeypatch, tmp_path):
    runtime = notification_runtime
    rec = await runtime.delegations.start_delegation(
        parent_session_id=runtime.parent.id, agent_name="Notification worker", request="Close after original committed result",
    )
    save = runtime.manager.db.compare_and_set_delegation_state
    entered, release = asyncio.Event(), asyncio.Event()
    closing = confirming = None

    async def hold_completion_commit(*args, **kwargs):
        if args[0] == rec.delegation_id and json.loads(args[2])["state"] == "completed":
            entered.set()
            await release.wait()
        return await save(*args, **kwargs)

    monkeypatch.setattr(runtime.manager.db, "compare_and_set_delegation_state", hold_completion_commit)
    try:
        await asyncio.wait_for(runtime.entered.wait(), 2)
        original = next(receipt for sid, work, receipt in runtime.receipts
                        if sid == rec.delegation_id and work == rec.work_id)
        await release_model(runtime, rec)
        async with asyncio.timeout(2):
            while await runtime.manager.db.get_committed_terminal(rec.delegation_id, original.request_id) is None:
                await asyncio.sleep(.005)
        closing = asyncio.create_task(runtime.manager.db.close())
        done, _ = await asyncio.wait({closing}, timeout=.05)
        assert not done and not entered.is_set()
        runtime.release.set()
        completion = await asyncio.wait_for(original.wait(), 2)
        assert completion.state == "completed" and completion.notification_state == "failed"
        assert completion.recording_state == "committed" and completion.outcome.status == "success"
        await asyncio.wait_for(entered.wait(), 2)
        confirming = asyncio.create_task(runtime.delegations.confirm_recording())
        done, _ = await asyncio.wait({closing, confirming}, timeout=.05)
        assert not done and rec.state == "running"
        release.set()
        await asyncio.wait_for(asyncio.gather(closing, confirming), 3)
        cold = Database(str(tmp_path / "delegations.sqlite3"))
        await cold.initialize()
        try:
            saved = json.loads(await cold.load_delegation_state(
                rec.delegation_id, parent_session_id=runtime.parent.id, user_id=runtime.parent.user_id,
            ))
            assert saved["state"] == "completed" and saved["work_id"] == rec.work_id
            assert saved["terminal_disposition"] == "closed"
            assert (await cold.get_committed_terminal(rec.delegation_id, original.request_id))["status"] == "success"
        finally:
            await cold.close()
        assert len(runtime.starts) == 1
    finally:
        release.set()
        await asyncio.gather(*(task for task in (closing, confirming) if task is not None), return_exceptions=True)


async def test_unavailable_child_without_a_terminal_is_not_reported_as_success(notification_runtime, monkeypatch):
    runtime = notification_runtime
    runtime.controls.enabled = False
    rec = await runtime.delegations.start_delegation(
        parent_session_id=runtime.parent.id, agent_name="Notification worker", request="Keep unknown effects without replay",
    )
    async with asyncio.timeout(2):
        while not any(event.get("session_id") == rec.delegation_id and event.get("type") == "assistant_text"
                      for event in runtime.frames):
            await asyncio.sleep(.005)
    original = next(receipt for sid, work, receipt in runtime.receipts
                    if sid == rec.delegation_id and work == rec.work_id)
    read = runtime.manager.db.has_delivery_notice

    async def fail_after_actual_read(session_id, delivery_id):
        assert await read(session_id, delivery_id) is False
        raise RuntimeError("Original child owner ended before selecting any terminal")

    monkeypatch.setattr(runtime.manager.db, "has_delivery_notice", fail_after_actual_read)
    with pytest.raises(SessionOwnerUnavailable):
        await runtime.manager.retain_result_notice(rec.delegation_id, "failed-owner", "Do not replace the original work")
    completion = await asyncio.wait_for(original.wait(), 2)
    assert completion.state == "unavailable" and completion.outcome is None
    assert completion.notification_state == "unavailable"
    await asyncio.wait_for(runtime.delegations.confirm_recording(), 2)
    assert rec.state == "interrupted" and "no committed outcome" in rec.error
    assert await runtime.manager.db.get_committed_terminal(rec.delegation_id, original.request_id) is None
    rows = await runtime.manager.db.load_messages(rec.delegation_id)
    assert not [row for row in rows if row["type"] == "result"]
    assert any(row["role"] == "user" and "Keep unknown effects without replay" in row["content"] for row in rows)
    replies = runtime.manager.dispatch_snapshot(runtime.parent.id)["items"]
    assert len(replies) == 1 and replies[0]["summary"].startswith("[agent-error:")
    assert len(runtime.starts) == 1
