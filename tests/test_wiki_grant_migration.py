"""Retained supported work keeps its identity and provable finite authority."""

import sqlite3
from contextlib import closing

import pytest

from server.wiki import grants
from server.wiki.jobs import WikiJobs
from server.wiki.registry import registry
from tests import test_wiki_api as wiki_api

client = wiki_api.client


async def test_readmission_retains_original_recording_reference_for_its_document(client, tmp_path):
    from server.wiki.history import WikiHistory

    root = tmp_path / "vault"
    (root / "Library").mkdir(parents=True)
    for name in ("a", "b"):
        (root / f"Library/{name}.md").write_text(f"# {name}\nOriginal argument.\n")
    await wiki_api.configure(client, root)
    jobs = WikiJobs((await wiki_api.session_manager.db.get_system_agent())["user_id"])
    job = jobs.maintain("recording-readmission", "", "Review these exact notes",
                        ["Library/a.md", "Library/b.md"], None, [], "human")
    items = job["items"]
    reference = {"session_id": "original-session", "request_id": "original-request", "recording_state": "unconfirmed",
                 "attempt": job["attempt"], "phase": "unknown", "work_id": "wiki-original-request"}
    first = next(item for item in items if item["path"] == "Library/a.md")
    first.update(status="attention", worker_request=reference)
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
    registry.configure((await wiki_api.session_manager.db.get_system_agent())["user_id"], root, "replacement-curator")
    refused = await client.post(f"/api/wiki/jobs/{job['id']}/retry", headers=client.headers)
    assert refused.status_code == 409
    accepted = await client.post(f"/api/wiki/jobs/{job['id']}/readmit", headers=client.headers,
                                 json={"paths": ["Library/a.md", "Library/b.md"]})
    assert accepted.status_code == 200, accepted.text
    current = WikiJobs((await wiki_api.session_manager.db.get_system_agent())["user_id"])
    by_path = {item["path"]: item for item in accepted.json()["items"]}
    assert {key: by_path[first["path"]]["worker_request"][key] for key in reference} == reference
    assert by_path[first["path"]]["worker_request"]["retired"] is True
    assert by_path[first["path"]]["revision"] == observed["revision"] != first["revision"]
    assert "recording_failure" not in by_path["Library/b.md"]
    assert {item["doc"] for item in by_path.values()} == {item["doc"] for item in items}
    assert scope_facts(current.store) == original_scope


def stopped_job(user_id, root, document, *, creation=None):
    jobs = WikiJobs(user_id)
    job = jobs.maintain("retained-authority", "", "Finite stopped work",
                        [document["path"]], None, [], "human", creation=creation)
    jobs.update(job["id"], status="interrupted", session_id=None)
    registry.configure(user_id, root, "replacement-curator")
    return job


