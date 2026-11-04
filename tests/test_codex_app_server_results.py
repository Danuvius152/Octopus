"""Native final selection through the actual App and named-delegation consumers."""

import asyncio

import pytest

from server.agent_manager import AgentManager
from server.app_agent import AppAgentManager
from server.applications import ApplicationManager
from server.config import settings
from server.delegations import DelegationManager
from tests import native_app_server as native_tests
from tests.native_app_server import (
    ROOT,
    child_end,
    child_start,
    inject,
    item,
    terminal,
    until,
)
from tests.session_runtime import wait_dispatch_idle

native_runtime = native_tests.native_runtime


@pytest.mark.parametrize("tool_ended", [False, True])
async def test_native_app_stream_status_and_history_keep_the_original_tool_outcome(native_runtime, tmp_path, monkeypatch, tool_ended):
    manager, database, session, mode, starts, _events = native_runtime
    mode[0] = "control_output"
    monkeypatch.setattr(settings, "applications_dir", str(tmp_path / "apps"))
    await AgentManager(database).update_agent(session.agent_id, user_id=session.user_id, backend="codex")
    apps = ApplicationManager()
    apps.bind(session_mgr=manager, db=database)
    app_agent = AppAgentManager()
    app_agent.bind(session_mgr=manager, db=database, app_mgr=apps)
    app_dir = tmp_path / "apps" / "original-outcome"
    app_dir.mkdir(parents=True)
    await database.save_application(app_id="original-outcome", name="Original outcome", agent_id=session.agent_id,
        app_dir=str(app_dir), entrypoint="index.html", status="ready", created_at="now", updated_at="now", user_id=session.user_id)
    app_row = await database.get_application("original-outcome")
    starts.clear()  # Static agent configuration inspection is not an execution.

    async def collect(turn):
        return [event async for event in app_agent.stream_turn(turn)]

    try:
        turn = await app_agent.begin_turn(app_row, message="Try the operation once")
        consuming = asyncio.create_task(collect(turn))
        original = manager.get_session(turn.session_id)
        await until(lambda: original._run.backend is not None and original._run.backend.active_turn is not None)
        tool = {"id": "connector-call", "type": "mcpToolCall", "server": "connector", "tool": "write",
                "arguments": {}, "status": "inProgress"}
        frames = [{"method": "item/started", "params": {"threadId": ROOT, "turnId": "turn-1", "item": tool}}]
        if tool_ended:
            frames.append({"method": "item/completed", "params": {"threadId": ROOT, "turnId": "turn-1", "item": {
                **tool, "status": "failed", "error": {"message": "The operation was refused"}}}})
        inject(tmp_path, *frames, item(ROOT, "turn-1", "final", "Saved model output"), terminal())
        stream = await asyncio.wait_for(consuming, 4)
        await wait_dispatch_idle(manager, original.id)
        saved = await database.read_session_result(original.id, turn.request_id)
        status = await app_agent.get_request_status(app_row["id"], original.id, turn.request_id)
        assert saved.outcome.status == ("success" if tool_ended else "failed")
        assert status["state"] == ("completed" if tool_ended else "failed")
        assert status["outcome"]["status"] == saved.outcome.status
        assert status["reply"] == "Saved model output"
        assert [event["type"] for event in stream if event["type"] in {"error", "done"}] == ["done" if tool_ended else "error"]
        if not tool_ended:
            assert "model turn completed" in stream[-1]["message"].lower()
        history = await app_agent.get_conversation(app_row["id"], original.id)
        assert [row["text"] for row in history["messages"] if row["role"] == "assistant"] == ["Saved model output"]
        assert len(starts) == 1
    finally:
        app_agent.shutdown()
        apps.shutdown()


def answer_frames(turn_id="turn-1"):
    commentary = item(ROOT, "turn-1", "progress", "Root progress")
    commentary["params"]["item"]["phase"] = "commentary"
    frames = [commentary, child_start(), item("child", "child-turn", "same", "Child answer"), child_end(),
            child_start(turn="followup"), item(ROOT, "turn-1", "one", "First final"),
            item("child", "followup", "same", "Child followup"),
            item(ROOT, "turn-1", "two", "Second final"), terminal()]
    def rebase(value):
        if isinstance(value, dict):
            return {key: rebase(child) for key, child in value.items()}
        if isinstance(value, list):
            return [rebase(child) for child in value]
        return turn_id if value == "turn-1" else value
    return rebase(frames)


