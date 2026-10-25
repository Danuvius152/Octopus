"""Bound repeated work at the public navigation and grant admission seams."""

from contextlib import contextmanager

import pytest

from server.wiki import format, grants
from server.wiki import maintenance_inputs as inputs
from server.wiki.curation import WikiCuration
from tests.test_wiki_commonmark_locations import store as store
from tests.test_wiki_maintenance_inputs import prepared as prepared


@pytest.mark.parametrize("operation", ["documents", "search"])
def test_full_navigation_connections_do_not_grow_per_result(store, monkeypatch, operation):
    for number in range(4):
        store.path(f"note-{number}.md").write_text(f"# Note {number}\n\nsharedmarker evidence.\n")
    store.refresh()
    connect, calls = store.connect, []

    @contextmanager
    def counted():
        calls.append(1)
        with connect() as db:
            yield db

    monkeypatch.setattr(store, "connect", counted)
    counts = []
    for limit in (1, 4):
        calls.clear()
        result = store.documents(limit=limit) if operation == "documents" else store.search("sharedmarker", limit=limit)
        assert len(result["items"]) == limit
        assert all(item["metadata"]["source_refs"] == [] for item in result["items"])
        counts.append(len(calls))
    assert counts[0] == counts[1], counts


def test_interleaved_raw_pages_reuse_coordinates(store, monkeypatch):
    curation = WikiCuration(store)
    for name in ("a", "b"):
        store.path(f"{name}.md").write_text(f"# {name}\n\nEvidence.\n")
        curation.blocks(f"{name}.md", limit=1)
    parse, calls = format._markdown.parse, []

    def counted(text, *args, **kwargs):
        calls.append(text)
        return parse(text, *args, **kwargs)

    monkeypatch.setattr(format._markdown, "parse", counted)
    for name in ("a", "b", "a", "b"):
        assert curation.blocks(f"{name}.md", limit=1)["blocks"]
    assert not calls


def test_binding_one_grant_does_not_reinvalidate_other_job_members(prepared):
    jobs, docs = prepared
    items = [{"doc": row["id"], "path": row["path"], "revision": row["revision"], "status": "pending"}
             for row in docs.values()]
    job = jobs.enqueue("grant-invalidation", {"kind": "curation"}, items)
    with jobs.store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute("INSERT INTO documents SELECT 'extra', 'Library/extra.md', revision, title,kind,text,pages,quality,metadata,signature FROM documents LIMIT 1")
        db.execute("DELETE FROM maintenance_dirty")
        db.execute("DELETE FROM maintenance_dirty_paths")
        db.execute("DELETE FROM maintenance_job_dirty")
        grants.bind_document(db, job["id"], "extra", "Library/extra.md", "observed", "selected")
        assert [row[0] for row in db.execute("SELECT doc_id FROM maintenance_dirty")] == ["extra"]
        assert not db.execute("SELECT 1 FROM maintenance_job_dirty").fetchone()
        assert {row[0] for row in db.execute("SELECT doc_id FROM maintenance_job_members WHERE job_id=?", (job["id"],))} == {item["doc"] for item in items}


@pytest.mark.parametrize("relation", ["receipt", "publication"])
def test_item_progress_invalidates_its_outputs_and_preserves_other_members(prepared, relation):
    jobs, docs = prepared
    items = [{"doc": docs[name]["id"], "path": docs[name]["path"], "revision": docs[name]["revision"], "status": "pending"}
             for name in ("a", "b")]
    job = jobs.enqueue("progress-invalidation", {"kind": "curation"}, items)
    with jobs.store.connect() as db:
        if relation == "receipt":
            db.execute("INSERT INTO operation_receipts(id,job_id,item_doc,doc_id,path,operation,reason,created_at,details) VALUES(?,?,?,?,?,'no_change','test','now','{}')",
                       ("output", job["id"], docs["a"]["id"], docs["c"]["id"], docs["c"]["path"]))
        else:
            db.execute("INSERT INTO changes VALUES('output',?,'before','after','worker','test','applied','now')", (docs["c"]["path"],))
            db.execute("INSERT INTO change_retention VALUES('output',?,1,1,NULL)", (docs["c"]["id"],))
            db.execute("INSERT INTO job_publications VALUES(?,?,?,?,?,?,1)",
                       (job["id"], docs["a"]["id"], docs["a"]["revision"], "output", docs["c"]["path"], docs["c"]["revision"]))
    assert inputs.normalize(jobs.store)
    with jobs.store.connect() as db:
        db.execute("DELETE FROM maintenance_dirty")
        db.execute("DELETE FROM maintenance_dirty_paths")
    job["items"][0]["status"] = "failed"
    jobs.update(job["id"], items=job["items"])
    with jobs.store.connect() as db:
        assert {row[0] for row in db.execute("SELECT doc_id FROM maintenance_dirty")} == {docs[name]["id"] for name in ("a", "c")}
        assert not inputs.ready(db, [docs["a"]["id"]])
    assert inputs.normalize(jobs.store)
    jobs.update(job["id"], status="running")
    with jobs.store.connect() as db:
        assert {row[0] for row in db.execute("SELECT doc_id FROM maintenance_dirty")} == {row["id"] for row in docs.values()}
