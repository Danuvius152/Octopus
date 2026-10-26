"""Source rechecks settle without manufacturing another content change."""

import json
import uuid
from types import SimpleNamespace

import pytest

from server.mcp_identity import mint
from server.wiki import registry, source_bindings
from server.wiki.history import WikiHistory, atomic
from server.wiki.tasks import tasks
from tests import test_wiki_maintenance_state as maintenance

client = maintenance.client


async def confirm(client, config, jobs, path, key, *, read_sources=True):
    job = jobs.maintain(key, "", "Recheck the existing direct sources", [path], None, [], "human")
    session = SimpleNamespace(id="source-confirmation-worker", user_id=jobs.user_id,
                              agent_id=config["curator_agent_id"])
    jobs.update(job["id"], status="running", session_id=session.id)
    tasks.assign(session.id, job["id"], job["items"])
    token = tasks.open(session, "maintain")
    headers = {"Authorization": "Bearer " + mint(session.id, user_id=session.user_id, wiki_task_id=token)}
    try:
        response = await client.get("/api/wiki/read", params={"doc": path}, headers=headers)
        assert response.status_code == 200, response.text
        document = response.json()
        checks = []
        for ref in document["metadata"]["source_refs"]:
            if read_sources:
                source = await client.get("/api/wiki/read", params={"doc": ref["doc"]}, headers=headers)
                assert source.status_code == 200, source.text
                current = source.json()
            else:
                current = jobs.store.read(ref["doc"])
            checks.append({"resource": ref["resource"], "doc": current["id"],
                           "revision": current["revision"], "location": ref["location"]})
        response = await client.post("/api/wiki/revise", headers=headers, json={
            "doc": document["id"], "expected_revision": document["revision"],
            "operation": "confirm_sources", "checks": checks,
            "reason": "The current sources still support the existing explanation",
        })
        assert response.status_code == (200 if read_sources else 422), response.text
        value = response.json()
    finally:
        tasks.close(token)
        tasks.unassign(session.id)
    item = job["items"][0]
    if not read_sources:
        with jobs.store.connect() as db:
            assert not db.execute("SELECT 1 FROM operation_receipts WHERE job_id=?", (job["id"],)).fetchone()
        return document, value
    assert jobs.recover_publication(job["id"], item)
    jobs.update(job["id"], status="completed", items=job["items"], session_id=None)
    return document, value


async def test_three_reciprocal_notes_finish_source_review_without_version_churn(client, tmp_path):
    files = {
        "Library/a.md": "---\nsources:\n  - {resource: b.md, revision: earlier, location: L1-L2}\n  - {resource: c.md, revision: earlier, location: L1-L2}\n---\n# A\nExisting explanation A.\n",
        "Library/b.md": "---\nsources:\n  - {resource: a.md, revision: earlier, location: L1-L2}\n---\n# B\nExisting explanation B.\n",
        "Library/c.md": "---\nsources:\n  - {resource: a.md, revision: earlier, location: L1-L2}\n---\n# C\nExisting explanation C.\n",
    }
    vault, config, jobs = await maintenance.prepare(client, tmp_path, files)
    original = {path: (vault / path).read_bytes() for path in files}
    for path in files:
        before, result = await confirm(client, config, jobs, path, "recheck-" + path)
        assert result["revision"] == before["revision"], "A source check is not a new article version"
        assert result["changed"] is False and result["change_id"] is None
    assert (await maintenance.state(client))["items"] == []
    for path, raw in original.items():
        assert (vault / path).read_bytes() == raw
        current = (await maintenance.current_read(client, path)).json()
        assert current["stale"] is False
        assert all(ref["status"] == "indexed" for ref in current["metadata"]["source_refs"])
    # A real content update must still invalidate its direct dependants.
    source = vault / "Library/b.md"
    source.write_text(source.read_text() + "\nA material new condition.\n")
    await maintenance.current_read(client, "Library/b.md")
    changed = (await maintenance.state(client, "Library/a.md"))["items"][0]
    assert changed["status"] == "unchanged" and changed["source_warnings"] == {"changed": 1}
    assert changed["actionable"]
    unaffected = (await maintenance.state(client, "Library/c.md"))["items"][0]
    assert unaffected["source_warnings"] == {} and not unaffected["actionable"]


