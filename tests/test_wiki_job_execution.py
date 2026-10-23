"""Admission intent, exact original completion and cancellation crash windows."""

import asyncio

import pytest

from server.database import Database
from server.mcp_identity import work_is_open
from server.models import TurnOutcome
from server.wiki import job_execution
from server.wiki.jobs import JobOwner, WikiWorker
from server.wiki.store import WikiError
from server.wiki.tasks import tasks
from tests import test_wiki_api as wiki_api
from tests.test_wiki_job_events import enqueue
from tests.test_wiki_jobs import Curator, process
from tests.test_wiki_jobs import client as client
from tests.test_wiki_jobs import jobs as jobs


@pytest.fixture
async def disk_manager(jobs, tmp_path):
    database = Database(str(tmp_path / "execution-main.db"))
    await database.initialize()
    agent_id = jobs.registration["curator_agent_id"]
    if await database.get_agent(agent_id) is None:
        await database.save_agent(agent_id=agent_id, name="Recovery worker", created_at="time", updated_at="time", backend="codex")
    await database.save_session("original-worker", "Original worker", str(tmp_path), "time", agent_id=agent_id, origin="wiki")

    class RecoveryOnly(Curator):
        async def start_message(self, *args, **kwargs):
            raise AssertionError("Original execution recovery must not admit a model")

        async def create_session(self, *args, **kwargs):
            raise AssertionError("Original execution recovery must not create another worker")

    manager = RecoveryOnly(jobs)
    manager.db = database
    try:
        yield manager
    finally:
        await manager.db.close()


def prepare(jobs, job):
    running = jobs.update(job["id"], status="running")
    reference = job_execution.prepare(jobs, running, running["items"], "original-worker", "original-request")
    return jobs.get(job["id"]), reference


async def pause_human_before_claim(manager, session_id, prompt, request_id):
    """Keep a real accepted correction unstarted through the public Stop path."""
    paused = asyncio.Event()

    async def stop_original_claim(event):
        if (event.get("session_id") == session_id and event.get("request_id") == request_id
                and event.get("type") == "dequeued" and not paused.is_set()):
            await manager.interrupt(session_id)
            paused.set()

    manager.on_broadcast(request_id, stop_original_claim)
    try:
        receipt = await manager.start_message(session_id, prompt, request_id=request_id)
        assert receipt.accepted
        await asyncio.wait_for(paused.wait(), 5)
        await manager.await_session_execution(session_id, timeout=5)
        return receipt
    finally:
        manager.remove_broadcast(request_id)


async def record_terminal(database, request_id="original-request", *, content_id=None):
    outcome = TurnOutcome(request_id=content_id or request_id, status="success").model_dump()
    await database.record_terminal(session_id="original-worker", request_id=request_id, seq=0,
                                   role="assistant", type="result", content=outcome, created_at="time")


async def test_exact_request_is_committed_before_model_admission_and_matches_assignment(jobs):
    job = enqueue(jobs)
    seen = []

    class Observed(Curator):
        async def start_message(self, session_id, prompt, **kwargs):
            current = self.jobs.get(job["id"])
            reference = current["items"][0]["worker_request"]
            assert reference["phase"] == "prepared"
            assert reference["attempt"] == current["attempt"]
            assert reference["session_id"] == current["session_id"] == session_id
            assert reference["request_id"] == kwargs["request_id"]
            assert reference["work_id"] == kwargs["work_id"]
            assert work_is_open(reference["work_id"])
            assignment = tasks.assignments[session_id]
            assert assignment["assigned_request_id"] == reference["request_id"]
            assert assignment["assigned_attempt"] == reference["attempt"]
            seen.append(dict(reference))
            return await super().start_message(session_id, prompt, **kwargs)

    manager = Observed(jobs)
    finished = await process(jobs, job, manager)
    assert finished["status"] == "completed" and manager.turns == 1
    reference = finished["items"][0]["worker_request"]
    assert reference["request_id"] == seen[0]["request_id"]
    assert reference["phase"] == "settled" and reference["recording_state"] == "committed"
    assert not work_is_open(reference["work_id"])


async def test_prepared_commit_without_terminal_never_readmits_or_resets_budget(jobs, disk_manager):
    job, reference = prepare(jobs, enqueue(jobs))
    before = jobs.store.path(job["items"][0]["path"]).read_bytes()
    jobs.recover_jobs(previous_writers_retired=True)
    assert jobs.next_queued() == job["id"]
    worker = WikiWorker()
    worker.manager = disk_manager
    await worker.process(jobs, job["id"])
    attention = jobs.get(job["id"])
    item = attention["items"][0]
    assert attention["attempt"] == reference["attempt"]
    assert attention["status"] == "completed_with_attention"
    assert item["worker_request"]["request_id"] == reference["request_id"]
    assert item.get("retry_count", 0) == 0
    assert disk_manager.turns == disk_manager.sessions == 0
    assert jobs.store.path(item["path"]).read_bytes() == before
    retried = jobs.retry(job["id"])
    assert retried["status"] == "interrupted" and retried["attempt"] == attention["attempt"]


