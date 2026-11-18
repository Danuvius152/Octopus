"""Shared original delegation runtime, subprocess retirement and observations."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from server.agent_manager import AgentManager
from server.database import Database
from server.delegations import DelegationManager
from server.harness import get_harness
from server.harness.claude_code import ClaudeDriver
from server.mcp_identity import verify
from server.sessions import SessionManager
from tests.accounts import create_account
from tests.harness_profiles import process_profile, session_turn_argv


@pytest.fixture
async def notification_runtime(tmp_path, monkeypatch):
    database = Database(str(tmp_path / "delegations.sqlite3"))
    await database.initialize()
    owner, _ = await create_account(database, extra_roots=[tmp_path])
    manager = SessionManager()
    await manager.initialize(database)
    root = await database.get_system_agent()
    await AgentManager(database).create_agent(name="Notification worker", backend="codex", user_id=owner["id"])
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
    controls = SimpleNamespace(enabled=True, archive_error=None)
    failure = asyncio.CancelledError("Original delegated question publication was cancelled")
    harness = get_harness("codex")

    def argv(context):
        scope = verify(next(entry.credential for entry in context.mcp_servers if entry.key == "bg"))
        assert scope is not None
        starts.append((scope, context))
        return [sys.executable, "-u", str(model), scope.session_id, str(tmp_path / f"{scope.run_lease_id}.result")], {"cwd": context.working_dir}

    monkeypatch.setattr(harness, "profile", process_profile(
        harness.profile, binary=sys.executable, prepare_resume=None, activity_observer=None,
        new_driver=ClaudeDriver, can_steer=True, reusable=True, build_turn_argv=session_turn_argv(harness.profile, argv),
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
    manager.on_broadcast("fail-original-delegation-question", fail_original_question, required=True)
    delegations = DelegationManager()
    delegations.bind(manager, database)

    async def confirm_recording():
        if controls.archive_error is None:
            await delegations.confirm_recording()
            return
        with pytest.raises(RuntimeError, match="Delegation command cleanup failed") as blocked:
            await delegations.confirm_recording()
        assert blocked.value.__cause__ is controls.archive_error

    try:
        yield SimpleNamespace(
            manager=manager, delegations=delegations, parent=parent, starts=starts, receipts=receipts,
            frames=frames, entered=question_entered, release=release_question, controls=controls, root=tmp_path,
            confirm_recording=confirm_recording,
        )
    finally:
        release_question.set()
        for scope, _context in starts:
            (tmp_path / f"{scope.run_lease_id}.result").touch()
        try:
            try:
                await asyncio.wait_for(manager.drain_turns(timeout=3), 4)
            except RuntimeError as error:
                if error is not controls.archive_error:
                    raise
            await manager.stop_all_held_processes()
            await confirm_recording()
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


async def wait_round_finished(runtime, rec):
    async with asyncio.timeout(2):
        while rec.state == "running" or runtime.manager.has_pending_or_active_work(rec.delegation_id, rec.work_id):
            await asyncio.sleep(.005)
    await asyncio.wait_for(runtime.confirm_recording(), 2)


async def starts(runtime, session_id, count):
    async with asyncio.timeout(3):
        while True:
            observed = [scope for scope, _ in runtime.starts if scope.session_id == session_id]
            if len(observed) >= count and (runtime.root / f"{observed[count - 1].run_lease_id}.input").exists():
                return
            await asyncio.sleep(.005)


def observe_terminal_deliveries(runtime, monkeypatch):
    """Record actual public submissions without replacing any receipt."""
    submit = runtime.manager.start_message
    delivered = []

    async def observe(session_id, content, **kwargs):
        receipt = await submit(session_id, content, **kwargs)
        delivery_id = kwargs.get("delivery_id", "")
        if delivery_id.startswith("agent-terminal:"):
            delivered.append((session_id, content, kwargs, receipt))
        return receipt

    monkeypatch.setattr(runtime.manager, "start_message", observe)
    return delivered
