"""Incremental maintenance reads retain evidence semantics and bounded warm cost."""

import re
from contextlib import contextmanager

import pytest

from server.wiki import maintenance_projection, schema, source_bindings
from server.wiki.maintenance_state import status
from server.wiki.store import WikiError
from tests.test_wiki_maintenance_inputs import enqueue, receipt
from tests.test_wiki_maintenance_inputs import prepared as prepared


def observe(jobs, doc=None, **kwargs):
    return status(jobs.store, jobs.registration, jobs.user_id, doc=doc, **kwargs)


def trace_reads(store, monkeypatch):
    statements = []
    connect = store.connect

    @contextmanager
    def traced():
        with connect() as db:
            db.set_trace_callback(statements.append)
            yield db

    monkeypatch.setattr(store, "connect", traced)
    return statements


def test_warm_page_does_not_reload_job_items_or_receipt_history(prepared, monkeypatch):
    jobs, docs = prepared
    for number in range(30):
        enqueue(jobs, docs["a"], "history-" + str(number))
    first = observe(jobs, limit=1)
    calls = trace_reads(jobs.store, monkeypatch)
    next_page = observe(jobs, limit=1, cursor=first["next_cursor"])
    assert next_page["ready"] and next_page["total"] == 3
    assert not any(re.search(r"\bFROM jobs\b.*\bORDER BY rowid\b", sql, re.I | re.S) for sql in calls)
    assert not any("FROM operation_receipts" in sql for sql in calls)


def test_single_clean_document_does_not_normalize_an_unrelated_job(prepared, monkeypatch):
    jobs, docs = prepared
    first = observe(jobs, docs["a"]["id"])["items"][0]
    job = enqueue(jobs, docs["b"])
    calls = trace_reads(jobs.store, monkeypatch)
    current = observe(jobs, docs["a"]["id"])["items"][0]
    assert current["review"] == first["review"] and current["actionable"]
    assert not any(re.search(r"SELECT .*\bitems\b.*FROM jobs\b", sql, re.I | re.S) for sql in calls)
    with jobs.store.connect() as db:
        assert db.execute("SELECT 1 FROM maintenance_job_dirty WHERE job_id=?", (job["id"],)).fetchone()


def test_review_digest_uses_current_binding_policy_without_resolver_writes(prepared):
    jobs, docs = prepared
    jobs.store.path(docs["a"]["path"]).write_text("---\nsources:\n  - resource: absent.md\n    revision: missing-version\n    location: L1\n---\n# A\n")
    jobs.store.refresh()
    first = observe(jobs, docs["a"]["id"])["items"][0]
    with jobs.store.connect() as db:
        active = source_bindings.current(db, docs["a"]["id"])
        binding = active["entries"][0]["binding_id"]
        assert db.execute("SELECT policy FROM source_bindings WHERE id=?", (binding,)).fetchone()[0] == "unresolved"
        db.execute("UPDATE source_bindings SET policy='unknown' WHERE id=?", (binding,))
        proofs = list(map(tuple, db.execute("SELECT * FROM source_bindings ORDER BY id")))
    current = observe(jobs, docs["a"]["id"])["items"][0]
    assert current["current"] == first["current"]
    assert current["review"]["dependency_generation"] != first["review"]["dependency_generation"]
    assert current["source_warnings"] == {"unknown": 1}
    with jobs.store.connect() as db:
        assert list(map(tuple, db.execute("SELECT * FROM source_bindings ORDER BY id"))) == proofs