@pytest.mark.parametrize("kind", ["selected_note", "selected_asset_note", "reserved_material", "existing_material"])
async def test_current_work_keeps_exact_authority_and_files_after_reopen(client, tmp_path, kind):
    root = tmp_path / "vault"
    material = "# Material\n\nPreserve these exact submitted bytes 原文.\n"
    path = "Library/assets/project/notes.md" if kind == "selected_asset_note" else "Library/note.md"
    if kind == "existing_material":
        path = "Library/material.md"
    if kind != "reserved_material":
        (root / path).parent.mkdir(parents=True)
        (root / path).write_text(material if kind == "existing_material" else "# Original\n\nKeep this argument.\n")
    (root / "Library").mkdir(parents=True, exist_ok=True)
    (root / "Library/unselected.md").write_text("# Not selected\nNo write authority was granted.\n")
    config = await wiki_api.configure(client, root)
    owner = (await wiki_api.session_manager.db.get_system_agent())["user_id"]
    jobs = WikiJobs(owner)
    document = jobs.store.read(path) if kind != "reserved_material" else None
    submitted = material if kind in {"reserved_material", "existing_material"} else ""
    original = jobs.maintain("retained-exact-scope", submitted, "Finite work", [] if submitted else [path], None, [], "human")
    original_files = {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*.md")}
    config_path = registry.state(owner) / "config.json"
    original_config = config_path.read_bytes()
    with jobs.store.connect() as db:
        original_rows = {table: [tuple(row) for row in db.execute(f"SELECT * FROM {table} ORDER BY rowid")]
                         for table in ("jobs", *grants.TABLES, "job_requests", "wiki_job_state", "wiki_job_outbox")}
    registry.close()
    current = WikiJobs(owner)
    job = current.get(original["id"])
    assert job["status"] == original["status"] == ("completed" if kind == "existing_material" else "queued")
    assert job["items"] == original["items"]
    assert job["payload"] == original["payload"]
    assert job["grant"] == original["grant"]
    assert job["grant"]["registration_generation"] == config["registration_generation"]
    assert job["grant"]["allowed_new_paths"] == []
    assert [item["doc_id"] for item in job["grant"]["selected_documents"]] == ([] if document is None else [document["id"]])
    with current.store.connect() as db:
        for table, rows in original_rows.items():
            assert [tuple(row) for row in db.execute(f"SELECT * FROM {table} ORDER BY rowid")] == rows
        reservations = [tuple(row) for row in db.execute("SELECT path,purpose,state FROM wiki_path_reservations")]
    assert reservations == ([(f"Library/Inbox/{job['id']}.md", "framework_material", "reserved")]
                            if kind == "reserved_material" else [])
    if submitted:
        first = current.ingest(job["id"])
        assert current.ingest(job["id"])["id"] == first["id"]
        assert (root / first["path"]).read_bytes() == material.encode()
        if document is not None:
            assert first["id"] == document["id"]
        with current.store.connect() as db:
            assert db.execute("SELECT count(*) FROM changes").fetchone()[0] == int(document is None)
            if document is None:
                reservation = db.execute("SELECT state,document_id,output_revision FROM wiki_path_reservations WHERE job_id=?",
                                         (job["id"],)).fetchone()
                assert tuple(reservation) == ("bound", first["id"], first["revision"])
        expected_files = original_files | {first["path"]: material.encode()}
    else:
        assert current.next_queued() == job["id"]
        expected_files = original_files
    assert {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*.md")} == expected_files
    assert config_path.read_bytes() == original_config


async def test_completed_retained_rows_do_not_gain_new_authority(client, tmp_path):
    root = tmp_path / "vault"
    root.mkdir()
    await wiki_api.configure(client, root)
    owner = (await wiki_api.session_manager.db.get_system_agent())["user_id"]
    jobs = WikiJobs(owner)
    raw = '{"kind":"curation","opaque":{"original":true}}'
    with jobs.store.connect() as db:
        db.execute("""INSERT INTO jobs(id,request_key,payload_hash,payload,status,items,created_at,updated_at)
                      VALUES('completed-original','completed-original','hash',?,'completed','[]','original-time','original-time')""", (raw,))
        db.execute("INSERT INTO job_requests VALUES('completed-original','hash','completed-original')")
    registry.close()
    current = WikiJobs(owner)
    job = current.get("completed-original")
    assert job["status"] == "completed" and job["grant"] is None and job["error"] is None
    with current.store.connect() as db:
        assert db.execute("SELECT payload FROM jobs WHERE id='completed-original'").fetchone()[0] == raw
        assert db.execute("SELECT count(*) FROM wiki_job_grants").fetchone()[0] == 0
    assert current.retry("completed-original") == job
    assert current.next_queued() is None


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
    jobs = WikiJobs((await wiki_api.session_manager.db.get_system_agent())["user_id"])
    proposal = {"purpose": "requested_knowledge_page", "paths": ["Library/new.md"]} if path is None else None
    job = jobs.maintain("retain-job", "", "Finite work", [path] if path else [], None, [], "human", creation=proposal)
    if path:
        async with assigned(job, old_config, client=client) as (headers, _):
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
    registry.configure((await wiki_api.session_manager.db.get_system_agent())["user_id"], root, "a-new-curator")
    current = WikiJobs((await wiki_api.session_manager.db.get_system_agent())["user_id"])
    assert current.next_queued() is None
    refused = await client.post(f"/api/wiki/jobs/{job['id']}/retry", headers=client.headers)
    assert refused.status_code == 409
    foreign = await client.post(f"/api/wiki/jobs/{job['id']}/readmit", headers=client.headers, json={"paths": [outsider]})
    body = {"paths": [path]} if path else {"creation": proposal}
    accepted = await client.post(f"/api/wiki/jobs/{job['id']}/readmit", headers=client.headers, json=body)
    assert accepted.status_code == 200, accepted.text
    assert foreign.status_code == 409 and "identities" in foreign.text, foreign.text
    assert accepted.json()["id"] == job["id"] and accepted.json()["request_key"] == job["request_key"]
    assert accepted.json()["grant"]["registration_generation"] == registry.config((await wiki_api.session_manager.db.get_system_agent())["user_id"])["registration_generation"]
    replay = await client.post(f"/api/wiki/jobs/{job['id']}/readmit", headers=client.headers, json=body)
    assert replay.status_code == 200 and replay.json() == accepted.json(), replay.text
    new_asset = "Library/assets/project/new.md"
    new_page = await client.post("/api/wiki/maintain", headers=client.headers, json={
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
    store = registry.store((await wiki_api.session_manager.db.get_system_agent())["user_id"])
    document = store.read("Library/note.md")
    job = stopped_job((await wiki_api.session_manager.db.get_system_agent())["user_id"], root, document)
    WikiJobs((await wiki_api.session_manager.db.get_system_agent())["user_id"])
    session = SimpleNamespace(id="scope-caller", user_id=(await wiki_api.session_manager.db.get_system_agent())["user_id"], agent_id=config["curator_agent_id"])
    token = tasks.open(session, "auto")
    try:
        headers = {"Authorization": "Bearer " + mint(session.id, user_id=session.user_id, wiki_task_id=token)}
        forged = await client.post(f"/api/wiki/jobs/{job['id']}/readmit", headers=headers, json={"paths": [document["path"]]})
        assert forged.status_code == 403
    finally:
        tasks.close(token)
    accepted = await client.post(f"/api/wiki/jobs/{job['id']}/readmit", headers=client.headers, json={"paths": [document["path"]]})
    assert accepted.status_code == 200, accepted.text
    changed = await client.post(f"/api/wiki/jobs/{job['id']}/readmit", headers=client.headers, json={"creation": {"purpose": "requested_knowledge_page"}})
    assert changed.status_code == 409


@pytest.mark.parametrize("missing", [*grants.TABLES, "marker"])
async def test_publication_rejects_lost_grant_authority_without_rebuilding_search_cache(client, tmp_path, missing):
    from server.storage_verification import verify_wiki

    root = tmp_path / "vault"
    root.mkdir()
    await wiki_api.configure(client, root)
    job = WikiJobs((await wiki_api.session_manager.db.get_system_agent())["user_id"]).maintain("retained-output", "", "Create one requested page", [], None, [], "human",
                                 creation={"purpose": "requested_knowledge_page", "paths": ["Library/new.md"]})
    store = registry.store((await wiki_api.session_manager.db.get_system_agent())["user_id"])
    with store.connect() as db:
        db.execute("DROP TABLE search_documents")
        if missing == "marker":
            db.execute("DELETE FROM wiki_protocols WHERE name='wiki_grants'")
        else:
            db.execute("DROP TABLE " + missing)
    with closing(sqlite3.connect(store.db_path.as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        with pytest.raises(RuntimeError, match="current durable structure|incompatible structure or protocol"):
            verify_wiki(db, {"root": str(store.root), "vault_id": store.vault_id})
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
    store = registry.store((await wiki_api.session_manager.db.get_system_agent())["user_id"])
    document = store.read("Library/note.md")
    job = stopped_job((await wiki_api.session_manager.db.get_system_agent())["user_id"], root, document,
                      creation={"purpose": "requested_knowledge_page", "paths": ["Library/new.md"]})
    current = WikiJobs((await wiki_api.session_manager.db.get_system_agent())["user_id"])
    accepted = await client.post(f"/api/wiki/jobs/{job['id']}/readmit", headers=client.headers, json={
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
