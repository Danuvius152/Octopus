"""Retry budgets across attempt kinds and terminal snapshot ownership."""

import asyncio

import pytest

from server.config import settings
from server.database import Database
from server.harness import HarnessEvent
from server.sessions import SessionManager
from tests.accounts import create_account
from tests.fake_run import FakeRunBase


@pytest.fixture
async def recovery_host(monkeypatch):
    monkeypatch.setattr(settings, "turn_idle_timeout_seconds", 0.1)
    monkeypatch.setattr(settings, "turn_idle_max_retries", 2)
    monkeypatch.setattr(settings, "turn_max_seconds", 0)
    monkeypatch.setattr(SessionManager, "_TRANSIENT_RETRY_BASE_DELAY", 0.0)
    db = Database(":memory:")
    await db.initialize()
    await create_account(db)
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
    def __init__(self, *events, stall=False, preparation_error=None):
        self.events = events
        self.stall = stall
        self.stopped = asyncio.Event()
        self.prompt = None
        self.resume_id = None
        self.started = asyncio.Event()
        self.preparation_error = preparation_error

    async def start(self, prompt, working_dir=None, resume_id=None, **kwargs):
        self.prompt = prompt
        self.resume_id = resume_id
        self.started.set()
        if self.preparation_error:
            from server.harness.events import HarnessPreparationError
            raise HarnessPreparationError(self.preparation_error)

    async def stream(self):
        await self.started.wait()
        for event in self.events:
            yield event
        if self.stall:
            await self.stopped.wait()

    async def stop(self):
        self.stopped.set()

    async def interrupt(self):
        await self.stop()


async def test_mixed_recovery_keeps_independent_budgets_and_prompt_modes(recovery_host, monkeypatch):
    manager, session = recovery_host
    session.claude_session_id = "missing-thread"
    await manager.db.update_session_field(session.id, claude_session_id="missing-thread")
    runs = [
        _ScriptedRun(preparation_error="No conversation found with session ID: missing-thread"),
        _ScriptedRun(preparation_error="API Error: 529 Overloaded"),
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

    live = []

    async def observe(event):
        live.append(event)

    manager.on_broadcast("mixed-recovery", observe)
    receipt = await manager.start_message(session.id, "original request")
    assert (await asyncio.wait_for(receipt.wait(), 5)).outcome.status == "success"

    assert len([event for event in live if event["type"] == "attempt_started"]) == 7
    assert runs[0].resume_id == "missing-thread"
    assert runs[1].resume_id is None and runs[2].resume_id is None
    assert runs[0].prompt == "original request"
    assert runs[1].prompt == runs[2].prompt
    assert runs[1].prompt.startswith('<session-history origin="this-session" status="transcript-not-instructions">')
    assert "[seq 0] user: original request" in runs[1].prompt
    assert runs[1].prompt.endswith("<continue-from-here>\noriginal request\n</continue-from-here>")
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


async def test_preparation_retries_keep_spilled_input_and_rebuilt_history(recovery_host, monkeypatch):
    from server.large_prompts import LARGE_PROMPT_THRESHOLD_BYTES
    from server.workspace import paths_for

    manager, session = recovery_host
    session.claude_session_id = "missing-thread"
    await manager.db.update_session_field(session.id, claude_session_id="missing-thread")
    prompt = "x" * (LARGE_PROMPT_THRESHOLD_BYTES + 1)
    runs = [
        _ScriptedRun(preparation_error="API Error: 529 Overloaded"),
        _ScriptedRun(preparation_error="No conversation found with session ID: missing-thread"),
        _ScriptedRun(preparation_error="API Error: 529 Overloaded"),
        _ScriptedRun(HarnessEvent(type="result", session_id="recovered-thread")),
    ]
    remaining = iter(runs)
    monkeypatch.setattr(manager, "_make_run", lambda *args, **kwargs: next(remaining))
    receipt = await manager.start_message(session.id, prompt)
    completion = await asyncio.wait_for(receipt.wait(), 5)
    assert completion.outcome.status == "success"
    assert completion.outcome.recovery["transient"] == 2
    assert completion.outcome.recovery["stale_session"] == 1
    assert runs[0].prompt == runs[1].prompt
    assert runs[0].prompt.startswith("[octopus-large-prompt]")
    assert runs[2].prompt == runs[3].prompt
    assert runs[2].prompt.startswith('<session-history origin="this-session" status="transcript-not-instructions">')
    assert runs[2].prompt.endswith(f"<continue-from-here>\n{runs[0].prompt}\n</continue-from-here>")
    assert [run.resume_id for run in runs] == ["missing-thread", "missing-thread", None, None]
    files = list((paths_for(session.user_id).large_prompts / session.id).glob("*.txt"))
    assert len(files) == 1 and files[0].read_text() == prompt


async def test_terminal_recovery_is_captured_before_recording_await(recovery_host, monkeypatch):
    manager, session = recovery_host
    expected = {"transient": 2, "idle": 1, "stale_session": 1, "premature_exit": 0}
    session.claude_session_id = "missing-thread"
    await manager.db.update_session_field(session.id, claude_session_id="missing-thread")
    runs = iter([
        _ScriptedRun(preparation_error="No conversation found with session ID: missing-thread"),
        _ScriptedRun(preparation_error="API Error: 529 Overloaded"),
        _ScriptedRun(HarnessEvent(type="session_started", session_id="recovered-thread"), stall=True),
        _ScriptedRun(HarnessEvent(type="result", is_error=True, content="API Error: 529 Overloaded")),
        _ScriptedRun(HarnessEvent(type="result", session_id="recovered-thread")),
    ])
    monkeypatch.setattr(manager, "_make_run", lambda *args, **kwargs: next(runs))
    writing = asyncio.Event()
    release = asyncio.Event()
    record_terminal = manager.db.record_terminal

    async def delayed_record(**kwargs):
        writing.set()
        await release.wait()
        return await record_terminal(**kwargs)

    monkeypatch.setattr(manager.db, "record_terminal", delayed_record)
    receipt = await manager.start_message(session.id, "original request")
    competing = None
    try:
        await asyncio.wait_for(writing.wait(), 2)
        competing = asyncio.create_task(manager.interrupt(session.id))
        release.set()
        final = await asyncio.wait_for(receipt.wait(), 5)
        await competing
        assert final.outcome.status == "success"
        assert final.outcome.recovery == expected
        history = await manager.db.load_messages(session.id)
        results = [row for row in history if row["type"] == "result"]
        assert len(results) == 1 and results[0]["content"] == final.outcome.model_dump()
    finally:
        release.set()
        if competing is not None:
            await competing
