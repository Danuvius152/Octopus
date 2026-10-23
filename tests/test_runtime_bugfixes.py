"""Runtime regressions: exercise owners and durable outcomes, without models."""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from mcp.server.fastmcp import FastMCP

from server import deps, main, mcp_http, mcp_identity
from server.agent_manager import AgentManager
from server.config import settings
from server.database import Database
from server.harness.codex import CODEX, CodexEventParser
from server.harness.run import HarnessRun
from server.monitor import MetricsStore, query, set_store
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


def trusted_readmit(jobs, job_id):
    with pytest.raises(WikiError, match="re-admission") as refused:
        jobs.retry(job_id)
    assert refused.value.status == 409
    retained = jobs.get(job_id)
    paths = [] if retained["payload"].get("material") else [item["path"] for item in retained["items"]]
    return jobs.readmit(job_id, paths)


async def test_startup_isolates_unavailable_vault_and_rejoins_after_explicit_wake(service, tmp_path, caplog):
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
    registry.close()
    broken_owner, broken_root, broken_job = prepared[0]
    broken_root.rename(tmp_path / "disconnected")
    async with main.app.router.lifespan_context(main.app):
        healthy_owner, _, healthy_job = prepared[1]
        await eventually(lambda: WikiJobs(healthy_owner).get(healthy_job)["status"] == "completed")
        # Invalid registration blocks this owner until an explicit wake.
        await asyncio.wait_for(asyncio.shield(jobs_module.worker.tasks[broken_owner]), 1)
        failures = [r for r in caplog.records if "Wiki worker blocked" in r.message and r.args[0] == broken_owner]
        assert len(failures) == 1
        assert "vault root is unavailable" in caplog.text
        (tmp_path / "disconnected").rename(broken_root)
        assert WikiJobs(broken_owner).get(broken_job)["status"] == "queued"
        jobs_module.worker.wake(broken_owner)
        await eventually(lambda: WikiJobs(broken_owner).get(broken_job)["status"] == "completed")
        assert not service._admission_closed
    assert not jobs_module.worker.tasks


async def test_stopped_blocked_worker_does_not_reopen_when_root_returns(jobs, monkeypatch):
    root = jobs.store.root
    moved = root.with_name("offline")
    root.rename(moved)
    worker = WikiWorker()
    reading, release, finished = threading.Event(), threading.Event(), threading.Event()
    try:
        await worker.start(test_wiki_jobs.wiki_api.session_manager)
        task = worker.tasks[None]
        await asyncio.wait_for(asyncio.shield(task), 1)
        is_dir = Path.is_dir

        def hold_missing_root(path, *args, **kwargs):
            if path == root:
                reading.set()
                try:
                    assert release.wait(2)
                    return is_dir(path, *args, **kwargs)
                finally:
                    finished.set()
            return is_dir(path, *args, **kwargs)

        # Hold the receiver's real filesystem check after its curator has
        # ended. Stop must join that I/O before propagating its cancellation,
        # even when the completed read returns the missing-root domain error.
        monkeypatch.setattr(Path, "is_dir", hold_missing_root)
        assert await asyncio.to_thread(reading.wait, 2)
        receiver = worker.notice_task
        stopping = asyncio.create_task(worker.stop(timeout=1))
        await asyncio.sleep(0)
        assert not finished.is_set() and not stopping.done()
        release.set()
        await asyncio.wait_for(stopping, 2)
        assert finished.is_set() and receiver.cancelled()
        assert task.done()
    finally:
        release.set()
        moved.rename(root)
        await worker.stop()
    worker.wake(None)
    assert not worker.tasks


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
            tokens.append(grants.open(session, "maintain", request_id=session._run.request.request_id))
            entered.set()
            await release.wait()
            from server.harness import HarnessEvent
            yield HarnessEvent(type="result")

        async def stop(self):
            release.set()

        async def interrupt(self):
            release.set()

    def held_run(session, *args):
        run = HeldRun()
        run.work_id = session._run.work_id
        return run

    monkeypatch.setattr(manager, "_make_run", held_run)
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
        # Registration retires the old observer. Finish the existing background
        # reconciliation before expecting a new maintenance execution to run.
        await registry.reconcile()
        assert current.store.index_state()["index_status"] == "ready"
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
        # The synthetic CLI opened extra probe scopes above. They must retire
        # before re-admission, even though their old write authority is revoked.
        reason = "original cancellation and result recording" if ending == "cancel" else "assignment has not retired"
        with pytest.raises(WikiError, match=reason) as owned:
            current.readmit(job["id"], [])
        assert owned.value.status == 409
        for token in tokens:
            grants.close(token)
        if ending == "cancel":
            await current.cancel(job["id"], manager)
            # Repeated cancellation reuses its original work. Once the probe
            # scopes retire, explicitly verify the retained result evidence.
            await current.settle_cancelled(job["id"], manager)
            assert current.get(job["id"])["settlement_phase"] == "confirmed"
        assert trusted_readmit(current, job["id"])["status"] == "queued"
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
        trusted_readmit(current, job["id"])
        resumed = WikiWorker()
        resumed.manager = test_wiki_jobs.Curator(current)
        await resumed.run(None)
        assert current.get(job["id"])["status"] == "completed"
        assert current.store.path("Library/published.md").read_bytes() == published
        assert resumed.manager.turns == 0
    finally:
        await worker.stop()


