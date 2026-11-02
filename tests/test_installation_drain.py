"""Natural handoff exercises real owners behind the public control endpoint."""

from __future__ import annotations

import asyncio
import importlib
import json
import sqlite3
import sys
import threading
import time
from contextlib import asynccontextmanager, closing
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from server import codex_login, deps, main, oauth_login
from server.app_agent import AppAgentManager
from server.app_backends import BackendSupervisor
from server.bg_tasks import BgTaskManager
from server.config import settings
from server.database import Database
from server.delegations import DelegationManager
from server.harness import Harness, OneShotContext, get_harness
from server.harness.registry import _REGISTRY
from server.installation_admission import InstallationAdmission
from server.research.manager import ResearchManager
from server.service_health import ServiceHealth
from server.sessions import SessionManager
from server.wiki import app_bridge, job_events, job_notifications
from server.wiki import jobs as wiki_jobs
from server.wiki.registry import WikiRegistry
from server.wiki.store import WikiError
from tests.accounts import create_account


@pytest.fixture
async def installation(tmp_path, monkeypatch):
    db = Database(str(tmp_path / "installation.sqlite3"))
    await db.initialize()
    await create_account(db, extra_roots=[tmp_path])
    manager = SessionManager()
    await manager.initialize(db)
    monkeypatch.setattr(main, "session_manager", manager)
    for name, factory in (("bg_task_manager", BgTaskManager), ("research_manager", ResearchManager),
                          ("delegation_manager", DelegationManager), ("app_agent_manager", AppAgentManager),
                          ("backend_supervisor", BackendSupervisor)):
        monkeypatch.setattr(main, name, factory())
    monkeypatch.setattr(codex_login, "codex_login_manager", codex_login.CodexLoginManager())
    monkeypatch.setattr(oauth_login, "oauth_login_manager", oauth_login.OAuthLoginManager())

    class InstallationWorker(wiki_jobs.WikiWorker):
        # A separate installation owns separate cancellation records. Retain
        # the original class's failed owners for their original test/fixture.
        executing = {}
        cancellations = {}

    worker = InstallationWorker()
    monkeypatch.setattr(wiki_jobs, "worker", worker)
    registry = WikiRegistry()
    registry_module = importlib.import_module("server.wiki.registry")
    monkeypatch.setattr(registry_module, "registry", registry)
    monkeypatch.setattr(wiki_jobs, "registry", registry)
    monkeypatch.setattr(job_notifications, "registry", registry)
    monkeypatch.setattr(app_bridge, "registry", registry)
    application = FastAPI()
    application.add_middleware(InstallationAdmission)
    application.add_api_route("/api/installation/quiesce", main.quiesce_installation, methods=["POST"])
    application.add_api_route("/ready", main.readiness)
    application.dependency_overrides[deps.current_user] = lambda: {"is_admin": True}
    health = ServiceHealth()
    health.open_admission()
    application.state.service_health = health
    application.state.installation_owner = SimpleNamespace(profile=object(), offline_request=None)
    agent = await db.get_system_agent()
    session = await manager.create_session(agent["id"], "Original installation")
    try:
        async with AsyncClient(transport=ASGITransport(app=application), base_url="http://test") as client:
            yield SimpleNamespace(client=client, app=application, manager=manager, session=session,
                                  registry=registry, worker=worker, db=db)
    finally:
        try:
            await registry.stop()
        finally:
            await db.close()


async def test_handoff_waits_for_an_admitted_lifecycle_operation_outside_its_short_lock(installation):
    running = installation
    request_id = str(uuid4())
    async with running.manager.agent_operation(running.session.agent_id):
        response = await running.client.post("/api/installation/quiesce", json={"request_id": request_id})
        assert response.status_code == 409
        assert (await running.client.get("/ready")).json() == {"status": "ready"}
        assert not running.manager._admission_closed
    response = await running.client.post("/api/installation/quiesce", json={"request_id": request_id})
    assert response.status_code == 200


