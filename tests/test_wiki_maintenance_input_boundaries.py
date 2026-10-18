"""Replacement, read snapshots and combined cache repair retain input proofs."""

import sqlite3

import pytest

from server.wiki import maintenance_inputs as inputs
from server.wiki import schema, source_bindings
from server.wiki.event_inbox import EventInbox
from server.wiki.registry import registry
from tests.test_wiki_maintenance_inputs import (
    captured,
    enqueue,
    members,
    publish,
    receipt,
    summary,
)
from tests.test_wiki_maintenance_inputs import (
    prepared as prepared,
)


def facts(store):
    tables = ["jobs", "job_requests", "operation_receipts", "job_publications",
              "changes", "change_retention", "change_management", "wiki_protocols",
              *source_bindings.TABLES]
    with store.connect() as db:
        return {table: sorted(map(tuple, db.execute("SELECT * FROM " + table)), key=repr)
                for table in tables}


def projection_state(store):
    with store.connect() as db:
        return {table: sorted(map(tuple, db.execute("SELECT * FROM " + table)), key=repr)
                for table in inputs.TABLES}


def replace_receipt(db, job, source, output):
    db.execute("""INSERT OR REPLACE INTO operation_receipts
                  VALUES('receipt',?,?,?,?,?,?,'no_change','replacement',NULL,'later','{}')""",
               (job["id"], source["id"], output["id"], output["path"],
                source["revision"], output["revision"]))


@pytest.mark.parametrize("rollback", [False, True])
def test_review_receipt_replacement_tracks_both_jobs_and_rolls_back(prepared, rollback):
    jobs, docs = prepared
    first = enqueue(jobs, docs["a"], "first")
    second = enqueue(jobs, docs["c"], "second")
    with jobs.store.connect() as db:
        receipt(db, first, docs["a"], docs["b"])
    assert inputs.normalize(jobs.store)
    old = captured(jobs.store, docs["b"]["id"])
    original_facts, original_projection = facts(jobs.store), projection_state(jobs.store)
    with jobs.store.connect() as db:
        # REPLACE must also be correct on explicitly supported direct writers.
        db.execute("PRAGMA recursive_triggers=ON")
        db.execute("BEGIN IMMEDIATE")
        replace_receipt(db, second, docs["c"], docs["a"])
        assert not inputs.publish_documents(db, old, {docs["b"]["id"]: summary(docs["b"])})
        assert not db.execute("SELECT 1 FROM maintenance_job_members WHERE job_id=? AND doc_id=?",
                              (first["id"], docs["b"]["id"])).fetchone()
        assert db.execute("SELECT 1 FROM maintenance_job_members WHERE job_id=? AND doc_id=?",
                          (second["id"], docs["a"]["id"])).fetchone()
        assert not inputs.ready(db, [docs["a"]["id"], docs["c"]["id"]])
        if rollback:
            db.rollback()
    if rollback:
        assert facts(jobs.store) == original_facts
        assert projection_state(jobs.store) == original_projection
        assert captured(jobs.store, docs["b"]["id"]) == old
    else:
        assert members(jobs.store, first) == {docs["a"]["id"]}
        assert inputs.normalize(jobs.store)
        assert not publish(jobs.store, old, docs["b"])
        assert captured(jobs.store, docs["b"]["id"])["tokens"] != old["tokens"]


@pytest.mark.parametrize("operation", ["insert", "update"])
@pytest.mark.parametrize("rollback", [False, True])
def test_review_request_key_replacement_removes_old_job_members(prepared, operation, rollback):
    jobs, docs = prepared
    first = enqueue(jobs, docs["a"], "first")
    second = enqueue(jobs, docs["c"], "second")
    with jobs.store.connect() as db:
        receipt(db, first, docs["a"], docs["b"])
    assert inputs.normalize(jobs.store)
    old = captured(jobs.store, docs["b"]["id"])
    original_facts, original_projection = facts(jobs.store), projection_state(jobs.store)
    with jobs.store.connect() as db:
        db.execute("PRAGMA recursive_triggers=ON")
        db.execute("BEGIN IMMEDIATE")
        key = db.execute("SELECT request_key FROM jobs WHERE id=?", (first["id"],)).fetchone()[0]
        if operation == "insert":
            db.execute("""INSERT OR REPLACE INTO jobs
                          SELECT id,?,payload_hash,payload,status,items,error,session_id,created_at,updated_at
                          FROM jobs WHERE id=?""", (key, second["id"]))
        else:
            db.execute("UPDATE OR REPLACE jobs SET request_key=? WHERE id=?", (key, second["id"]))
        for table in ("maintenance_job_members", "maintenance_job_items", "maintenance_job_dirty"):
            assert not db.execute("SELECT 1 FROM " + table + " WHERE job_id=?", (first["id"],)).fetchone()
        assert not inputs.publish_documents(db, old, {docs["b"]["id"]: summary(docs["b"])})
        if rollback:
            db.rollback()
    if rollback:
        assert facts(jobs.store) == original_facts
        assert projection_state(jobs.store) == original_projection
    else:
        assert not publish(jobs.store, old, docs["b"])
        assert members(jobs.store, second) == {docs["c"]["id"]}


