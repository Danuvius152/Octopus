"""A job change and its observable version must commit as one durable fact."""

import json

import pytest

from server.wiki import job_events
from server.wiki.history import WikiHistory
from server.wiki.jobs import WikiWorker
from tests.test_wiki_jobs import client as client
from tests.test_wiki_jobs import jobs as jobs


def enqueue(jobs, key="event-contract"):
    WikiHistory(jobs.store).write(
        "Library/note.md", b"# Note\nKeep this argument and its condition.\n",
        None, "human", "Create isolated input",
    )
    return jobs.maintain(key, "", "Review existing note", ["Library/note.md"], None, [], "human")


@pytest.mark.parametrize("legacy", [
    {"recording_failure": {"attempt": 1, "session_id": "old", "request_id": "original"}},
    {"recording_failure": "1"}, {"index_recovery": True}, {"recording_recovery": "true"},
    {"execution_unknown": [1]}, {"execution_unknown": []}, {"status": "running"}, {"status": None},
    {"recording_failure": {"attempt": 1, "session_id": "old", "request_id": "original"},
     "worker_request": {"attempt": 2, "session_id": "new", "request_id": "replacement", "phase": "settled"}},
])
async def test_legacy_notification_projection_matches_codec_without_rewriting_jobs(jobs, legacy):
    job = enqueue(jobs)
    item = job["items"][0] | legacy
    item.pop("version")
    raw = json.dumps([item])
    with jobs.store.connect() as db:
        db.execute("UPDATE jobs SET items=? WHERE id=?", (raw, job["id"]))
        event = dict(db.execute("SELECT * FROM wiki_job_outbox WHERE job_id=? ORDER BY state_version DESC LIMIT 1",
                                (job["id"],)).fetchone())
    current = jobs.get(job["id"])
    snapshot = json.loads(event["body"])
    assert snapshot["progress"] == current["progress"]
    reference = current["items"][0].get("worker_request", {})
    expected_pending = int(bool(reference.get("request_id")) and reference.get("phase") not in {"settled", "rejected"})
    assert snapshot["recording_pending"] == expected_pending
    jobs.recover_jobs()
    with jobs.store.connect() as db:
        assert db.execute("SELECT items FROM jobs WHERE id=?", (job["id"],)).fetchone()[0] == raw
    assert jobs.get(job["id"])["state_version"] == current["state_version"]


async def test_item_progress_changes_version_even_while_job_remains_running(jobs):
    job = enqueue(jobs)
    assert job["state_version"] >= 1
    running = jobs.update(job["id"], status="running")
    assert running["attempt"] == 1
    assert running["state_version"] == job["state_version"] + 1
    running["items"][0]["index_recovery"] = True
    progressed = jobs.update(job["id"], items=running["items"])
    assert progressed["status"] == "running"
    assert progressed["attempt"] == running["attempt"]
    assert progressed["state_version"] == running["state_version"] + 1


async def test_same_state_and_same_submission_do_not_create_new_versions(jobs):
    job = enqueue(jobs)
    before = job["state_version"]
    repeated = jobs.maintain("event-contract", "", "Review existing note",
                            ["Library/note.md"], None, [], "human")
    unchanged = jobs.update(job["id"], status=job["status"], items=job["items"], error=None)
    assert repeated["id"] == unchanged["id"] == job["id"]
    assert repeated["state_version"] == unchanged["state_version"] == before


async def test_outbox_is_created_and_rolled_back_with_the_job_transition(jobs):
    job = enqueue(jobs)
    with jobs.store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute("UPDATE jobs SET status='running' WHERE id=?", (job["id"],))
        state = db.execute("SELECT * FROM wiki_job_state WHERE job_id=?", (job["id"],)).fetchone()
        assert state["state_version"] == job["state_version"] + 1
        event = db.execute("SELECT * FROM wiki_job_outbox WHERE job_id=? AND state_version=?",
                           (job["id"], state["state_version"])).fetchone()
        snapshot = json.loads(event["body"])
        assert snapshot["status"] == "running"
        assert snapshot["attempt"] == 1
        assert snapshot["job_id"] == job["id"]
        assert "payload" not in snapshot and "material" not in snapshot
        db.rollback()
    restored = jobs.get(job["id"])
    assert restored["status"] == job["status"]
    assert restored["state_version"] == job["state_version"]
    with jobs.store.connect() as db:
        assert db.execute("SELECT 1 FROM wiki_job_outbox WHERE job_id=? AND state_version>?",
                          (job["id"], job["state_version"])).fetchone() is None


async def test_receipt_becomes_observable_without_faking_item_completion(jobs):
    job = enqueue(jobs)
    item = job["items"][0]
    with jobs.store.connect() as db:
        db.execute("""INSERT INTO operation_receipts VALUES(
            'event-receipt',?,?,?,?,?,?,'no_change','Verified current bytes',NULL,'time','{}')""",
                   (job["id"], item["doc"], item["doc"], item["path"], item["revision"], item["revision"]))
    observed = jobs.get(job["id"])
    assert observed["state_version"] == job["state_version"] + 1
    assert observed["items"][0]["status"] == "pending"
    assert observed["status"] == job["status"]
    with jobs.store.connect() as db:
        event = db.execute("SELECT body FROM wiki_job_outbox WHERE job_id=? ORDER BY state_version DESC LIMIT 1", (job["id"],)).fetchone()
    snapshot = json.loads(event[0])
    assert snapshot["effects"] == {"recorded": 1, "applied": 1, "index_pending": 0}
    assert snapshot["outcome_refs"] == [{"doc_id": item["doc"], "path": item["path"], "revision": item["revision"]}]
    assert snapshot["progress"]["completed"] == 0


