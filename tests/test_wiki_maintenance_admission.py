"""Final admission owns one writer; failed commits cannot leave accepted work."""

import asyncio
import sqlite3
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta
from uuid import uuid4

import pytest

from server import mcp_identity
from server.wiki import maintenance_admission, maintenance_selections, schema
from server.wiki.jobs import worker
from server.wiki.store import WikiError
from server.wiki.tasks import tasks
from tests.test_wiki_maintenance_selections import FILES, select, submission
from tests.test_wiki_maintenance_state import client as client
from tests.test_wiki_maintenance_state import entry, prepare


def ledger_counts(store):
    with store.connect() as db:
        return tuple(db.execute("SELECT count(*) FROM " + table).fetchone()[0]
                     for table in ("jobs", "job_requests", "job_selection_evidence"))


async def test_concurrent_equivalent_selection_admissions_commit_one_job_and_both_observations(client, tmp_path, monkeypatch):
    _, _, jobs = await prepare(client, tmp_path, FILES)
    selections = [await select(client, limit=1) for _ in range(2)]
    wakes = []
    monkeypatch.setattr(worker, "wake", lambda _: wakes.append(True))
    responses = await asyncio.gather(*(client.post("/api/wiki/maintain", headers=client.headers,
        json=submission(selection, selection["items"][0])) for selection in selections))
    assert all(response.status_code == 200 for response in responses), [r.text for r in responses]
    assert responses[0].json()["id"] == responses[1].json()["id"]
    assert ledger_counts(jobs.store) == (1, 1, 2) and wakes == [True]
    result = jobs.get(responses[0].json()["id"])
    assert {row["selection_id"] for row in result["selection_evidence"]} == {s["id"] for s in selections}
    assert all(row["review"] == selections[0]["items"][0]["review"] for row in result["selection_evidence"])


async def test_final_transaction_exit_failure_rolls_back_before_worker_wake(client, tmp_path, monkeypatch):
    _, _, jobs = await prepare(client, tmp_path, FILES)
    selection = await select(client, limit=1)
    connect, injected, wakes = jobs.store.connect, [], []

    @contextmanager
    def fail_before_commit():
        with connect() as db:
            yield db
            if db.in_transaction and db.execute("SELECT 1 FROM job_selection_evidence LIMIT 1").fetchone():
                injected.append(True)
                raise WikiError("Injected failure at final transaction exit", 503)

    monkeypatch.setattr(jobs.store, "connect", fail_before_commit)
    monkeypatch.setattr(worker, "wake", lambda _: wakes.append(True))
    response = await client.post("/api/wiki/maintain", headers=client.headers,
                                 json=submission(selection, selection["items"][0]))
    assert response.status_code == 503 and injected == [True], response.text
    assert ledger_counts(jobs.store) == (0, 0, 0) and not wakes


@pytest.mark.parametrize("revocation", ["turn", "replacement"])
async def test_authority_lost_after_preparation_is_rechecked_before_accepting(client, tmp_path, monkeypatch, revocation):
    _, _, jobs = await prepare(client, tmp_path, FILES)
    prepare_admission = maintenance_admission.prepare
    async with entry(client) as (headers, task):
        selection = await select(client, headers=headers, limit=1)

        def revoke(*args, **kwargs):
            prepare_admission(*args, **kwargs)
            token = next(token for token, active in tasks.active.items() if active is task)
            if revocation == "turn":
                tasks.close(token)
            else:
                # Grants are immutable; rebinding the token cannot lend a new
                # turn's authority to the already prepared old request.
                tasks.active[token] = replace(task, mode="read")

        monkeypatch.setattr(maintenance_admission, "prepare", revoke)
        response = await client.post("/api/wiki/maintain", headers=headers,
                                     json=submission(selection, selection["items"][0]))
        assert response.status_code == 403, response.text
        assert ledger_counts(jobs.store) == (0, 0, 0)


