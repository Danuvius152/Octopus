"""Service shutdown uses the real lifespan and captured subprocess owners."""
from __future__ import annotations

import asyncio
import sys
from contextlib import asynccontextmanager
from dataclasses import replace
from functools import partial

import pytest
from fastapi import FastAPI

from server import codex_login, main
from server import monitor as monitor_pkg
from server.agent_manager import AgentManager
from server.app_agent import AppAgentManager
from server.app_backends import BackendSupervisor
from server.applications import ApplicationManager
from server.bg_tasks import BgTaskManager
from server.config import settings
from server.delegations import DelegationManager
from server.harness import get_harness
from server.harness.profile import StdinMode
from server.mcp_http import build_servers, mount_all
from server.mcp_http import lifespan as mcp_lifespan
from server.research.manager import ResearchManager
from server.sessions import SessionManager
from server.wiki import jobs as wiki_jobs
from server.wiki.registry import registry
from server.wiki.store import WikiError


@pytest.fixture
async def service(tmp_path, monkeypatch):
    servers = build_servers(fresh=True)
    application = FastAPI(lifespan=main.lifespan)
    mount_all(application, servers)
    monkeypatch.setattr(main, "app", application)
    monkeypatch.setattr(main, "mcp_lifespan", partial(mcp_lifespan, servers=servers))
    for name in ("agents_dir", "applications_dir", "users_root", "wiki_dir",
                 "research_dir", "large_prompts_dir", "default_working_dir"):
        monkeypatch.setattr(settings, name, str(tmp_path / name))
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "service.db"))
    monkeypatch.setattr(settings, "metrics_db_path", str(tmp_path / "metrics.db"))
    monkeypatch.setattr(settings, "master_key_file", str(tmp_path / "master.key"))
    monkeypatch.setattr(settings, "enable_tunnel", False)
    stores = []
    store_factory = main.MetricsStore

    def metrics_store(*args):
        store = store_factory(*args)
        stores.append(store)
        return store

    monkeypatch.setattr(main, "MetricsStore", metrics_store)
    manager = SessionManager()
    monkeypatch.setattr(main, "session_manager", manager)
    for name, factory in (("bg_task_manager", BgTaskManager),
                          ("delegation_manager", DelegationManager),
                          ("application_manager", ApplicationManager),
                          ("app_agent_manager", AppAgentManager),
                          ("backend_supervisor", BackendSupervisor),
                          ("research_manager", ResearchManager)):
        monkeypatch.setattr(main, name, factory())
    monkeypatch.setattr(wiki_jobs, "worker", type(wiki_jobs.worker)())
    monkeypatch.setattr(codex_login, "codex_login_manager", codex_login.CodexLoginManager())
    yield manager
    # A deliberately unconfirmed lifespan retains these resources. Fault
    # gates are released by each test before the fixture confirms retirement.
    await manager.drain_turns()
    await main.bg_task_manager.shutdown()
    await wiki_jobs.worker.stop()
    await main.research_manager.shutdown()
    await main.backend_supervisor.shutdown()
    await codex_login.codex_login_manager.shutdown()
    for task in asyncio.all_tasks():
        if task.get_name() == "metrics-sampler":
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    for store in stores:
        await store.stop()
    monitor_pkg.set_store(None)
    await registry.stop()
    if manager.db is not None:
        await manager.db.close()


async def test_scheduler_startup_failure_cleans_all_acquired_resources(service, monkeypatch):
    initialize = main.ScheduleRunner.initialize
    captured = []

    async def fail_after_start(runner):
        captured.append(runner)
        await initialize(runner)
        raise RuntimeError("injected scheduler startup failure")

    monkeypatch.setattr(main.ScheduleRunner, "initialize", fail_after_start)
    with pytest.raises(RuntimeError, match="injected scheduler"):
        async with main.app.router.lifespan_context(main.app):
            pytest.fail("startup must fail")
    assert not captured[0]._scheduler.running
    assert service.db._conn is None
    assert service._admission_closed


