"""Owner recovery keeps unknown history closed and the live intake observable."""

import asyncio
import hashlib
import json
import sqlite3
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from server.mcp_identity import mint
from server.wiki.registry import WikiRegistry, registry
from server.wiki.store import WikiError, WikiStore
from server.wiki.tasks import tasks
from tests import wiki_api as wiki_api

client = wiki_api.client


@pytest.fixture
async def uncertain(client, tmp_path):
    root = tmp_path / "vault"
    (root / "Library").mkdir(parents=True)
    path = root / "Library/note.md"
    path.write_bytes(b"# Original\nKeep this argument and its conditions.\n")
    config = await wiki_api.configure(client, root)
    owner = (await wiki_api.session_manager.db.get_system_agent())["user_id"]
    store = registry.store(owner)
    original = store.read("Library/note.md")
    with store.connect() as db:
        db.execute("UPDATE filesystem_state SET uncertain='Isolated missing-event history' WHERE singleton=1")
    yield client, root, store, original, config
    if getattr(registry, "_recoveries", None) is not None:
        registry._recoveries.close()


def sql(store, statement, parameters=()):
    with sqlite3.connect(store.db_path) as db:
        db.row_factory = sqlite3.Row
        return [dict(row) for row in db.execute(statement, parameters)]


async def plan(client, retain=()):
    response = await client.post("/api/wiki/recovery/plan", headers=client.headers, json={"retain": list(retain)})
    assert response.status_code == 200, response.text
    return response.json()


async def approve(client, result):
    response = await client.post(f"/api/wiki/recovery/{result['recovery_id']}/approve", headers=client.headers,
                                 json={"plan_hash": result["plan_hash"]})
    assert response.status_code == 200, response.text
    return response.json()


async def apply(client, result):
    return await client.post(f"/api/wiki/recovery/{result['recovery_id']}/apply", headers=client.headers,
                             json={"plan_hash": result["plan_hash"]})


async def finish(client, result):
    for _ in range(12):
        response = await apply(client, result)
        assert response.status_code == 200, response.text
        if response.json()["phase"] == "complete":
            return response.json()
    raise AssertionError("The bounded synthetic recovery did not reach its durable receipt")


async def test_inspection_and_evidence_export_work_without_constructing_the_uncertain_store(uncertain):
    client, root, store, original, _ = uncertain
    before = sql(store, "SELECT * FROM filesystem_state")
    denied = await client.get("/api/wiki/read", params={"doc": original["id"]}, headers=client.headers)
    assert denied.status_code == 503
    response = await client.get("/api/wiki/recovery", headers=client.headers)
    assert response.status_code == 200, response.text
    assert response.json()["filesystem"]["uncertain"] == before[0]["uncertain"]
    exported = await client.post("/api/wiki/recovery/export", headers=client.headers)
    assert exported.status_code == 200, exported.text
    receipt = exported.json()
    raw = Path(receipt["path"]).read_bytes()
    assert len(raw) == receipt["bytes"] and hashlib.sha256(raw).hexdigest() == receipt["sha256"]
    assert json.loads(raw)["filesystem"] == before[0]
    assert sql(store, "SELECT * FROM filesystem_state") == before
    assert (root / original["path"]).read_bytes() == b"# Original\nKeep this argument and its conditions.\n"


async def test_model_scope_never_receives_owner_recovery_or_approval(uncertain):
    client, _, _, _, config = uncertain
    owner = (await wiki_api.session_manager.db.get_system_agent())["user_id"]
    session = SimpleNamespace(id="recovery-model", user_id=owner, agent_id=config["curator_agent_id"])
    token = tasks.open(session, "maintain")
    headers = {"Authorization": "Bearer " + mint(session.id, user_id=session.user_id, wiki_task_id=token)}
    try:
        for method, path, body in (("GET", "/api/wiki/recovery", None),
                                   ("POST", "/api/wiki/recovery/export", None),
                                   ("POST", "/api/wiki/recovery/plan", {"retain": []})):
            response = await client.request(method, path, headers=headers, json=body)
            assert response.status_code == 403, response.text
    finally:
        tasks.close(token)


