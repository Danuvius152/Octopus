"""Real admission, consumers and lifecycle seams for the framework review."""

import asyncio
import json
import sys
from pathlib import Path

import pytest

from server.app_agent import AppAgentManager
from server.config import settings
from server.database import Database
from server.harness import HarnessEvent, get_harness
from server.harness.claude_code import ClaudeDriver
from server.sessions import SessionManager
from tests.accounts import create_account
from tests.fake_run import FakeRunBase
from tests.harness_profiles import process_profile


@pytest.fixture
async def runtime(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "default_working_dir", str(tmp_path))
    db = Database(":memory:")
    await db.initialize()
    await create_account(db, extra_roots=[tmp_path])
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
    await db.save_application(app_id="app", name="Reader", app_dir=str(root),
                              created_at="time", updated_at="time", user_id=agent["user_id"], status="ready")
    session = await manager.create_session(agent["id"], "App", str(root), backend="codex", origin="app", app_id="app")
    await manager.interrupt(session.id)
    seen = []

    class Run(FakeRunBase):
        def __init__(self):
            self.started = asyncio.Event()

        async def start(self, prompt, *args, **kwargs):
            self.prompt = prompt
            self.started.set()

        async def stream(self):
            await self.started.wait()
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
        turn = await apps.begin_turn({"id": "app", "name": "Reader", "user_id": agent["user_id"]}, message="App question", conversation_id=session.id)
        correction = await manager.start_message(session.id, "Human correction")
        corrected = await asyncio.wait_for(correction.wait(), 2)
        assert corrected.outcome.status == "success"
        assert seen == ["Human correction"]
        assert not turn.done.is_set()
        assert turn.reply() == "" and turn.queue.empty()
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
    vault = root / "vault"
    vault.mkdir()
    registry.configure(agent["user_id"], vault, agent["id"])
    apps = AppAgentManager()
    apps.bind(manager, db, None)
    request = "/wiki maintain Library/a.md\nQuoted material: Library/evil.md"
    observed = []

    class Run(FakeRunBase):
        def __init__(self):
            self.started = asyncio.Event()
            self.wiki_task_id = None

        def bind_wiki_task(self, task_id):
            self.wiki_task_id = task_id

        async def start(self, prompt, *args, **kwargs):
            self.prompt = prompt
            self.started.set()

        async def stream(self):
            await self.started.wait()
            task = tasks.active[self.wiki_task_id]
            assert task.request == request
            assert task.origin == "app" and task.app_id == "reader"
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
    row = {"id": "reader", "name": "Reader", "agent_id": agent["id"], "user_id": agent["user_id"], "app_dir": str(root / "reader")}
    await db.save_application(app_id=row["id"], name=row["name"], app_dir=row["app_dir"],
                              agent_id=agent["id"], user_id=agent["user_id"],
                              created_at="time", updated_at="time", status="ready")
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
    from server.agent_manager import AgentManager
    from server.users import UserManager
    owner = await UserManager(db).create_user(username="quota", password="password1")
    agent = await AgentManager(db).create_agent(name="Quota owner", user_id=owner["id"], backend="codex")
    a = await manager.create_session(agent["id"], "A", None, backend="codex")
    b = await manager.create_session(agent["id"], "B", None, backend="codex")
    started = asyncio.Queue()
    stop_entered, stop_release, run_hold = asyncio.Event(), asyncio.Event(), asyncio.Event()
    runs = []

    class Run(FakeRunBase):
        def __init__(self):
            self.ready = asyncio.Event()
            self.retired = asyncio.Event()
            self.prompt = None
            self.alive = False
            self.stops = 0

        async def start(self, prompt, *args, **kwargs):
            self.prompt = prompt
            self.alive = True
            self.ready.set()

        async def stream(self):
            await self.ready.wait()
            started.put_nowait(self.prompt)
            if self.prompt == "A":
                await run_hold.wait()
            yield HarnessEvent(type="result", is_error=False)

        async def stop(self):
            self.stops += 1
            if self.prompt == "A":
                stop_entered.set()
                await stop_release.wait()
            self.alive = False
            self.retired.set()

        async def interrupt(self):
            pass

        def is_alive(self):
            return self.alive

    def make_run(*args, **kwargs):
        run = Run()
        runs.append(run)
        return run

    monkeypatch.setattr(manager, "_make_run", make_run)
    retirement = None
    try:
        original = await manager.start_message(a.id, "A")
        assert await asyncio.wait_for(started.get(), 2) == "A"
        receipt = await manager.start_message(b.id, "B", source="bg")
        retirement = asyncio.create_task(manager.delete_session(a.id) if retire == "delete" else manager.archive_session(a.id, replace=False))
        await asyncio.wait_for(stop_entered.wait(), 2)
        assert started.empty()
        assert len(runs) == 1 and runs[0].is_alive() and not runs[0].retired.is_set()
        assert manager.running_turns_for(owner["id"]) == 1
        stop_release.set()
        await asyncio.wait_for(retirement, 2)
        assert runs[0].retired.is_set() and not runs[0].is_alive() and runs[0].stops == 1
        assert await asyncio.wait_for(started.get(), 2) == "B"
        assert (await asyncio.wait_for(receipt.wait(), 2)).outcome.status == "success"
        assert (await asyncio.wait_for(original.wait(), 2)).outcome.status == "interrupted"
        assert manager.running_turns_for(owner["id"]) == 0
    finally:
        stop_release.set()
        run_hold.set()
        if retirement is not None:
            await retirement