@pytest.mark.parametrize("selected", [False, True])
@pytest.mark.parametrize("revocation", ["run", "work"])
@pytest.mark.parametrize("existing", [False, True])
async def test_original_mcp_scope_revoked_after_preparation_cannot_accept_or_add_evidence(
    client, tmp_path, monkeypatch, selected, revocation, existing,
):
    _, _, jobs = await prepare(client, tmp_path, FILES)
    monkeypatch.setattr(mcp_identity, "_run_leases", {})
    monkeypatch.setattr(mcp_identity, "_closed_works", set())
    prepare_admission = maintenance_admission.prepare
    wakes = []
    monkeypatch.setattr(worker, "wake", lambda _: wakes.append(True))
    async with entry(client) as (diagnostic_headers, task):
        diagnostic = mcp_identity.verify(diagnostic_headers["Authorization"].removeprefix("Bearer "))
        lease, work = uuid4().hex, uuid4().hex
        mcp_identity.register_run(lease, task.session_id, task.user_id, work)
        bearer = mcp_identity.mint(task.session_id, user_id=task.user_id,
                                  wiki_task_id=diagnostic.wiki_task_id, run_lease_id=lease, work_id=work)
        scope = mcp_identity.verify(bearer)
        assert mcp_identity.scope_is_live(scope)
        headers = {"Authorization": "Bearer " + bearer}
        first, second = await select(client, headers=headers, limit=1), await select(client, headers=headers, limit=1)

        def body(selection):
            result = submission(selection, selection["items"][0])
            if not selected:
                result.pop("selection_id")
                result.pop("selection_ordinal")
            return result

        if existing:
            accepted = await client.post("/api/wiki/maintain", headers=headers, json=body(first))
            assert accepted.status_code == 200, accepted.text
        before = ledger_counts(jobs.store)
        wakes.clear()
        injected = []

        def retire(*args, **kwargs):
            prepare_admission(*args, **kwargs)
            if revocation == "run":
                mcp_identity.revoke_run(lease)
            else:
                mcp_identity.close_work(work)
            assert tasks.active[diagnostic.wiki_task_id] is task
            assert not mcp_identity.scope_is_live(scope)
            injected.append(True)

        monkeypatch.setattr(maintenance_admission, "prepare", retire)
        rejected = await client.post("/api/wiki/maintain", headers=headers, json=body(second))
        assert injected == [True] and rejected.status_code == 403, rejected.text
        assert ledger_counts(jobs.store) == before and not wakes


async def test_unavailable_final_read_is_not_reported_as_a_missing_document(client, tmp_path, monkeypatch):
    _, _, jobs = await prepare(client, tmp_path, FILES)
    selection = await select(client, limit=1)

    def unavailable(*args, **kwargs):
        raise WikiError("Known filesystem notifications are unavailable", 503)

    monkeypatch.setattr(jobs.store, "read", unavailable)
    response = await client.post("/api/wiki/maintain", headers=client.headers,
                                 json=submission(selection, selection["items"][0]))
    assert response.status_code == 503 and "unavailable" in response.json()["detail"], response.text
    assert ledger_counts(jobs.store) == (0, 0, 0)


@pytest.mark.parametrize("statement", [
    "UPDATE index_state SET error='Synthetic index failure' WHERE singleton=1",
    "UPDATE filesystem_state SET armed_epoch='uncommitted-reception' WHERE singleton=1",
    "UPDATE catalog_state SET pending=1 WHERE singleton=1",
])
async def test_readiness_lost_after_preparation_prevents_final_admission(client, tmp_path, monkeypatch, statement):
    _, _, jobs = await prepare(client, tmp_path, FILES)
    selection = await select(client, limit=1)
    prepare_admission = maintenance_admission.prepare

    def change(*args, **kwargs):
        prepare_admission(*args, **kwargs)
        with jobs.store.connect() as db:
            db.execute(statement)

    monkeypatch.setattr(maintenance_admission, "prepare", change)
    response = await client.post("/api/wiki/maintain", headers=client.headers,
                                 json=submission(selection, selection["items"][0]))
    assert response.status_code == 503 and response.json()["code"] == "maintenance_updating", response.text
    assert ledger_counts(jobs.store) == (0, 0, 0)


