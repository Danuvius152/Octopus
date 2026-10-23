"""Published V9 work keeps its identity and needs provable, finite authority."""

import hashlib
import json
import sqlite3

import pytest

from server.wiki import grants, maintenance_inputs, schema, source_bindings
from server.wiki.jobs import WikiJobs
from server.wiki.registry import registry
from server.wiki.store import WikiError
from tests import test_wiki_api as wiki_api
from tests.wiki_schema_fixtures import remove_future_selections

client = wiki_api.client


async def test_readmission_retains_original_recording_reference_for_its_document(client, tmp_path):
    from server.wiki.history import WikiHistory

    root = tmp_path / "vault"
    (root / "Library").mkdir(parents=True)
    for name in ("a", "b"):
        (root / f"Library/{name}.md").write_text(f"# {name}\nOriginal argument.\n")
    await wiki_api.configure(client, root)
    jobs = WikiJobs(None)
    job = jobs.maintain("recording-readmission", "", "Review these exact notes",
                        ["Library/a.md", "Library/b.md"], None, [], "human")
    items = job["items"]
    reference = {"session_id": "original-session", "request_id": "original-request", "recording_state": "unconfirmed"}
    first = next(item for item in items if item["path"] == "Library/a.md")
    first.update(status="attention", recording_failure=reference)
    jobs.update(job["id"], status="interrupted", items=items, session_id=None)
    WikiHistory(jobs.store).write(first["path"], b"# A\nHuman revision after interruption.\n",
                                  first["revision"], "human", "save")
    observed = jobs.store.read(first["doc"])

    def scope_facts(store):
        with store.connect() as db:
            return (tuple(db.execute("SELECT * FROM wiki_job_grants WHERE job_id=?", (job["id"],)).fetchone()),
                    [tuple(row) for row in db.execute("SELECT * FROM wiki_grant_documents WHERE job_id=? ORDER BY doc_id", (job["id"],))])

    original_scope = scope_facts(jobs.store)
    jobs.recover_jobs(previous_writers_retired=True)
    registry.configure(None, root, "replacement-curator")
    refused = await client.post(f"/api/wiki/jobs/{job['id']}/retry", headers=wiki_api.HEADERS)
    assert refused.status_code == 409
    accepted = await client.post(f"/api/wiki/jobs/{job['id']}/readmit", headers=wiki_api.HEADERS,
                                 json={"paths": ["Library/a.md", "Library/b.md"]})
    assert accepted.status_code == 200, accepted.text
    current = WikiJobs(None)
    by_path = {item["path"]: item for item in accepted.json()["items"]}
    assert by_path[first["path"]]["legacy_evidence"]["recording_failure"] == reference
    assert by_path[first["path"]]["worker_request"]["retired"] is True
    assert by_path[first["path"]]["revision"] == observed["revision"] != first["revision"]
    assert "recording_failure" not in by_path["Library/b.md"]
    assert {item["doc"] for item in by_path.values()} == {item["doc"] for item in items}
    assert scope_facts(current.store) == original_scope


def legacy_job(store, *, paths, document=None, material="", status="queued", key="legacy-key"):
    """Build real V9 rows after checking that every future ledger is empty."""
    WikiJobs(None)
    registry.close()
    request = {"material": material, "scope": "Organize the requested material", "paths": paths,
               "target": None, "sources": [], "actor": "human", "created_changes": {}}
    payload = {"kind": "submission" if material else "curation", "new_only": bool(material),
               "material": material, "scope": request["scope"], "paths": paths,
               "target": None, "sources": [], "actor": "human", "request": request}
    items = [] if document is None else [{"doc": document["id"], "path": document["path"],
                                         "revision": document["revision"], "quality": "good", "status": "pending"}]
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    with sqlite3.connect(store.db_path) as db:
        db.execute("PRAGMA recursive_triggers=ON")
        maintenance_inputs.discard_derived(db)
        remove_future_selections(db)
        for table in source_bindings.TABLES:
            db.execute("DROP TABLE " + table)
        db.execute("ALTER TABLE app_trash DROP COLUMN source_plan")
        db.execute("DROP TABLE wiki_protocols")
        db.execute("DELETE FROM wiki_schema WHERE version>=10")
        db.execute("INSERT INTO jobs VALUES(?,?,?,?,?,?,NULL,NULL,'original-time','original-time')",
                   ("old-job", key, hashlib.sha256(raw.encode()).hexdigest(), raw, status, json.dumps(items)))
        db.execute("INSERT INTO job_requests VALUES(?,?,?)", (key, WikiJobs.request_hash(request), "old-job"))
    return raw, items


