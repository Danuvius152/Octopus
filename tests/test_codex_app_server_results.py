"""Native final selection through the actual App and named-delegation consumers."""

import asyncio

import pytest

from server.agent_manager import AgentManager
from server.app_agent import AppAgentManager
from server.applications import ApplicationManager
from server.config import settings
from server.delegations import DelegationManager
from tests import test_codex_app_server_submission as native_tests
from tests.test_codex_app_server_output import ROOT, child_end, child_start, item
from tests.test_codex_app_server_questions import inject, terminal, until
from tests.test_runtime_review_races import wait_dispatch_idle

native_runtime = native_tests.native_runtime


def answer_frames():
    commentary = item(ROOT, "turn-1", "progress", "Root progress")
    commentary["params"]["item"]["phase"] = "commentary"
    return [commentary, child_start(), item("child", "child-turn", "same", "Child answer"), child_end(),
            child_start(turn="followup"), item(ROOT, "turn-1", "one", "First final"),
            item("child", "followup", "same", "Child followup"),
            item(ROOT, "turn-1", "two", "Second final"), terminal()]


@pytest.mark.parametrize("delivery", ["ask", "stream"])
async def test_native_app_reply_stream_and_refresh_use_the_same_final(native_runtime, tmp_path, monkeypatch, delivery):
    manager, database, session, mode, starts, _ = native_runtime
    mode[0] = "control_output"
    monkeypatch.setattr(settings, "applications_dir", str(tmp_path / "apps"))
    await AgentManager(database).update_agent(session.agent_id, user_id=session.user_id, backend="codex")
    apps = ApplicationManager()
    apps.bind(session_mgr=manager, db=database)
    app_agent = AppAgentManager()
    app_agent.bind(session_mgr=manager, db=database, app_mgr=apps)
    app_dir = tmp_path / "apps" / "native-app"
    app_dir.mkdir(parents=True)
    await database.save_application(app_id="native-app", name="Native results", description="An existing app",
        agent_id=session.agent_id, session_id=None, app_dir=str(app_dir), entrypoint="index.html", status="ready",
        created_at="2026-11-01T00:00:00+00:00", updated_at="2026-11-01T00:00:00+00:00", user_id=session.user_id)
    app_row = await database.get_application("native-app")
    starts.clear()

    async def collect_stream(turn):
        return [event async for event in app_agent.stream_turn(turn)]

    try:
        if delivery == "ask":
            operation = asyncio.create_task(app_agent.ask(app_row, message="Read the final"))
        else:
            turn = await app_agent.begin_turn(app_row, message="Read the final")
            operation = asyncio.create_task(collect_stream(turn))
        await until(lambda: any(s.app_id == app_row["id"] and s._run.backend is not None
                               and s._run.backend.active_turn is not None for s in manager.sessions.values()))
        inject(tmp_path, *answer_frames())
        reply = await asyncio.wait_for(operation, 4)
        if delivery == "stream":
            assert [event["text"] for event in reply if event["type"] == "progress"] == ["Root progress"]
            assert [event["text"] for event in reply if event["type"] == "message"] == ["First final", "Second final"]
            reply = next(event for event in reply if event["type"] == "done")
            conversation = turn.session_id
            request = turn.request_id
        else:
            conversation, request = reply["conversation_id"], reply["request_id"]
        assert reply["reply"] == "First final\n\nSecond final"
        saved = await app_agent.get_request_status(app_row["id"], conversation, request)
        assert saved["state"] == "completed" and saved["reply"] == reply["reply"]
        history = await app_agent.get_conversation(app_row["id"], conversation)
        assert [message["text"] for message in history["messages"] if message["role"] == "assistant"] == ["First final", "Second final"]
        assert len(starts) == 1
    finally:
        app_agent.shutdown()
        apps.shutdown()


async def test_named_delegation_delivers_only_the_bound_roots_selected_final(native_runtime, tmp_path):
    manager, database, parent, mode, starts, _ = native_runtime
    mode[0] = "control_output"
    await manager.interrupt(parent.id)
    await AgentManager(database).create_agent(name="Native worker", backend="codex", user_id=parent.user_id)
    starts.clear()
    delegations = DelegationManager()
    delegations.bind(manager, database)
    try:
        work = await delegations.start_delegation(parent_session_id=parent.id,
            agent_name="Native worker", request="Return the root final")
        child = manager.get_session(work.delegation_id)
        await until(lambda: child._run.backend is not None and child._run.backend.active_turn is not None)
        inject(tmp_path, *answer_frames())
        await wait_dispatch_idle(manager, child.id)
        async with asyncio.timeout(4):
            while (saved := await database.get_source_submission("delegation", f"agent-terminal:{work.work_id}",
                                                                 session_id=parent.id)) is None:
                await asyncio.sleep(0)
        assert saved["payload"]["content"].endswith(
            f"[agent-reply:Native worker delegation={work.delegation_id}]\nFirst final\n\nSecond final")
        assert len(starts) == 1 and work.state == "completed"
    finally:
        delegations.shutdown()
