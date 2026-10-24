"""Explicit controls retain their original attempt after a lost response."""

import pytest

from server.wiki import job_execution
from server.wiki.store import WikiError
from tests.test_wiki_job_events import enqueue
from tests.test_wiki_job_execution import disk_manager as disk_manager
from tests.test_wiki_job_execution import prepare, record_terminal
from tests.test_wiki_jobs import client as client
from tests.test_wiki_jobs import jobs as jobs


def test_retry_response_loss_reuses_original_control_after_worker_claim(jobs):
    job, reference = prepare(jobs, enqueue(jobs))
    job_execution.transition(jobs, job["id"], reference, "rejected", disposition="not_admitted", retired=True)
    original = jobs.update(job["id"], status="failed", session_id=None)
    control = {"operation_id": "continue-original", "expected_attempt": original["attempt"],
               "expected_vault_id": jobs.registration["vault_id"]}
    accepted = jobs.retry(job["id"], **control)
    assert accepted["status"] == "queued"
    jobs.next_queued()
    current = jobs.get(job["id"])
    assert current["attempt"] == original["attempt"] + 1
    replayed = jobs.retry(job["id"], **control)
    assert replayed["state_version"] == current["state_version"]
    assert replayed["attempt"] == current["attempt"]
    with pytest.raises(WikiError, match="another control"):
        jobs.retry(job["id"], **(control | {"expected_attempt": current["attempt"]}))


def test_absent_runtime_does_not_prove_prepared_execution_retired(jobs):
    job, reference = prepare(jobs, enqueue(jobs))
    jobs.update(job["id"], status="failed", session_id=None)
    with pytest.raises(WikiError, match="retir"):
        jobs.retry(job["id"], operation_id="unsafe-reentry", expected_attempt=reference["attempt"])
    assert jobs.get(job["id"])["status"] == "failed"


async def test_explicit_verify_uses_original_terminal_and_never_creates_model_work(jobs, disk_manager, client):
    job, reference = prepare(jobs, enqueue(jobs))
    jobs.recover_jobs(previous_writers_retired=True)
    await record_terminal(disk_manager.db)
    from server.wiki.jobs import worker

    previous = worker.manager
    worker.manager = disk_manager
    control = {"operation_id": "verify-original", "expected_attempt": reference["attempt"],
               "expected_vault_id": jobs.registration["vault_id"]}
    try:
        for _ in range(2):
            result = await client.post(f"/api/wiki/jobs/{job['id']}/verify", json=control, headers=client.headers)
            assert result.status_code == 200, result.text
            assert result.json()["attempt"] == reference["attempt"]
            assert result.json()["items"][0]["status"] == "attention"
        assert jobs.get(job["id"])["items"][0]["worker_request"]["terminal"]["request_id"] == reference["request_id"]
        assert disk_manager.turns == disk_manager.sessions == 0
    finally:
        worker.manager = previous


async def test_verify_keeps_completed_item_from_prior_attempt_while_verifying_new_work(jobs, disk_manager):
    from server.wiki.history import WikiHistory
    from server.wiki.tasks import tasks
    from tests.test_wiki_jobs import Curator

    WikiHistory(jobs.store).write("Library/note.md", b"# Note\nKeep the original.\n", None, "human", "create")
    WikiHistory(jobs.store).write("Library/other.md", b"# Other\nKeep the original.\n", None, "human", "create")
    job = jobs.maintain("mixed-attempts", "", "Review both", ["Library/note.md", "Library/other.md"], None, [], "human")
    job = jobs.update(job["id"], status="running")
    old = job_execution.prepare(jobs, job, job["items"][:1], "original-worker", "original-request")
    tasks.assign(old["session_id"], job["id"], job["items"][:1], registration=jobs.registration,
                 request_id=old["request_id"], attempt=old["attempt"])
    try:
        async for _ in Curator(jobs)._events(old["session_id"], "Publish", request_id=old["request_id"]):
            pass
    finally:
        tasks.unassign(old["session_id"])
    await record_terminal(disk_manager.db)
    job_execution.transition(jobs, job["id"], old, "settled", terminal={"request_id": old["request_id"], "status": "success"},
                             recording_state="committed", retired=True)
    job = jobs.get(job["id"])
    assert jobs.settle_item(job["id"], job["items"][0])
    jobs.update(job["id"], status="failed", items=job["items"], session_id=None)
    jobs.retry(job["id"])
    jobs.next_queued()
    job = jobs.get(job["id"])
    current = job_execution.prepare(jobs, job, job["items"][1:], "original-worker", "second-request")
    job_execution.transition(jobs, job["id"], current, "accepted", retired=True)
    await disk_manager.db.record_terminal(session_id="original-worker", request_id="second-request", seq=1,
                                          role="assistant", type="result", created_at="time",
                                          content={"request_id": "second-request", "status": "success"})
    jobs.update(job["id"], status="failed", session_id=None)
    verified = await jobs.verify(job["id"], disk_manager)
    assert verified["attempt"] == 2
    assert verified["items"][0]["status"] == "done"
    assert verified["items"][0]["worker_request"]["attempt"] == 1
    assert verified["items"][1]["worker_request"]["terminal"]["request_id"] == "second-request"
    assert disk_manager.turns == disk_manager.sessions == 0


