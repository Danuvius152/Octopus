"""Wiki fixture teardown retains the database's original producer-drain order."""

import asyncio
import os
import socket
import sys

import pytest

from server.harness import get_harness
from server.sessions import ownership
from tests import wiki_api
from tests.harness_profiles import process_profile
from tests.wiki_api import client as client
from tests.wiki_job_runtime import active_worker, worker_callback


@pytest.mark.parametrize("failure", [RuntimeError, asyncio.CancelledError])
async def test_worker_setup_releases_assignment_when_open_fails(client, tmp_path, monkeypatch, failure):
    from server.wiki.jobs import WikiJobs, WikiWorker
    from server.wiki.tasks import tasks

    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "note.md").write_text("# Note\nOriginal conditions.\n")
    config = await wiki_api.configure(client, vault)
    before_tasks = dict(tasks.active)
    before_assignments = dict(tasks.assignments)
    before_executions, before_cancellations = dict(WikiWorker.executing), dict(WikiWorker.cancellations)
    attempted = []
    job_ids = []

    def fail_open(session, *args, **kwargs):
        attempted.append(session.id)
        job_ids.append(tasks.assignments[session.id]["job_id"])
        raise failure("worker setup interrupted")

    monkeypatch.setattr(tasks, "open", fail_open)
    try:
        with pytest.raises(failure):
            async with active_worker(config, ["note.md"], client=client):
                pytest.fail("Failed setup yielded a worker")
        assert tasks.active == before_tasks
        assert tasks.assignments == before_assignments
        assert WikiWorker.executing == before_executions and WikiWorker.cancellations == before_cancellations
        owner = await wiki_api.session_manager.db.get_system_agent()
        jobs = WikiJobs(owner["user_id"])
        for job_id in job_ids:
            retired = jobs.get(job_id)
            assert retired["status"] == "cancelled" and retired["settlement_phase"] == "confirmed"
            assert retired["session_id"] is None
    finally:
        for session_id in attempted:
            tasks.unassign(session_id)