@pytest.mark.parametrize("transition", ["move", "journal_move", "unchanged_save", "reopen_rebuild", "edit", "restore_same_bytes", "restore_after_edit", "copy", "replace_source"])
async def test_source_checks_follow_moves_and_rebuilds_but_not_restores_or_copies(client, tmp_path, transition):
    vault, config, jobs = await maintenance.prepare(client, tmp_path, {"Library/source.md": "# Source\nA condition.\n"})
    store = jobs.store
    history = WikiHistory(store)
    raw = b"---\nsources:\n  - {resource: source.md, revision: earlier, location: L1-L2}\n---\n# Note\nExisting interpretation.\n"
    created = history.write("Library/note.md", raw, None, "human", "Create a source-backed note")
    before, result = await confirm(client, config, jobs, "Library/note.md", "initial-confirmation")
    with store.connect() as db:
        observation = source_bindings.current(db, before["id"])
    path = "Library/note.md"
    if transition in {"move", "journal_move"}:
        path = "Library/moved.md"
        if transition == "journal_move":
            atomic(history.state / "external-change.json", json.dumps({
                "version": 2, "source_binding_protocol": 1, "id": uuid.uuid4().hex,
                "before": history.capture_external(), "paths": None,
                "actor": "app:sapphire", "reason": "rename",
            }).encode())
        (vault / "Library/note.md").rename(vault / path)
        if transition == "journal_move":
            history.recover()
        store.refresh()
    elif transition == "unchanged_save":
        history.write(path, raw, before["revision"], "human", "Save the same words")
    elif transition == "reopen_rebuild":
        with store.connect() as db:
            db.execute("DROP TABLE wiki_edges")
            db.execute("DROP TABLE wiki_edge_lookups")
            db.execute("DROP TABLE maintenance_projection")
        registry.block(jobs.user_id)
        registry.unblock(jobs.user_id)
        store = registry.store(jobs.user_id)
    elif transition in {"edit", "restore_after_edit"}:
        edited = history.write(path, raw + b"\nA new qualification.\n", before["revision"], "human", "Edit the interpretation")
        if transition == "restore_after_edit":
            history.restore(edited["change_id"], edited["revision"], "before", "human")
    elif transition == "restore_same_bytes":
        history.restore(created["change_id"], before["revision"], "after", "human")
    elif transition == "copy":
        (vault / path).rename(vault / "Library/moved.md")
        store.refresh()
        history.restore(created["change_id"], None, "after", "human")
        original = store.read(before["id"])
        assert not original["stale"]
    else:
        source = vault / "Library/source.md"
        original = store.read("Library/source.md")
        raw_source = source.read_bytes()
        source.unlink()
        store.refresh()
        source.write_bytes(raw_source)
        store.refresh()
        assert store.read("Library/source.md")["id"] != original["id"]
    current = (await maintenance.current_read(client, path)).json()
    ref = current["metadata"]["source_refs"][0]
    if transition in {"move", "journal_move", "unchanged_save", "reopen_rebuild"}:
        assert not current["stale"] and ref["confirmation_receipt_id"] == result["receipt_id"]
        assert ref["confirmed_revision"] == ref["current_revision"]
        assert not any(item["current"]["id"] == current["id"] for item in (await maintenance.state(client))["items"])
        with store.connect() as db:
            assert source_bindings.current(db, current["id"])["id"] == observation["id"]
    else:
        assert current["stale"]
        if transition != "replace_source":
            assert "confirmed_revision" not in ref
        assert ref["status"] == ("missing" if transition == "replace_source" else "changed")