@pytest.mark.parametrize("path", ["Library/note.md", "Library/assets/project/notes.md"])
async def test_v9_exact_queued_work_gains_no_new_page_rights_and_keeps_original_request(client, tmp_path, path):
    root = tmp_path / "vault"
    (root / "Library").mkdir(parents=True)
    (root / path).parent.mkdir(parents=True, exist_ok=True)
    (root / path).write_text("# Original\n\nKeep this argument.\n")
    config = await wiki_api.configure(client, root)
    store = registry.store(None)
    document = store.read(path)
    raw, items = legacy_job(store, paths=[document["path"]], document=document)
    jobs = WikiJobs(None)
    migrated = jobs.get("old-job")
    assert migrated["status"] == "queued"
    assert migrated["items"] == [item | {"version": 1} for item in items]
    assert migrated["grant"]["registration_generation"] == config["registration_generation"]
    assert migrated["grant"]["allowed_new_paths"] == []
    assert [item["doc_id"] for item in migrated["grant"]["selected_documents"]] == [document["id"]]
    with jobs.store.connect() as db:
        assert db.execute("SELECT payload FROM jobs").fetchone()[0] == raw
        assert db.execute("SELECT count(*) FROM wiki_path_reservations").fetchone()[0] == 0
    assert jobs.next_queued() == "old-job"


async def test_v9_broad_work_requires_same_job_readmission_and_retry_cannot_wash_it(client, tmp_path):
    root = tmp_path / "vault"
    (root / "Library").mkdir(parents=True)
    (root / "Library/note.md").write_text("# Original\n\nKeep this argument.\n")
    await wiki_api.configure(client, root)
    store = registry.store(None)
    document = store.read("Library/note.md")
    raw, _ = legacy_job(store, paths=[], document=document)
    jobs = WikiJobs(None)
    blocked = jobs.get("old-job")
    assert blocked["status"] == "failed"
    detail = json.loads(blocked["error"])
    assert detail["code"] == "scope_review_required" and detail["proposed_grant"]["paths"] == [document["path"]]
    assert jobs.next_queued() is None
    refused = await client.post("/api/wiki/jobs/old-job/retry", headers=wiki_api.HEADERS)
    assert refused.status_code == 409
    body = {"paths": [document["path"]]}
    response = await client.post("/api/wiki/jobs/old-job/readmit", headers=wiki_api.HEADERS, json=body)
    assert response.status_code == 200, response.text
    accepted = response.json()
    assert accepted["id"] == "old-job" and accepted["request_key"] == "legacy-key" and accepted["status"] == "queued"
    replay = await client.post("/api/wiki/jobs/old-job/readmit", headers=wiki_api.HEADERS, json=body)
    assert replay.status_code == 200 and replay.json()["grant"] == accepted["grant"]
    with jobs.store.connect() as db:
        assert db.execute("SELECT count(*) FROM jobs").fetchone()[0] == 1
        assert db.execute("SELECT payload FROM jobs").fetchone()[0] == raw
        assert db.execute("SELECT count(*) FROM wiki_grant_readmissions").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM changes").fetchone()[0] == 0


