"""An App finishes from the original completion after its notification prefix."""

from __future__ import annotations

import asyncio
import sys

import pytest

from server.agent_manager import AgentManager
from server.app_agent import AppAgentError, AppAgentManager
from server.applications import ApplicationManager
from server.config import settings
from server.harness import get_harness
from server.harness.claude_code import ClaudeDriver
from tests import test_session_owner_publication as publication_tests
from tests.harness_profiles import process_profile

notices = publication_tests.notices


@pytest.mark.parametrize("cancelled_terminal", [False, True])
async def test_app_answer_waits_for_the_original_terminal_notification_prefix(
    notices, tmp_path, monkeypatch, cancelled_terminal,
):
    manager, original, client = notices
    monkeypatch.setattr(settings, "applications_dir", str(tmp_path / "applications"))
    applications = ApplicationManager()
    applications.bind(session_mgr=manager, db=manager.db)
    app_agent = AppAgentManager()
    app_agent.bind(session_mgr=manager, db=manager.db, app_mgr=applications)
    agent = await AgentManager(manager.db).create_agent(name="Original App agent", backend="codex", user_id=original.user_id)
    await manager.db.save_application(
        app_id="original-prefix-app", name="Original App", agent_id=agent["id"],
        description="A ready App with an original agent conversation.",
        app_dir=str(tmp_path / "applications" / "original"), status="ready",
        user_id=original.user_id,
        created_at="2026-10-21T00:00:00+00:00", updated_at="2026-10-21T00:00:00+00:00",
    )
    application = await manager.db.get_application("original-prefix-app")
    assert application is not None
    model = tmp_path / "app_prefix_model.py"
    model.write_text(
        "import json,sys\n"
        "sys.stdin.readline()\n"
        "print(json.dumps({'type':'thread.started','thread_id':'app-prefix-thread'}),flush=True)\n"
        "print(json.dumps({'type':'item.completed','item':{'id':'body','type':'agent_message','text':'The complete original reply.'}}),flush=True)\n"
        "print(json.dumps({'type':'turn.completed','usage':{'input_tokens':1,'output_tokens':1}}),flush=True)\n"
        "sys.stdin.read()\n"
    )
    harness = get_harness("codex")
    monkeypatch.setattr(harness, "profile", process_profile(
        harness.profile, binary=sys.executable, activity_observer=None, prepare_resume=None,
        new_driver=ClaudeDriver, can_steer=True, reusable=True,
        build_turn_argv=lambda context: ([sys.executable, "-u", str(model)], {"cwd": context.working_dir}),
    ))
    entered, release = asyncio.Event(), asyncio.Event()
    question_entered, release_question = asyncio.Event(), asyncio.Event()
    received = []
    questioned_sessions = []

    async def hold_original_terminal(event):
        if event.get("type") == "assistant_text" and cancelled_terminal and not questioned_sessions:
            questioned_sessions.append(event["session_id"])
            assert await manager.create_pending_question(event["session_id"], [{"question": "Original App question"}])
        if event.get("type") == "question_request" and cancelled_terminal:
            question_entered.set()
            await release_question.wait()
        if event.get("type") == "result":
            received.append(event["request_id"])
            entered.set()
            await release.wait()
            if cancelled_terminal:
                raise asyncio.CancelledError("The original App terminal subscriber was cancelled.")

    # Register after App: its own result handler has run when this gate holds.
    manager.on_broadcast("hold-app-terminal-tail", hold_original_terminal)
    asking = asyncio.create_task(app_agent.ask(application, message="Return the complete reply."))
    try:
        if cancelled_terminal:
            await asyncio.wait_for(question_entered.wait(), 2)
            # Observe the real committed terminal while its original question
            # prefix still holds the deferred notification operation.
            async with asyncio.timeout(2):
                while True:
                    detail = await client.get(f"/api/sessions/{questioned_sessions[0]}")
                    if any(row["type"] == "result" for row in detail.json()["messages"]):
                        break
                    await asyncio.sleep(.01)
            release_question.set()
        await asyncio.wait_for(entered.wait(), 3)
        done, _ = await asyncio.wait({asking}, timeout=.15)
        assert not done, "App returned success before its original terminal prefix finished."
        release.set()
        if cancelled_terminal:
            with pytest.raises(AppAgentError, match="original request notification could not be delivered") as failed:
                await asyncio.wait_for(asking, 3)
            assert failed.value.status_code == 502
            detail = await client.get(f"/api/sessions/{questioned_sessions[0]}")
            terminal = [row for row in detail.json()["messages"] if row["type"] == "result"]
            assert len(terminal) == 1 and terminal[0]["content"]["status"] == "success"
            assert any(row.get("content") == "The complete original reply." for row in detail.json()["messages"])
            await asyncio.wait_for(manager.drain_turns(timeout=2), 3)
        else:
            response = await asyncio.wait_for(asking, 3)
            assert response["reply"] == "The complete original reply."
        assert len(received) == 1
    finally:
        release.set()
        release_question.set()
        await asyncio.gather(asking, return_exceptions=True)
        app_agent.shutdown()
        applications.shutdown()
