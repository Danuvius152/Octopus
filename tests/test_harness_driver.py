"""Protocol operations use the same owned process without shared wire assumptions."""

import asyncio
import sys
from dataclasses import replace

from server.harness import Harness
from server.harness.codex import CODEX
from server.harness.driver import ArgvDriver


async def test_driver_prepares_then_sends_its_own_input_protocol(tmp_path):
    class LineDriver(ArgvDriver):
        input_in_argv = False

        async def start_input(self, io, context):
            assert io.input_may_have_run is False
            await io.write_input((context.prompt + "\n").encode())

    script = (
        "import json,sys\n"
        "text=sys.stdin.readline().rstrip('\\n')\n"
        "print(json.dumps({'type':'item.completed','item':"
        "{'type':'agent_message','text':text}}),flush=True)\n"
        "print(json.dumps({'type':'turn.completed','usage':{}}),flush=True)\n"
    )
    profile = replace(
        CODEX, new_driver=LineDriver, activity_observer=None,
        build_turn_argv=lambda context: ([sys.executable, "-c", script], {"cwd": context.working_dir}),
    )
    run = Harness(profile).create_run()
    try:
        await run.start("protocol-owned input", str(tmp_path))
        async with asyncio.timeout(3):
            events = [event async for event in run.stream()]
        assert [event.content for event in events if event.type == "text"] == ["protocol-owned input"]
        assert run.input_may_have_run is True
        assert events[-1].type == "result"
    finally:
        await run.stop()
    assert run.retirement_state == "proven"
