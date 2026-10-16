"""Runtime regressions: exercise owners and durable outcomes, without models."""
from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from mcp.server.fastmcp import FastMCP

from server import main, mcp_http, mcp_identity
from server.agent_manager import AgentManager
from server.config import settings
from server.database import Database
from server.harness.codex import CODEX, CodexEventParser
from server.harness.run import HarnessRun
from server.monitor import MetricsStore, query, set_store
from server.sessions import outcomes
from server.users import UserManager
from server.wiki import jobs as jobs_module
from server.wiki.history import WikiHistory
from server.wiki.jobs import WikiJobs, WikiWorker
from server.wiki.registry import registry
from server.wiki.store import WikiError
from tests import test_service_shutdown, test_session_manager, test_wiki_jobs
from tests.fake_run import FakeRunBase

service = test_service_shutdown.service
manager = test_session_manager.manager
client = test_wiki_jobs.client
jobs = test_wiki_jobs.jobs


async def eventually(check, timeout=3.0):
    async with asyncio.timeout(timeout):
        while not check():
            await asyncio.sleep(0.01)


async def test_startup_isolates_unavailable_vault_and_rejoins_without_restart(service, tmp_path, monkeypatch, caplog):
    db = Database(settings.db_path)
    await db.initialize()
    users = UserManager(db)
    prepared = []
    raw = "# Source\nPreserved original.\n"
    for name in ("broken", "healthy"):
        owner = (await users.create_user(username=name, password="password1"))["id"]
        agent = await AgentManager(db).create_agent(name=name, user_id=owner, backend="codex", mcp_servers=[])
        root = tmp_path / name
        root.mkdir()
        registry.configure(owner, root, agent["id"])
        account_jobs = WikiJobs(owner)
        WikiHistory(account_jobs.store).write("Library/source.md", raw.encode(), None, "human", "source")
        task = account_jobs.enqueue(name, {"kind": "submission", "material": raw, "scope": "reuse original", "target": None}, [])
        assert task["status"] == "queued"
        prepared.append((owner, root, task["id"]))
    await db.close()
    broken_owner, broken_root, broken_job = prepared[0]
    broken_root.rename(tmp_path / "disconnected")
    monkeypatch.setattr(WikiWorker, "RETRY_INITIAL_SECONDS", 0.1, raising=False)
    monkeypatch.setattr(WikiWorker, "RETRY_MAX_SECONDS", 0.2, raising=False)
    async with main.app.router.lifespan_context(main.app):
        healthy_owner, _, healthy_job = prepared[1]
        await eventually(lambda: WikiJobs(healthy_owner).get(healthy_job)["status"] == "completed")
        # No tight retry loop while one mounted root is absent.
        await asyncio.sleep(0.25)
        failures = [r for r in caplog.records if "Wiki worker unavailable" in r.message]
        assert 1 <= len(failures) <= 3
        assert "vault root is unavailable" in caplog.text
        (tmp_path / "disconnected").rename(broken_root)
        await eventually(lambda: WikiJobs(broken_owner).get(broken_job)["status"] == "completed")
        assert not service._admission_closed
    assert not jobs_module.worker.tasks


async def test_unavailable_worker_cancellation_stops_retry(jobs, monkeypatch):
    root = jobs.store.root
    moved = root.with_name("offline")
    root.rename(moved)
    worker = WikiWorker()
    monkeypatch.setattr(worker, "RETRY_INITIAL_SECONDS", 60.0, raising=False)
    try:
        await worker.start(test_wiki_jobs.wiki_api.session_manager)
        await asyncio.sleep(0.05)
        task = worker.tasks[None]
        await asyncio.wait_for(worker.stop(), 1)
        assert task.cancelled()
    finally:
        moved.rename(root)


async def test_shutdown_during_job_claim_records_interruption(jobs, monkeypatch):
    job = jobs.maintain("claim-cancellation", "# Claim\nKeep original.\n", "organize", [], None, [], "human")
    claimed, release = threading.Event(), threading.Event()
    original = WikiJobs.next_queued

    def held_claim(self):
        job_id = original(self)
        if job_id:
            claimed.set()
            assert release.wait(2)
        return job_id

    monkeypatch.setattr(WikiJobs, "next_queued", held_claim)
    worker = WikiWorker()
    try:
        await worker.start(test_wiki_jobs.wiki_api.session_manager)
        assert await asyncio.to_thread(claimed.wait, 2)
        worker.begin_shutdown()
        release.set()
        await worker.stop()
        assert jobs.get(job["id"])["status"] == "interrupted"
    finally:
        release.set()
        await worker.stop()