async def test_handoff_observes_original_oauth_task_then_seals_login_admission(installation, monkeypatch):
    from server.harness.login import OAuthCompletion
    from server.oauth_credentials import OAuthCredentials
    from tests.oauth_provider import patch_provider

    running = installation
    manager = oauth_login.oauth_login_manager
    provider = patch_provider(monkeypatch)
    entered, release = asyncio.Event(), asyncio.Event()
    exchange = provider.exchange_code

    async def paused(**kwargs):
        entered.set()
        await release.wait()
        return await exchange(**kwargs)

    monkeypatch.setattr(provider, "exchange_code", paused)
    login = await manager.start(user_id=running.session.user_id)
    request = OAuthCompletion(running.session.user_id, login.id, "code", "Personal")
    waiter = asyncio.create_task(manager.complete(request, OAuthCredentials(running.db)))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        response = await running.client.post("/api/installation/quiesce", json={"request_id": str(uuid4())})
        assert response.status_code == 409
        assert any("oauth login" in blocker for blocker in response.json()["blockers"])
        release.set()
        result = await manager.complete(request, OAuthCredentials(running.db))
        assert await running.db.get_credential(result.id, running.session.user_id)
        response = await running.client.post("/api/installation/quiesce", json={"request_id": str(uuid4())})
        assert response.status_code == 200
        with pytest.raises(RuntimeError, match="admission is closed"):
            await manager.start(user_id=running.session.user_id)
    finally:
        release.set()
        await asyncio.gather(waiter, return_exceptions=True)
        await manager.shutdown()


@pytest.mark.parametrize("interruption", ["cancelled", "timeout"])
async def test_oneshot_unknown_keeps_original_process_in_handoff_drain(
    installation, tmp_path, monkeypatch, interruption, short_process_shutdown,
):
    from dataclasses import replace

    running = installation
    spawn = asyncio.create_subprocess_exec
    created = asyncio.get_running_loop().create_future()
    release = asyncio.Event()

    async def late_handle(*argv, **kwargs):
        process = await spawn(*argv, **kwargs)
        created.set_result(process)
        await release.wait()
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", late_handle)
    harness = Harness(replace(get_harness("codex").profile, binary=sys.executable,
        build_oneshot_argv=lambda ctx: ([sys.executable, "-c", "import time; time.sleep(60)"], {"cwd": str(tmp_path)})))
    monkeypatch.setitem(_REGISTRY, "codex", harness)
    calling = asyncio.create_task(harness.run_oneshot(OneShotContext(prompt="original")))
    process = await asyncio.wait_for(created, 2)
    try:
        calling.cancel("first cancellation")
        await asyncio.sleep(0)
        if interruption == "cancelled":
            calling.cancel("second cancellation")
        with pytest.raises(RuntimeError, match="retirement.*unconfirmed"):
            await asyncio.wait_for(asyncio.shield(calling), 3)
        assert process.returncode is None
        refused = await running.client.post("/api/installation/quiesce", json={"request_id": str(uuid4())})
        assert refused.status_code == 409
        assert any("harness" in reason for reason in refused.json()["blockers"])
        release.set()
        await asyncio.wait_for(process.wait(), 2)
        async with asyncio.timeout(2):
            while harness.drain_blockers():
                await asyncio.sleep(0)
        accepted = await running.client.post("/api/installation/quiesce", json={"request_id": str(uuid4())})
        assert accepted.status_code == 200
        with pytest.raises(Exception, match="closing|shutting down"):
            await harness.run_oneshot(OneShotContext(prompt="too late"))
    finally:
        release.set()
        if process.returncode is None:
            process.kill()
        await process.wait()
        await asyncio.gather(calling, return_exceptions=True)