@pytest.mark.parametrize("binding", ["curator_agent_id", "app_id"])
async def test_unconfirmed_recording_survives_rebinding_without_regeneration(jobs, binding):
    from server.models import TurnOutcome
    from server.sessions.submission import RequestCompletion, SubmissionReceipt

    class UnconfirmedThenRebound(test_wiki_jobs.Curator):
        committed = False

        def get_session(self, session_id):
            return None

        async def get_committed_terminal(self, session_id, request_id):
            return {"request_id": request_id, "status": "success"} if self.committed else None

        async def start_message(self, session_id, prompt, **kwargs):
            async for _ in self._events(session_id, prompt, **kwargs):
                pass
            config = registry.config(None)
            config[binding] = "replacement-binding"
            registry.configure(None, Path(config["root"]), config["curator_agent_id"], config["app_id"])
            request_id = self.original_request_id = kwargs["request_id"]
            completion = asyncio.get_running_loop().create_future()
            completion.set_result(RequestCompletion(input_request_id=request_id, request_id=request_id,
                state="recording_failed", recording_state="unconfirmed",
                outcome=TurnOutcome(request_id=request_id, status="success")))
            return SubmissionReceipt("accepted", request_id, completion)

    job = jobs.maintain("unconfirmed-rebind", "# Original\nKeep this source.\n", "organize", [], "Library/unconfirmed.md", [], "human")
    manager = UnconfirmedThenRebound(jobs)
    worker = WikiWorker()
    await worker.start(manager)
    try:
        await asyncio.wait_for(worker.tasks[None], 3)
        current = WikiJobs(None)
        first = current.get(job["id"])
        assert first["status"] == "failed"
        reference = first["items"][0]["worker_request"]
        assert reference["request_id"] == manager.original_request_id
        assert reference["phase"] == "unknown" and reference["recording_state"] == "unconfirmed"
        retained = current.store.path("Library/unconfirmed.md").read_bytes()
        attention = await test_wiki_jobs.process(current, trusted_readmit(current, job["id"]), manager)
        assert attention["status"] == "completed_with_attention"
        assert manager.turns == 1
        manager.committed = True
        finished = await test_wiki_jobs.process(current, current.retry(job["id"]), manager)
        assert finished["status"] == "completed"
        assert manager.turns == 1
        assert current.store.path("Library/unconfirmed.md").read_bytes() == retained
    finally:
        await worker.stop()