async def test_concurrent_delete_callers_share_one_retirement_owner(runtime, monkeypatch):
    manager, db, agent, root = runtime
    session = await manager.create_session(agent["id"], "Delete", str(root), backend="codex")
    model, identity = root / "delete_model.py", root / "delete_model.pid"
    entered, release = root / "delete_entered", root / "delete_release"
    original_input = root / "delete_input.json"
    model.write_text(
        "import json,os,sys,time\nfrom pathlib import Path\n"
        "Path(sys.argv[1]).write_text(str(os.getpid()))\n"
        "Path(sys.argv[4]).write_text(sys.stdin.readline())\n"
        "print(json.dumps({'type':'thread.started','thread_id':'owned-delete-thread'}),flush=True)\n"
        "print(json.dumps({'type':'item.completed','item':{'id':'original','type':'agent_message','text':'Original body before deletion'}}),flush=True)\n"
        "sys.stdin.read()\n"
        "Path(sys.argv[2]).write_text('Original stdin reached EOF')\n"
        "while not Path(sys.argv[3]).exists(): time.sleep(.005)\n"
    )
    harness = get_harness("codex")
    monkeypatch.setattr(harness, "profile", process_profile(
        harness.profile, binary=sys.executable, activity_observer=None, prepare_resume=None,
        new_driver=ClaudeDriver, can_steer=True, reusable=True,
        build_turn_argv=lambda context: (
            [sys.executable, "-u", str(model), str(identity), str(entered), str(release), str(original_input)],
            {"cwd": context.working_dir},
        ),
    ))
    spawn = asyncio.create_subprocess_exec
    delete_row = db.delete_session
    processes, calls, deletion_tasks = [], [], []
    body_seen, second_requested = asyncio.Event(), asyncio.Event()

    async def observe_spawn(*args, **kwargs):
        process = await spawn(*args, **kwargs)
        if str(model) in args:
            processes.append(process)
        return process

    async def observe_real_delete(session_id):
        calls.append(session_id)
        deletion_tasks.append(asyncio.current_task())
        return await delete_row(session_id)

    async def observe_body(event):
        if event.get("session_id") == session.id and event.get("content") == "Original body before deletion":
            body_seen.set()

    async def second_waiter():
        second_requested.set()
        return await manager.delete_session(session.id)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", observe_spawn)
    monkeypatch.setattr(db, "delete_session", observe_real_delete)
    manager.on_broadcast("original-owned-delete-body", observe_body)
    first, second = None, None
    readers = set()
    try:
        original = await manager.start_message(session.id, "Original input before deletion")
        await asyncio.wait_for(body_seen.wait(), 2)
        assert len(processes) == 1 and processes[0].pid == int(identity.read_text())
        readers = {task for task in asyncio.all_tasks() if task.get_name() == "codex-stdout"}
        assert len(readers) == 1
        first = asyncio.create_task(manager.delete_session(session.id))
        async with asyncio.timeout(2):
            while not entered.exists():
                await asyncio.sleep(.01)
        second = asyncio.create_task(second_waiter())
        await asyncio.wait_for(second_requested.wait(), 2)
        assert calls == [] and processes[0].returncode is None
        late = await manager.start_message(session.id, "Too late")
        assert not late.accepted and (await late.wait()).outcome is None
        first.cancel("disconnected delete caller")
        with pytest.raises(asyncio.CancelledError):
            await first
        assert not second.done() and processes[0].returncode is None
        rows = await db.load_messages(session.id)
        assert [row["content"] for row in rows if row["role"] == "user"] == ["Original input before deletion"]
        assert [row["content"] for row in rows if row["role"] == "assistant" and row["type"] == "text"] == ["Original body before deletion"]
        assert json.loads(original_input.read_text())["message"]["content"] == "Original input before deletion"
        release.touch()
        assert await asyncio.wait_for(second, 2)
        assert calls == [session.id] and len(deletion_tasks) == 1
        assert deletion_tasks[0] not in {first, second} and deletion_tasks[0].done() and not deletion_tasks[0].cancelled()
        assert manager.get_session(session.id) is None
        assert all(row["id"] != session.id for row in await db.load_sessions(include_archived=True))
        assert processes[0].returncode == 0 and not Path(f"/proc/{processes[0].pid}").exists()
        assert all(task.done() for task in readers)
        assert (await asyncio.wait_for(original.wait(), 2)).outcome.status == "interrupted"
    finally:
        release.touch()
        try:
            await asyncio.wait_for(manager.delete_session(session.id), 8)
        finally:
            for process in processes:
                if process.returncode is None:
                    process.kill()
                await asyncio.wait_for(process.wait(), 5)
            await asyncio.gather(*(task for task in (first, second) if task is not None), return_exceptions=True)
            await asyncio.gather(*readers, return_exceptions=True)