async def test_new_no_change_baseline_after_preparation_invalidates_the_frozen_observation(client, tmp_path, monkeypatch):
    from server.wiki import maintenance_projection
    from tests.test_wiki_maintenance_inputs import enqueue, receipt

    _, _, jobs = await prepare(client, tmp_path, FILES)
    selection = await select(client, limit=1)
    frozen = selection["items"][0]
    doc = jobs.store.read(frozen["doc_id"], length=1)
    prepare_admission = maintenance_admission.prepare
    injected = []

    def complete_equivalent_review(*args, **kwargs):
        prepare_admission(*args, **kwargs)
        job = enqueue(jobs, doc, "completed-before-admission", status="done", index_complete=True,
                      output=doc["path"], output_revision=doc["revision"], receipt_id="receipt")
        with jobs.store.connect() as db:
            receipt(db, job, doc, doc)
        jobs.update(job["id"], status="completed")
        assert maintenance_projection.settle(jobs.store, doc=doc["id"])
        injected.append(job["id"])

    monkeypatch.setattr(maintenance_admission, "prepare", complete_equivalent_review)
    response = await client.post("/api/wiki/maintain", headers=client.headers, json=submission(selection, frozen))
    assert response.status_code == 409 and response.json()["code"] == "review_changed", response.text
    assert len(injected) == 1 and ledger_counts(jobs.store) == (1, 1, 0)
    assert jobs.store.read(frozen["doc_id"], length=1)["revision"] == frozen["observed_revision"]


@pytest.mark.parametrize("selected", [False, True])
async def test_final_writer_has_no_nested_connection_or_synchronizing_read_and_wakes_after_commit(client, tmp_path, monkeypatch, selected):
    _, _, jobs = await prepare(client, tmp_path, FILES)
    selection = await select(client, limit=1)
    body = submission(selection, selection["items"][0])
    if not selected:
        body.pop("selection_id")
        body.pop("selection_ordinal")
    inside = []
    visited = []
    connect, read, synchronize = jobs.store.connect, jobs.store.read, jobs.store.synchronize
    current, commit = maintenance_admission._current, maintenance_admission.commit

    @contextmanager
    def connection():
        assert not inside, "Final admission opened another connection"
        with connect() as db:
            yield db

    def check(*args, **kwargs):
        assert args[1].in_transaction
        inside.append(True)
        visited.append(True)
        return current(*args, **kwargs)

    def final(*args, **kwargs):
        try:
            return commit(*args, **kwargs)
        finally:
            inside.clear()

    def guarded_read(*args, **kwargs):
        assert not inside, "Final admission synchronized a document"
        return read(*args, **kwargs)

    def guarded_sync(*args, **kwargs):
        assert not inside, "Final admission synchronized the index"
        return synchronize(*args, **kwargs)

    wakes = []
    monkeypatch.setattr(jobs.store, "connect", connection)
    monkeypatch.setattr(jobs.store, "read", guarded_read)
    monkeypatch.setattr(jobs.store, "synchronize", guarded_sync)
    monkeypatch.setattr(maintenance_admission, "_current", check)
    monkeypatch.setattr(maintenance_admission, "commit", final)
    monkeypatch.setattr(worker, "wake", lambda _: wakes.append(ledger_counts(jobs.store)))
    first = await client.post("/api/wiki/maintain", headers=client.headers, json=body)
    assert first.status_code == 200, first.text
    replay = await client.post("/api/wiki/maintain", headers=client.headers, json=body)
    assert replay.status_code == 200 and replay.json()["id"] == first.json()["id"], replay.text
    assert visited == [True, True] and wakes == [(1, 1, int(selected))]