@pytest.mark.parametrize("committed", [False, True])
async def test_rebinding_terminal_commit_failure_preserves_original_without_regeneration(tmp_path, monkeypatch, committed):
    from server.sessions import SessionManager

    proof_path = tmp_path / "wiki-terminal-proof.json"
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-q", str(Path(__file__).resolve()),
         "-o", "python_functions=_wiki_terminal_commit_child"],
        cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=15,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1",
             "OCTOPUS_TEST_WIKI_TERMINAL_PROOF": str(proof_path),
             "OCTOPUS_TEST_WIKI_TERMINAL_COMMITTED": str(int(committed))},
    )
    assert result.returncode == 70, result.stdout + result.stderr
    proof = json.loads(proof_path.read_text())
    assert proof["selected"]["status"] == "success"
    assert proof["calls"] == ["model"]
    assert not proof_path.with_suffix(".published").exists()
    monkeypatch.setattr(settings, "wiki_dir", proof["wiki_state"])
    current = WikiJobs(None)
    assert current.registration["app_id"] == "replacement-app"
    assert current.store.path("Library/blocked.md").read_bytes() == bytes.fromhex(proof["published_hex"])
    assert b"Keep this source." in current.store.path("Library/blocked.md").read_bytes()
    database = Database(proof["database"])
    await database.initialize()
    manager = SessionManager()
    await manager.initialize(database)
    calls = []

    def forbid_regeneration(*args, **kwargs):
        calls.append("new model")
        raise AssertionError("Verification must not regenerate the original published work")

    monkeypatch.setattr(manager, "_make_run", forbid_regeneration)
    try:
        session = manager.get_session(proof["session_id"])
        assert session is not None
        terminal = await database.get_committed_terminal(proof["session_id"], proof["request_id"])
        assert terminal == (proof["selected"] if committed else None)
        assert manager.dispatch_snapshot(session.id)["items"] == []
        rows = await database.load_messages(session.id)
        assert [row["content"] for row in rows if row["role"] == "user"] == [proof["input"]]
        assert [row["tool_input"]["request_context"]["request_id"] for row in rows
                if row["role"] == "user"] == [proof["request_id"]]
        original = current.get(proof["job_id"])
        original_reference = original["items"][0]["worker_request"]
        assert original_reference["request_id"] == proof["request_id"]
        assert original_reference["session_id"] == proof["session_id"]
        assert original_reference["attempt"] == proof["original_attempt"]
        assert original["receipts"]
        if not committed:
            assert [row for row in rows if row["type"] == "result"] == []
            assert original_reference["phase"] != "settled"
            assert calls == []
            # The original file receipt survives independently of the absent
            # execution terminal. P5 owns cold item classification/readmission.
            return
        # The fault child really exited above; this is the startup owner's
        # previous-writer proof, not a guess based on absent in-memory state.
        current.recover_jobs(previous_writers_retired=True)
        readmitted = trusted_readmit(current, proof["job_id"])
        finished = await asyncio.wait_for(test_wiki_jobs.process(current, readmitted, manager), 5)
        # Rechecking the committed original request creates no new execution.
        assert finished["attempt"] == readmitted["attempt"]
        assert finished["status"] == ("completed" if committed else "completed_with_attention")
        reference = finished["items"][0]["worker_request"]
        assert reference["request_id"] == proof["request_id"]
        assert reference["session_id"] == proof["session_id"]
        assert reference["attempt"] == proof["original_attempt"]
        assert current.store.path("Library/blocked.md").read_bytes() == bytes.fromhex(proof["published_hex"])
        assert calls == []
    finally:
        await database.close()


async def _wiki_terminal_commit_child(jobs, monkeypatch, tmp_path):
    from server.harness import HarnessEvent
    from server.sessions import SessionManager
    from server.wiki.curation import WikiCuration

    proof_path = Path(os.environ["OCTOPUS_TEST_WIKI_TERMINAL_PROOF"])
    committed = os.environ["OCTOPUS_TEST_WIKI_TERMINAL_COMMITTED"] == "1"
    path = str(proof_path.with_suffix(".db"))
    database = Database(path)
    await database.initialize()
    agent_id = jobs.registration["curator_agent_id"]
    await database.save_agent(agent_id=agent_id, name="Original curator", created_at="time", updated_at="time", backend="codex")
    manager = SessionManager()
    await manager.initialize(database)
    calls = []

    class PublishingRun(FakeRunBase):
        def __init__(self):
            self.started = asyncio.Event()

        async def start(self, *args, **kwargs):
            calls.append("model")
            self.started.set()

        async def stream(self):
            await self.started.wait()
            from server.mcp_identity import mint, verify
            from server.wiki.tasks import tasks

            session_id = jobs.get(job["id"])["session_id"]
            doc = jobs.store.read("Library/blocked.md")
            session = manager.get_session(session_id)
            token = tasks.open(session, "maintain", request_id=session._run.request.request_id)
            try:
                task = tasks.call(tasks.active[token], verify(mint(session_id, wiki_task_id=token)))
                tasks.record_read(task, doc)
                await asyncio.to_thread(WikiCuration(jobs.store).organize, doc["path"], doc["revision"],
                    "Original conclusion and conditions.", [], [], [], "curator", "publish", task=task)
            finally:
                tasks.close(token)
            yield HarnessEvent(type="result")

        async def stop(self):
            pass

    monkeypatch.setattr(manager, "_make_run", lambda *args, **kwargs: PublishingRun())
    job = jobs.maintain("blocked-recording-rebind", "# Original\nKeep this source.\n", "organize", [], "Library/blocked.md", [], "human")
    selected = None
    input_content = None
    execute, commit = database.conn.execute, database.conn.commit

    async def observe_sql(sql, parameters=()):
        nonlocal selected, input_content
        cursor = await execute(sql, parameters)
        if sql.startswith("INSERT INTO messages "):
            if parameters[2] == "user":
                input_content = json.loads(parameters[4])
            if parameters[3] == "result":
                selected = json.loads(parameters[4])
        return cursor

    async def uncertain_commit():
        if selected is None:
            return await commit()
        if committed:
            await commit()
        original = jobs.get(job["id"])
        config = registry.config(None)
        registry.configure(None, Path(config["root"]), config["curator_agent_id"], "replacement-app")
        proof_path.write_text(json.dumps({
            "database": path, "wiki_state": str(settings.wiki_dir), "job_id": job["id"],
            "session_id": original["session_id"], "request_id": selected["request_id"],
            "original_attempt": original["attempt"],
            "input": input_content,
            "selected": selected, "calls": calls,
            "published_hex": jobs.store.path("Library/blocked.md").read_bytes().hex(),
        }))
        raise OSError("actual Wiki worker terminal commit acknowledgment lost")

    async def observe(event):
        if event.get("type") == "result":
            proof_path.with_suffix(".published").touch()

    manager.on_broadcast("wiki-terminal-proof", observe)
    monkeypatch.setattr(database.conn, "execute", observe_sql)
    monkeypatch.setattr(database.conn, "commit", uncertain_commit)
    worker = WikiWorker()
    await worker.start(manager)
    await asyncio.wait_for(asyncio.shield(worker.tasks[None]), 10)
    raise AssertionError("An uncertain terminal commit returned to the Wiki worker")


