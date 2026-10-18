"""Ledger mutations and stale calculations preserve projection input barriers."""

import json
import sqlite3

import pytest

from server.config import settings
from server.wiki import maintenance_inputs as inputs
from server.wiki.jobs import WikiJobs
from server.wiki.registry import registry
from server.wiki.store import WikiError


@pytest.fixture
def prepared(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "wiki_dir", str(tmp_path / "state"))
    root = tmp_path / "vault"
    (root / "Library").mkdir(parents=True)
    for name in ("a", "b", "c"):
        (root / f"Library/{name}.md").write_text(f"# {name}\nCurrent words.\n")
    registry.configure(None, root, "curator")
    jobs = WikiJobs(None)
    docs = {name: jobs.store.read(f"Library/{name}.md") for name in ("a", "b", "c")}
    try:
        yield jobs, docs
    finally:
        registry.close()


def enqueue(jobs, doc, key="work", **fields):
    return jobs.enqueue(key, {"kind": "curation"}, [{"doc": doc["id"], "path": doc["path"],
                        "revision": doc["revision"], "status": "pending", **fields}])


def captured(store, identifier):
    with store.connect() as db:
        db.execute("BEGIN")
        return inputs.capture_documents(db, [identifier])


def summary(doc):
    return {"path": doc["path"], "candidate": True, "status": "unreviewed",
            "summary": {"current": {key: doc[key] for key in ("id", "path", "revision")}}, "accepted_paths": []}


def publish(store, token, doc):
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        return inputs.publish_documents(db, token, {doc["id"]: summary(doc)})


def members(store, job):
    with store.connect() as db:
        return {row[0] for row in db.execute(
            "SELECT doc_id FROM maintenance_job_members WHERE job_id=?", (job["id"],))}


def receipt(db, job, source, target):
    db.execute("INSERT INTO operation_receipts VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
               ("receipt", job["id"], source["id"], target["id"], target["path"], source["revision"],
                target["revision"], "no_change", "test", None, "now", "{}"))


def test_new_job_blocks_old_document_publication_before_normalization(prepared):
    jobs, docs = prepared
    before = captured(jobs.store, docs["a"]["id"])
    job = enqueue(jobs, docs["a"])
    assert docs["a"]["id"] in members(jobs.store, job)
    with jobs.store.connect() as db:
        assert not inputs.ready(db, [docs["a"]["id"]])
        assert db.execute("SELECT count(*) FROM maintenance_job_items").fetchone()[0] == 0
    assert not publish(jobs.store, before, docs["a"])
    assert captured(jobs.store, docs["a"]["id"]) is None
    assert inputs.normalize(jobs.store)
    assert captured(jobs.store, docs["a"]["id"])["tokens"] != before["tokens"]


def test_unrelated_dirty_job_does_not_block_or_retoken_another_document(prepared):
    jobs, docs = prepared
    before = captured(jobs.store, docs["b"]["id"])
    enqueue(jobs, docs["a"])
    assert captured(jobs.store, docs["b"]["id"]) == before
    assert publish(jobs.store, before, docs["b"])
    with jobs.store.connect() as db:
        assert db.execute("SELECT count(*) FROM maintenance_job_dirty").fetchone()[0] == 1


def test_normalizer_cannot_publish_an_old_map_over_a_new_job_token(prepared):
    jobs, docs = prepared
    job = enqueue(jobs, docs["a"])
    with jobs.store.connect() as db:
        db.execute("BEGIN")
        old = inputs.capture_jobs(db, [docs["a"]["id"]])
    job["items"][0]["doc"] = docs["b"]["id"]
    jobs.update(job["id"], items=job["items"])
    assert captured(jobs.store, docs["b"]["id"]) is None
    with jobs.store.connect() as db:
        before = db.execute("SELECT cause_generation FROM maintenance_dirty WHERE doc_id=?", (docs["b"]["id"],)).fetchone()[0]
        db.execute("BEGIN IMMEDIATE")
        assert not inputs.publish_jobs(db, old)
        assert db.execute("SELECT count(*) FROM maintenance_job_items").fetchone()[0] == 0
    assert inputs.normalize(jobs.store, [docs["b"]["id"]])
    assert captured(jobs.store, docs["b"]["id"])["tokens"] == {docs["b"]["id"]: before}
    assert members(jobs.store, job) == {docs["b"]["id"]}