@pytest.mark.parametrize("existing", [False, True])
async def test_failure_after_provenance_insert_rolls_back_job_alias_and_evidence(client, tmp_path, monkeypatch, existing):
    _, _, jobs = await prepare(client, tmp_path, FILES)
    first, second = await select(client, limit=1), await select(client, limit=1)
    if existing:
        accepted = await client.post("/api/wiki/maintain", headers=client.headers, json=submission(first, first["items"][0]))
        assert accepted.status_code == 200, accepted.text
    accept = maintenance_selections.accept
    injected, wakes = [], []

    def crash(*args, **kwargs):
        accept(*args, **kwargs)
        injected.append(True)
        raise WikiError("Injected failure after accepted evidence", 503)

    monkeypatch.setattr(maintenance_selections, "accept", crash)
    monkeypatch.setattr(worker, "wake", lambda _: wakes.append(True))
    failed = await client.post("/api/wiki/maintain", headers=client.headers, json=submission(second, second["items"][0]))
    assert failed.status_code == 503 and injected == [True], failed.text
    assert ledger_counts(jobs.store) == ((1, 1, 1) if existing else (0, 0, 0))
    assert not wakes


async def test_selection_expiring_after_preparation_cannot_commit_a_job(client, tmp_path, monkeypatch):
    _, _, jobs = await prepare(client, tmp_path, FILES)
    selection = await select(client, limit=1)
    prepare_admission = maintenance_admission.prepare

    def expire(*args, **kwargs):
        prepare_admission(*args, **kwargs)
        later = datetime.fromisoformat(selection["expires_at"]) + timedelta(seconds=1)
        monkeypatch.setattr(maintenance_selections, "utcnow", lambda: later)

    monkeypatch.setattr(maintenance_admission, "prepare", expire)
    response = await client.post("/api/wiki/maintain", headers=client.headers, json=submission(selection, selection["items"][0]))
    assert response.status_code == 410 and response.json()["code"] == "selection_expired", response.text
    assert ledger_counts(jobs.store) == (0, 0, 0)


@pytest.mark.parametrize("after_prepare", [False, True])
async def test_source_only_change_rejects_frozen_review_without_accepting_provenance(client, tmp_path, monkeypatch, after_prepare):
    from server.wiki.store import revision

    source = "# Source\nOriginal support.\n"
    target = "---\nsources:\n  - resource: source.md\n    revision: " + revision(source.encode()) + "\n    location: L1-L2\n---\n# A\nDerived.\n"
    root, _, jobs = await prepare(client, tmp_path, FILES | {"Library/a.md": target, "Library/source.md": source})
    selection = await select(client, limit=1)
    frozen = selection["items"][0]
    assert frozen["path"] == "Library/a.md"

    def change_source():
        (root / "Library/source.md").write_text("# Source\nChanged support only.\n")
        jobs.store.refresh()

    if after_prepare:
        prepare_admission = maintenance_admission.prepare

        def race(*args, **kwargs):
            prepare_admission(*args, **kwargs)
            change_source()

        monkeypatch.setattr(maintenance_admission, "prepare", race)
    else:
        change_source()
    rejected = await client.post("/api/wiki/maintain", headers=client.headers, json=submission(selection, frozen))
    assert rejected.status_code in {409, 503}, rejected.text
    assert jobs.store.read(frozen["doc_id"])["revision"] == frozen["observed_revision"]
    assert ledger_counts(jobs.store) == (0, 0, 0)


@pytest.mark.parametrize("statement", [
    "DELETE FROM maintenance_selections",
    "DELETE FROM maintenance_selection_items WHERE ordinal=0",
    "UPDATE maintenance_selection_items SET review_json='{}' WHERE ordinal=0",
    "DELETE FROM job_selection_evidence",
    "UPDATE job_selection_evidence SET review_key='forged'",
])
async def test_accepted_observation_and_provenance_cannot_be_deleted_or_rewritten(client, tmp_path, statement):
    _, _, jobs = await prepare(client, tmp_path, FILES)
    selection = await select(client, limit=1)
    response = await client.post("/api/wiki/maintain", headers=client.headers, json=submission(selection, selection["items"][0]))
    assert response.status_code == 200, response.text
    with jobs.store.connect() as db, pytest.raises(sqlite3.IntegrityError):
        db.execute(statement)
    result = jobs.get(response.json()["id"])
    assert result["selection_evidence"][0]["review"] == selection["items"][0]["review"]