@pytest.mark.parametrize("operation", ["receipt_replace", "job_replace", "job_update", "delete"])
def test_review_direct_writer_without_recursive_triggers_fails_atomically(prepared, operation):
    jobs, docs = prepared
    first = enqueue(jobs, docs["a"], "first")
    second = enqueue(jobs, docs["c"], "second")
    with jobs.store.connect() as db:
        receipt(db, first, docs["a"], docs["b"])
    assert inputs.normalize(jobs.store)
    original_facts, original_projection = facts(jobs.store), projection_state(jobs.store)
    with sqlite3.connect(jobs.store.db_path) as db:
        db.execute("PRAGMA recursive_triggers=OFF")
        with pytest.raises(sqlite3.IntegrityError, match="recursive triggers"):
            if operation == "receipt_replace":
                replace_receipt(db, second, docs["c"], docs["a"])
            elif operation == "job_replace":
                db.execute("""INSERT OR REPLACE INTO jobs
                              SELECT ?,request_key,payload_hash,payload,status,items,error,session_id,created_at,updated_at
                              FROM jobs WHERE id=?""", ("replacement", first["id"]))
            elif operation == "job_update":
                key = db.execute("SELECT request_key FROM jobs WHERE id=?", (first["id"],)).fetchone()[0]
                db.execute("UPDATE OR REPLACE jobs SET request_key=? WHERE id=?", (key, second["id"]))
            else:
                db.execute("DELETE FROM operation_receipts WHERE id='receipt'")
        # ABORT must undo the complete statement even when its exception is caught.
        db.commit()
    assert facts(jobs.store) == original_facts
    assert projection_state(jobs.store) == original_projection


def test_review_all_product_writers_enable_recursive_triggers(prepared):
    jobs, _ = prepared
    inbox = EventInbox(jobs.store.db_path, jobs.store.root, jobs.store.vault_id)
    for writer in (jobs.store, inbox):
        with writer.connect() as db:
            assert db.execute("PRAGMA recursive_triggers").fetchone()[0] == 1


@pytest.mark.parametrize("change", [{"recording_failure": "failed"}, {"index_complete": False}])
def test_review_capture_before_normalization_cannot_publish_after_it(prepared, change):
    jobs, docs = prepared
    job = enqueue(jobs, docs["a"], status="done", index_complete=True)
    assert inputs.normalize(jobs.store)
    job["items"][0].update(change)
    jobs.update(job["id"], items=job["items"])
    with jobs.store.connect() as db:
        db.execute("BEGIN")
        token = db.execute("SELECT cause_generation FROM maintenance_dirty WHERE doc_id=?", (docs["a"]["id"],)).fetchone()[0]
        assert db.execute("SELECT index_complete,recording_failure FROM maintenance_job_items WHERE job_id=?",
                          (job["id"],)).fetchone()[:] == (1, 0)
        old = inputs.capture_documents(db, [docs["a"]["id"]])
    assert old is None  # Updating is not a proof authorizing stale normalized input.
    assert inputs.normalize(jobs.store)
    assert not publish(jobs.store, old, docs["a"])
    fresh = captured(jobs.store, docs["a"]["id"])
    assert fresh["tokens"] == {docs["a"]["id"]: token}
    assert publish(jobs.store, fresh, docs["a"])


def seed_history(jobs, docs):
    job = enqueue(jobs, docs["a"])
    with jobs.store.connect() as db:
        receipt(db, job, docs["a"], docs["b"])
        db.execute("INSERT INTO changes VALUES('change',?,'before','after','worker','test','applied','now')", (docs["b"]["path"],))
        db.execute("INSERT INTO change_retention VALUES('change',?,1,1,NULL)", (docs["b"]["id"],))
        db.execute("INSERT INTO change_management VALUES('change',NULL,'{}')")
        db.execute("INSERT INTO job_publications VALUES(?,?,?,?,?,?,1)",
                   (job["id"], docs["a"]["id"], docs["a"]["revision"], "change", docs["b"]["path"], docs["b"]["revision"]))
    assert inputs.normalize(jobs.store)