@pytest.mark.parametrize("binding", ["curator_agent_id", "app_id"])
@pytest.mark.parametrize("ending", ["complete", "cancel", "shutdown"])
async def test_live_registration_change_settles_old_worker_and_allows_retry(jobs, client, monkeypatch, binding, ending):
    from server.wiki.tasks import tasks as grants

    manager = test_wiki_jobs.wiki_api.session_manager
    entered, release = asyncio.Event(), asyncio.Event()
    tokens = []

    class HeldRun(FakeRunBase):
        async def start(self, *args, **kwargs):
            pass

        async def stream(self):
            session = next(s for s in manager.sessions.values() if s.origin == "wiki")
            tokens.append(grants.open(session, "maintain"))
            entered.set()
            await release.wait()
            from server.harness import HarnessEvent
            yield HarnessEvent(type="result")

        async def stop(self):
            release.set()

    monkeypatch.setattr(manager, "_make_run", lambda *a, **k: HeldRun())
    job = jobs.maintain("registration-change", "# Original\nRetain source.\n", "organize", [], "Library/source.md", [], "human")
    worker = WikiWorker()
    await worker.start(manager)
    try:
        await asyncio.wait_for(entered.wait(), 2)
        old_config = registry.config(None)
        config = dict(old_config)
        if binding == "curator_agent_id":
            config[binding] = (await AgentManager(manager.db).create_agent(name="new curator", backend="codex"))["id"]
        else:
            config[binding] = "new-application-binding"
        registry.configure(None, Path(config["root"]), config["curator_agent_id"], config["app_id"])
        current = WikiJobs(None)
        with pytest.raises(WikiError, match="retired"):
            jobs.get(job["id"])
        # The old process cannot inherit publication authority even when only
        # the optional application binding (not the agent id) changed.
        scope = SimpleNamespace(wiki_task_id=tokens[0], session_id=grants.active[tokens[0]].session_id, user_id=None)
        with pytest.raises(WikiError) as denied:
            grants.require(scope, write=True, publish=True)
        assert denied.value.status == 403
        original = current.store.path("Library/source.md").read_bytes()
        response = await client.post("/api/wiki/publish", headers={"Authorization": "Bearer " + mcp_identity.mint(
            scope.session_id, wiki_task_id=tokens[0])}, json={
                "path": "Library/source.md", "content": "forbidden overwrite", "kind": "original",
                "expected_revision": current.store.read("Library/source.md")["revision"], "reason": "stale worker",
            })
        assert response.status_code == 403
        assert current.store.path("Library/source.md").read_bytes() == original
        if ending == "cancel":
            await current.cancel(job["id"], manager)
        if ending == "shutdown":
            worker.begin_shutdown()
        release.set()
        await asyncio.wait_for(asyncio.shield(worker.tasks[None]), 3) if ending != "shutdown" else await worker.stop()
        result = current.get(job["id"])
        assert result["status"] == {"complete": "failed", "cancel": "cancelled", "shutdown": "interrupted"}[ending]
        assert not grants.assignments
        # Same idempotency key still identifies this durable job, no duplicate.
        assert current.maintain("registration-change", "# Original\nRetain source.\n", "organize", [], "Library/source.md", [], "human")["id"] == job["id"]
        assert current.retry(job["id"])["status"] == "queued"
        resumed = WikiWorker()
        resumed.manager = test_wiki_jobs.Curator(current)
        await resumed.run(None)
        assert current.get(job["id"])["status"] == "completed"
        assert current.store.path("Library/source.md").read_text().count("Retain source.") == 1
    finally:
        release.set()
        await worker.stop()
        for token in tokens:
            grants.close(token)


@pytest.mark.parametrize("binding", ["curator_agent_id", "app_id"])
async def test_publication_survives_registration_change_without_another_model_run(jobs, binding):
    class PublishedThenRebound(test_wiki_jobs.Curator):
        def get_session(self, session_id):
            return None

        async def _events(self, *args, **kwargs):
            async for event in super()._events(*args, **kwargs):
                if event["type"] == "result":
                    config = registry.config(None)
                    config[binding] = "replacement-binding"
                    registry.configure(None, Path(config["root"]), config["curator_agent_id"], config["app_id"])
                yield event

    job = jobs.maintain("published-rebind", "# Published\nPreserved original.\n", "organize", [], "Library/published.md", [], "human")
    manager = PublishedThenRebound(jobs)
    worker = WikiWorker()
    await worker.start(manager)
    try:
        await asyncio.wait_for(worker.tasks[None], 3)
        current = WikiJobs(None)
        assert current.get(job["id"])["status"] == "failed"
        published = current.store.path("Library/published.md").read_bytes()
        current.retry(job["id"])
        resumed = WikiWorker()
        resumed.manager = test_wiki_jobs.Curator(current)
        await resumed.run(None)
        assert current.get(job["id"])["status"] == "completed"
        assert current.store.path("Library/published.md").read_bytes() == published
        assert resumed.manager.turns == 0
    finally:
        await worker.stop()