async def test_committed_original_terminal_and_publication_resume_without_new_request(jobs, disk_manager):
    job, reference = prepare(jobs, enqueue(jobs))
    tasks.assign(reference["session_id"], job["id"], job["items"], registration=jobs.registration,
                 request_id=reference["request_id"], attempt=reference["attempt"])
    try:
        async for _ in Curator(jobs)._events(reference["session_id"], "Publish original output", request_id=reference["request_id"]):
            pass
    finally:
        tasks.unassign(reference["session_id"])
    raw = jobs.store.path(job["items"][0]["path"]).read_bytes()
    await record_terminal(disk_manager.db)
    path = disk_manager.db._db_path
    await disk_manager.db.close()
    disk_manager.db = Database(path)
    await disk_manager.db.initialize()
    jobs.recover_jobs(previous_writers_retired=True)
    assert jobs.next_queued() == job["id"]
    worker = WikiWorker()
    worker.manager = disk_manager
    await worker.process(jobs, job["id"])
    done = jobs.get(job["id"])
    assert done["status"] == "completed" and done["attempt"] == reference["attempt"]
    assert done["items"][0]["worker_request"]["phase"] == "settled"
    assert done["items"][0]["index_complete"] is True
    assert done["items"][0].get("retry_count", 0) == 0
    assert jobs.store.path(job["items"][0]["path"]).read_bytes() == raw
    assert disk_manager.turns == disk_manager.sessions == 0


async def test_mismatched_original_terminal_is_not_proof(jobs, disk_manager):
    job, reference = prepare(jobs, enqueue(jobs))
    await record_terminal(disk_manager.db, content_id="another-request")
    await jobs.cancel(job["id"], disk_manager)
    await jobs.settle_cancelled(job["id"], disk_manager)
    assert jobs.get(job["id"])["settlement_phase"] == "attention"
    with pytest.raises(WikiError, match="Confirm the original cancellation"):
        jobs.retry(job["id"])
    with pytest.raises(WikiError, match="Confirm the original cancellation"):
        jobs.readmit(job["id"], [])
    assert jobs.get(job["id"])["items"][0]["worker_request"] == reference


async def test_accepted_original_is_not_settled_while_writer_is_owned(jobs, disk_manager):
    job, reference = prepare(jobs, enqueue(jobs))
    job_execution.transition(jobs, job["id"], reference, "accepted", disposition="accepted")
    await record_terminal(disk_manager.db)
    key = (str(jobs.store.state.resolve()), job["id"])
    owner = JobOwner(entered_attempt=job["attempt"])
    WikiWorker.executing[key] = owner
    try:
        await jobs.cancel(job["id"], disk_manager)
        assert jobs.get(job["id"])["settlement_phase"] == "pending"
    finally:
        assert WikiWorker.executing.pop(key) is owner
        job_execution.transition(jobs, job["id"], reference, reference["phase"], retired=True)
        owner.retired.set()
    await jobs.settle_cancelled(job["id"], disk_manager)
    assert jobs.get(job["id"])["settlement_phase"] == "confirmed"


async def test_cancel_before_admission_closes_exact_work_and_rejection_keeps_original_id(jobs, tmp_path):
    job = enqueue(jobs)
    manager = wiki_api.session_manager
    session = await manager.create_session(jobs.registration["curator_agent_id"], "Pending admission", str(tmp_path))
    running = jobs.update(job["id"], status="running")
    reference = job_execution.prepare(jobs, running, running["items"], session.id, "unadmitted-original")
    jobs.mark_cancelled(job["id"])
    assert not work_is_open(reference["work_id"])
    receipt = await manager.start_message(session.id, "Must not run", request_id=reference["request_id"],
                                          source="wiki", work_id=reference["work_id"])
    assert not receipt.accepted and receipt.request_id == reference["request_id"]
    completion = await receipt.wait()
    job_execution.require_completion(reference, completion)
    job_execution.transition(jobs, job["id"], reference, "rejected", disposition=receipt.disposition)
    await jobs.settle_cancelled(job["id"], manager)
    assert jobs.get(job["id"])["settlement_phase"] == "confirmed"
    assert manager.dispatch_snapshot(session.id)["items"] == []
    assert await manager.db.load_messages(session.id) == []


