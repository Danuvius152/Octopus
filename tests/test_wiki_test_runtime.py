"""Wiki fixture teardown retains the database's original producer-drain order."""

import asyncio
import os
import sys

import pytest

from server.harness import get_harness
from server.sessions import ownership
from tests import wiki_api
from tests.harness_profiles import process_profile


async def test_http_fixture_teardown_retires_an_active_original_process(tmp_path, monkeypatch, short_process_shutdown):
    # Exercise the actual fixture's teardown, including the path reached when
    # an assertion fails while its model stand-in is still running.
    fixture = wiki_api.client.__wrapped__(tmp_path, monkeypatch)
    await anext(fixture)
    manager = wiki_api.session_manager
    database = manager.db
    ready = asyncio.Event()
    harness = get_harness("codex")

    def argv(context):
        return [sys.executable, "-u", "-c",
            "import json,time\n"
            "print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':'ready'}}),flush=True)\n"
            "time.sleep(3600)\n"], {"cwd": context.working_dir}

    async def observe(event):
        if event.get("type") == "assistant_text" and event.get("content") == "ready":
            ready.set()

    monkeypatch.setattr(harness, "profile", process_profile(harness.profile, build_turn_argv=argv))
    # Bound the regression's incorrect wait; real process signals and joining
    # remain active. Correct teardown cancels the producer before this wait.
    monkeypatch.setattr(ownership, "_BACKEND_RETIRE_TIMEOUT", .2)
    manager.on_broadcast("fixture-original-process", observe)
    try:
        agent = await database.get_system_agent()
        session = await manager.create_session(agent["id"], "Fixture cleanup", str(tmp_path), backend="codex")
        receipt = await manager.start_message(session.id, "Keep running")
        await asyncio.wait_for(ready.wait(), 3)
        process = session._run.backend._process
        assert process.returncode is None
        await asyncio.wait_for(fixture.aclose(), 3)
        completion = await asyncio.wait_for(receipt.wait(), 1)
        assert completion.outcome.status == "interrupted"
        assert completion.recording_state == "committed"
        assert await receipt.execution.wait_resources() == ("proven",)
        assert process.returncode is not None
        with pytest.raises(ProcessLookupError):
            os.kill(process.pid, 0)
        assert database._closed
    finally:
        await fixture.aclose()