@pytest.mark.parametrize("failure", [RuntimeError, asyncio.CancelledError])
async def test_callback_setup_failure_retires_only_its_worker_and_server(client, tmp_path, monkeypatch, failure):
    import uvicorn

    from server.mcp_identity import verify
    from server.wiki.jobs import WikiJobs, WikiWorker
    from server.wiki.tasks import tasks
    from tests import callback_api

    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "note.md").write_text("# Note\nOriginal conditions.\n")
    config = await wiki_api.configure(client, vault)
    readiness = callback_api._await_namespaces
    serve, namespaces = uvicorn.Server.serve, callback_api._own_namespaces
    failed_scopes, job_ids, ports = [], [], []
    owned = []
    ready = asyncio.Event()

    async def observe_server(*args, **kwargs):
        owned.append(asyncio.current_task())
        await serve(*args, **kwargs)

    async def observe_namespaces(*args, **kwargs):
        owned.append(asyncio.current_task())
        await namespaces(*args, **kwargs)

    async def fail_after_readiness(port, servers, token):
        await readiness(port, servers, token)
        scope = verify(token)
        failed_scopes.append(scope)
        job_ids.append(tasks.assignments[scope.session_id]["job_id"])
        ports.append(port)
        ready.set()
        if failure is RuntimeError:
            raise RuntimeError("callback setup interrupted")
        await asyncio.Event().wait()

    async def open_callback():
        async with worker_callback(config, ["note.md"], client=client):
            pytest.fail("Failed callback setup yielded a worker")

    async with active_worker(config, ["note.md"], client=client) as sibling:
        before_tasks, before_assignments = dict(tasks.active), dict(tasks.assignments)
        before_executions, before_cancellations = dict(WikiWorker.executing), dict(WikiWorker.cancellations)
        with monkeypatch.context() as patch:
            patch.setattr(callback_api, "_await_namespaces", fail_after_readiness)
            patch.setattr(uvicorn.Server, "serve", observe_server)
            patch.setattr(callback_api, "_own_namespaces", observe_namespaces)
            attempt = asyncio.create_task(open_callback())
            try:
                await asyncio.wait_for(ready.wait(), 10)
                if failure is asyncio.CancelledError:
                    attempt.cancel()
                with pytest.raises(failure):
                    await attempt
            finally:
                if not attempt.done():
                    attempt.cancel()
                await asyncio.gather(attempt, return_exceptions=True)
        assert len(failed_scopes) == 1
        failed_scope = failed_scopes[0]
        assert failed_scope.session_id != sibling.scope.session_id
        assert tasks.active == before_tasks and tasks.assignments == before_assignments
        assert WikiWorker.executing == before_executions and WikiWorker.cancellations == before_cancellations
        assert owned and all(task.done() for task in owned)
        assert wiki_api.session_manager.get_session(failed_scope.session_id) is None
        jobs = WikiJobs(sibling.scope.user_id)
        retired = jobs.get(job_ids[0])
        assert retired["status"] == "cancelled" and retired["settlement_phase"] == "confirmed"
        assert retired["session_id"] is None
        assert jobs.get(sibling.task.job_id)["status"] == "running"
        with pytest.raises(OSError):
            socket.create_connection(("127.0.0.1", ports[0]), timeout=1)
        read = await client.get("/api/wiki/read", params={"doc": "note.md"}, headers=sibling.headers)
        assert read.status_code == 200, read.text
        assert tasks.resolve(sibling.scope) is sibling.task
        async with worker_callback(config, ["note.md"], client=client) as (following, host):
            assert following.scope.session_id not in {failed_scope.session_id, sibling.scope.session_id}
            assert tasks.resolve(following.scope) is following.task
            assert not following.task.reads
            assert tasks.resolve(sibling.scope) is sibling.task
        assert host._task.done() and host._owner.done()
        retired = jobs.get(following.task.job_id)
        assert retired["status"] == "cancelled" and retired["settlement_phase"] == "confirmed"
        assert tasks.active == before_tasks and tasks.assignments == before_assignments
        assert WikiWorker.executing == before_executions and WikiWorker.cancellations == before_cancellations


async def test_worker_cancelled_during_creation_retires_the_original_late_session(client, tmp_path, monkeypatch):
    from server.wiki.jobs import WikiJobs

    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "note.md").write_text("# Note\nOriginal conditions.\n")
    config = await wiki_api.configure(client, vault)
    manager = wiki_api.session_manager
    create = manager._create_session
    created, release, returned = asyncio.Event(), asyncio.Event(), asyncio.Event()
    sessions = []

    async def delayed_creation(*args, **kwargs):
        session = await create(*args, **kwargs)
        sessions.append(session)
        created.set()
        await release.wait()
        returned.set()
        return session

    async def setup():
        async with active_worker(config, ["note.md"], client=client):
            pytest.fail("Cancelled setup yielded a worker")

    monkeypatch.setattr(manager, "_create_session", delayed_creation)
    attempt = asyncio.create_task(setup())
    try:
        await asyncio.wait_for(created.wait(), 5)
        attempt.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await attempt
        await asyncio.wait_for(returned.wait(), 5)
        assert manager.get_session(sessions[0].id) is None
        jobs = WikiJobs(sessions[0].user_id).list()["items"]
        assert len(jobs) == 1 and jobs[0]["status"] == "cancelled"
        assert jobs[0]["settlement_phase"] == "confirmed"
    finally:
        release.set()
        await asyncio.gather(attempt, return_exceptions=True)
        for session in sessions:
            await manager.delete_session(session.id)


