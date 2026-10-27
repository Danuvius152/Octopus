"""A real background process returns to its fixed caller after archive."""

import asyncio
import shlex
import sys

import pytest

from server.bg_tasks import BgTaskManager
from tests.test_research_session_return import returning_research as returning_research


@pytest.mark.parametrize("paused", [False, True])
@pytest.mark.parametrize("exit_code", [0, 7])
async def test_background_command_survives_archive_and_returns_once(returning_research, tmp_path, paused, exit_code):
    db, manager, _, _, _, processed = returning_research
    agent = await db.get_system_agent()
    session = await manager.create_session(agent["id"], "Original background caller")
    gate, executions = tmp_path / "release", tmp_path / "executions"
    program = (
        "import pathlib,sys,time\n"
        "gate=pathlib.Path(sys.argv[1])\n"
        "while not gate.exists(): time.sleep(0.01)\n"
        "with open(sys.argv[2],'a') as output: output.write('once\\n')\n"
        "print('retained output',flush=True)\n"
        "sys.exit(int(sys.argv[3]))\n"
    )
    command = shlex.join([sys.executable, "-c", program, str(gate), str(executions), str(exit_code)])
    finished = asyncio.Event()
    accepted = []

    async def deliver(record):
        accepted.append(await manager.deliver_bg_result(record))
        finished.set()

    background = BgTaskManager()
    background.bind(db, deliver, manager._broadcast)
    await background.start()
    try:
        record = await background.start_task(session_id=session.id, command=command, working_dir=str(tmp_path))
        if paused:
            await manager.interrupt(session.id)
        await manager.archive_session(session.id, replace=False)
        gate.touch()
        await asyncio.wait_for(finished.wait(), 5)
        assert accepted == [True]
        assert manager.dispatch_snapshot(session.id)["paused"] is paused
        if paused:
            assert processed.empty()
            await manager.resume_dispatch(session.id)
        prompt = await asyncio.wait_for(processed.get(), 3)
        assert "[bg-task-result]" in prompt and "retained output" in prompt
        assert await manager.deliver_bg_result(record) is True
        assert processed.empty()
        assert executions.read_text() == "once\n"
        stored = await db.get_bg_task(record.id)
        assert stored["stdout"] == "retained output\n" and stored["exit_code"] == exit_code
    finally:
        gate.touch()
        await background.shutdown()