def test_multiple_inputs_for_one_output_do_not_hide_failed_work(prepared):
    jobs, docs = prepared
    job = enqueue(jobs, docs["a"], status="done", index_complete=True,
                  output=docs["b"]["path"], output_revision=docs["b"]["revision"])
    job["items"].append({"doc": docs["c"]["id"], "path": docs["c"]["path"], "revision": docs["c"]["revision"], "status": "failed"})
    jobs.update(job["id"], status="completed_with_attention", items=job["items"])
    with jobs.store.connect() as db:
        receipt(db, job, docs["a"], docs["b"])
        db.execute("""INSERT INTO operation_receipts
                      SELECT 'second',job_id,?,doc_id,path,?,output_revision,operation,reason,change_id,created_at,details
                      FROM operation_receipts WHERE id='receipt'""", (docs["c"]["id"], docs["c"]["revision"]))
    jobs.store.path(docs["b"]["path"]).write_text("# B\nA later human version.\n")
    jobs.store.refresh()
    current = observe(jobs, docs["b"]["id"])["items"][0]
    assert current["status"] == "changed" and current["baseline"]["id"] == "receipt"
    assert current["attempt_count"] == 1 and not current["actionable"]
    assert current["attempts"][0] == {"job_id": job["id"], "status": "completed_with_attention", "recovery": "execution_unfinished"}


@pytest.mark.parametrize("association", ["receipt", "publication"])
def test_output_keeps_its_exact_remote_input_without_other_items_of_the_job(prepared, association):
    jobs, docs = prepared
    job = enqueue(jobs, docs["a"], status="failed")
    job["items"].append({"doc": docs["c"]["id"], "path": docs["c"]["path"],
                         "revision": docs["c"]["revision"], "status": "failed"})
    jobs.update(job["id"], status="completed_with_attention", items=job["items"])
    with jobs.store.connect() as db:
        if association == "receipt":
            receipt(db, job, docs["a"], docs["b"])
        else:
            db.execute("INSERT INTO changes VALUES('change',?,'before','after','worker','test','applied','now')",
                       (docs["b"]["path"],))
            db.execute("INSERT INTO change_retention VALUES('change',?,1,1,NULL)", (docs["b"]["id"],))
            db.execute("INSERT INTO job_publications VALUES(?,?,?,?,?,?,1)",
                       (job["id"], docs["a"]["id"], docs["a"]["revision"], "change",
                        docs["b"]["path"], docs["b"]["revision"]))
    output = observe(jobs, docs["b"]["id"])["items"][0]
    assert output["attempt_count"] == 1 and not output["actionable"]
    assert output["attempts"] == [{"job_id": job["id"], "status": "completed_with_attention",
                                   "recovery": "execution_unfinished"}]


def test_mutation_between_calculation_and_publication_cannot_publish_stale_ready(prepared, monkeypatch):
    jobs, docs = prepared
    calculate = maintenance_projection.Batch.calculate
    injected = []

    def racing(batch):
        result = calculate(batch)
        if docs["a"]["id"] in batch.identifiers and not injected:
            injected.append(enqueue(jobs, docs["a"]))
        return result

    monkeypatch.setattr(maintenance_projection.Batch, "calculate", racing)
    current = observe(jobs, docs["a"]["id"])["items"][0]
    assert injected and current["attempt_count"] == 1 and not current["actionable"]
    assert current["attempts"][0]["job_id"] == injected[0]["id"]


def test_continuous_mutation_returns_updating_with_dirty_proof_retained(prepared, monkeypatch):
    jobs, docs = prepared
    calculate = maintenance_projection.Batch.calculate
    injected = []

    def racing(batch):
        result = calculate(batch)
        injected.append(enqueue(jobs, docs["a"], "race-" + str(len(injected))))
        return result

    with monkeypatch.context() as patch:
        patch.setattr(maintenance_projection.Batch, "calculate", racing)
        updating = observe(jobs, docs["a"]["id"])
    assert len(injected) == 3
    assert not updating["ready"] and updating["index_status"] == "updating" and updating["total"] is None
    with jobs.store.connect() as db:
        assert db.execute("SELECT 1 FROM maintenance_dirty WHERE doc_id=?", (docs["a"]["id"],)).fetchone()
    current = observe(jobs, docs["a"]["id"])["items"][0]
    assert current["attempt_count"] == 3 and not current["actionable"]


