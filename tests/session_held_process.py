"""Shared original-process setup and retirement for Owner lifecycle tests."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from server.database import Database
from server.deps import get_session_manager
from server.harness import get_harness
from server.harness.claude_code import ClaudeDriver
from server.harness.run import HarnessRun
from server.routers.sessions import router
from server.sessions import SessionManager
from tests.accounts import create_account
from tests.harness_profiles import process_profile


@pytest.fixture
async def held_rewind(tmp_path, monkeypatch, request):
    directory = tmp_path / "original"
    directory.mkdir()
    source = directory / "source.txt"
    source.write_text("Keep the original workspace bytes.\n")
    model = directory / "held_model.py"
    identity_path, input_path = directory / "identity.json", directory / "input.json"
    retirement_path = directory / "retirement.json"
    model.write_text(
        "import json,os,signal,sys,time\nfrom pathlib import Path\n"
        "signal.signal(signal.SIGTERM,lambda *_:None)\n"
        "fields=Path(f'/proc/{os.getpid()}/stat').read_text().rsplit(')',1)[1].split()\n"
        "Path(sys.argv[1]).write_text(json.dumps({'pid':os.getpid(),'pgid':os.getpgrp(),'start_time':fields[19]}))\n"
        "Path(sys.argv[2]).write_text(sys.stdin.readline())\n"
        "print(json.dumps({'type':'thread.started','thread_id':'original-rewind-thread'}),flush=True)\n"
        "print(json.dumps({'type':'item.completed','item':{'id':'original-answer','type':'agent_message','text':'Original confirmed answer.'}}),flush=True)\n"
        "print(json.dumps({'type':'turn.completed','usage':{}}),flush=True)\n"
        "sys.stdin.read()\n"
        "began=time.monotonic()\n"
        "time.sleep(float(sys.argv[4]))\n"
        "Path(sys.argv[3]).write_text(json.dumps({'eof_at':began,'exited_at':time.monotonic()}))\n"
    )
    harness = get_harness("codex")
    monkeypatch.setattr(harness, "profile", process_profile(
        harness.profile, binary=sys.executable, new_driver=ClaudeDriver, can_steer=True, reusable=True,
        activity_observer=None, prepare_resume=None,
        build_turn_argv=lambda context: (
            [sys.executable, "-u", str(model), str(identity_path), str(input_path),
             str(retirement_path), str(request.param)],
            {"cwd": context.working_dir},
        ),
    ))
    spawn, stream = asyncio.create_subprocess_exec, HarnessRun.stream
    processes, readers = [], set()

    async def capture_spawn(*args, **kwargs):
        process = await spawn(*args, **kwargs)
        if str(model) in args:
            processes.append(process)
        return process

    async def capture_stream(run):
        readers.add(asyncio.current_task())
        async for event in stream(run):
            yield event

    monkeypatch.setattr(asyncio, "create_subprocess_exec", capture_spawn)
    monkeypatch.setattr(HarnessRun, "stream", capture_stream)
    database = Database(str(tmp_path / "rewind.sqlite3"))
    await database.initialize()
    _, bearer = await create_account(database, extra_roots=[tmp_path])
    manager = SessionManager()
    await manager.initialize(database)
    agent = await database.get_system_agent()
    parent = await manager.create_session(agent["id"], "Original parent", str(directory), backend="codex")
    events = []

    async def observe(event):
        events.append(event)

    manager.on_broadcast("original-rewind", observe)
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_session_manager] = lambda: manager
    async with AsyncClient(
        transport=ASGITransport(app), base_url="http://test",
        headers={"Authorization": "Bearer " + bearer},
    ) as client:
        try:
            prompt = "Keep this original request and completed answer."
            receipt = await manager.start_message(parent.id, prompt, work_id=f"original-work-{parent.id}")
            completion = await asyncio.wait_for(receipt.wait(), 3)
            assert completion.outcome.status == "success" and completion.recording_state == "committed"
            assert len(processes) == len(readers) == 1
            identity = json.loads(identity_path.read_text())
            assert identity["pid"] == identity["pgid"] == processes[0].pid
            assert Path(f"/proc/{identity['pid']}").exists() and processes[0].returncode is None
            assert json.loads(input_path.read_text())["message"]["content"] == prompt
            assert all(reader.done() for reader in readers)
            stdout = {task for task in asyncio.all_tasks() if task.get_name() == "codex-stdout"}
            owner_tasks = {task for task in asyncio.all_tasks()
                           if task.get_name() == f"session-owner-{parent.id}" or task.get_name() == "session-publication"}
            assert len(stdout) == 1 and len(owner_tasks) == 2
            yield SimpleNamespace(
                manager=manager, database=database, parent=parent, client=client,
                events=events, receipt=receipt, completion=completion, identity=identity,
                processes=processes, readers=readers, stdout=stdout, owner_tasks=owner_tasks,
                source=source, retirement_path=retirement_path, prompt=prompt,
            )
        finally:
            try:
                await manager.drain_turns(timeout=7)
                await manager.stop_all_held_processes()
            finally:
                for process in processes:
                    if process.returncode is None:
                        process.kill()
                    await asyncio.wait_for(process.wait(), 3)
                await database.close()
                await asyncio.gather(*readers, return_exceptions=True)