async def test_cancel_verifies_replaced_request_from_same_attempt_without_transitioning_it(jobs, disk_manager):
    job, old = prepare(jobs, enqueue(jobs))
    await record_terminal(disk_manager.db)
    job_execution.transition(jobs, job["id"], old, "settled", recording_state="committed", retired=True)
    job = jobs.get(job["id"])
    current = job_execution.prepare(jobs, job, job["items"], "original-worker", "reread-request")
    job_execution.transition(jobs, job["id"], current, "accepted", retired=True)
    await disk_manager.db.record_terminal(session_id="original-worker", request_id="reread-request", seq=1,
                                          role="assistant", type="result", created_at="time",
                                          content={"request_id": "reread-request", "status": "cancelled"})
    jobs.mark_cancelled(job["id"])
    await jobs.settle_cancelled(job["id"], disk_manager)
    cancelled = jobs.get(job["id"])
    assert cancelled["settlement_phase"] == "confirmed"
    item = cancelled["items"][0]
    assert item["worker_request"]["terminal"]["request_id"] == "reread-request"
    assert item["previous_worker_requests"][0]["request_id"] == "original-request"
    assert disk_manager.turns == disk_manager.sessions == 0


async def test_verifying_failed_ingest_without_items_cannot_claim_completion(jobs, disk_manager):
    job = jobs.maintain("empty-failed", "# Input\nRetain this material.\n", "Review", [], None, [], "human")
    jobs.update(job["id"], status="failed", error="Ingest never completed")
    verified = await jobs.verify(job["id"], disk_manager)
    assert verified["status"] == "completed_with_attention"
    assert verified["error"] == "Ingest never completed"
    assert verified["items"] == []
    assert disk_manager.turns == disk_manager.sessions == 0


def test_scope_readmission_keeps_evidence_for_original_items_outside_new_scope(jobs):
    from server.wiki.history import WikiHistory
    from server.wiki.registry import registry

    WikiHistory(jobs.store).write("Library/other.md", b"# Other\nRetain the other original.\n", None, "human", "create")
    first = enqueue(jobs)
    job = jobs.maintain("finite-readmission", "", "Review both", [first["items"][0]["path"], "Library/other.md"], None, [], "human")
    running = jobs.update(job["id"], status="running")
    reference = job_execution.prepare(jobs, running, running["items"], "original", "both-original")
    job_execution.transition(jobs, job["id"], reference, "rejected", disposition="not_admitted", retired=True)
    old = jobs.update(job["id"], status="failed", session_id=None)
    registry.configure(jobs.user_id, jobs.store.root, "replacement-curator")
    from server.wiki.jobs import WikiJobs

    current = WikiJobs(jobs.user_id)
    accepted = current.readmit(job["id"], [first["items"][0]["path"]], operation_id="narrow-original",
                               expected_attempt=old["attempt"], expected_vault_id=current.registration["vault_id"])
    retained = next(item for item in accepted["items"] if item["path"] == "Library/other.md")
    assert retained["worker_request"]["request_id"] == "both-original"
    assert retained["status"] == "excluded"


async def test_retry_reacquires_only_creation_paths_inside_readmitted_scope(jobs):
    from server.wiki.jobs import WikiJobs
    from server.wiki.registry import registry

    job = jobs.maintain("narrow-creations", "", "Create the two explicit pages", [], None, [], "human",
                        creation={"purpose": "requested_knowledge_page", "paths": ["Library/keep.md", "Library/exclude.md"]})
    jobs.update(job["id"], status="failed")
    registry.configure(jobs.user_id, jobs.store.root, "replacement-curator")
    current = WikiJobs(jobs.user_id)
    readmitted = current.readmit(job["id"], [], creation={"purpose": "requested_knowledge_page", "paths": ["Library/keep.md"]})
    excluded = next(item for item in readmitted["items"] if item["path"] == "Library/exclude.md")
    assert excluded["status"] == "excluded"
    await current.cancel(job["id"], None)
    await current.settle_cancelled(job["id"], None)
    retried = current.retry(job["id"])
    reservations = {row["path"]: row["state"] for row in retried["grant"]["reservations"]}
    assert reservations == {"Library/keep.md": "reserved", "Library/exclude.md": "released"}
    assert [item["path"] for item in retried["items"] if item["status"] != "excluded"] == ["Library/keep.md"]
    assert all(not current.store.path(path).exists() for path in reservations)