async def recover_first_attempt(tmp_path, starts, target):
    failure = terminal("failed")
    failure["params"]["turn"]["error"] = {"codexErrorInfo": "serverOverloaded", "message": "Selected model is at capacity"}
    inject(tmp_path, item(ROOT, "turn-1", "failed-final", "Earlier failed-attempt output"), failure)
    await until(lambda: len(starts) == 2 and target()._run.backend is not None
                and target()._run.backend.active_turn is not None and target()._run.backend.active_turn.turn_id == "turn-2")


@pytest.mark.parametrize("delivery", ["ask", "stream"])
@pytest.mark.parametrize("recovery", [False, True])
async def test_native_app_reply_stream_and_refresh_use_the_same_final(native_runtime, tmp_path, monkeypatch, delivery, recovery):
    manager, database, session, mode, starts, _ = native_runtime
    mode[0] = "control_output"
    monkeypatch.setattr(manager, "_TRANSIENT_RETRY_BASE_DELAY", .001)
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
        if recovery:
            await recover_first_attempt(tmp_path, starts, lambda: next(s for s in manager.sessions.values() if s.app_id == app_row["id"]))
            assert not operation.done()
        inject(tmp_path, *answer_frames("turn-2" if recovery else "turn-1"))
        reply = await asyncio.wait_for(operation, 4)
        if delivery == "stream":
            assert [event["text"] for event in reply if event["type"] == "progress"] == ["Root progress"]
            last_reset = max((index for index, event in enumerate(reply) if event["type"] == "reset"), default=-1)
            assert [event["text"] for event in reply[last_reset + 1:] if event["type"] == "message"] == ["First final", "Second final"]
            assert len([event for event in reply if event["type"] == "done"]) == 1
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
        assert len(starts) == (2 if recovery else 1)
    finally:
        app_agent.shutdown()
        apps.shutdown()


@pytest.mark.parametrize("recovery", [False, True])
async def test_named_delegation_delivers_only_the_bound_roots_selected_final(native_runtime, tmp_path, monkeypatch, recovery):
    manager, database, parent, mode, starts, _ = native_runtime
    mode[0] = "control_output"
    monkeypatch.setattr(manager, "_TRANSIENT_RETRY_BASE_DELAY", .001)
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
        if recovery:
            await recover_first_attempt(tmp_path, starts, lambda: child)
            assert work.state != "completed"
        inject(tmp_path, *answer_frames("turn-2" if recovery else "turn-1"))
        await wait_dispatch_idle(manager, child.id)
        async with asyncio.timeout(4):
            while (saved := await database.get_source_submission("delegation", f"agent-terminal:{work.work_id}",
                                                                 session_id=parent.id)) is None:
                await asyncio.sleep(0)
        assert saved["payload"]["content"].endswith(
            f"[agent-reply:Native worker delegation={work.delegation_id}]\nFirst final\n\nSecond final")
        assert len(starts) == (2 if recovery else 1) and work.state == "completed"
    finally:
        delegations.shutdown()


async def test_native_app_build_waits_through_provider_recovery(native_runtime, tmp_path, monkeypatch):
    from pathlib import Path

    manager, database, parent, mode, starts, _ = native_runtime
    mode[0] = "control_output"
    monkeypatch.setattr(manager, "_TRANSIENT_RETRY_BASE_DELAY", .001)
    monkeypatch.setattr(settings, "applications_dir", str(tmp_path / "apps"))
    await AgentManager(database).update_agent(parent.agent_id, user_id=parent.user_id, backend="codex")
    apps = ApplicationManager()
    apps.bind(session_mgr=manager, db=database)
    starts.clear()  # Static configuration validation did not launch a host.
    session = None
    try:
        app = await apps.create_application(name="Recovered artifact", description="Build", agent_id=parent.agent_id)
        session = manager.get_session(app["session_id"])
        await until(lambda: session._run.backend is not None and session._run.backend.active_turn is not None)
        artifact = Path(app["app_dir"]) / app["entrypoint"]
        artifact.write_text("<html>Original artifact</html>")
        await recover_first_attempt(tmp_path, starts, lambda: session)
        assert (await database.get_application(app["id"]))["status"] == "building"
        inject(tmp_path, *answer_frames("turn-2"))
        await wait_dispatch_idle(manager, session.id)
        async with asyncio.timeout(3):
            while (updated := await database.get_application(app["id"]))["status"] == "building":
                await asyncio.sleep(.005)
        assert updated["status"] == "ready" and artifact.read_text() == "<html>Original artifact</html>"
        rows = await database.load_messages(session.id)
        assert len([row for row in rows if row["type"] == "result"]) == 1 and len(starts) == 2
    finally:
        if session is not None:
            await manager.interrupt(session.id)
            await manager.await_session_execution(session.id, timeout=5)
        apps.shutdown()