async def test_default_plan_requires_approval_and_ends_old_identity_without_editing_bytes(uncertain):
    client, root, store, original, _ = uncertain
    owner = (await wiki_api.session_manager.db.get_system_agent())["user_id"]
    raw = (root / original["path"]).read_bytes()
    result = await plan(client)
    assert result["phase"] == "prepared" and result["files_count"] == 1
    entry = result["files"][0]
    assert entry["document_id"] != original["id"] and entry["revision"] == original["revision"]
    refused = await apply(client, result)
    assert refused.status_code == 409
    assert sql(store, "SELECT path FROM document_identities WHERE id=?", (original["id"],)) == [{"path": original["path"]}]
    await approve(client, result)
    complete = await finish(client, result)
    assert complete["completion_receipt"] and complete["mapping_receipt"]
    repeated = await apply(client, result)
    assert repeated.status_code == 200 and repeated.json()["completion_receipt"] == complete["completion_receipt"]
    current = registry.store(owner).read(original["path"])
    assert current["id"] == entry["document_id"]
    assert sql(store, "SELECT path FROM document_identities WHERE id=?", (original["id"],)) == [{"path": None}]
    with pytest.raises(WikiError) as gone:
        registry.store(owner).read(original["id"])
    assert gone.value.status == 404 and (root / original["path"]).read_bytes() == raw


async def test_changed_plan_and_wrong_approval_do_not_mutate_identity(uncertain):
    client, root, store, original, _ = uncertain
    result = await plan(client)
    wrong = await client.post(f"/api/wiki/recovery/{result['recovery_id']}/approve", headers=client.headers,
                              json={"plan_hash": "0" * 64})
    assert wrong.status_code == 409
    await approve(client, result)
    edited = b"# Human edit\nA new condition, after the plan.\n"
    (root / original["path"]).write_bytes(edited)
    refused = await apply(client, result)
    assert refused.status_code == 409, refused.text
    assert sql(store, "SELECT path FROM document_identities WHERE id=?", (original["id"],)) == [{"path": original["path"]}]
    assert (root / original["path"]).read_bytes() == edited
    assert sql(store, "SELECT uncertain FROM filesystem_state")[0]["uncertain"]


async def test_intake_remains_live_while_normal_store_writer_drains(uncertain, monkeypatch):
    client, root, store, _, _ = uncertain
    owner = (await wiki_api.session_manager.db.get_system_agent())["user_id"]
    held, release, draining = threading.Event(), threading.Event(), threading.Event()
    original_block = registry.block

    def hold_writer():
        with store.operation():
            held.set()
            assert release.wait(10)

    def observe_block(owner):
        draining.set()
        return original_block(owner)

    thread = threading.Thread(target=hold_writer)
    thread.start()
    assert await asyncio.to_thread(held.wait, 5)
    monkeypatch.setattr(registry, "block", observe_block)
    pending = asyncio.create_task(plan(client))
    try:
        assert await asyncio.to_thread(draining.wait, 5)
        (root / "Library/new.md").write_bytes(b"# New while draining\n")
        await asyncio.to_thread(registry.recoveries().flush, owner)
        received = sql(store, "SELECT payload FROM filesystem_inbox WHERE status='pending'")
        assert any(record.get("path") == "Library/new.md" for row in received for record in json.loads(row["payload"])["records"])
        assert not store.events._closed and not pending.done()
        release.set()
        result = await pending
        assert result["files_count"] == 2 and not store.events._closed
    finally:
        release.set()
        await asyncio.gather(pending, return_exceptions=True)
        await asyncio.to_thread(thread.join, 5)
        assert not thread.is_alive()


async def test_cold_handoff_stays_closed_after_uncertainty_clear_before_takeover(uncertain):
    client, root, store, original, config = uncertain
    owner = (await wiki_api.session_manager.db.get_system_agent())["user_id"]
    result = await plan(client)
    await approve(client, result)
    for _ in range(8):
        response = await apply(client, result)
        assert response.status_code == 200, response.text
        if response.json()["phase"] == "handoff":
            break
    else:
        raise AssertionError("Expected the persisted handoff stage")
    before = sql(store, "SELECT * FROM filesystem_recoveries")
    assert sql(store, "SELECT uncertain FROM filesystem_state") == [{"uncertain": None}]
    registry.close()
    fresh = WikiRegistry()
    try:
        for open_store in (lambda: fresh.store(owner), lambda: WikiStore(root, store.state, config["vault_id"])):
            with pytest.raises(WikiError, match="recovery") as closed:
                open_store()
            assert closed.value.status == 503
        assert sql(store, "SELECT * FROM filesystem_recoveries") == before
        assert sql(store, "SELECT id FROM document_identities WHERE path=?", (original["path"],))[0]["id"] == result["files"][0]["document_id"]
    finally:
        fresh.close()