@pytest.mark.parametrize("stage", ["background", "metrics", "mcp", "registry", "wiki_worker"])
@pytest.mark.parametrize("cancel", [False, True])
async def test_partial_startup_failure_or_cancel_releases_started_owners(service, monkeypatch, stage, cancel):
    from server.monitor.store import MetricsStore
    error = asyncio.CancelledError(f"injected {stage}") if cancel else RuntimeError(f"injected {stage}")
    worker_task = None
    if stage == "mcp":
        original = main.mcp_lifespan

        @asynccontextmanager
        async def broken(application):
            async with original(application):
                raise error
                yield  # Keep the injected context manager's generator shape.

        monkeypatch.setattr(main, "mcp_lifespan", broken)
    else:
        owner, method = {
            "background": (main.bg_task_manager, "start"),
            "metrics": (MetricsStore, "initialize"),
            "registry": (registry, "start"),
            "wiki_worker": (wiki_jobs.worker, "start"),
        }[stage]
        original = getattr(owner, method)

        async def broken(*args, **kwargs):
            nonlocal worker_task
            await original(*args, **kwargs)
            if stage == "wiki_worker":
                worker_task = asyncio.create_task(asyncio.Event().wait(), name="partial-account-worker")
                wiki_jobs.worker.tasks["partially-started-account"] = worker_task
            raise error

        monkeypatch.setattr(owner, method, broken)
    with pytest.raises(BaseException) as failed:
        async with main.app.router.lifespan_context(main.app):
            pytest.fail("injected startup must not serve")
    assert f"injected {stage}" in repr(failed.value)
    assert service.db._conn is None
    assert service._admission_closed
    assert registry._task is None
    if worker_task is not None:
        assert worker_task.done()
    assert not [task for task in asyncio.all_tasks() if not task.done() and task.get_name() in {
        "wiki-index-sync", "metrics-sampler", "app-backend-reaper", "mcp-lifespan", "partial-account-worker",
    }]


async def test_cancel_while_mcp_is_entering_retires_its_owner(service, monkeypatch):
    original = main.mcp_lifespan
    entered, released, allow_ready = asyncio.Event(), asyncio.Event(), asyncio.Event()

    @asynccontextmanager
    async def delayed(application):
        async with original(application):
            entered.set()
            try:
                await allow_ready.wait()
                yield
            finally:
                released.set()

    monkeypatch.setattr(main, "mcp_lifespan", delayed)

    async def startup():
        async with main.app.router.lifespan_context(main.app):
            pytest.fail("cancelled startup must not serve")

    task = asyncio.create_task(startup())
    try:
        await asyncio.wait_for(entered.wait(), 3)
        task.cancel("cancel-before-mcp-ready")
        done, _ = await asyncio.wait({task}, timeout=1)
        assert task in done, "startup cancellation did not join the entering MCP owner"
        with pytest.raises(asyncio.CancelledError):
            await task
        assert released.is_set()
        assert service.db._conn is None
        assert not [owner for owner in asyncio.all_tasks()
                    if not owner.done() and owner.get_name() == "mcp-lifespan"]
    finally:
        allow_ready.set()
        await asyncio.gather(task, return_exceptions=True)


def synthetic_cli(monkeypatch, starts, code=None):
    if code is None:
        code = (
            "import sys,json\n"
            "print(json.dumps({'type':'thread.started','thread_id':'test-thread'}),flush=True)\n"
            "print(json.dumps({'type':'item.completed','item':{'id':'item_0',"
            "'type':'agent_message','text':'ready'}}),flush=True)\n"
            "for line in sys.stdin: pass\n"
        )

    def argv(context):
        starts.append(context.prompt)
        return [sys.executable, "-u", "-c", code], {"cwd": context.working_dir}

    harness = get_harness("codex")
    monkeypatch.setattr(harness, "profile", replace(
        harness.profile, build_turn_argv=argv, stdin_mode=StdinMode.STREAM_JSON,
        activity_observer=None,
    ))