@pytest.mark.parametrize("failure", ["exit_proof", "private_configuration"])
async def test_failed_oneshot_cleanup_remains_a_handoff_blocker(installation, tmp_path, monkeypatch, failure):
    from dataclasses import replace

    from server import process_groups
    from server.harness.private_settings import PrivateSettings

    private = PrivateSettings(3, {"env": {"SYNTHETIC_KEY": "test-only"}})
    close = private.close
    if failure == "exit_proof":
        async def reject_exit(*args, **kwargs):
            raise RuntimeError("exit proof unavailable")
        monkeypatch.setattr(process_groups, "wait_group_exit", reject_exit)
    else:
        def reject_close():
            raise RuntimeError("private configuration cleanup failed")
        monkeypatch.setattr(private, "close", reject_close)

    harness = Harness(replace(get_harness("codex").profile, binary=sys.executable,
        build_oneshot_argv=lambda ctx: (
            [sys.executable, "-c", "print('finished')", "settings"],
            {"cwd": str(tmp_path), "private_settings": private},
        ), parse_oneshot_stdout=lambda output: output))
    monkeypatch.setitem(_REGISTRY, "codex", harness)
    try:
        with pytest.raises(RuntimeError, match="retirement.*unconfirmed"):
            await harness.run_oneshot(OneShotContext(prompt="original"))
        refused = await installation.client.post("/api/installation/quiesce", json={"request_id": str(uuid4())})
        assert refused.status_code == 409
        assert any("harness" in reason for reason in refused.json()["blockers"])
        with pytest.raises(RuntimeError, match="exit proof|private configuration"):
            await harness.shutdown()
    finally:
        close()


@pytest.mark.parametrize("resource", ["configuration", "database", "question"])
async def test_original_owner_exposes_configuration_writer_and_question_obligations(installation, resource):
    running = installation

    @asynccontextmanager
    async def hold():
        if resource == "configuration":
            async with running.db.configuration_lock:
                yield
        elif resource == "database":
            async with running.db.transaction():
                yield
        else:
            question = await running.manager.create_pending_question(running.session.id, [{"question": "Original pending question"}])
            try:
                yield
            finally:
                await running.manager.close_pending_question(running.session.id, question)

    request_id = str(uuid4())
    async with hold():
        response = await running.client.post("/api/installation/quiesce", json={"request_id": request_id})
        assert response.status_code == 409
        assert (await running.client.get("/ready")).json() == {"status": "ready"}
    async with asyncio.timeout(2):
        while True:
            response = await running.client.post("/api/installation/quiesce", json={"request_id": request_id})
            if response.status_code == 200:
                break
            await asyncio.sleep(0)


async def test_handoff_waits_for_original_notice_publication_after_its_commit(installation):
    running = installation
    entered, release = asyncio.Event(), asyncio.Event()

    async def publish(event):
        if event.get("type") == "delivery_notice":
            entered.set()
            await release.wait()

    running.manager.on_broadcast("held-notice", publish)
    notice = asyncio.create_task(running.manager.retain_result_notice(running.session.id, "notice", "Original result"))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        response = await running.client.post("/api/installation/quiesce", json={"request_id": str(uuid4())})
        assert response.status_code == 409
    finally:
        release.set()
        await notice


async def test_handoff_waits_for_an_already_scheduled_sync_before_it_borrows_a_store(installation, tmp_path, monkeypatch):
    running = installation
    root = tmp_path / "vault"
    root.mkdir()
    running.registry.configure(None, root, "curator")
    store = running.registry.store(None)
    synchronize = store.synchronize
    entered, release = threading.Event(), threading.Event()

    def held_sync():
        entered.set()
        assert release.wait(5)
        return synchronize()

    monkeypatch.setattr(store, "synchronize", held_sync)
    running.registry.start_reconciler()
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        assert store._users == 0
        response = await running.client.post("/api/installation/quiesce", json={"request_id": str(uuid4())})
        assert response.status_code == 409
        assert (await running.client.get("/ready")).json()["status"] == "ready"
    finally:
        release.set()