@pytest.mark.parametrize("binding", ["curator_agent_id", "app_id"])
@pytest.mark.parametrize("retry_immediately", [False, True])
async def test_recording_reference_survives_a_cancellation_between_control_read_and_write(jobs, monkeypatch, binding, retry_immediately):
    from server.wiki import job_execution

    WikiHistory(jobs.store).write("Library/cancel-race.md", b"# Original\nRetained.\n", None, "human", "source")
    job = jobs.maintain("cancel-recording-race", "", "organize", ["Library/cancel-race.md"], None, [], "human")
    captured = jobs.update(job["id"], status="running", session_id="original-session")
    reference = job_execution.prepare(jobs, captured, captured["items"], "original-session", "original-request")
    config = registry.config(None)
    config[binding] = "replacement-binding"
    registry.configure(None, Path(config["root"]), config["curator_agent_id"], config["app_id"])
    current = WikiJobs(None)
    transition = job_execution.transition
    raced = False

    def racing_transition(control, job_id, original, phase, **fields):
        nonlocal raced
        if not raced:
            raced = True
            current.mark_cancelled(job_id)
            if retry_immediately:
                with pytest.raises(WikiError, match="cancellation"):
                    current.retry(job_id)
        return transition(control, job_id, original, phase, **fields)

    monkeypatch.setattr(job_execution, "transition", racing_transition)
    completion = SimpleNamespace(input_request_id="original-request", request_id="original-request",
                                 outcome=None, recording_state="unconfirmed")
    await WikiWorker()._mark_recording_failure(jobs, job["id"], reference, completion)
    result = current.get(job["id"])
    assert raced
    assert result["status"] == "cancelled" and result["attempt"] == reference["attempt"]
    retained = result["items"][0]["worker_request"]
    assert retained["request_id"] == "original-request"
    assert retained["phase"] == "unknown" and retained["recording_state"] == "unconfirmed"