async def test_repeated_index_fact_does_not_create_another_version(jobs):
    job = enqueue(jobs)
    item = job["items"][0]
    with jobs.store.connect() as db:
        change = db.execute("SELECT id FROM changes WHERE path=? AND status='applied' ORDER BY rowid DESC LIMIT 1", (item["path"],)).fetchone()[0]
        db.execute("INSERT INTO job_publications VALUES(?,?,?,?,?,?,1)", (
            job["id"], item["doc"], item["revision"], change, item["path"], item["revision"],
        ))
    before = jobs.get(job["id"])["state_version"]
    with jobs.store.connect() as db:
        db.execute("UPDATE job_publications SET indexed=1 WHERE change_id=?", (change,))
    assert jobs.get(job["id"])["state_version"] == before
    with jobs.store.connect() as db:
        db.execute("UPDATE job_publications SET indexed=0 WHERE change_id=?", (change,))
    assert jobs.get(job["id"])["state_version"] == before + 1


async def test_queued_cancellation_is_confirmed_without_a_model_or_writer(jobs):
    job = enqueue(jobs)
    cancelled = await jobs.cancel(job["id"], None)
    assert cancelled["status"] == "cancelled"
    await jobs.settle_cancelled(job["id"], None)
    cancelled = jobs.get(job["id"])
    assert cancelled["settlement_phase"] == "confirmed"
    assert cancelled["state_version"] > job["state_version"]


async def test_late_unconfirmed_observation_cannot_regress_confirmed_cancellation(jobs):
    job = enqueue(jobs)
    await jobs.cancel(job["id"], None)
    await jobs.settle_cancelled(job["id"], None)
    confirmed = jobs.get(job["id"])
    jobs._record_cancellation_phase(confirmed, "attention")
    current = jobs.get(job["id"])
    assert current["settlement_phase"] == "confirmed"
    assert current["state_version"] == confirmed["state_version"]


async def test_cancelled_execution_is_not_final_while_the_writer_is_owned(jobs):
    job = enqueue(jobs)
    jobs.update(job["id"], status="running")
    key = (str(jobs.store.state.resolve()), job["id"])
    owner = object()
    WikiWorker.executing[key] = owner
    try:
        cancelled = await jobs.cancel(job["id"], None)
        assert cancelled["status"] == "cancelled"
        assert cancelled["settlement_phase"] == "pending"
    finally:
        assert WikiWorker.executing.pop(key) is owner


async def test_retry_of_completed_job_returns_its_existing_result(jobs):
    job = enqueue(jobs)
    completed = jobs.update(job["id"], status="completed")
    replayed = jobs.retry(job["id"])
    assert replayed["id"] == completed["id"]
    assert replayed["status"] == "completed"
    assert replayed["state_version"] == completed["state_version"]


async def test_late_subscriber_gets_current_version_after_prior_delivery_was_acked(jobs):
    job = enqueue(jobs)
    with jobs.store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        first = job_events.subscribe(db, job["id"], job_events.Requester(None, "chat", "original", "request-1"))
    completed = jobs.update(job["id"], status="completed")
    with jobs.store.connect() as db:
        for event in job_events.pending(db):
            assert event["subscriber_id"] == first
            job_events.acknowledge(db, event["event_id"], first, "delivered")
        assert not job_events.pending(db)
    with jobs.store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        following = job_events.subscribe(db, job["id"], job_events.Requester(None, "chat", "following", "request-2"))
    with jobs.store.connect() as db:
        pending = job_events.pending(db)
        assert len(pending) == 1
        assert pending[0]["subscriber_id"] == following
        assert pending[0]["state_version"] == completed["state_version"]
        assert json.loads(pending[0]["body"])["status"] == "completed"
    assert jobs.get(job["id"])["state_version"] == completed["state_version"]


async def test_same_chat_subscribes_once_even_from_a_later_request(jobs):
    job = enqueue(jobs)
    identifiers = []
    for request in ("request-1", "request-2", "request-1"):
        with jobs.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            identifiers.append(job_events.subscribe(
                db, job["id"], job_events.Requester(None, "chat", "original", request)))
    assert len(set(identifiers)) == 1
    with jobs.store.connect() as db:
        assert db.execute("SELECT count(*) FROM wiki_job_subscribers").fetchone()[0] == 1
        assert len(job_events.pending(db)) == 1
    assert jobs.get(job["id"])["state_version"] == job["state_version"]


async def test_event_snapshot_limits_outputs_and_keeps_the_full_job_evidence(jobs):
    job = enqueue(jobs)
    items = [job["items"][0] | {"status": "done", "output": f"Library/result-{number}.md", "output_revision": "r"}
             for number in range(200)]
    updated = jobs.update(job["id"], items=items)
    with jobs.store.connect() as db:
        body = db.execute("SELECT body FROM wiki_job_outbox WHERE job_id=? AND state_version=?",
                          (job["id"], updated["state_version"])).fetchone()[0]
    snapshot = json.loads(body)
    assert len(snapshot["outcome_refs"]) == 10
    assert snapshot["outcome_count"] == 200
    assert snapshot["outcome_refs_truncated"]
    assert len(body.encode()) < 24000
    assert len(jobs.get(job["id"])["items"]) == 200