async def test_v9_unstarted_material_uses_one_finite_framework_reservation(client, tmp_path):
    root = tmp_path / "vault"
    root.mkdir()
    await wiki_api.configure(client, root)
    store = registry.store(None)
    legacy_job(store, paths=[], material="# Material\n\nPreserve submitted bytes.\n")
    jobs = WikiJobs(None)
    job = jobs.get("old-job")
    assert job["status"] == "queued" and job["grant"]["allowed_new_paths"] == []
    with jobs.store.connect() as db:
        reservations = db.execute("SELECT path,purpose,state FROM wiki_path_reservations").fetchall()
    assert [tuple(row) for row in reservations] == [("Library/Inbox/old-job.md", "framework_material", "reserved")]
    created = jobs.ingest("old-job")
    repeated = jobs.ingest("old-job")
    assert created["id"] == repeated["id"]
    assert (root / "Library/Inbox/old-job.md").read_text() == "# Material\n\nPreserve submitted bytes.\n"
    with jobs.store.connect() as db:
        assert db.execute("SELECT count(*) FROM changes").fetchone()[0] == 1


async def test_v9_material_with_exact_input_identity_is_reused_without_a_second_raw_file(client, tmp_path):
    root = tmp_path / "vault"
    (root / "Library").mkdir(parents=True)
    material = "# Material\n\nPreserve these exact submitted bytes.\n"
    (root / "Library/material.md").write_text(material)
    await wiki_api.configure(client, root)
    store = registry.store(None)
    document = store.read("Library/material.md")
    legacy_job(store, paths=[], document=document, material=material)
    jobs = WikiJobs(None)
    job = jobs.get("old-job")
    assert job["status"] == "queued"
    assert [row["doc_id"] for row in job["grant"]["selected_documents"]] == [document["id"]]
    reused = jobs.ingest("old-job")
    assert reused["id"] == document["id"]
    with jobs.store.connect() as db:
        assert db.execute("SELECT count(*) FROM changes").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM wiki_path_reservations").fetchone()[0] == 0
    assert [path.relative_to(root).as_posix() for path in root.rglob("*.md")] == ["Library/material.md"]


async def test_completed_v9_rows_are_not_reinterpreted_as_new_authority(client, tmp_path):
    root = tmp_path / "vault"
    root.mkdir()
    await wiki_api.configure(client, root)
    store = registry.store(None)
    raw, _ = legacy_job(store, paths=[], status="completed")
    jobs = WikiJobs(None)
    job = jobs.get("old-job")
    assert job["status"] == "completed" and job["grant"] is None and job["error"] is None
    with jobs.store.connect() as db:
        assert db.execute("SELECT payload FROM jobs").fetchone()[0] == raw
        assert db.execute("SELECT count(*) FROM wiki_job_grants").fetchone()[0] == 0
    # Replaying completed work returns its existing result, including on old
    # jobs with no surviving write grant. It does not create new authority.
    assert jobs.retry("old-job") == job
    assert jobs.next_queued() is None