@pytest.mark.parametrize("value,exact", [(True, 1), (1, 0), ("true", 0), (None, 0)])
@pytest.mark.parametrize("failure,truth", [({}, 0), ({"opaque": "private"}, 1), ([], 0), ("failure", 1)])
def test_normalization_preserves_exact_true_and_python_failure_truth(prepared, value, exact, failure, truth):
    jobs, docs = prepared
    enqueue(jobs, docs["a"], index_complete=value, recording_failure=failure,
            index_recovery=failure, output_revision={"bad_type": True}, receipt_id=failure)
    assert inputs.normalize(jobs.store, [docs["a"]["id"]])
    with jobs.store.connect() as db:
        row = dict(db.execute("SELECT * FROM maintenance_job_items").fetchone())
    assert row["index_complete"] == exact
    assert row["recording_failure"] == row["index_recovery"] == row["receipt_required"] == truth
    assert row["output_revision_valid"] == 0
    assert "private" not in json.dumps(row)


def test_receipt_retarget_invalidates_old_and_new_exact_outputs_without_path_lookup(prepared):
    jobs, docs = prepared
    job = enqueue(jobs, docs["a"])
    with jobs.store.connect() as db:
        receipt(db, job, docs["a"], docs["b"])
    assert inputs.normalize(jobs.store)
    old = captured(jobs.store, docs["b"]["id"])
    current = captured(jobs.store, docs["c"]["id"])
    with jobs.store.connect() as db:
        db.execute("UPDATE operation_receipts SET doc_id=?,path=? WHERE id='receipt'", (docs["c"]["id"], docs["b"]["path"]))
    assert members(jobs.store, job) == {docs["a"]["id"], docs["c"]["id"]}
    assert captured(jobs.store, docs["b"]["id"])["tokens"] != old["tokens"]
    assert captured(jobs.store, docs["c"]["id"]) is None
    assert inputs.normalize(jobs.store)
    assert captured(jobs.store, docs["c"]["id"])["tokens"] != current["tokens"]


def test_publication_retention_retarget_and_delete_keep_old_members_invalidated(prepared):
    jobs, docs = prepared
    job = enqueue(jobs, docs["a"])
    with jobs.store.connect() as db:
        db.execute("INSERT INTO changes VALUES('change',?,'before','after','worker','test','applied','now')", (docs["b"]["path"],))
        db.execute("INSERT INTO change_retention VALUES('change',?,1,1,NULL)", (docs["b"]["id"],))
        db.execute("INSERT INTO job_publications VALUES(?,?,?,?,?,?,1)",
                   (job["id"], docs["a"]["id"], docs["a"]["revision"], "change", docs["b"]["path"], docs["b"]["revision"]))
    assert inputs.normalize(jobs.store)
    before = captured(jobs.store, docs["b"]["id"])
    with jobs.store.connect() as db:
        db.execute("UPDATE change_retention SET document_id=? WHERE change_id='change'", (docs["c"]["id"],))
    assert docs["b"]["id"] not in members(jobs.store, job)
    assert docs["c"]["id"] in members(jobs.store, job)
    assert captured(jobs.store, docs["b"]["id"])["tokens"] != before["tokens"]
    assert inputs.normalize(jobs.store)
    before = captured(jobs.store, docs["c"]["id"])
    with jobs.store.connect() as db:
        db.execute("DELETE FROM job_publications WHERE change_id='change'")
    assert members(jobs.store, job) == {docs["a"]["id"]}
    assert captured(jobs.store, docs["c"]["id"])["tokens"] != before["tokens"]