def test_move_out_of_directory_removes_the_old_candidate_without_losing_baseline(prepared):
    jobs, docs = prepared
    job = enqueue(jobs, docs["a"], status="done", index_complete=True,
                  output=docs["a"]["path"], output_revision=docs["a"]["revision"])
    with jobs.store.connect() as db:
        receipt(db, job, docs["a"], docs["a"])
    assert observe(jobs)["total"] == 2
    jobs.store.path("Other").mkdir()
    jobs.store.refresh()  # Arm the destination watch before moving its inode.
    jobs.store.path(docs["a"]["path"]).rename(jobs.store.path("Other/moved.md"))
    jobs.store.refresh()
    listing = observe(jobs)
    assert listing["total"] == 2 and all(row["current"]["id"] != docs["a"]["id"] for row in listing["items"])
    moved = observe(jobs, docs["a"]["id"])["items"][0]
    assert moved["status"] == "unchanged" and moved["path_changed"] and not moved["actionable"]
    assert moved["current"]["path"] == "Other/moved.md" and moved["baseline"]["path"] == docs["a"]["path"]


def test_unrelated_dirty_directory_does_not_rebuild_the_requested_page(prepared, monkeypatch):
    jobs, _ = prepared
    first = observe(jobs)
    jobs.store.path("Other").mkdir()
    for number in range(30):
        jobs.store.path(f"Other/note-{number}.md").write_text(f"# Outside {number}\n")
    jobs.store.refresh()
    calls = trace_reads(jobs.store, monkeypatch)
    current = observe(jobs)
    assert [row["review"] for row in current["items"]] == [row["review"] for row in first["items"]]
    assert not any("FROM operation_receipts" in sql for sql in calls)
    with jobs.store.connect() as db:
        assert db.execute("SELECT 1 FROM maintenance_dirty_paths WHERE path LIKE 'Other/%'").fetchone()


def test_old_path_links_are_live_without_becoming_review_key_input(prepared):
    jobs, docs = prepared
    job = enqueue(jobs, docs["a"], status="done", index_complete=True,
                  output=docs["a"]["path"], output_revision=docs["a"]["revision"])
    with jobs.store.connect() as db:
        receipt(db, job, docs["a"], docs["a"])
    observe(jobs, docs["a"]["id"])
    jobs.store.path(docs["a"]["path"]).rename(jobs.store.path("Library/moved.md"))
    jobs.store.refresh()
    before = observe(jobs, docs["a"]["id"])["items"][0]
    jobs.store.path("map.md").write_text("# Map\n[[a]]\n")
    jobs.store.refresh()
    linked = observe(jobs, docs["a"]["id"])["items"][0]
    assert linked["historical_path_links"] == 1 and linked["review"] == before["review"]
    jobs.store.path("Library/else").mkdir()
    jobs.store.path("Library/else/a.md").write_text("# Another current owner of that short name\n")
    jobs.store.refresh()
    ambiguous = observe(jobs, docs["a"]["id"])["items"][0]
    assert ambiguous["historical_path_links"] == 0 and ambiguous["review"] == before["review"]


def test_late_confirmed_binding_survives_summary_rebuild_with_the_same_review(prepared):
    jobs, docs = prepared
    jobs.store.path(docs["a"]["path"]).write_text("---\nsources:\n  - resource: future.md\n    revision: earlier\n---\n# A\n")
    jobs.store.refresh()
    original = observe(jobs, docs["a"]["id"])["items"][0]
    with jobs.store.connect() as db:
        old_observation = source_bindings.current(db, docs["a"]["id"])
    assert old_observation["entries"][0]["policy"] == "unresolved"
    jobs.store.path("Library/future.md").write_text("# Future\nNew evidence.\n")
    jobs.store.refresh()
    confirmed = observe(jobs, docs["a"]["id"])["items"][0]
    assert confirmed["review"] != original["review"] and confirmed["source_warnings"] == {"changed": 1}
    with jobs.store.connect() as db:
        assert source_bindings.observation(db, old_observation["id"])["entries"][0]["policy"] == "unresolved"
        db.execute("DROP TABLE maintenance_projection")
    schema.initialize(jobs.store)
    rebuilt = observe(jobs, docs["a"]["id"])["items"][0]
    assert rebuilt["review"] == confirmed["review"]


@pytest.mark.parametrize("field,first,last", [("resource", "absent-a.md", "absent-b.md"),
                                            ("location", "L1", "L2"), ("revision", "r1", "r2")])