async def test_a_later_source_change_can_be_rechecked_without_touching_the_note(client, tmp_path):
    files = {"Library/note.md": "---\nsources:\n  - {resource: source.md, revision: earlier, location: L1-L2}\n---\n# Note\nExisting interpretation.\n",
             "Library/source.md": "# Source\nA condition.\n"}
    vault, config, jobs = await maintenance.prepare(client, tmp_path, files)
    before, first = await confirm(client, config, jobs, "Library/note.md", "first")
    source = vault / "Library/source.md"
    source.write_text(source.read_text() + "\nA new qualification.\n")
    await maintenance.current_read(client, "Library/source.md")
    _, second = await confirm(client, config, jobs, "Library/note.md", "second")
    current = (await maintenance.current_read(client, "Library/note.md")).json()
    ref = current["metadata"]["source_refs"][0]
    assert current["revision"] == first["revision"] == second["revision"] == before["revision"]
    assert not current["stale"] and first["receipt_id"] != second["receipt_id"]
    assert ref["confirmation_receipt_id"] == second["receipt_id"]
    assert ref["confirmed_revision"] == ref["current_revision"]


async def test_source_confirmation_still_requires_actual_source_reading(client, tmp_path):
    files = {"Library/note.md": "---\nsources:\n  - {resource: source.md, revision: earlier, location: L1-L2}\n---\n# Note\nExisting interpretation.\n",
             "Library/source.md": "# Source\nA condition.\n"}
    vault, config, jobs = await maintenance.prepare(client, tmp_path, files)
    before = (vault / "Library/note.md").read_bytes()
    await confirm(client, config, jobs, "Library/note.md", "unread", read_sources=False)
    assert (vault / "Library/note.md").read_bytes() == before
    current = (await maintenance.current_read(client, "Library/note.md")).json()
    assert current["stale"] and "confirmed_revision" not in current["metadata"]["source_refs"][0]


async def test_confirmed_source_is_rechecked_against_an_unindexed_disk_edit(client, tmp_path, monkeypatch):
    files = {"Library/note.md": "---\nsources:\n  - {resource: source.md, revision: earlier, location: L1-L2}\n---\n# Note\nExisting interpretation.\n",
             "Library/source.md": "# Source\nA condition.\n"}
    vault, config, jobs = await maintenance.prepare(client, tmp_path, files)
    document, _ = await confirm(client, config, jobs, "Library/note.md", "checked")
    store = jobs.store
    public = store.public
    edited = []

    def edit_after_synchronizing(row, **kwargs):
        if row["id"] == document["id"] and not edited:
            source = vault / "Library/source.md"
            source.write_text(source.read_text() + "\nA changed condition.\n")
            edited.append(True)
        return public(row, **kwargs)

    monkeypatch.setattr(store, "public", edit_after_synchronizing)
    current = (await maintenance.current_read(client, "Library/note.md")).json()
    ref = current["metadata"]["source_refs"][0]
    assert edited and current["stale"] and ref["status"] == "changed"
    assert ref["confirmed_revision"] != ref["current_revision"]


async def test_confirmed_unversioned_source_deletion_is_stale_on_every_surface(client, tmp_path):
    files = {"Library/note.md": "---\nsources:\n  - {resource: source.md, location: L1-L2}\n---\n# Dependent\nExisting interpretation.\n",
             "Library/source.md": "# Source\nA condition.\n"}
    vault, config, jobs = await maintenance.prepare(client, tmp_path, files)
    document, result = await confirm(client, config, jobs, "Library/note.md", "check-unversioned")
    before = (await maintenance.current_read(client, document["id"])).json()
    ref = before["metadata"]["source_refs"][0]
    assert not before["stale"] and ref["revision"] is None
    assert ref["confirmed_revision"] == ref["current_revision"]
    assert ref["confirmation_receipt_id"] == result["receipt_id"]
    (vault / "Library/source.md").unlink()
    jobs.store.refresh()
    current = (await maintenance.current_read(client, document["id"])).json()
    assert current["stale"] and current["metadata"]["source_refs"][0]["status"] == "missing"
    for route, params, collection in [
        ("documents", {"directory": "Library", "view": "full"}, "items"),
        ("search", {"query": "Dependent", "intent": "lookup", "view": "full"}, "items"),
        ("graph", {"doc": document["id"]}, "nodes"),
    ]:
        response = await client.get("/api/wiki/" + route, params=params)
        assert response.status_code == 200, response.text
        note = next(item for item in response.json()[collection] if item["id"] == document["id"])
        assert note["stale"] is True, route
    state = (await maintenance.state(client, document["id"]))["items"][0]
    assert state["source_warnings"] == {"missing": 1}
