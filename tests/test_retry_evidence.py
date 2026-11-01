"""Recovery follows input/conversation evidence, including lost output."""

import asyncio
from dataclasses import replace

import pytest

from server.config import settings
from server.database import Database
from server.harness import HarnessEvent, get_harness
from server.harness.events import HarnessPreparationError
from server.sessions import SessionManager
from tests.accounts import create_account
from tests.fake_run import FakeRunBase
from tests.harness_profiles import process_profile


@pytest.fixture
async def host(monkeypatch):
    harness = get_harness("codex")
    monkeypatch.setattr(harness, "profile", process_profile(harness.profile))
    monkeypatch.setattr(settings, "turn_idle_timeout_seconds", .08)
    monkeypatch.setattr(settings, "turn_max_seconds", 0)
    monkeypatch.setattr(SessionManager, "_TRANSIENT_RETRY_BASE_DELAY", 0)
    db = Database(":memory:")
    await db.initialize()
    await create_account(db)
    manager = SessionManager()
    await manager.initialize(db)
    try:
        yield manager, await db.get_system_agent()
    finally:
        await manager.stop_all_held_processes()
        await db.close()


class LostOutput(FakeRunBase):
    def __init__(self, effect, failure, conversation=None):
        self.effect, self.failure, self.conversation = effect, failure, conversation
        self.prompts = []
        self.started = asyncio.Event()
        self.stopped = asyncio.Event()

    async def start(self, prompt, working_dir=None, resume_id=None, **kwargs):
        self.prompts.append((prompt, resume_id))
        # The external effect happened; its tool_use and text never arrived.
        self.effect.write_text(self.effect.read_text() + "effect\n" if self.effect.exists() else "effect\n")
        self.started.set()

    async def stream(self):
        await self.started.wait()
        if self.conversation:
            yield HarnessEvent(type="session_started", session_id=self.conversation)
        if self.failure == "idle":
            await self.stopped.wait()
        else:
            yield HarnessEvent(type="result", is_error=self.failure != "success", content={
                "transient": "API Error: 503 Service Unavailable",
                "stale": "No conversation found with session ID: old-thread",
                "success": "",
            }[self.failure])

    async def stop(self):
        self.stopped.set()

    async def interrupt(self):
        await self.stop()


@pytest.mark.parametrize("backend", ["claude-code", "codex"])
@pytest.mark.parametrize("failure", ["idle", "transient", "stale"])
async def test_missing_output_does_not_authorize_replay(host, monkeypatch, tmp_path, backend, failure):
    manager, agent = host
    session = await manager.create_session(agent["id"], "Lost output", backend=backend)
    # An old resume id alone says nothing about this input's effects.
    session.claude_session_id = "old-thread"
    first = LostOutput(tmp_path / "effects", failure)
    replacement = LostOutput(tmp_path / "effects", "success")
    runs = iter([first, replacement])
    monkeypatch.setattr(manager, "_make_run", lambda *args, **kwargs: next(runs))
    receipt = await manager.start_message(session.id, "perform the operation")
    completion = await asyncio.wait_for(receipt.wait(), 2)
    assert completion.outcome.status != "success"
    assert len(first.prompts) == 1 and replacement.prompts == []
    assert first.effect.read_text() == "effect\n"
    assert not any(completion.outcome.recovery.values())


@pytest.mark.parametrize("backend", ["claude-code", "codex"])
@pytest.mark.parametrize("failure", ["idle", "transient"])
async def test_captured_conversation_allows_continuation_without_output(host, monkeypatch, tmp_path, backend, failure):
    manager, agent = host
    session = await manager.create_session(agent["id"], "Continue", backend=backend)
    first = LostOutput(tmp_path / "original", failure, "current-thread")
    replacement = LostOutput(tmp_path / "continuation", "success")
    runs = iter([first, replacement])
    monkeypatch.setattr(manager, "_make_run", lambda *args, **kwargs: next(runs))
    receipt = await manager.start_message(session.id, "perform the operation")
    completion = await asyncio.wait_for(receipt.wait(), 2)
    assert completion.outcome.status == "success"
    prompt, resume = replacement.prompts[0]
    assert prompt.startswith(("Continue the user's", "The previous attempt"))
    assert "Check existing files and tool outcomes" in prompt
    assert resume == "current-thread"
    assert first.effect.read_text() == "effect\n"


async def test_preparation_failure_can_retry_original_input(host, monkeypatch, tmp_path):
    manager, agent = host
    session = await manager.create_session(agent["id"], "Prepare", backend="codex")

    class Preparation(LostOutput):
        input_may_have_run = False

        async def start(self, prompt, *args, **kwargs):
            self.prompts.append((prompt, None))
            raise HarnessPreparationError("API Error: 503 Service Unavailable")

    preparation = Preparation(tmp_path / "unused", "transient")
    replacement = LostOutput(tmp_path / "effects", "success")
    runs = iter([preparation, replacement])
    monkeypatch.setattr(manager, "_make_run", lambda *args, **kwargs: next(runs))
    receipt = await manager.start_message(session.id, "original input")
    completion = await asyncio.wait_for(receipt.wait(), 2)
    assert completion.outcome.status == "success"
    assert replacement.prompts == [("original input", None)]
    assert not preparation.effect.exists()


async def test_conversation_id_does_not_enable_unsupported_continuation(host, monkeypatch, tmp_path):
    manager, agent = host
    harness = get_harness("codex")
    monkeypatch.setattr(harness, "profile", replace(harness.profile, can_continue=False))
    session = await manager.create_session(agent["id"], "No continuation", backend="codex")
    first = LostOutput(tmp_path / "effects", "transient", "current-thread")
    replacement = LostOutput(tmp_path / "effects", "success")
    runs = iter([first, replacement])
    monkeypatch.setattr(manager, "_make_run", lambda *args, **kwargs: next(runs))
    receipt = await manager.start_message(session.id, "perform the operation")
    assert (await asyncio.wait_for(receipt.wait(), 2)).outcome.status == "failed"
    assert not replacement.prompts and first.effect.read_text() == "effect\n"
