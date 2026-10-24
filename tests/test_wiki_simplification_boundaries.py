"""Simplifications preserve observation identity, history and writer ownership."""

import asyncio
import json
from contextlib import contextmanager

import pytest

from server.wiki import app_bridge, catalog, maintenance_inputs, schema
from server.wiki.history import WikiHistory
from tests.test_wiki_history import history as history
from tests.test_wiki_maintenance_input_boundaries import facts
from tests.test_wiki_maintenance_inputs import (
    captured,
    enqueue,
    members,
    publish,
    receipt,
)
from tests.test_wiki_maintenance_inputs import prepared as prepared
from tests.test_wiki_maintenance_selections import FILES, select, submission
from tests.test_wiki_maintenance_state import client as client
from tests.test_wiki_maintenance_state import prepare, state


@pytest.mark.parametrize("operation", [None, "null", "false", "{}", "[]", '""', "invalid"])
@pytest.mark.parametrize("resolved", [False, True])
def test_history_proposal_presence_and_private_columns(history, monkeypatch, operation, resolved):
    change = history.record("note.md", b"original", b"candidate", "human", "proposal", "conflict")
    with history.store.connect() as db:
        if operation is not None:
            db.execute("INSERT INTO change_proposals VALUES(?,?)", (change["id"], operation))
        if resolved:
            db.execute("UPDATE change_retention SET resolved_at='resolved' WHERE change_id=?", (change["id"],))
    connect, calls = history.store.connect, []

    @contextmanager
    def counted():
        calls.append(True)
        with connect() as db:
            yield db

    with history.locked(), monkeypatch.context() as patch:
        patch.setattr(history.store, "connect", counted)
        if operation == "invalid" and not resolved:
            with pytest.raises(json.JSONDecodeError):
                history._history("note.md")
        else:
            page = history._history("note.md", limit=1)
            item = page["items"][0]
            assert item["id"] == change["id"] and page["next_offset"] is None
            assert "proposal_operation" not in item
            assert item["recoverable"]["after"] is (operation is None)
            if operation is not None and not resolved:
                assert "proposed_operation" in item
                assert item["proposed_operation"] == json.loads(operation)
            else:
                assert "proposed_operation" not in item
            assert ("proposed_content" in item) is (operation is None and not resolved)
    assert len(calls) == 1


def test_membership_survives_removal_of_one_of_two_roles(prepared):
    jobs, docs = prepared
    job = enqueue(jobs, docs["a"])
    with jobs.store.connect() as db:
        receipt(db, job, docs["a"], docs["a"])
    assert maintenance_inputs.normalize(jobs.store)
    before = captured(jobs.store, docs["a"]["id"])
    with jobs.store.connect() as db:
        count = db.execute("SELECT count(*) FROM maintenance_job_members WHERE job_id=?", (job["id"],)).fetchone()[0]
        assert count == 1
        db.execute("DELETE FROM operation_receipts WHERE id='receipt'")
    assert members(jobs.store, job) == {docs["a"]["id"]}
    assert maintenance_inputs.normalize(jobs.store)
    assert not publish(jobs.store, before, docs["a"])
    job["items"][0]["doc"] = docs["b"]["id"]
    jobs.update(job["id"], items=job["items"])
    assert members(jobs.store, job) == {docs["b"]["id"]}