async def test_terminal_wiki_job_keeps_unacked_delivery_owned_between_receiver_attempts(installation, tmp_path, monkeypatch):
    from tests.test_wiki_job_events import enqueue

    running = installation
    root = tmp_path / "vault"
    root.mkdir()
    running.registry.configure(running.session.user_id, root, "curator")
    jobs = wiki_jobs.WikiJobs(running.session.user_id)
    job = enqueue(jobs)
    with jobs.store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        job_events.subscribe(db, job["id"], job_events.Requester(jobs.user_id, "human"))
    jobs.update(job["id"], status="completed")
    request_id = str(uuid4())
    response = await running.client.post("/api/installation/quiesce", json={"request_id": request_id})
    assert response.status_code == 409

    receive = running.db.receive_wiki_job_notice

    async def failed_receive(*args, **kwargs):
        raise RuntimeError("Temporary receive failure")

    monkeypatch.setattr(running.db, "receive_wiki_job_notice", failed_receive)
    with pytest.raises(RuntimeError, match="Temporary receive"):
        await job_notifications.dispatch(jobs, running.manager)
    # No receiver task is currently running; the original durable ACK is missing.
    response = await running.client.post("/api/installation/quiesce", json={"request_id": request_id})
    assert response.status_code == 409
    monkeypatch.setattr(running.db, "receive_wiki_job_notice", receive)
    await job_notifications.dispatch(jobs, running.manager)
    response = await running.client.post("/api/installation/quiesce", json={"request_id": request_id})
    assert response.status_code == 200


async def test_a_partial_seal_never_publishes_a_success_receipt(installation, monkeypatch):
    running = installation
    request_id = str(uuid4())

    def failed_gate():
        raise RuntimeError("Second producer gate failed")

    monkeypatch.setattr(running.worker, "begin_shutdown", failed_gate)
    # Transport turns the real endpoint failure into its HTTP response.
    async with AsyncClient(transport=ASGITransport(app=running.app, raise_app_exceptions=False), base_url="http://test") as client:
        response = await client.post("/api/installation/quiesce", json={"request_id": request_id})
        assert response.status_code == 500
        ready = await client.get("/ready")
    assert ready.status_code == 503
    assert ready.json() == {"status": "stopping"}
    assert running.app.state.installation_owner.offline_request is None
    assert running.manager._admission_closed


async def test_success_receipt_can_be_retrieved_only_by_its_original_request(installation):
    running = installation
    request_id = str(uuid4())
    first = await running.client.post("/api/installation/quiesce", json={"request_id": request_id})
    assert first.status_code == 200
    second = await running.client.post("/api/installation/quiesce", json={"request_id": request_id})
    assert second.status_code == 200
    assert first.json() == second.json() == {"status": "stopping", "request_id": request_id}
    ready = await running.client.get("/ready")
    assert ready.json() == {"status": "stopping", "offline_request": request_id}
    other = await running.client.post("/api/installation/quiesce", json={"request_id": str(uuid4())})
    assert other.status_code == 409


@pytest.mark.parametrize("boundary", ["registry", "store"])
async def test_handoff_refuses_contended_thread_admission_without_waiting(installation, tmp_path, boundary):
    running = installation
    root = tmp_path / "vault"
    root.mkdir()
    running.registry.configure(None, root, "curator")
    store = running.registry.store(None)
    entered, release = threading.Event(), threading.Event()
    lock = running.registry._lock if boundary == "registry" else store._lifecycle

    def held_boundary():
        with lock:
            entered.set()
            release.wait(3)

    thread = asyncio.create_task(asyncio.to_thread(held_boundary))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        start = time.monotonic()
        response = await running.client.post("/api/installation/quiesce", json={"request_id": str(uuid4())})
        assert response.status_code == 409
        assert time.monotonic() - start < .5
    finally:
        release.set()
        await thread