@pytest.mark.asyncio
async def test_lifespan_shutdown_rejects_terminal_followup_and_records_service_interruption(
    service, tmp_path, monkeypatch,
):
    starts = []
    synthetic_cli(monkeypatch, starts)
    ready = asyncio.Event()
    deliveries = []

    async with main.app.router.lifespan_context(main.app):
        agent = await service.db.get_system_agent()
        parent = await service.create_session(agent["id"], "parent", str(tmp_path), backend="codex")
        child = await service.create_session(agent["id"], "child", str(tmp_path), backend="codex")

        async def observe(event):
            if event.get("session_id") != child.id:
                return
            if event.get("type") == "assistant_text":
                ready.set()
            if event.get("type") == "result":
                deliveries.append(await service.start_message(
                    parent.id, "terminal followup", source="delegation", delivery_id="terminal",
                ))

        service.on_broadcast("terminal-followup", observe)
        receipt = await service.start_message(child.id, "keep running")
        await asyncio.wait_for(ready.wait(), 5)

    completion = await receipt.wait()
    assert completion.outcome.status == "interrupted"
    assert completion.outcome.code == "service_shutdown"
    assert completion.recording_state == "committed"
    assert len(deliveries) == 1 and deliveries[0].disposition == "closed"
    assert (await deliveries[0].wait()).reason == "shutdown"
    assert starts == ["keep running"]


@pytest.mark.asyncio
async def test_lifespan_shutdown_preserves_wiki_job_as_interrupted(service, tmp_path, monkeypatch):
    starts = []
    synthetic_cli(monkeypatch, starts)
    ready = asyncio.Event()

    async with main.app.router.lifespan_context(main.app):
        curator = await AgentManager(service.db).create_agent(name="Curator", backend="codex")
        vault = tmp_path / "vault"
        (vault / "Library").mkdir(parents=True)
        registry.configure(None, vault, curator["id"])
        jobs = wiki_jobs.WikiJobs(None)

        async def observe(event):
            if event.get("type") == "assistant_text":
                ready.set()

        service.on_broadcast("curator-ready", observe)
        job = jobs.maintain(
            "source", "# Material\nOriginal words.\n", "Preserve the original words.",
            [], "Library/note.md", [], "human",
        )
        await asyncio.wait_for(ready.wait(), 5)

    with pytest.raises(WikiError) as stopped:
        wiki_jobs.WikiJobs(None)
    assert stopped.value.status == 503
    registry.resume_access()
    saved = wiki_jobs.WikiJobs(None).get(job["id"])
    assert saved["status"] == "interrupted"
    assert saved["items"][0]["status"] == "pending"
    assert len(starts) == 1


@pytest.mark.asyncio
async def test_user_stop_keeps_its_reason_when_service_shutdown_follows(service, tmp_path, monkeypatch):
    starts = []
    synthetic_cli(monkeypatch, starts)
    ready, paused, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    stopping = None
    try:
        async with main.app.router.lifespan_context(main.app):
            agent = await service.db.get_system_agent()
            session = await service.create_session(agent["id"], "Stop race", str(tmp_path), backend="codex")

            async def observe(event):
                if event.get("type") == "assistant_text":
                    ready.set()
                if (event.get("type") == "dispatch_state"
                        and event.get("dispatch_state", {}).get("paused") and not paused.is_set()):
                    paused.set()
                    await release.wait()

            service.on_broadcast("pause-gate", observe)
            receipt = await service.start_message(session.id, "running")
            await asyncio.wait_for(ready.wait(), 5)
            stopping = asyncio.create_task(service.interrupt(session.id))
            await asyncio.wait_for(paused.wait(), 5)
        completion = await receipt.wait()
        assert completion.outcome.status == "interrupted"
        assert completion.outcome.code == "interrupted"
        assert completion.outcome.message == "(interrupted by user)"
    finally:
        release.set()
        if stopping is not None:
            await stopping


@pytest.mark.asyncio
async def test_unconfirmed_lifespan_keeps_selected_success_writer_and_database(service, tmp_path, monkeypatch):
    starts = []
    synthetic_cli(monkeypatch, starts, (
        "import json\nprint(json.dumps({'type':'thread.started','thread_id':'test-thread'}),flush=True)\n"
        "print(json.dumps({'type':'turn.completed','usage':{}}),flush=True)\n"
    ))
    entered, release = asyncio.Event(), asyncio.Event()
    receipt = None
    try:
        with pytest.raises(Exception) as failure:
            async with main.app.router.lifespan_context(main.app):
                agent = await service.db.get_system_agent()
                session = await service.create_session(agent["id"], "Selected result", str(tmp_path), backend="codex")
                record = service.db.record_terminal

                async def blocked_record(**kwargs):
                    entered.set()
                    await release.wait()
                    return await record(**kwargs)

                monkeypatch.setattr(service.db, "record_terminal", blocked_record)
                monkeypatch.setattr(service, "drain_turns", partial(service.drain_turns, timeout=.03))
                receipt = await service.start_message(session.id, "complete once")
                await asyncio.wait_for(entered.wait(), 5)
        assert "unconfirmed" in repr(failure.value)
        assert await service.db.count_messages(session.id) >= 1
        late = await service.start_message(session.id, "late return", source="bg")
        assert late.disposition == "closed"
        release.set()
        completion = await asyncio.wait_for(receipt.wait(), 5)
        assert completion.outcome.status == "success"
        assert completion.recording_state == "committed"
        assert starts == ["complete once"]
    finally:
        release.set()
        if receipt is not None:
            await asyncio.wait_for(receipt.wait(), 5)


