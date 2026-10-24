"""Frozen 12/13 authority survives preparation before any current business read."""

import hashlib
import json
import sqlite3
from contextlib import closing

import pytest

from server.wiki import grants
from server.wiki.jobs import WikiJobs
from server.wiki.registry import WikiRegistry
from server.wiki.store import WikiStore
from tests.test_wiki_format_admission import registered_account as registered_account


@pytest.mark.parametrize("kind", ["selected_note", "selected_asset_note", "reserved_material", "existing_material"])
async def test_current_grant_and_material_keep_exact_authority(registered_account, kind):
    root, state, registration, owner = registered_account
    identifier = "retained-finite-authority"
    material = "# Material\nPreserve the original submitted bytes 原文.\n"
    selected_path = "Library/assets/project/notes.md" if kind == "selected_asset_note" else "Library/note.md"
    if kind == "existing_material":
        selected_path = "Library/material.md"
    if kind != "reserved_material":
        (root / selected_path).parent.mkdir(parents=True, exist_ok=True)
        (root / selected_path).write_text(material if kind == "existing_material" else "# Original note\nKeep its conditions.\n")
    (root / "Library").mkdir(exist_ok=True)
    (root / "Library/unselected.md").write_text("# Not selected\nNo write authority was granted.\n")
    with closing(WikiStore(root, state, registration["vault_id"])) as producer:
        producer.refresh()
        selected = producer.read(selected_path) if kind != "reserved_material" else None
    original_files = {path.relative_to(root).as_posix(): path.read_bytes() for path in root.rglob("*.md")}
    original_config = (state / "config.json").read_bytes()
    items = [] if selected is None else [{"version": 1, "doc": selected["id"], "path": selected_path,
                                          "revision": selected["revision"], "status": "pending"}]
    submitted = material if kind in {"reserved_material", "existing_material"} else ""
    paths = [] if submitted else [selected_path]
    request = {"material": submitted, "scope": "Original finite work", "paths": paths,
               "target": None, "sources": [], "actor": "human", "created_changes": []}
    payload = {"kind": "curation", "new_only": False, "material": submitted, "scope": request["scope"],
               "paths": paths, "target": None, "sources": [], "actor": "human", "request": request}
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    with closing(sqlite3.connect(state / "index.sqlite3")) as target:
        target.row_factory = sqlite3.Row
        target.execute("PRAGMA recursive_triggers=ON")
        target.execute("""INSERT INTO jobs(id,request_key,payload_hash,payload,status,items,created_at,updated_at)
            VALUES(?,'original-key',?,?,'queued',?,'original-created','original-updated')""",
            (identifier, hashlib.sha256(encoded.encode()).hexdigest(), encoded, json.dumps(items, ensure_ascii=False)))
        target.execute("INSERT INTO job_requests VALUES('original-key',?,?)", (WikiJobs.request_hash(request), identifier))
        reserved = ({"path": f"Library/Inbox/{identifier}.md", "path_key": str(root / f"Library/Inbox/{identifier}.md")}
                    if kind == "reserved_material" else None)
        grants.issue(target, registration, identifier, items, material=reserved)
        original = {table: list(map(tuple, target.execute(f"SELECT * FROM {table} ORDER BY rowid")))
                    for table in (*grants.TABLES, "job_requests", "wiki_job_state", "wiki_job_outbox")}
        before_job = dict(target.execute("SELECT * FROM jobs WHERE id=?", (identifier,)).fetchone())
        assert "admission_sequence" in {row[1] for row in target.execute("PRAGMA table_info(jobs)")}
        target.commit()
    # Exercise the real registration publication, which binds this generation
    # before creation authorization can consume the retained grant.
    with closing(WikiRegistry()) as reopened:
        current = reopened.store(owner)
        jobs = WikiJobs(owner, store=current, registration=registration)
        job = jobs.get(identifier)
        assert job["status"] == "queued"
        assert job["items"] == [item | {"version": 1} for item in items]
        assert job["payload"] == payload
        assert job["grant"]["registration_generation"] == registration["registration_generation"]
        assert job["grant"]["allowed_new_paths"] == []
        assert [item["doc_id"] for item in job["grant"]["selected_documents"]] == ([] if selected is None else [selected["id"]])
        with current.connect() as db:
            for table, rows in original.items():
                assert list(map(tuple, db.execute(f"SELECT * FROM {table} ORDER BY rowid"))) == rows
            after_job = dict(db.execute("SELECT * FROM jobs WHERE id=?", (identifier,)).fetchone())
            assert {key: after_job[key] for key in before_job if key != "items"} == {key: value for key, value in before_job.items() if key != "items"}
        current.refresh()
        if submitted:
            first = jobs.ingest(identifier)
            assert jobs.ingest(identifier)["id"] == first["id"]
            assert (root / first["path"]).read_text() == material
            if selected is not None:
                assert first["id"] == selected["id"]
            with current.connect() as db:
                assert db.execute("SELECT count(*) FROM changes WHERE reason=?", ("Ingest job " + identifier,)).fetchone()[0] == int(selected is None)
                if selected is None:
                    reservation = db.execute("SELECT state,document_id,output_revision FROM wiki_path_reservations WHERE job_id=?", (identifier,)).fetchone()
                    assert tuple(reservation) == ("bound", first["id"], first["revision"])
        else:
            assert jobs.next_queued() == identifier
        for path, raw in original_files.items():
            assert (root / path).read_bytes() == raw
    assert (state / "config.json").read_bytes() == original_config
