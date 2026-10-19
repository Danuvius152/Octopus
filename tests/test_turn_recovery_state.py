"""Retry budgets across attempt kinds and terminal snapshot ownership."""

import asyncio

import pytest

from server.config import settings
from server.database import Database
from server.harness import HarnessEvent
from server.sessions import SessionManager
from server.sessions.turns import _Settlement
from tests.fake_run import FakeRunBase


@pytest.fixture
async def recovery_host(monkeypatch):
    monkeypatch.setattr(settings, "turn_idle_timeout_seconds", 0.1)
    monkeypatch.setattr(settings, "turn_idle_max_retries", 2)
    monkeypatch.setattr(settings, "turn_max_seconds", 0)
    monkeypatch.setattr(SessionManager, "_TRANSIENT_RETRY_BASE_DELAY", 0.0)
    db = Database(":memory:")
    await db.initialize()
    manager = SessionManager()
    await manager.initialize(db)
    agent = await db.get_system_agent()
    session = await manager.create_session(agent["id"], "Recovery", backend="claude-code")
    try:
        yield manager, session
    finally:
        await manager.stop_all_held_processes()
        await db.close()


class _ScriptedRun(FakeRunBase):
    def __init__(self, *events, stall=False):
        self.events = events
        self.stall = stall
        self.stopped = asyncio.Event()
        self.prompt = None
        self.resume_id = None

    async def start(self, prompt, working_dir=None, resume_id=None, **kwargs):
        self.prompt = prompt
        self.resume_id = resume_id

    async def stream(self):
        for event in self.events:
            yield event
        if self.stall:
            await self.stopped.wait()

    async def stop(self):
        self.stopped.set()


async def test_mixed_recovery_keeps_independent_budgets_and_prompt_modes(recovery_host, monkeypatch):
    manager, session = recovery_host
    session.claude_session_id = "missing-thread"
    runs = [
        _ScriptedRun(HarnessEvent(
            type="result", is_error=True,
            content="No conversation found with session ID: missing-thread",
        )),
        _ScriptedRun(
            HarnessEvent(type="session_started", session_id="failed-no-output-thread"),
            HarnessEvent(type="result", is_error=True, content="API Error: 529 Overloaded"),
        ),
        _ScriptedRun(
            HarnessEvent(type="session_started", session_id="recovered-thread"),
            HarnessEvent(type="tool_use", tool_name="Read", tool_input={"file_path": "/file"}, tool_use_id="tool"),
            HarnessEvent(type="tool_result", tool_use_id="tool", content="existing output"),
        ),
        _ScriptedRun(stall=True),
        _ScriptedRun(HarnessEvent(type="result", is_error=True, content="API Error: 529 Overloaded")),
        _ScriptedRun(stall=True),
        _ScriptedRun(
            HarnessEvent(type="text", content="finished"),
            HarnessEvent(type="result", session_id="recovered-thread"),
        ),
    ]
    remaining = iter(runs)
    monkeypatch.setattr(manager, "_make_run", lambda *args, **kwargs: next(remaining))

    live = [event async for event in manager._run_backend(session, "original request")]

    assert len([event for event in live if event["type"] == "attempt_started"]) == 7
    assert runs[0].resume_id == "missing-thread"
    assert runs[1].resume_id is None and runs[2].resume_id is None
    assert runs[1].prompt == runs[2].prompt == "original request"
    assert runs[3].prompt == "continue"
    assert "without backend activity" in runs[4].prompt
    assert "Continue the user's unfinished request" in runs[5].prompt
    assert runs[6].prompt == runs[4].prompt
    assert all(run.resume_id == "recovered-thread" for run in runs[3:])
    for code in ("transient_retry", "turn_timeout_retry"):
        markers = [event for event in live if event.get("code") == code]
        assert [event["content"]["attempt"] for event in markers] == [1, 2]
        assert all(event["content"]["maximum"] == 2 for event in markers)
    assert sum(event.get("code") == "stale_session" for event in live) == 1
    assert sum(event.get("code") == "premature_exit" for event in live) == 1
    finals = [event for event in live if event["type"] == "result"]
    assert len(finals) == 1 and finals[0]["content"]["status"] == "success"
    assert finals[0]["content"]["recovery"] == {
        "transient": 2, "idle": 2, "stale_session": 1, "premature_exit": 1,
    }
    history = await manager.db.load_messages(session.id)
    assert history[-1]["content"] == finals[0]["content"]


async def test_terminal_recovery_is_captured_before_recording_await(recovery_host, monkeypatch):
    manager, session = recovery_host
    expected = {"transient": 2, "idle": 1, "stale_session": 1, "premature_exit": 0}
    settlement = _Settlement(recovery=dict(expected))
    writing = asyncio.Event()
    release = asyncio.Event()
    record_terminal = manager.db.record_terminal

    async def delayed_record(**kwargs):
        writing.set()
        await release.wait()
        return await record_terminal(**kwargs)

    monkeypatch.setattr(manager.db, "record_terminal", delayed_record)
    selected = asyncio.create_task(manager._settle_turn(session, settlement, status="success"))
    try:
        await asyncio.wait_for(writing.wait(), 2)
        settlement.recovery.update({kind: count + 1 for kind, count in expected.items()})
        competing = asyncio.create_task(manager._settle_turn(session, settlement, status="interrupted"))
        release.set()
        final, same_final = await asyncio.gather(selected, competing)
        assert same_final == final
        assert final["content"]["status"] == "success"
        assert final["content"]["recovery"] == expected
        assert settlement.outcome.recovery == expected
        history = await manager.db.load_messages(session.id)
        assert len(history) == 1 and history[0]["content"] == final["content"]
    finally:
        release.set()
        await selected