def test_dependency_digest_distinguishes_selectors_locators_and_recorded_versions(prepared, field, first, last):
    jobs, docs = prepared

    def content(value):
        source = {"resource": "absent.md", "location": "L1", "revision": "r1", field: value}
        return "---\nsources:\n  - " + "\n    ".join(f"{name}: {item}" for name, item in source.items()) + "\n---\n# A\n"

    generations = []
    for value in (first, last):
        jobs.store.path(docs["a"]["path"]).write_text(content(value))
        jobs.store.refresh()
        generations.append(observe(jobs, docs["a"]["id"])["items"][0]["review"]["dependency_generation"])
    assert generations[0] != generations[1]


def test_rebuilt_projection_stales_an_old_collection_cursor(prepared):
    jobs, _ = prepared
    first = observe(jobs, limit=1)
    with jobs.store.connect() as db:
        db.execute("DROP TABLE maintenance_projection")
    schema.initialize(jobs.store)
    with pytest.raises(WikiError) as stale:
        observe(jobs, limit=1, cursor=first["next_cursor"])
    assert stale.value.status == 409


def test_document_error_readiness_and_dirty_scope_use_indexes(prepared):
    from server.wiki.index_state import DOCUMENT_ERROR

    jobs, _ = prepared
    with jobs.store.connect() as db:
        errors = [row[3] for row in db.execute("EXPLAIN QUERY PLAN SELECT count(*) FROM documents WHERE " + DOCUMENT_ERROR)]
        dirty = [row[3] for row in db.execute("""EXPLAIN QUERY PLAN SELECT DISTINCT p.doc_id FROM maintenance_dirty_paths p
            JOIN maintenance_dirty d ON d.doc_id=p.doc_id WHERE p.path>=? AND p.path<?""", ("Library/", "Library0"))]
    assert any("maintenance_document_errors" in step for step in errors)
    assert any("SEARCH" in step and "maintenance_dirty_scope" in step for step in dirty)


@pytest.mark.parametrize("single", [False, True])
def test_readiness_checks_job_barrier_even_if_dirty_document_rows_are_lost(prepared, single):
    jobs, docs = prepared
    observe(jobs)
    enqueue(jobs, docs["a"])
    with jobs.store.connect() as db:
        db.execute("DELETE FROM maintenance_dirty WHERE doc_id=?", (docs["a"]["id"],))
        db.execute("DELETE FROM maintenance_dirty_paths WHERE doc_id=?", (docs["a"]["id"],))
    current = observe(jobs, docs["a"]["id"] if single else None)
    assert not current["ready"] and current["index_status"] == "updating" and not current["items"]


def test_success_replay_compares_the_output_with_the_final_read_snapshot(prepared, monkeypatch):
    from server.wiki import maintenance_admission

    jobs, docs = prepared
    first = observe(jobs, docs["a"]["id"])["items"][0]
    review = first["review"]
    job = jobs.maintain(review["review_key"], "", "", [docs["a"]["path"]], None, [], "human", review=review)
    job["items"][0].update(status="done", index_complete=True, output=docs["a"]["path"],
                           output_revision=docs["a"]["revision"])
    jobs.update(job["id"], status="completed", items=job["items"])
    with jobs.store.connect() as db:
        receipt(db, job, docs["a"], docs["a"])
    observe(jobs, docs["a"]["id"])
    prepare_admission = maintenance_admission.prepare
    changed = []

    def edit_after_current_read(current_jobs, current_review):
        prepare_admission(current_jobs, current_review)
        store = current_jobs.store
        store.path(docs["a"]["path"]).write_text("# A\nA newer human version before the final snapshot.\n")
        store.refresh()
        changed.append(True)
        return maintenance_projection.settle(store, doc=current_review["doc"])

    monkeypatch.setattr(maintenance_admission, "prepare", edit_after_current_read)
    with pytest.raises(WikiError) as stale:
        jobs.maintain(review["review_key"], "", "", [docs["a"]["path"]], None, [], "human", review=review)
    assert changed and stale.value.status == 409