async def test_captured_web_lifecycle_prevents_idle_but_replays_and_silence_do_not(monkeypatch):
    rows = [json.loads(line) for line in (Path(__file__).parent / "fixtures/codex-web-search-0.162.1.jsonl").read_text().splitlines()]
    clock = SimpleNamespace(now=299.0)
    state = {"last": 0.0, "tripped": None}
    run = HarnessRun(CODEX)
    ticks = []

    def touch(_):
        state["last"] = clock.now
        ticks.append(clock.now)

    run.bind_activity(touch)
    for row in rows:
        await run._handle_line(json.dumps(row))
    assert ticks == [299.0, 299.0]
    clock.now = 301.0
    for row in [rows[0], rows[1], rows[1]]:
        await run._handle_line(json.dumps(row))
    assert ticks == [299.0, 299.0]

    class Backend:
        stopped = False

        async def stop(self):
            self.stopped = True

    backend = Backend()
    checked_301 = asyncio.Event()
    continue_clock = asyncio.Event()

    async def advance(_):
        if not checked_301.is_set():
            checked_301.set()
        else:
            await continue_clock.wait()
            clock.now = 600.0

    monkeypatch.setattr(settings, "turn_idle_timeout_seconds", 300)
    monkeypatch.setattr(settings, "turn_max_seconds", 0)
    monkeypatch.setattr(outcomes, "time", SimpleNamespace(monotonic=lambda: clock.now))
    monkeypatch.setattr(outcomes, "asyncio", SimpleNamespace(sleep=advance, create_task=asyncio.create_task))
    watcher = test_session_manager.SessionManager()._start_turn_watchdog(backend, state, started_at=0.0)
    await checked_301.wait()
    await asyncio.sleep(0)
    assert state["tripped"] is None and not backend.stopped
    run._begin_retirement()
    await run.stop()
    await run._handle_line(json.dumps({"type": "item.completed", "item": {"type": "web_search", "id": "exec-11111111-1111-4111-8111-111111111111"}}))
    assert ticks == [299.0, 299.0]
    continue_clock.set()
    await watcher
    assert backend.stopped and state["tripped"] == ("idle", 300)


@pytest.mark.parametrize("failed", [False, True])
def test_codex_results_measure_monotonic_attempt_duration(monkeypatch, failed):
    import server.harness.codex as codex

    clock = SimpleNamespace(now=100.0)
    monkeypatch.setattr(codex, "time", SimpleNamespace(monotonic=lambda: clock.now))
    parser = CodexEventParser()
    parser.parse({"type": "turn.started"})
    clock.now = 100.125
    result = parser.parse({"type": "turn.failed" if failed else "turn.completed", "error": {"message": "failure"}}).events[0]
    assert result.duration_ms == 125
    assert result.is_error is failed


async def test_codex_attempt_durations_reach_persisted_metrics_across_retry(manager, monkeypatch, tmp_path):
    import server.harness.codex as codex
    import server.sessions.turns as turns

    store = MetricsStore(str(tmp_path / "metrics.db"))
    await store.initialize()
    set_store(store)
    clock = SimpleNamespace(now=10.0)
    monkeypatch.setattr(codex, "time", SimpleNamespace(monotonic=lambda: clock.now))
    monkeypatch.setattr(turns, "time", SimpleNamespace(monotonic=lambda: clock.now))
    monkeypatch.setattr(type(manager), "_TRANSIENT_RETRY_BASE_DELAY", 0)
    agent = await manager.db.get_system_agent()
    session = await manager.create_session(agent["id"], "duration", str(tmp_path), backend="codex")
    starts = []

    class ParsedRun(FakeRunBase):
        async def start(self, *a, **k):
            self.parser = CodexEventParser()
            self.parser.parse({"type": "turn.started"})
            self.ordinal = len(starts)
            starts.append(clock.now)

        async def stream(self):
            clock.now += (self.ordinal + 1) / 4
            raw = {"type": "turn.failed", "error": {"message": "API Error: 529 Overloaded"}} if self.ordinal == 0 else {"type": "turn.completed"}
            for event in self.parser.parse(raw).events:
                yield event

        async def stop(self):
            pass

        async def interrupt(self):
            await self.stop()

    monkeypatch.setattr(manager, "_make_run", lambda *a, **k: ParsedRun())
    try:
        events = [event async for event in manager._run_backend(session, "request")]
        assert events[-1]["type"] == "result" and not events[-1]["is_error"]
        assert events[-1]["duration_ms"] == 750
        await store.drain()
        rows = await store.conn.execute_fetchall("SELECT duration_ms,ok,detail FROM events WHERE kind='turn' ORDER BY rowid")
        assert [(row[0], row[1]) for row in rows] == [(250, 0), (500, 1)]
        details = [json.loads(row[2]) for row in rows]
        assert all(row["duration_scope"] == "attempt" for row in details)
        assert len({row["attempt_id"] for row in details}) == 2
        assert len({row["request_id"] for row in details}) == 1
        report = await query.turns(store.conn, "24h")
        assert report[0]["avg_ms"] == 375
    finally:
        set_store(None)
        await store.stop()