@pytest.mark.parametrize("missing", [("maintenance_input_state", "search_fts"),
                                    ("maintenance_dirty", "wiki_edges")])
@pytest.mark.parametrize("entry", ["schema", "reopen"])
def test_review_combined_cache_loss_repairs_through_real_startup(prepared, missing, entry):
    jobs, docs = prepared
    seed_history(jobs, docs)
    old = captured(jobs.store, docs["a"]["id"])
    original = facts(jobs.store)
    with jobs.store.connect() as db:
        for table in missing:
            db.execute("DROP TABLE " + table)
    if entry == "schema":
        schema.initialize(jobs.store)
        store = jobs.store
    else:
        registry.close()
        registry.resume_access()
        store = registry.store(None)
    assert facts(store) == original
    assert not publish(store, old, docs["a"])
    assert inputs.normalize(store)
    fresh = captured(store, docs["a"]["id"])
    assert fresh["nonce"] != old["nonce"]
    assert publish(store, fresh, docs["a"])


def test_review_interrupted_combined_repair_rolls_back_schema_and_facts(prepared, monkeypatch):
    jobs, docs = prepared
    seed_history(jobs, docs)
    old = captured(jobs.store, docs["a"]["id"])
    original_facts = facts(jobs.store)
    with jobs.store.connect() as db:
        db.execute("DROP TABLE maintenance_dirty")
        db.execute("DROP TABLE wiki_edges")
        original_schema = list(map(tuple, db.execute("SELECT type,name,sql FROM sqlite_master ORDER BY type,name")))
    initialize = inputs.initialize

    def interrupted(db):
        initialize(db)
        raise RuntimeError("synthetic repair interruption")

    with monkeypatch.context() as patch:
        patch.setattr(inputs, "initialize", interrupted)
        with pytest.raises(RuntimeError, match="repair interruption"):
            schema.initialize(jobs.store)
    with jobs.store.connect() as db:
        assert list(map(tuple, db.execute("SELECT type,name,sql FROM sqlite_master ORDER BY type,name"))) == original_schema
        assert inputs.state(db)["rebuild_nonce"] == old["nonce"]
    assert facts(jobs.store) == original_facts
    schema.initialize(jobs.store)
    assert not publish(jobs.store, old, docs["a"])


def test_review_incompatible_owned_index_is_repaired(prepared):
    jobs, docs = prepared
    with jobs.store.connect() as db:
        db.execute("DROP INDEX maintenance_members_document")
        db.execute("CREATE INDEX maintenance_members_document ON maintenance_job_members(job_id)")
    schema.initialize(jobs.store)
    with jobs.store.connect() as db:
        assert [row[2] for row in db.execute("PRAGMA index_info(maintenance_members_document)")] == ["doc_id", "job_id"]
        plan = [row[3] for row in db.execute("EXPLAIN QUERY PLAN SELECT job_id FROM maintenance_job_members WHERE doc_id=?",
                                            (docs["a"]["id"],))]
        assert any("SEARCH" in step and "maintenance_members_document" in step for step in plan)


@pytest.mark.parametrize("missing", ["jobs", "maintenance_inputs"])
def test_review_combined_cache_loss_never_bypasses_durable_validation(prepared, missing):
    from server.wiki.store import WikiError

    jobs, docs = prepared
    seed_history(jobs, docs)
    with jobs.store.connect() as db:
        db.execute("DROP TABLE maintenance_input_state")
        db.execute("DROP TABLE search_fts")
        if missing == "jobs":
            db.execute("DROP TABLE jobs")
        else:
            db.execute("DELETE FROM wiki_protocols WHERE name='maintenance_inputs'")
        before = list(map(tuple, db.execute("SELECT type,name,sql FROM sqlite_master ORDER BY type,name")))
    with pytest.raises(WikiError) as unavailable:
        schema.initialize(jobs.store)
    assert unavailable.value.status == 503
    with jobs.store.connect() as db:
        assert list(map(tuple, db.execute("SELECT type,name,sql FROM sqlite_master ORDER BY type,name"))) == before


def test_review_healthy_schema_initialization_retains_nonce_and_clean_projection(prepared):
    jobs, docs = prepared
    seed_history(jobs, docs)
    assert publish(jobs.store, captured(jobs.store, docs["a"]["id"]), docs["a"])
    before = projection_state(jobs.store)
    schema.initialize(jobs.store)
    assert projection_state(jobs.store) == before
