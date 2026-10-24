"""Item-match proofs survive normalization without becoming job outcomes."""

import pytest

from server.wiki import maintenance_inputs as inputs
from server.wiki.maintenance_state import status
from tests.test_wiki_maintenance_inputs import enqueue, receipt
from tests.test_wiki_maintenance_inputs import prepared as prepared


def observed(jobs, doc):
    return status(jobs.store, jobs.registration, jobs.user_id, doc=doc["id"])["items"][0]


def event_facts(db):
    return {table: list(map(tuple, db.execute(f"SELECT * FROM {table} ORDER BY rowid")))
            for table in ("wiki_job_state", "wiki_job_outbox")}


def test_review_item_match_is_persisted_without_a_new_job_event(prepared):
    jobs, docs = prepared
    doc = docs["a"]
    job = enqueue(jobs, doc, status="done", index_complete=True,
                  output=doc["path"], output_revision=doc["revision"])
    with jobs.store.connect() as db:
        receipt(db, job, doc, doc)
        original_events = event_facts(db)
        original_sequence = db.execute("SELECT rowid FROM jobs WHERE id=?", (job["id"],)).fetchone()[0]
        assert original_sequence > 0

    current = observed(jobs, doc)
    assert current["status"] == "unchanged" and current["baseline"]["id"] == "receipt"
    with jobs.store.connect() as db:
        proof = dict(db.execute("SELECT * FROM operation_receipts WHERE id='receipt'").fetchone())
        assert proof.get("item_match_sequence") == original_sequence, "Accepted item matching must persist with its receipt"
        assert event_facts(db) == original_events
        assert not db.execute("SELECT 1 FROM maintenance_job_dirty WHERE job_id=?", (job["id"],)).fetchone()

    assert observed(jobs, doc)["review"] == current["review"]
    with jobs.store.connect() as db:
        assert event_facts(db) == original_events


def test_receipt_mutation_after_capture_rejects_stale_item_matching(prepared):
    jobs, docs = prepared
    doc = docs["a"]
    job = enqueue(jobs, doc, status="done", index_complete=True,
                  output=doc["path"], output_revision=doc["revision"])
    with jobs.store.connect() as db:
        receipt(db, job, doc, doc)
    with jobs.store.connect() as db:
        db.execute("BEGIN")
        captured = inputs.capture_jobs(db, [doc["id"]])
    assert [entry["job_id"] for entry in captured["jobs"]] == [job["id"]]
    with jobs.store.connect() as db:
        db.execute("UPDATE operation_receipts SET output_revision='not-the-item-output' WHERE id='receipt'")
    with jobs.store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        assert not inputs.publish_jobs(db, captured)

    assert inputs.normalize(jobs.store, [doc["id"]])
    current = observed(jobs, doc)
    assert current["status"] == "unknown" and current["baseline"] is None
    with jobs.store.connect() as db:
        proof = dict(db.execute("SELECT * FROM operation_receipts WHERE id='receipt'").fetchone())
        assert proof.get("item_match_sequence") is None


def test_live_item_that_no_longer_matches_revokes_its_old_receipt_proof(prepared):
    jobs, docs = prepared
    doc = docs["a"]
    job = enqueue(jobs, doc, status="done", index_complete=True,
                  output=doc["path"], output_revision=doc["revision"])
    with jobs.store.connect() as db:
        receipt(db, job, doc, doc)
    assert observed(jobs, doc)["status"] == "unchanged"
    job["items"][0]["status"] = "attention"
    jobs.update(job["id"], status="completed_with_attention", items=job["items"])
    current = observed(jobs, doc)
    assert current["baseline"] is None and current["status"] == "unknown"
    assert current["attempt_count"] == 1
    with jobs.store.connect() as db:
        assert db.execute("SELECT item_match_sequence FROM operation_receipts WHERE id='receipt'").fetchone()[0] is None


@pytest.mark.parametrize("action", ["delete", "replace"])
def test_ordinary_job_removal_cannot_preserve_a_retirement_proof(prepared, action):
    jobs, docs = prepared
    doc = docs["a"]
    job = enqueue(jobs, doc, status="done", index_complete=True,
                  output=doc["path"], output_revision=doc["revision"])
    with jobs.store.connect() as db:
        receipt(db, job, doc, doc)
    assert observed(jobs, doc)["status"] == "unchanged"
    with jobs.store.connect() as db:
        original = db.execute("SELECT item_match_sequence FROM operation_receipts WHERE id='receipt'").fetchone()[0]
        assert original is not None
        if action == "delete":
            db.execute("DELETE FROM jobs WHERE id=?", (job["id"],))
        else:
            db.execute("""INSERT OR REPLACE INTO jobs(
                id,request_key,payload_hash,payload,status,items,error,session_id,created_at,updated_at,attempt_errors)
                SELECT id,request_key,payload_hash,payload,status,items,error,session_id,created_at,updated_at,attempt_errors
                FROM jobs WHERE id=?""", (job["id"],))
        assert db.execute("SELECT item_match_sequence FROM operation_receipts WHERE id='receipt'").fetchone()[0] is None
    current = observed(jobs, doc)
    if action == "delete":
        assert current["status"] == "unknown" and current["baseline"] is None
    else:
        assert current["status"] == "unchanged" and current["baseline"]["id"] == "receipt"
        with jobs.store.connect() as db:
            assert db.execute("SELECT item_match_sequence FROM operation_receipts WHERE id='receipt'").fetchone()[0] > original


def test_admission_order_is_not_reused_after_reverse_replacement_and_deletion(prepared):
    jobs, docs = prepared
    first = enqueue(jobs, docs["a"], "first")
    second = enqueue(jobs, docs["b"], "second")
    with jobs.store.connect() as db:
        previous = db.execute("SELECT max(admission_sequence) FROM jobs").fetchone()[0]
        key = db.execute("SELECT request_key FROM jobs WHERE id=?", (second["id"],)).fetchone()[0]
        db.execute("UPDATE OR REPLACE jobs SET request_key=? WHERE id=?", (key, first["id"]))
        assert db.execute("SELECT id FROM jobs").fetchall()[0][0] == first["id"]
    third = enqueue(jobs, docs["c"], "third")
    with jobs.store.connect() as db:
        current = db.execute("SELECT admission_sequence FROM jobs WHERE id=?", (third["id"],)).fetchone()[0]
        assert current > previous
        db.execute("DELETE FROM jobs")
    fourth = enqueue(jobs, docs["a"], "fourth")
    with jobs.store.connect() as db:
        assert db.execute("SELECT admission_sequence FROM jobs WHERE id=?", (fourth["id"],)).fetchone()[0] > current