@pytest.mark.parametrize("path", ["Library/note.md", "Library/assets/project/notes.md", "Library/ASSETS/project/notes.markdown", None])
async def test_registration_change_needs_explicit_same_job_readmission(client, tmp_path, path):
    from tests.test_wiki_grant_v2 import assigned

    root = tmp_path / "vault"
    (root / "Library").mkdir(parents=True)
    if path:
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text("# Original\n\nKeep this argument.\n")
    outsider = "Library/assets/project/unassigned.md"
    (root / outsider).parent.mkdir(parents=True, exist_ok=True)
    (root / outsider).write_text("# Another identity\n\nThis note was not selected.\n")
    old_config = await wiki_api.configure(client, root)
    jobs = WikiJobs(None)
    proposal = {"purpose": "requested_knowledge_page", "paths": ["Library/new.md"]} if path is None else None
    job = jobs.maintain("retain-job", "", "Finite work", [path] if path else [], None, [], "human", creation=proposal)
    if path:
        with assigned(job, old_config) as (headers, _):
            note = await client.get("/api/wiki/read", headers=headers, params={"doc": path})
            assert note.status_code == 200, note.text
            recorded = await client.post("/api/wiki/revise", headers=headers, json={
                "doc": note.json()["id"], "expected_revision": note.json()["revision"],
                "operation": "no_change", "reason": "Retain this original primary review receipt"})
            assert recorded.status_code == 200, recorded.text
        jobs.update(job["id"], status="interrupted", session_id=None)

    def retained_facts(store):
        with store.connect() as db:
            return {
                "request": tuple(db.execute("SELECT request_key,payload_hash,payload FROM jobs WHERE id=?", (job["id"],)).fetchone()),
                "aliases": [tuple(row) for row in db.execute("SELECT * FROM job_requests WHERE job_id=?", (job["id"],))],
                "grant": tuple(db.execute("SELECT * FROM wiki_job_grants WHERE job_id=?", (job["id"],)).fetchone()),
                "identities": [tuple(row) for row in db.execute("SELECT * FROM wiki_grant_documents WHERE job_id=? ORDER BY doc_id", (job["id"],))],
                "receipts": [tuple(row) for row in db.execute("SELECT * FROM operation_receipts WHERE job_id=? ORDER BY id", (job["id"],))],
            }

    before = retained_facts(jobs.store)
    assert len(before["receipts"]) == (1 if path else 0)
    original = (root / path).read_bytes() if path else None
    registry.configure(None, root, "a-new-curator")
    current = WikiJobs(None)
    assert current.next_queued() is None
    refused = await client.post(f"/api/wiki/jobs/{job['id']}/retry", headers=wiki_api.HEADERS)
    assert refused.status_code == 409
    foreign = await client.post(f"/api/wiki/jobs/{job['id']}/readmit", headers=wiki_api.HEADERS, json={"paths": [outsider]})
    body = {"paths": [path]} if path else {"creation": proposal}
    accepted = await client.post(f"/api/wiki/jobs/{job['id']}/readmit", headers=wiki_api.HEADERS, json=body)
    assert accepted.status_code == 200, accepted.text
    assert foreign.status_code == 409 and "identities" in foreign.text, foreign.text
    assert accepted.json()["id"] == job["id"] and accepted.json()["request_key"] == job["request_key"]
    assert accepted.json()["grant"]["registration_generation"] == registry.config(None)["registration_generation"]
    replay = await client.post(f"/api/wiki/jobs/{job['id']}/readmit", headers=wiki_api.HEADERS, json=body)
    assert replay.status_code == 200 and replay.json() == accepted.json(), replay.text
    new_asset = "Library/assets/project/new.md"
    new_page = await client.post("/api/wiki/maintain", headers=wiki_api.HEADERS, json={
        "idempotency_key": "not-a-new-assets-grant", "creation": {"purpose": "requested_knowledge_page", "paths": [new_asset]}})
    assert new_page.status_code == 422 and not (root / new_asset).exists(), new_page.text
    assert retained_facts(current.store) == before
    assert not path or (root / path).read_bytes() == original
    with current.store.connect() as db:
        assert db.execute("SELECT registration_generation FROM wiki_job_grants").fetchone()[0] == old_config["registration_generation"]
        assert db.execute("SELECT count(*) FROM jobs").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM wiki_grant_readmissions").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM wiki_path_reservations").fetchone()[0] == (0 if path else 1)
    assert current.next_queued() == job["id"]