async def test_worker_exit_cancelled_during_settlement_joins_the_original_cleanup(client, tmp_path, monkeypatch):
    from server.wiki.jobs import WikiJobs, WikiWorker

    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "note.md").write_text("# Note\nOriginal conditions.\n")
    config = await wiki_api.configure(client, vault)
    cancel = WikiWorker._cancel_attempt
    entered, release, settled = asyncio.Event(), asyncio.Event(), asyncio.Event()
    cleanup_tasks, scopes = [], []

    async def delayed_settlement(*args, **kwargs):
        cleanup_tasks.append(asyncio.current_task())
        entered.set()
        await release.wait()
        result = await cancel(*args, **kwargs)
        settled.set()
        return result

    async def use_worker():
        async with active_worker(config, ["note.md"], client=client) as worker:
            scopes.append(worker)

    monkeypatch.setattr(WikiWorker, "_cancel_attempt", staticmethod(delayed_settlement))
    attempt = asyncio.create_task(use_worker())
    try:
        await asyncio.wait_for(entered.wait(), 5)
        attempt.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await attempt
        assert settled.is_set() and all(task.done() for task in cleanup_tasks)
        retired = WikiJobs(scopes[0].scope.user_id).get(scopes[0].task.job_id)
        assert retired["status"] == "cancelled" and retired["settlement_phase"] == "confirmed"
    finally:
        release.set()
        await asyncio.gather(attempt, *cleanup_tasks, return_exceptions=True)


async def test_callback_exit_cancelled_during_stop_joins_the_original_host(client, tmp_path, monkeypatch):
    from tests.callback_api import CallbackApi

    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "note.md").write_text("# Note\nOriginal conditions.\n")
    config = await wiki_api.configure(client, vault)
    stop = CallbackApi.stop
    entered, release, stopped = asyncio.Event(), asyncio.Event(), asyncio.Event()
    hosts = []

    async def delayed_stop(host):
        entered.set()
        await release.wait()
        await stop(host)
        stopped.set()

    async def use_callback():
        async with worker_callback(config, ["note.md"], client=client) as (_, host):
            hosts.append(host)

    monkeypatch.setattr(CallbackApi, "stop", delayed_stop)
    attempt = asyncio.create_task(use_callback())
    try:
        await asyncio.wait_for(entered.wait(), 10)
        attempt.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await attempt
        assert stopped.is_set()
        assert hosts[0]._task.done() and hosts[0]._owner.done()
    finally:
        release.set()
        await asyncio.gather(attempt, return_exceptions=True)
        for host in hosts:
            await stop(host)


async def test_http_fixture_teardown_retires_an_active_original_process(tmp_path, monkeypatch, short_process_shutdown):
    # Exercise the actual fixture's teardown, including the path reached when
    # an assertion fails while its model stand-in is still running.
    fixture = wiki_api.client.__wrapped__(tmp_path, monkeypatch)
    await anext(fixture)
    manager = wiki_api.session_manager
    database = manager.db
    ready = asyncio.Event()
    harness = get_harness("codex")

    def argv(context):
        return [sys.executable, "-u", "-c",
            "import json,time\n"
            "print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':'ready'}}),flush=True)\n"
            "time.sleep(3600)\n"], {"cwd": context.working_dir}

    async def observe(event):
        if event.get("type") == "assistant_text" and event.get("content") == "ready":
            ready.set()

    monkeypatch.setattr(harness, "profile", process_profile(harness.profile, build_turn_argv=argv))
    # Bound the regression's incorrect wait; real process signals and joining
    # remain active. Correct teardown cancels the producer before this wait.
    monkeypatch.setattr(ownership, "_BACKEND_RETIRE_TIMEOUT", .2)
    manager.on_broadcast("fixture-original-process", observe)
    try:
        agent = await database.get_system_agent()
        session = await manager.create_session(agent["id"], "Fixture cleanup", str(tmp_path), backend="codex")
        receipt = await manager.start_message(session.id, "Keep running")
        await asyncio.wait_for(ready.wait(), 3)
        process = session._run.backend._process
        assert process.returncode is None
        await asyncio.wait_for(fixture.aclose(), 3)
        completion = await asyncio.wait_for(receipt.wait(), 1)
        assert completion.outcome.status == "interrupted"
        assert completion.recording_state == "committed"
        assert await receipt.execution.wait_resources() == ("proven",)
        assert process.returncode is not None
        with pytest.raises(ProcessLookupError):
            os.kill(process.pid, 0)
        assert database._closed
    finally:
        await fixture.aclose()