async def test_late_old_attempt_cannot_overwrite_a_new_claim(jobs):
    job, reference = prepare(jobs, enqueue(jobs))
    job_execution.transition(jobs, job["id"], reference, "settled", recording_state="committed", retired=True)
    jobs.update(job["id"], status="failed", session_id=None)
    jobs.retry(job["id"])
    assert jobs.next_queued() == job["id"]
    before = jobs.get(job["id"])
    assert before["attempt"] == reference["attempt"] + 1
    with pytest.raises(WikiError, match="superseded"):
        job_execution.transition(jobs, job["id"], reference, "settled", recording_state="committed")
    assert jobs.get(job["id"])["state_version"] == before["state_version"]


async def test_cancelled_attempt_rejects_prepare_without_saving_new_identity(jobs):
    job = jobs.update(enqueue(jobs)["id"], status="running")
    jobs.mark_cancelled(job["id"])
    with pytest.raises(WikiError, match="attempt changed"):
        job_execution.prepare(jobs, job, job["items"], "never-admitted", "never-prepared")
    current = jobs.get(job["id"])
    assert current["session_id"] is None
    assert "worker_request" not in current["items"][0]


async def test_human_correction_does_not_inherit_assignment_and_blocks_automatic_archive(jobs, tmp_path):
    manager = wiki_api.session_manager
    session = await manager.create_session(jobs.registration["curator_agent_id"], "Human correction", str(tmp_path))
    tasks.assign(session.id, "old-job", [], registration=jobs.registration, request_id="wiki-original", attempt=1)
    token = tasks.open(session, "auto", request_id="human-correction")
    archive = None
    try:
        assert tasks.active[token].job_id is None
        assert tasks.mode_for(session, None, request_id="human-correction") == "auto"
        correction = await pause_human_before_claim(manager, session.id, "Keep this new condition", "human-correction")
        archive = asyncio.create_task(manager.archive_session(session.id, replace=False, only_request_id="wiki-original"))
        await archive
        assert manager.get_session(session.id) is session
        assert [item["request_id"] for item in manager.dispatch_snapshot(session.id)["items"]] == [correction.request_id]
        assert not session._admission_closed
    finally:
        tasks.close(token)
        tasks.unassign(session.id)
        await manager.cancel_request(session.id, "human-correction")
        if archive is not None:
            await asyncio.gather(archive, return_exceptions=True)


async def test_failed_worker_cleanup_keeps_a_later_human_correction(jobs, tmp_path, monkeypatch):
    manager = wiki_api.session_manager
    session = await manager.create_session(jobs.registration["curator_agent_id"], "Worker failure", str(tmp_path))
    running = jobs.update(enqueue(jobs)["id"], status="running")
    reference = job_execution.prepare(jobs, running, running["items"], session.id, "failed-original")
    correction = await pause_human_before_claim(manager, session.id, "Preserve this new instruction", "later-human")
    archive = manager.archive_session

    async def guarded_archive(session_id, **kwargs):
        assert kwargs.get("only_request_id") == reference["request_id"]
        return await archive(session_id, **kwargs)

    monkeypatch.setattr(manager, "archive_session", guarded_archive)
    worker = WikiWorker()
    worker.manager = manager
    try:
        await worker._finish_failed_job(jobs.user_id, running["id"], "failed", "Worker stopped")
        assert manager.get_session(session.id) is session
        assert [item["request_id"] for item in manager.dispatch_snapshot(session.id)["items"]] == [correction.request_id]
        assert not session._admission_closed
    finally:
        await manager.cancel_request(session.id, correction.request_id)


async def test_cancel_during_session_creation_is_confirmed_only_after_live_worker_retires(jobs):
    job = enqueue(jobs)
    assert jobs.next_queued() == job["id"]
    entered, release = asyncio.Event(), asyncio.Event()

    class PausedBeforePrepare(Curator):
        async def create_session(self, *args, **kwargs):
            entered.set()
            await release.wait()
            return await super().create_session(*args, **kwargs)

        async def start_message(self, *args, **kwargs):
            raise AssertionError("A cancelled unprepared worker must never admit a request")

    manager = PausedBeforePrepare(jobs)
    worker = WikiWorker()
    worker.manager = manager
    process = asyncio.create_task(worker.process(jobs, job["id"]))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        cancelled = await jobs.cancel(job["id"], manager)
        assert cancelled["settlement_phase"] == "pending"
        release.set()
        await process
        current = jobs.get(job["id"])
        assert current["settlement_phase"] == "confirmed"
        assert current["attempt"] == 1
        assert current["items"][0]["worker_request"]["disposition"] == "not_admitted"
        assert manager.turns == 0
    finally:
        release.set()
        await asyncio.gather(process, return_exceptions=True)
