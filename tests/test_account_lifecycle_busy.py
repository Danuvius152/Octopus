"""Busy fork refusal does not block the current account's actual turn retirement."""

import asyncio
import sys
from dataclasses import replace

import pytest
from httpx import ASGITransport, AsyncClient

from server import deps
from server.config import settings
from server.database import Database
from server.harness import get_harness
from server.harness.profile import StdinMode
from server.main import app
from server.session_manager import SessionManager
from tests.accounts import create_account


@pytest.mark.parametrize("operation", ["fork", "duplicate"])
async def test_busy_fork_refuses_without_blocking_owned_stop(
    tmp_path, monkeypatch, operation, short_process_shutdown,
):
    monkeypatch.setattr(settings, "master_key", "busy-lifecycle-master")
    monkeypatch.setattr(settings, "agents_dir", str(tmp_path / "unused-global-agents"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    database = Database(str(tmp_path / "busy-lifecycle.db"))
    await database.initialize()
    owner, token = await create_account(database, extra_roots=[tmp_path])
    manager = SessionManager()
    await manager.initialize(database)
    app.dependency_overrides[deps.get_session_manager] = lambda: manager

    model = tmp_path / "busy_turn.py"
    retired = tmp_path / "retired"
    model.write_text(
        "import json,sys\n"
        "from pathlib import Path\n"
        "sys.stdin.readline()\n"
        "print(json.dumps({'type':'thread.started','thread_id':'busy-lifecycle-thread'}),flush=True)\n"
        "print(json.dumps({'type':'item.completed','item':{'id':'active','type':'agent_message','text':'The original turn is still running.'}}),flush=True)\n"
        "sys.stdin.read()\n"
        "Path(sys.argv[1]).write_text('The original process reached stdin EOF.')\n"
    )
    harness = get_harness("codex")
    monkeypatch.setattr(harness, "profile", replace(
        harness.profile, binary=sys.executable, activity_observer=None,
        prepare_resume=None, stdin_mode=StdinMode.STREAM_JSON,
        build_turn_argv=lambda context: (
            [sys.executable, "-u", str(model), str(retired)], {"cwd": context.working_dir},
        ),
    ))
    processes = []
    spawn = asyncio.create_subprocess_exec

    async def observe_spawn(*argv, **kwargs):
        process = await spawn(*argv, **kwargs)
        if str(model) in argv:
            processes.append(process)
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", observe_spawn)
    ready = asyncio.Event()

    async def observe_output(event):
        if event["type"] == "assistant_text":
            ready.set()

    manager.on_broadcast("busy-lifecycle-output", observe_output)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    original = b"Keep these original workspace bytes.\x00\xff\n"
    (workspace / "original.bin").write_bytes(original)
    agent = await database.get_system_agent(owner["id"])
    parent = await manager.create_session(
        agent["id"], "Continuous original turn", str(workspace), backend="codex",
    )
    branch = stopping = None
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test",
            headers={"Authorization": "Bearer " + token},
        ) as client:
            receipt = await manager.start_message(parent.id, "Keep working until I stop this turn.")
            assert receipt.accepted
            await asyncio.wait_for(ready.wait(), 3)
            assert len(processes) == 1 and processes[0].returncode is None

            branch = asyncio.create_task(client.post(
                f"/api/sessions/{parent.id}/{operation}",
                json={"rewind_to_msg_seq": 0} if operation == "fork" else {},
            ))
            done, _ = await asyncio.wait({branch}, timeout=.25)
            stopping = asyncio.create_task(manager.interrupt(parent.id))
            await asyncio.wait({stopping}, timeout=.25)
            assert done, "Busy fork waited on the live turn instead of refusing."
            refused = await branch
            assert refused.status_code == 409, refused.text
            assert refused.json()["detail"]["reason"] == "fork_blocked_parent_turn_active"

            await asyncio.wait_for(asyncio.shield(stopping), 5)
            await manager.await_session_execution(parent.id, timeout=5)
            assert manager.get_session(parent.id, owner["id"]) is parent
            assert parent.user_id == await database.get_session_owner(parent.id) == owner["id"]
            assert processes[0].returncode == 0
            assert retired.read_text() == "The original process reached stdin EOF."
            assert (await asyncio.wait_for(receipt.wait(), 3)).outcome.status == "interrupted"
            listed = await client.get("/api/sessions")
            assert listed.status_code == 200, listed.text
            assert [row["id"] for row in listed.json()] == [parent.id]
            assert [row["id"] for row in await database.load_sessions(include_archived=True)] == [parent.id]
            detail = await client.get(f"/api/sessions/{parent.id}")
            assert detail.status_code == 200, detail.text
            assert any(message["content"] == "Keep working until I stop this turn."
                       for message in detail.json()["messages"])
            assert (workspace / "original.bin").read_bytes() == original
    finally:
        # Retire the original real process before joining either command.
        await asyncio.wait_for(manager.interrupt(parent.id), 5)
        for process in processes:
            if process.returncode is None:
                process.kill()
            await process.wait()
        await asyncio.wait_for(asyncio.gather(
            *(task for task in (branch, stopping) if task is not None), return_exceptions=True,
        ), 5)
        await manager.drain_turns()
        manager.remove_broadcast("busy-lifecycle-output")
        app.dependency_overrides.pop(deps.get_session_manager, None)
        await database.close()