async def test_expired_cleanup_keeps_accepted_full_evidence_and_removes_only_unaccepted_selection(client, tmp_path, monkeypatch):
    _, _, jobs = await prepare(client, tmp_path, FILES)
    accepted, unaccepted = await select(client, limit=1), await select(client, limit=1)
    response = await client.post("/api/wiki/maintain", headers=client.headers, json=submission(accepted, accepted["items"][0]))
    assert response.status_code == 200, response.text
    later = max(datetime.fromisoformat(s["expires_at"]) for s in (accepted, unaccepted)) + timedelta(seconds=1)
    monkeypatch.setattr(maintenance_selections, "utcnow", lambda: later)
    with jobs.store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        maintenance_selections.cleanup(db)
        assert [r[0] for r in db.execute("SELECT id FROM maintenance_selections")] == [accepted["id"]]
        assert db.execute("SELECT count(*) FROM maintenance_selection_items").fetchone()[0] == 3
    job = jobs.get(response.json()["id"])
    assert job["selection_evidence"][0]["review"] == accepted["items"][0]["review"]


@pytest.mark.parametrize("missing", ["maintenance_selections", "maintenance_selection_items", "job_selection_evidence", "marker"])
async def test_missing_initialized_selection_ledger_fails_before_disposable_cache_repair(client, tmp_path, missing):
    _, _, jobs = await prepare(client, tmp_path, FILES)
    with jobs.store.connect() as db:
        db.execute("DROP TABLE search_documents")
        if missing == "marker":
            db.execute("DELETE FROM wiki_protocols WHERE name='maintenance_selections'")
        else:
            db.execute("DROP TABLE " + missing)
    with pytest.raises(WikiError) as unavailable:
        schema.initialize(jobs.store)
    assert unavailable.value.status == 503
    with jobs.store.connect() as db:
        assert db.execute("SELECT count(*) FROM documents").fetchone()[0] == 3
        assert not db.execute("SELECT 1 FROM sqlite_master WHERE name='search_documents'").fetchone()
        if missing != "marker":
            assert not db.execute("SELECT 1 FROM sqlite_master WHERE name=?", (missing,)).fetchone()


@pytest.mark.parametrize("table", ["maintenance_selections", "maintenance_selection_items", "job_selection_evidence"])
async def test_accepted_selection_replace_cannot_bypass_guards_with_recursive_triggers_off(client, tmp_path, table):
    _, _, jobs = await prepare(client, tmp_path, FILES)
    selection = await select(client, limit=1)
    response = await client.post("/api/wiki/maintain", headers=client.headers, json=submission(selection, selection["items"][0]))
    assert response.status_code == 200, response.text
    db = sqlite3.connect(jobs.store.db_path)
    db.row_factory = sqlite3.Row
    try:
        db.execute("PRAGMA recursive_triggers=OFF")
        row = dict(db.execute("SELECT * FROM " + table + " LIMIT 1").fetchone())
        field = {"maintenance_selections": "expires_at", "maintenance_selection_items": "review_json",
                 "job_selection_evidence": "accepted_at"}[table]
        if field == "review_json":
            import json

            review = json.loads(row[field])
            review["review_key"] = "rewritten-observation"
            row[field] = json.dumps(review)
        else:
            row[field] = "2099-01-01T00:00:00+00:00"
        with db, pytest.raises(sqlite3.IntegrityError):
            db.execute("INSERT OR REPLACE INTO " + table + " VALUES(" + ",".join("?" for _ in row) + ")", tuple(row.values()))
    finally:
        db.close()


@pytest.mark.parametrize("table", ["maintenance_selections", "maintenance_selection_items"])
async def test_unaccepted_observations_cannot_be_replaced(client, tmp_path, table):
    _, _, jobs = await prepare(client, tmp_path, FILES)
    await select(client, limit=1)
    with jobs.store.connect() as db:
        row = dict(db.execute("SELECT * FROM " + table + " LIMIT 1").fetchone())
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("INSERT OR REPLACE INTO " + table + " VALUES(" + ",".join("?" for _ in row) + ")", tuple(row.values()))