@pytest.mark.parametrize("retirement", ["revoke", "close-work", "cancel-delete"])
async def test_retired_mcp_cleanup_is_owned_and_does_not_restore_tools(retirement):
    server = FastMCP("cleanup-test", json_response=True)
    app = FastAPI()
    transport_app = server.streamable_http_app()
    middleware = mcp_http._ScopeMiddleware(transport_app, "test")
    middleware.manager = server.session_manager
    response_ready = asyncio.Event()
    pause_response = False

    async def serving(scope, receive, send):
        async def observed_send(message):
            if pause_response and scope.get("method") == "DELETE" and message.get("status") == 200:
                response_ready.set()
                await asyncio.Event().wait()
            await send(message)
        await middleware(scope, receive, observed_send)

    app.mount("/mcp/test", serving)
    lease, work = f"cleanup-{retirement}", f"work-{retirement}"
    mcp_identity.register_run(lease, "owner-session", "owner", work)
    owner = mcp_identity.mint("owner-session", user_id="owner", run_lease_id=lease, work_id=work)
    headers = {"Authorization": f"Bearer {owner}", "Accept": "application/json, text/event-stream"}
    try:
        async with server.session_manager.run():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://127.0.0.1:65530") as client:
                response = await client.post("/mcp/test/mcp", headers=headers, json={
                    "jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                        "protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"},
                    },
                })
                assert response.status_code == 200
                sid = response.headers["mcp-session-id"]
                assert len(server.session_manager._server_instances) == 1
                identities = [("other-session", "owner", "other-run"), ("owner-session", "other", "other-tenant"), ("owner-session", "owner", "replacement-run")] if retirement == "cancel-delete" else []
                for session, user, other_lease in identities:
                    mcp_identity.register_run(other_lease, session, user, None)
                    try:
                        token = mcp_identity.mint(session, user_id=user, run_lease_id=other_lease)
                        denied = await client.delete("/mcp/test/mcp", headers={**headers, "Authorization": f"Bearer {token}", "Mcp-Session-Id": sid})
                        assert denied.status_code == 404
                        assert len(server.session_manager._server_instances) == 1
                    finally:
                        mcp_identity.revoke_run(other_lease)
                if retirement == "close-work":
                    mcp_identity.close_work(work)
                else:
                    mcp_identity.revoke_run(lease)
                h = {**headers, "Mcp-Session-Id": sid}
                refused = await client.post("/mcp/test/mcp", headers=h, json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
                assert refused.status_code == 401
                assert len(server.session_manager._server_instances) == 1
                if retirement == "cancel-delete":
                    # Cancel an in-flight response after termination, before
                    # its 200 can reach the client.
                    pause_response = True
                    deleted = asyncio.create_task(client.delete("/mcp/test/mcp", headers=h))
                    await asyncio.wait_for(response_ready.wait(), 2)
                    deleted.cancel()
                    result = (await asyncio.gather(deleted, return_exceptions=True))[0]
                    assert isinstance(result, asyncio.CancelledError)
                    await eventually(lambda: not server.session_manager._server_instances)
                else:
                    deleted = await client.delete("/mcp/test/mcp", headers=h)
                    assert deleted.status_code == 200
                assert not server.session_manager._server_instances
                repeated = await client.delete("/mcp/test/mcp", headers=h)
                assert repeated.status_code == 401
    finally:
        mcp_identity.revoke_run(lease)


async def test_memory_fork_bootstrap_cannot_emit_production_mcp_entries(tmp_path, monkeypatch):
    from tests.test_session_fork_real import _bootstrap

    db, mgr, agents = await _bootstrap(tmp_path, monkeypatch)
    try:
        agent = await agents.create_agent(name="memory-only", backend="codex")
        session = await mgr.create_session(agent["id"], "memory", str(tmp_path), backend="codex")
        config = mgr._run_config(session, agent)
        run = HarnessRun(CODEX, config)
        argv, _ = run.build_argv("remember", str(tmp_path))
        assert config.mcp_servers == []
        assert not any("mcp_servers." in arg for arg in argv)
    finally:
        await mgr.stop_all_held_processes()
        await db.close()