async def test_sealed_wiki_refuses_ordinary_work_and_shutdown_still_receives_final_changes(installation, tmp_path):
    running = installation
    root = tmp_path / "vault"
    root.mkdir()
    note = root / "note.md"
    note.write_text("# Before\nOriginal bytes.\n")
    running.registry.configure(None, root, "curator")
    store = running.registry.store(None)
    response = await running.client.post("/api/installation/quiesce", json={"request_id": str(uuid4())})
    assert response.status_code == 200
    with pytest.raises(WikiError, match="admission"):
        store.read("note.md")
    with pytest.raises(WikiError, match="admission"):
        await running.registry.run_sync(store.synchronize)
    note.write_text("# Final\nFinal observer synchronization is still owned.\n")
    await running.registry.stop()
    with closing(sqlite3.connect(store.db_path)) as db:
        assert db.execute("SELECT title FROM documents WHERE path='note.md'").fetchone()[0] == "Final"


async def test_a_cancelled_sync_waiter_keeps_its_original_thread_owned(installation):
    running = installation
    entered, release = threading.Event(), threading.Event()

    def original_operation():
        entered.set()
        assert release.wait(5)

    waiting = asyncio.create_task(running.registry.run_sync(original_operation))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        waiting.cancel()
        await asyncio.sleep(0)
        response = await running.client.post("/api/installation/quiesce", json={"request_id": str(uuid4())})
        assert response.status_code == 409
        assert not waiting.done()
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await waiting
    response = await running.client.post("/api/installation/quiesce", json={"request_id": str(uuid4())})
    assert response.status_code == 200


async def test_shutdown_crossing_reconcile_deadline_finishes_original_sync_and_closes_resources(installation, tmp_path, monkeypatch):
    running = installation
    root = tmp_path / "vault"
    root.mkdir()
    running.registry.configure(None, root, "curator")
    store = running.registry.store(None)
    synchronize = store.synchronize
    entered, release = threading.Event(), threading.Event()

    def held_sync():
        entered.set()
        assert release.wait(5)
        return synchronize()

    monkeypatch.setattr(store, "synchronize", held_sync)
    monkeypatch.setattr(settings, "wiki_reconcile_seconds", 0)
    running.registry.start_reconciler()
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        stopping = asyncio.create_task(running.registry.stop())
        await asyncio.sleep(0)
        release.set()
        await stopping
        assert store._retired and store._events_released
        assert running.registry.idle
    finally:
        release.set()


async def test_failed_app_bridge_cleanup_remains_a_handoff_blocker(installation, tmp_path, monkeypatch):
    running = installation
    root = tmp_path / "viewer.data" / "vaults" / "notes" / "repo"
    root.mkdir(parents=True)
    (root / "note.md").write_text("# Preserved\nOriginal content.\n")
    running.registry.configure(running.session.user_id, root, "curator", "viewer")
    row = {"id": "viewer", "app_dir": str(tmp_path / "viewer"), "user_id": running.session.user_id}
    mutation = await app_bridge.begin(row, "POST", "save", {"vault": "notes"}, json.dumps({"path": "note.md"}).encode())
    assert mutation is not None
    finish = mutation._finish

    def failed_cleanup():
        raise RuntimeError("Original journal cleanup failed")

    monkeypatch.setattr(mutation, "_finish", failed_cleanup)
    with pytest.raises(RuntimeError, match="journal cleanup"):
        await mutation.finish()
    response = await running.client.post("/api/installation/quiesce", json={"request_id": str(uuid4())})
    assert response.status_code == 409
    assert any("journal" in reason for reason in response.json()["blockers"])
    # Explicit original journal recovery confirms the effect before handoff.
    monkeypatch.setattr(mutation, "_finish", finish)
    mutation.history.recover()
    response = await running.client.post("/api/installation/quiesce", json={"request_id": str(uuid4())})
    assert response.status_code == 200