@pytest.mark.parametrize("repair", ["legacy_role_cache", "legacy_index", "reopen"])
async def test_storage_simplification_preserves_reviews_and_frozen_selection(client, tmp_path, repair):
    _, _, jobs = await prepare(client, tmp_path, FILES)
    a, b = (jobs.store.read(f"Library/{name}.md") for name in ("a", "b"))
    content = f"---\nsources:\n  - resource: b.md\n    revision: {b['revision']}\n    location: L1\n---\n# A\nKeep the source relationship.\n"
    WikiHistory(jobs.store).write(a["path"], content.encode(), a["revision"], "human", "source")
    a = jobs.store.read(a["id"])
    old_calculation = captured(jobs.store, a["id"])
    assert a["id"] in old_calculation["tokens"]
    before = await state(client, limit=1)
    selection = await select(client, limit=1)
    original_review = selection["items"][0]["review"]
    assert original_review["doc"] == a["id"]
    original_facts = facts(jobs.store)
    with jobs.store.connect() as db:
        original_edges = list(map(tuple, db.execute("SELECT * FROM wiki_edges ORDER BY source_doc,ordinal")))
        assert any(row[-2] for row in original_edges), "The digest control needs a nonempty lookup_key"
        if repair == "legacy_role_cache":
            db.execute("BEGIN IMMEDIATE")
            for row in db.execute("SELECT name FROM sqlite_master WHERE type='trigger'").fetchall():
                if row[0].startswith(maintenance_inputs.PREFIX):
                    db.execute(f'DROP TRIGGER "{row[0]}"')
            db.execute("DROP TABLE maintenance_job_members")
            db.execute("CREATE TABLE maintenance_job_members(job_id TEXT NOT NULL,doc_id TEXT NOT NULL,role TEXT NOT NULL,PRIMARY KEY(job_id,doc_id,role))")
            db.execute("UPDATE maintenance_input_state SET version='maintenance-inputs-v4'")
        elif repair == "legacy_index":
            db.execute("CREATE INDEX wiki_edge_lookup ON wiki_edges(lookup_key)")
    if repair == "reopen":
        from server.wiki.registry import registry

        registry.close()
        jobs.store = registry.store(jobs.user_id)
        jobs.store.refresh()
    else:
        schema.initialize(jobs.store)
    # Force an equivalent edge rebuild as well as schema/index initialization.
    with jobs.store.connect() as db:
        db.execute("INSERT OR IGNORE INTO catalog_dirty VALUES(?)", (a["id"],))
        db.execute("UPDATE catalog_state SET pending=1")
        catalog.finish(db)
        assert list(map(tuple, db.execute("SELECT * FROM wiki_edges ORDER BY source_doc,ordinal"))) == original_edges
        assert not db.execute("SELECT 1 FROM sqlite_master WHERE name='wiki_edge_lookup'").fetchone()
    after = await state(client, limit=1)
    assert after["items"][0]["review"] == original_review
    assert facts(jobs.store) == original_facts
    if repair == "legacy_role_cache":
        assert after["generation"] != before["generation"]
        stale = await client.get("/api/wiki/maintenance-status", headers=client.headers,
                                 params={"limit": 1, "cursor": before["next_cursor"]})
        assert stale.status_code == 409
        assert not publish(jobs.store, old_calculation, a)
    equivalent = await select(client, limit=1)
    accepted = []
    for selected in (selection, equivalent, selection):
        result = await client.post("/api/wiki/maintain", headers=client.headers,
                                   json=submission(selected, selected["items"][0]))
        assert result.status_code == 200, result.text
        accepted.append(result.json()["id"])
    assert len(set(accepted)) == 1


async def test_legacy_owner_shutdown_leaves_other_preparation_running(monkeypatch):
    assert not app_bridge._preparations
    entered = {None: asyncio.Event(), "other": asyncio.Event()}
    release = asyncio.Event()

    async def preparing(row, *args):
        entered[row["user_id"]].set()
        await release.wait()

    monkeypatch.setattr(app_bridge, "_begin", preparing)
    legacy = asyncio.create_task(app_bridge.begin({"user_id": None}, "POST", "save", {}))
    other = asyncio.create_task(app_bridge.begin({"user_id": "other"}, "POST", "save", {}))
    try:
        async with asyncio.timeout(3):
            await asyncio.gather(*(event.wait() for event in entered.values()))
            await app_bridge.shutdown(user_id=None)
            with pytest.raises(asyncio.CancelledError):
                await legacy
            assert not other.done()
            app_bridge.reopen(user_id=None)
            with pytest.raises(RuntimeError, match="have not retired"):
                app_bridge.reopen(user_id="other")
    finally:
        release.set()
        await asyncio.gather(legacy, other, return_exceptions=True)
    assert not app_bridge._preparations
