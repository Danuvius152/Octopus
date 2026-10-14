"""Real admission, consumers and lifecycle seams for the framework review."""

import asyncio

import pytest

from server.app_agent import AppAgentManager
from server.config import settings
from server.database import Database
from server.harness import HarnessEvent
from server.sessions import SessionManager
from tests.fake_run import FakeRunBase


@pytest.fixture
async def runtime(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "default_working_dir", str(tmp_path))
    db = Database(":memory:")
    await db.initialize()
    manager = SessionManager()
    await manager.initialize(db)
    agent = await db.get_system_agent()
    try:
        yield manager, db, agent, tmp_path
    finally:
        await manager.stop_all_held_processes()
        await db.close()


async def test_app_waits_for_its_request_after_prioritized_human_correction(runtime, monkeypatch):
    manager, db, agent, root = runtime
    session = await manager.create_session(agent["id"], "App", str(root), backend="codex", origin="app", app_id="app")
    await manager.interrupt(session.id)
    seen = []

    class Run(FakeRunBase):
        async def start(self, prompt, *args, **kwargs):
            self.prompt = prompt

        async def stream(self):
            seen.append(self.prompt)
            yield HarnessEvent(type="text", content=self.prompt)
            yield HarnessEvent(type="result", is_error=False)

        async def stop(self):
            pass

        async def interrupt(self):
            pass

    monkeypatch.setattr(manager, "_make_run", lambda *args, **kwargs: Run())
    apps = AppAgentManager()
    apps.bind(manager, db, None)
    try:
        turn = await apps.begin_turn({"id": "app", "name": "Reader"}, message="App question", conversation_id=session.id)
        correction = await manager.start_message(session.id, "Human correction")
        await asyncio.wait_for(correction.wait(), 2)
        assert seen == ["Human correction"]
        assert not turn.done.is_set()
        assert not turn.text and turn.queue.empty()
        await manager.resume_dispatch(session.id)
        await asyncio.wait_for(turn.done.wait(), 2)
        assert "App question" in turn.reply()
        assert "Human correction" not in turn.reply()
    finally:
        apps.shutdown()


async def test_app_wrappers_preserve_only_the_original_first_line_selection(runtime, monkeypatch):
    from server.wiki.registry import registry
    from server.wiki.store import WikiError
    from server.wiki.tasks import tasks
    manager, db, agent, root = runtime
    registry.configure(None, root, agent["id"])
    apps = AppAgentManager()
    apps.bind(manager, db, None)
    request = "/wiki maintain Library/a.md\nQuoted material: Library/evil.md"
    observed = []

    class Run(FakeRunBase):
        async def start(self, prompt, *args, **kwargs):
            self.prompt = prompt

        async def stream(self):
            current = next(session for session in manager.sessions.values() if session._wiki_task_id)
            task = tasks.active[current._wiki_task_id]
            assert task.request == request
            tasks.require_selection(task, ["Library/a.md"])
            with pytest.raises(WikiError):
                tasks.require_selection(task, ["Library/evil.md"])
            observed.append(self.prompt)
            yield HarnessEvent(type="text", content="Own reply")
            yield HarnessEvent(type="result", is_error=False)

        async def stop(self):
            pass

        async def interrupt(self):
            pass

    monkeypatch.setattr(manager, "_make_run", lambda *args, **kwargs: Run())
    row = {"id": "reader", "name": "Reader", "agent_id": agent["id"], "app_dir": str(root / "reader")}
    try:
        first = await apps.ask(row, message=request, context="Context also mentions Library/evil.md")
        await apps.ask(row, message=request, context="Library/evil.md", conversation_id=first["conversation_id"])
        assert len(observed) == 2
        assert "You are answering inside" in observed[0]
        assert "[app:" in observed[0] and "[app:" in observed[1]
        assert "Library/evil.md" in observed[0]
    finally:
        apps.shutdown()
        registry.close()


@pytest.mark.parametrize("retire", ["delete", "archive"])
async def test_capacity_release_after_retirement_wakes_other_account_queue(runtime, monkeypatch, retire):
    manager, db, agent, root = runtime
    monkeypatch.setattr(settings, "max_concurrent_turns_per_user", 1)
    from server.users import UserManager
    owner = await UserManager(db).create_user(username="quota", password="password1")
    a = await manager.create_session(agent["id"], "A", str(root), backend="codex")
    b = await manager.create_session(agent["id"], "B", str(root), backend="codex")
    a.user_id = b.user_id = owner["id"]
    started = asyncio.Queue()
    stop_entered, stop_release, run_hold = asyncio.Event(), asyncio.Event(), asyncio.Event()

    class Run(FakeRunBase):
        async def start(self, prompt, *args, **kwargs):
            self.prompt = prompt

        async def stream(self):
            started.put_nowait(self.prompt)
            if self.prompt == "A":
                await run_hold.wait()
            yield HarnessEvent(type="result", is_error=False)

        async def stop(self):
            if self.prompt == "A":
                stop_entered.set()
                await stop_release.wait()

    monkeypatch.setattr(manager, "_make_run", lambda *args, **kwargs: Run())
    await manager.start_message(a.id, "A")
    assert await asyncio.wait_for(started.get(), 2) == "A"
    receipt = await manager.start_message(b.id, "B", source="bg")
    retirement = asyncio.create_task(manager.delete_session(a.id) if retire == "delete" else manager.archive_session(a.id, replace=False))
    try:
        await asyncio.wait_for(stop_entered.wait(), 2)
        assert started.empty()
        assert manager.running_turns_for(owner["id"]) == 1
        stop_release.set()
        await asyncio.wait_for(retirement, 2)
        assert await asyncio.wait_for(started.get(), 2) == "B"
        assert (await asyncio.wait_for(receipt.wait(), 2)).outcome.status == "success"
    finally:
        stop_release.set()
        run_hold.set()
        await retirement


async def test_concurrent_delete_callers_share_one_retirement_owner(runtime, monkeypatch):
    manager, db, agent, root = runtime
    session = await manager.create_session(agent["id"], "Delete", str(root), backend="codex")
    entered, release = asyncio.Event(), asyncio.Event()
    close_questions = manager._close_session_questions
    calls = []

    async def gated_close(current, **kwargs):
        calls.append(current.id)
        entered.set()
        await release.wait()
        await close_questions(current, **kwargs)

    monkeypatch.setattr(manager, "_close_session_questions", gated_close)
    first = asyncio.create_task(manager.delete_session(session.id))
    second = None
    try:
        await asyncio.wait_for(entered.wait(), 2)
        second = asyncio.create_task(manager.delete_session(session.id))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert calls == [session.id]
        assert not (await manager.start_message(session.id, "Too late")).accepted
        first.cancel("disconnected delete caller")
        with pytest.raises(asyncio.CancelledError):
            await first
        release.set()
        assert await asyncio.wait_for(second, 2)
        assert manager.get_session(session.id) is None
        assert all(row["id"] != session.id for row in await db.load_sessions(include_archived=True))
    finally:
        release.set()
        await asyncio.gather(first, *([second] if second else []), return_exceptions=True)