@pytest.mark.asyncio
async def test_lifespan_reports_finished_but_unconfirmed_result_and_keeps_database(
    service, tmp_path, monkeypatch,
):
    starts = []
    synthetic_cli(monkeypatch, starts, (
        "import json\n"
        "print(json.dumps({'type':'thread.started','thread_id':'completed-thread'}),flush=True)\n"
        "print(json.dumps({'type':'turn.completed','usage':{}}),flush=True)\n"
    ))
    fail_storage = True
    receipt = None
    session = None
    try:
        with pytest.raises(Exception) as failure:
            async with main.app.router.lifespan_context(main.app):
                original = service.db.record_terminal

                async def record_terminal(**kwargs):
                    if fail_storage:
                        raise OSError("synthetic storage failure")
                    return await original(**kwargs)

                monkeypatch.setattr(service.db, "record_terminal", record_terminal)
                agent = await service.db.get_system_agent()
                session = await service.create_session(agent["id"], "completed", str(tmp_path), backend="codex")
                receipt = await service.start_message(session.id, "finish once")
                completion = await asyncio.wait_for(receipt.wait(), 5)
                assert completion.outcome.status == "success"
                assert completion.recording_state == "unconfirmed"
        assert "unconfirmed" in repr(failure.value)
        assert await service.db.count_messages(session.id) > 0
        assert starts == ["finish once"]
    finally:
        fail_storage = False
        if session is not None:
            from anyio import CancelScope

            with CancelScope(shield=True):
                await service.resume_dispatch(session.id)
    await service.drain_turns()
    assert starts == ["finish once"]


@pytest.mark.asyncio
async def test_lifespan_joins_retirement_already_started_by_held_reaper(
    service, tmp_path, monkeypatch,
):
    from server.sessions import processes

    entered, release, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()

    class CapturedObserver:
        def observe_stdout(self, obj):
            pass

        def consume_stderr(self, line):
            return False

        async def start(self):
            pass

        def invalidate(self):
            pass

        async def close(self):
            entered.set()
            await release.wait()
            finished.set()

    starts = []
    synthetic_cli(monkeypatch, starts, (
        "import json,sys\n"
        "print(json.dumps({'type':'thread.started','thread_id':'held-thread'}),flush=True)\n"
        "print(json.dumps({'type':'turn.completed','usage':{}}),flush=True)\n"
        "for line in sys.stdin: pass\n"
    ))
    harness = get_harness("codex")
    monkeypatch.setattr(harness, "profile", replace(
        harness.profile, activity_observer=lambda *args, **kwargs: CapturedObserver(),
    ))
    monkeypatch.setattr(processes, "_REAPER_INTERVAL_SECONDS", .01)
    monkeypatch.setattr(processes, "_HELD_PROCESS_IDLE_SECONDS", 0)
    monkeypatch.setattr(service, "drain_turns", partial(service.drain_turns, timeout=.03))
    try:
        with pytest.raises(Exception) as failure:
            async with main.app.router.lifespan_context(main.app):
                agent = await service.db.get_system_agent()
                session = await service.create_session(agent["id"], "held", str(tmp_path), backend="codex")
                receipt = await service.start_message(session.id, "finish and hold")
                completion = await asyncio.wait_for(receipt.wait(), 5)
                assert completion.outcome.status == "success"
                await asyncio.wait_for(entered.wait(), 5)
        assert "unconfirmed" in repr(failure.value)
        assert not finished.is_set()
        assert await service.db.count_messages(session.id) > 0
    finally:
        release.set()
        await asyncio.wait_for(finished.wait(), 5)
    await service.drain_turns()
    assert starts == ["finish and hold"]