def test_source_binding_confirmation_and_target_move_invalidate_owner_without_edge_rewrite(prepared):
    jobs, docs = prepared
    with jobs.store.connect() as db:
        db.execute("INSERT INTO source_bindings VALUES('binding',?,'selector','{}','Library','unresolved',NULL,'{}')", (docs["a"]["id"],))
    before = captured(jobs.store, docs["a"]["id"])
    with jobs.store.connect() as db:
        db.execute("UPDATE source_bindings SET policy='bound',target_doc=? WHERE id='binding'", (docs["b"]["id"],))
    assert not publish(jobs.store, before, docs["a"])
    before = captured(jobs.store, docs["a"]["id"])
    with jobs.store.connect() as db:
        db.execute("UPDATE documents SET path='Library/moved.md' WHERE id=?", (docs["b"]["id"],))
    assert not publish(jobs.store, before, docs["a"])


def test_job_delete_removes_members_and_normalized_items_but_dirties_old_outputs(prepared):
    jobs, docs = prepared
    job = enqueue(jobs, docs["a"])
    with jobs.store.connect() as db:
        receipt(db, job, docs["a"], docs["b"])
    inputs.normalize(jobs.store)
    before = captured(jobs.store, docs["b"]["id"])
    with jobs.store.connect() as db:
        db.execute("DELETE FROM jobs WHERE id=?", (job["id"],))
        assert db.execute("SELECT count(*) FROM maintenance_job_members").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM maintenance_job_items").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM maintenance_job_dirty").fetchone()[0] == 0
    assert captured(jobs.store, docs["b"]["id"])["tokens"] != before["tokens"]


@pytest.mark.parametrize("raw", ["invalid", "{}", "[null]", '[{"doc":1}]',
                                '[{"doc":"first","doc":"second"}]',
                                r'[{"doc":"first","\u0064oc":"second"}]',
                                '[{"doc":"first","recording_failure":{"x":1,"x":2}}]'])
def test_malformed_direct_job_mutation_aborts_without_hiding_members(prepared, raw):
    jobs, docs = prepared
    job = enqueue(jobs, docs["a"])
    assert inputs.normalize(jobs.store)
    before = captured(jobs.store, docs["a"]["id"])
    with pytest.raises(sqlite3.IntegrityError):
        with jobs.store.connect() as db:
            db.execute("UPDATE jobs SET items=? WHERE id=?", (raw, job["id"]))
    assert captured(jobs.store, docs["a"]["id"]) == before
    assert members(jobs.store, job) == {docs["a"]["id"]}


def test_derived_loss_changes_rebuild_nonce_and_cannot_accept_old_calculation(prepared):
    jobs, docs = prepared
    job = enqueue(jobs, docs["a"])
    inputs.normalize(jobs.store)
    before = captured(jobs.store, docs["a"]["id"])
    original_job = jobs.get(job["id"])
    with jobs.store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute("DROP TABLE maintenance_job_items")
        assert inputs.initialize(db)
    assert not publish(jobs.store, before, docs["a"])
    assert jobs.get(job["id"]) == original_job
    assert inputs.normalize(jobs.store)
    assert publish(jobs.store, captured(jobs.store, docs["a"]["id"]), docs["a"])


@pytest.mark.parametrize("missing", ["jobs", "job_requests"])
def test_initialized_job_ledger_loss_is_never_recreated_empty(prepared, missing):
    jobs, docs = prepared
    enqueue(jobs, docs["a"])
    with jobs.store.connect() as db:
        db.execute("DROP TABLE " + missing)
    with pytest.raises(WikiError, match="job ledger") as unavailable:
        WikiJobs(None)
    assert unavailable.value.status == 503
    with pytest.raises(WikiError, match="job ledger"):
        with jobs.store.connect() as db:
            inputs.ready(db, [docs["a"]["id"]])
    with jobs.store.connect() as db:
        assert missing not in inputs.names(db)


def test_missing_initialized_input_protocol_fails_without_cache_repair(prepared):
    jobs, _ = prepared
    with jobs.store.connect() as db:
        original = inputs.state(db)
        db.execute("DELETE FROM wiki_protocols WHERE name='maintenance_inputs'")
    with pytest.raises(WikiError, match="protocol ledger"):
        with jobs.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            inputs.initialize(db)
    with jobs.store.connect() as db:
        assert inputs.state(db) == original
    with pytest.raises(WikiError, match="protocol ledger"):
        with jobs.store.connect() as db:
            inputs.ready(db, [])
