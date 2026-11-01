"""Native admission closes before database settlement or OS retirement finishes."""

import asyncio
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from server.agent_manager import AgentManager
from server.deps import get_session_manager
from server.harness import codex_app_server
from server.routers import schedules
from server.sessions.client_submissions import submit_client_message
from tests.test_codex_app_server_submission import PEER
from tests.test_codex_app_server_submission import native_runtime as native_runtime
from tests.test_runtime_review_races import wait_dispatch_idle


@pytest.mark.parametrize("ending", ["terminal", "stop"])
async def test_native_tool_scope_closes_before_settlement_and_resource_retirement(
    native_runtime, tmp_path, monkeypatch, ending,
):
    manager, database, session, mode, starts, events = native_runtime
    mode[0] = "held" if ending == "stop" else "normal"
    await AgentManager(database).update_agent(session.agent_id, user_id=session.user_id, mcp_servers=["schedule"])
    release_process = tmp_path / "release-process"
    retiring = tmp_path / "retiring"
    wrapper = tmp_path / "owned_native_peer.py"
    wrapper.write_text(
        "import runpy,signal,time\nfrom pathlib import Path\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        f"runpy.run_path({str(PEER)!r}, run_name='__main__')\n"
        f"Path({str(retiring)!r}).touch()\n"
        f"while not Path({str(release_process)!r}).exists(): time.sleep(.005)\n"
    )

    def argv(context):
        starts.append(context)
        return [sys.executable, "-u", str(wrapper), str(tmp_path), mode[0]], {"cwd": context.working_dir}

    monkeypatch.setattr(codex_app_server, "build_argv", argv)
    entered, release_terminal = asyncio.Event(), asyncio.Event()
    record = database.record_terminal

    async def blocked_terminal(**kwargs):
        entered.set()
        await release_terminal.wait()
        await record(**kwargs)

    monkeypatch.setattr(database, "record_terminal", blocked_terminal)
    monkeypatch.setattr(schedules, "_db", database)
    app = FastAPI()
    app.include_router(schedules.session_router)
    app.dependency_overrides[get_session_manager] = lambda: manager
    stopping = None
    try:
        await submit_client_message(manager, session.id, "Exercise original native authority", [], ending, wiki_mode=None)
        async with asyncio.timeout(3):
            while not any(event.get("type") == "assistant_text" for event in events):
                await asyncio.sleep(.005)
        context = starts[-1]
        scope = next(entry for entry in context.mcp_servers if entry.key == "schedule")
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test",
                               headers={"Authorization": f"Bearer {scope.credential}"}) as client:
            endpoint = f"/api/sessions/{session.id}/schedules"
            if ending == "stop":
                assert (await client.get(endpoint)).status_code == 200
                stopping = asyncio.create_task(manager.interrupt(session.id))
                await stopping
                assert not entered.is_set(), "Native terminal recording follows the bounded original prefix"
            else:
                async with asyncio.timeout(3):
                    while not retiring.exists():
                        await asyncio.sleep(.005)
            row = await database.get_client_submission(session.id, ending)
            assert Path(f"/proc/{row['process_group']}").exists()
            assert not release_terminal.is_set() and not release_process.exists()
            assert (await client.get(endpoint)).status_code == 401
            release_process.touch()
            await asyncio.wait_for(entered.wait(), 3)
            assert (await client.get(endpoint)).status_code == 401
    finally:
        release_terminal.set()
        release_process.touch()
        if stopping is not None:
            await stopping
        await wait_dispatch_idle(manager, session.id)
