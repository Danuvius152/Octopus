"""Real producer samples shared with the browser decoder, outside HTTP OpenAPI."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from server.applications import ApplicationManager
from server.database import Database
from server.harness.events import HarnessEvent, SubagentUpdate
from server.models import TurnOutcome
from server.research.manager import ResearchManager
from server.routers.agents import _publish
from server.sessions import SessionManager
from server.sessions.client_submissions import submit_client_message
from server.sessions.commands import process_command
from server.sessions.presentation import session_info
from tests.accounts import create_account
from tests.fake_run import FakeRunBase

SAMPLES = Path(__file__).parents[1] / "web/src/lib/wsProducerFixtures.json"


async def test_python_producers_match_the_browser_samples(tmp_path, request, monkeypatch):
    """Exercise original constructors, with only identities/time normalized.

    Harness stdout events, original input/control receipts, full management
    rows and Research delivery use their own production boundaries. The Owner
    structured outcome additionally has the independent recordingReplay check.
    """
    db = Database(":memory:")
    await db.initialize()
    user, _ = await create_account(db, extra_roots=[tmp_path])
    manager = SessionManager()
    await manager.initialize(db)
    agent = await db.get_system_agent(user["id"])
    session = await manager.create_session(agent["id"], "Contract session", str(tmp_path), backend="codex")
    samples = {}
    release = asyncio.Event()

    async def capture(event):
        samples[event["type"]] = event

    manager.on_broadcast("ws-contract", capture)
    try:
        for event in (
            HarnessEvent(type="text_delta", content="Streaming text"),
            HarnessEvent(type="text", content="Complete text"),
            HarnessEvent(type="tool_use", tool_name="read_file", tool_input={"path": "README.md"}, tool_use_id="tool"),
            HarnessEvent(type="tool_result", tool_use_id="tool", content="File text"),
            HarnessEvent(type="subagent", subagent=SubagentUpdate(task_id="child", name="Helper")),
            HarnessEvent(type="question_request", tool_use_id="question", tool_input={"questions": [{"question": "Directory?"}]}),
            HarnessEvent(type="result", session_id="native-thread", content="Legacy result"),
        ):
            frame = manager._event_to_ws_message(session.id, event)
            assert frame is not None
            samples[frame["type"]] = frame

        # Stop on the original public pre-execution notification, before any
        # provider preparation. The real ledger and Owner construct the reply.
        paused = asyncio.Event()

        async def stop_before_execution(event):
            if event.get("type") == "dequeued" and event.get("session_id") == session.id:
                await manager.interrupt(session.id)
                paused.set()

        manager.on_broadcast("pause-contract-input", stop_before_execution)
        response = await submit_client_message(manager, session.id, "Retained original input", [], "submission", wiki_mode=None)
        await asyncio.wait_for(paused.wait(), 3)
        await manager.await_session_execution(session.id, timeout=3)
        manager.remove_broadcast("pause-contract-input")
        samples["submission"] = {"type": "submission", **response}
        snapshot = manager.dispatch_snapshot(session.id)
        samples["command_receipt"] = await process_command(manager, {
            "type": "interrupt", "session_id": session.id, "client_command_id": "command",
            "request_id": None, "attempt_id": None, "generation": snapshot["generation"], "revision": snapshot["revision"],
        }, user["id"])
        assert samples["command_receipt"]["accepted"] is True

        await _publish(manager, "agent_updated", agent)
        apps = ApplicationManager()
        apps.session_mgr = manager
        await db.save_application(app_id="application", name="Contract application", app_dir=str(tmp_path),
                                  agent_id=agent["id"], user_id=user["id"], created_at="now", updated_at="now")
        await apps._broadcast_application("application_updated", await db.get_application("application"))
        research = ResearchManager()
        research.session_mgr, research.db = manager, db
        await research._publish_failed({"session_id": session.id, "id": "research", "status": "interrupted", "error": "Original worker retired"})
        await db.create_research_job(job_id="research", session_id=session.id, question="Question", created_at="now")
        await research._advance_delivery("research", session.id, "original-return", "queued")

        # This is the actual restore constructor, including its SessionInfo.
        await manager.archive_session(session.id, replace=False)
        await manager.unarchive_session(session.id, user["id"])
        session = manager.sessions[session.id]
        assert samples["session_unarchived"]["session"] == session_info(session).model_dump()
        samples["recovery"] = await manager._recovery_frame(
            session, code="transient_retry", message="Retrying the original request", phase="retrying",
        )

        # A real competing terminal row exercises the Owner's failure producer.
        # Only the model transport is simulated; admission, SQLite conflict,
        # notification and retirement remain the production path.
        entered = asyncio.Event()

        class ConflictRun(FakeRunBase):
            async def start(self, *args, **kwargs):
                self.alive = True
                entered.set()

            async def stream(self):
                await release.wait()
                yield HarnessEvent(type="result")

            def is_alive(self):
                return self.alive

            async def stop(self):
                self.alive = False
                release.set()

        monkeypatch.setattr(manager, "_make_run", lambda *args, **kwargs: ConflictRun())
        receipt = await manager.start_message(session.id, "Conflicting original outcome", request_id="conflict-request")
        await asyncio.wait_for(entered.wait(), 3)
        _, next_seq, _ = await db.message_sequence_state(session.id)
        await db.record_terminal(request_id=receipt.request_id, session_id=session.id, seq=next_seq,
                                 role="system", type="result", is_error=True, created_at="now",
                                 content=TurnOutcome(request_id=receipt.request_id, status="failed").model_dump())
        release.set()
        completion = await asyncio.wait_for(receipt.wait(), 3)
        assert completion.state == "recording_failed"
        await manager.await_session_execution(session.id, timeout=3)
        assert "dispatch_state" not in samples["recording_failed"]

        replacements = {session.id: "session", agent["id"]: "agent", user["id"]: "owner", str(tmp_path): "/work"}

        def stable(value, key=""):
            if isinstance(value, dict):
                return {name: stable(item, name) for name, item in value.items()}
            if isinstance(value, list):
                return [stable(item, key) for item in value]
            if isinstance(value, str):
                if value and key in {"created_at", "updated_at", "completed_at", "started_at", "injected_at"}:
                    return "2026-01-01T00:00:00+00:00"
                if value and key in {"generation", "request_id", "active_request_id", "execution_owner_request_id", "work_id", "attempt_id"}:
                    return {"generation": "service", "work_id": "work", "attempt_id": "attempt"}.get(key, "request")
                return replacements.get(value, value)
            return value

        actual = stable(samples)
        if request.config.getoption("--update-ws-fixtures"):
            SAMPLES.write_text(json.dumps(actual, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
        assert actual == json.loads(SAMPLES.read_text())
    finally:
        release.set()
        await db.close()