async def test_captured_web_lifecycle_prevents_idle_but_replays_and_silence_do_not(manager, monkeypatch, tmp_path):
    rows = [json.loads(line) for line in (Path(__file__).parent / "fixtures/codex-web-search-0.162.1.jsonl").read_text().splitlines()]
    clock = SimpleNamespace(now=299.0)
    run = HarnessRun(CODEX)
    ticks = []

    def touch(_):
        ticks.append(clock.now)

    run.bind_activity(touch)
    for row in rows:
        await run._handle_line(json.dumps(row))
    assert ticks == [299.0, 299.0]
    clock.now = 301.0
    for row in [rows[0], rows[1], rows[1]]:
        await run._handle_line(json.dumps(row))
    assert ticks == [299.0, 299.0]

    run._begin_retirement()
    await run.stop()
    await run._handle_line(json.dumps({"type": "item.completed", "item": {"type": "web_search", "id": "exec-11111111-1111-4111-8111-111111111111"}}))
    assert ticks == [299.0, 299.0]

    observed = HarnessRun(CODEX)
    validated = []
    active, replay, replayed, stopped = (asyncio.Event() for _ in range(4))

    class WebRun(FakeRunBase):
        def __init__(self):
            self.started = asyncio.Event()

        def bind_activity(self, callback):
            def observe(timestamp):
                validated.append(timestamp)
                callback(timestamp)

            return observed.bind_activity(observe)

        def unbind_activity(self, token):
            observed.unbind_activity(token)

        async def start(self, *args, **kwargs):
            self.started.set()

        async def stream(self):
            await self.started.wait()
            for row in rows:
                await observed._handle_line(json.dumps(row))
            active.set()
            await replay.wait()
            for row in (rows[0], rows[1], rows[1]):
                await observed._handle_line(json.dumps(row))
            replayed.set()
            await stopped.wait()
            if False:
                yield

        async def stop(self):
            observed._begin_retirement()
            await observed.stop()
            stopped.set()

        async def interrupt(self):
            await self.stop()

    monkeypatch.setattr(settings, "turn_idle_timeout_seconds", .2)
    monkeypatch.setattr(settings, "turn_idle_max_retries", 0)
    monkeypatch.setattr(settings, "turn_max_seconds", 0)
    monkeypatch.setattr(manager, "_make_run", lambda *args, **kwargs: WebRun())
    agent = await manager.db.get_system_agent()
    session = await manager.create_session(agent["id"], "Captured web activity", str(tmp_path), backend="codex")
    receipt = await manager.start_message(session.id, "Wait for real web activity")
    waiting = asyncio.create_task(receipt.wait())
    try:
        await asyncio.wait_for(active.wait(), 2)
        assert len(validated) == 2
        await asyncio.sleep(.1)
        assert not waiting.done() and manager.session_is_executing(session)
        replay.set()
        await asyncio.wait_for(replayed.wait(), 2)
        assert len(validated) == 2
        completion = await asyncio.wait_for(waiting, 2)
        assert completion.outcome.status == "failed"
        assert completion.outcome.code == "turn_timeout"
        assert "without validated activity" in completion.outcome.message
        assert completion.outcome.recovery["idle"] == 0
        assert stopped.is_set()
    finally:
        replay.set()
        stopped.set()
        await asyncio.gather(waiting, return_exceptions=True)


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
    import server.sessions.owner_runtime as owner_runtime
    import server.sessions.turns as turns

    store = MetricsStore(str(tmp_path / "metrics.db"))
    await store.initialize()
    set_store(store)
    clock = SimpleNamespace(now=time.monotonic())
    monkeypatch.setattr(codex, "time", SimpleNamespace(monotonic=lambda: clock.now))
    monkeypatch.setattr(turns, "time", SimpleNamespace(monotonic=lambda: clock.now))
    monkeypatch.setattr(owner_runtime, "time", SimpleNamespace(monotonic=lambda: clock.now))
    monkeypatch.setattr(type(manager), "_TRANSIENT_RETRY_BASE_DELAY", 0)
    agent = await manager.db.get_system_agent()
    session = await manager.create_session(agent["id"], "duration", str(tmp_path), backend="codex")
    starts = []

    class ParsedRun(FakeRunBase):
        def __init__(self):
            self.started = asyncio.Event()

        async def start(self, *a, **k):
            self.parser = CodexEventParser()
            self.parser.parse({"type": "turn.started"})
            self.ordinal = len(starts)
            starts.append(clock.now)
            self.started.set()

        async def stream(self):
            await self.started.wait()
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
        events = []

        async def observe(event):
            events.append(event)

        manager.on_broadcast("attempt-duration", observe)
        receipt = await manager.start_message(session.id, "request")
        assert (await asyncio.wait_for(receipt.wait(), 5)).outcome.status == "success"
        terminal = [event for event in events if event["type"] == "result"]
        assert len(terminal) == 1 and not terminal[0]["is_error"]
        assert terminal[0]["duration_ms"] == 750
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
async def test_retired_mcp_cleanup_is_owned_and_does_not_restore_tools(retirement, manager, monkeypatch):
    users = UserManager(manager.db)
    owner_id = (await users.create_user(username="cleanup-owner", password="password1"))["id"]
    other_id = (await users.create_user(username="cleanup-other", password="password2"))["id"]
    monkeypatch.setattr(deps, "_user_manager", users)
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
    mcp_identity.register_run(lease, "owner-session", owner_id, work)
    owner = mcp_identity.mint("owner-session", user_id=owner_id, run_lease_id=lease, work_id=work)
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
                identities = [("other-session", owner_id, "other-run"), ("owner-session", other_id, "other-tenant"), ("owner-session", owner_id, "replacement-run")] if retirement == "cancel-delete" else []
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