async def test_readmission_cannot_be_forged_by_a_model_or_used_to_replace_prior_scope(client, tmp_path):
    from types import SimpleNamespace

    from server.mcp_identity import mint
    from server.wiki.tasks import tasks

    root = tmp_path / "vault"
    (root / "Library").mkdir(parents=True)
    (root / "Library/note.md").write_text("# Original\n\nKeep this argument.\n")
    config = await wiki_api.configure(client, root)
    store = registry.store(None)
    document = store.read("Library/note.md")
    legacy_job(store, paths=[], document=document)
    WikiJobs(None)
    session = SimpleNamespace(id="scope-caller", user_id=None, agent_id=config["curator_agent_id"])
    token = tasks.open(session, "auto")
    try:
        headers = {"Authorization": "Bearer " + mint(session.id, wiki_task_id=token)}
        forged = await client.post("/api/wiki/jobs/old-job/readmit", headers=headers, json={"paths": [document["path"]]})
        assert forged.status_code == 403
    finally:
        tasks.close(token)
    accepted = await client.post("/api/wiki/jobs/old-job/readmit", headers=wiki_api.HEADERS, json={"paths": [document["path"]]})
    assert accepted.status_code == 200, accepted.text
    changed = await client.post("/api/wiki/jobs/old-job/readmit", headers=wiki_api.HEADERS, json={"creation": {"purpose": "requested_knowledge_page"}})
    assert changed.status_code == 409


@pytest.mark.parametrize("missing", [*grants.TABLES, "marker"])
async def test_lost_grant_authority_is_not_rebuilt_with_search_cache(client, tmp_path, missing):
    root = tmp_path / "vault"
    root.mkdir()
    await wiki_api.configure(client, root)
    job = WikiJobs(None).maintain("retained-output", "", "Create one requested page", [], None, [], "human",
                                 creation={"purpose": "requested_knowledge_page", "paths": ["Library/new.md"]})
    store = registry.store(None)
    with store.connect() as db:
        db.execute("DROP TABLE search_documents")
        if missing == "marker":
            db.execute("DELETE FROM wiki_protocols WHERE name='wiki_grants'")
        else:
            db.execute("DROP TABLE " + missing)
    with pytest.raises(WikiError) as error:
        schema.initialize(store)
    assert error.value.status == 503
    with store.connect() as db:
        assert db.execute("SELECT id FROM jobs").fetchone()[0] == job["id"]
        assert db.execute("SELECT 1 FROM sqlite_master WHERE name='search_documents'").fetchone() is None
        if missing != "marker":
            assert db.execute("SELECT 1 FROM sqlite_master WHERE name=?", (missing,)).fetchone() is None


@pytest.mark.parametrize("table", list(grants.TABLES))
async def test_scope_facts_reject_replace_even_when_recursive_triggers_are_off(client, tmp_path, table):
    root = tmp_path / "vault"
    (root / "Library").mkdir(parents=True)
    (root / "Library/note.md").write_text("# Original\n\nKeep this argument.\n")
    await wiki_api.configure(client, root)
    store = registry.store(None)
    document = store.read("Library/note.md")
    legacy_job(store, paths=[], document=document)
    current = WikiJobs(None)
    accepted = await client.post("/api/wiki/jobs/old-job/readmit", headers=wiki_api.HEADERS, json={
        "paths": [document["path"]], "creation": {"purpose": "requested_knowledge_page", "paths": ["Library/new.md"]}})
    assert accepted.status_code == 200, accepted.text
    with sqlite3.connect(current.store.db_path) as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA recursive_triggers=OFF")
        row = dict(db.execute("SELECT * FROM " + table + " LIMIT 1").fetchone())
        changed = dict(row)
        field = {"wiki_job_grants": "policy", "wiki_grant_documents": "observed_revision",
                 "wiki_path_reservations": "path_key", "wiki_grant_readmissions": "request_hash"}[table]
        changed[field] = "untrusted replacement"
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("INSERT OR REPLACE INTO " + table + " VALUES(" + ",".join("?" for _ in changed) + ")", tuple(changed.values()))
        assert dict(db.execute("SELECT * FROM " + table + " LIMIT 1").fetchone()) == row
