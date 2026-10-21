"""Recovery interruption and source controls never use a registered real vault."""

import asyncio
import hashlib
import json
import sqlite3
import threading
import uuid
from pathlib import Path

import pytest

from server.wiki import origins, recovery_state, registry
from server.wiki.store import WikiStore
from tests import test_wiki_api as wiki_api
from tests.test_wiki_owner_recovery import apply, approve, finish, plan, sql
from tests.test_wiki_owner_recovery import client as client
from tests.test_wiki_owner_recovery import uncertain as uncertain
from tests.test_wiki_source_bindings import source


def handle():
    return next(iter(registry.recoveries()._handles.values()))


async def stage(client, result, wanted):
    for _ in range(12):
        status = (await client.get(f"/api/wiki/recovery/{result['recovery_id']}", headers=wiki_api.HEADERS)).json()
        if status["phase"] == wanted:
            return status
        response = await apply(client, result)
        assert response.status_code == 200, response.text
    raise AssertionError("Recovery did not reach its bounded stage")


@pytest.mark.parametrize("complete", [False, True])
async def test_recovery_receivers_keep_service_health_through_registration_replacement(uncertain, complete):
    client, root, _, original, _ = uncertain
    result = await plan(client)
    if complete:
        await approve(client, result)
        await finish(client, result)
        receiver = registry.store(None).events.journal
    else:
        receiver = handle().observer.journal
    if not complete:
        assert receiver.health is registry.health
        return
    current = registry.store(None).read(original["path"])
    from server.agent_manager import AgentManager

    created = await AgentManager(wiki_api.session_manager.db).create_agent(name="Replacement curator")
    configured = await client.put("/api/wiki/config", headers=wiki_api.HEADERS,
                                  json={"root": str(root), "curator_agent_id": created["id"]})
    assert configured.status_code == 200, configured.text
    read = await client.get("/api/wiki/read", params={"doc": original["path"]}, headers=wiki_api.HEADERS)
    assert read.status_code == 200, read.text
    assert read.json()["id"] == current["id"]
    assert read.json()["content"] == "# Original\nKeep this argument and its conditions.\n"
    assert registry.store(None).events.journal.health is registry.health


async def test_offline_body_root_keeps_recovery_diagnostics_and_evidence_available(uncertain):
    client, root, store, original, _ = uncertain
    result = await plan(client)
    await approve(client, result)
    registry.recoveries().close()
    before = sql(store, "SELECT * FROM filesystem_state")
    identities = sql(store, "SELECT * FROM document_identities")
    offline = root.with_name("offline-vault")
    root.rename(offline)
    try:
        inspected = await client.get("/api/wiki/recovery", headers=wiki_api.HEADERS)
        assert inspected.status_code == 200, inspected.text
        assert inspected.json()["root_available"] is False
        assert inspected.json()["filesystem"] == before[0]
        status = await client.get(f"/api/wiki/recovery/{result['recovery_id']}", headers=wiki_api.HEADERS)
        assert status.status_code == 200, status.text
        saved = await client.get(result["plan_url"], headers=wiki_api.HEADERS)
        assert saved.status_code == 200 and saved.json()["plan_hash"] == result["plan_hash"]
        exported = await client.post("/api/wiki/recovery/export", headers=wiki_api.HEADERS)
        assert exported.status_code == 200, exported.text
        receipt = exported.json()
        raw = Path(receipt["path"]).read_bytes()
        assert len(raw) == receipt["bytes"] and hashlib.sha256(raw).hexdigest() == receipt["sha256"]
        assert json.loads(raw)["root_available"] is False
        refused = await client.post("/api/wiki/recovery/plan", headers=wiki_api.HEADERS, json={"retain": []})
        assert refused.status_code in {409, 503}, refused.text
        refused = await apply(client, result)
        assert refused.status_code in {409, 503}, refused.text
        assert sql(store, "SELECT * FROM filesystem_state") == before
        assert sql(store, "SELECT * FROM document_identities") == identities
        assert not root.exists()
        assert (offline / original["path"]).read_bytes() == b"# Original\nKeep this argument and its conditions.\n"
    finally:
        offline.rename(root)


@pytest.mark.parametrize("when", ["prepared", "mapping_applied", "rebuilding", "handoff"])
async def test_new_after_cutoff_receipt_survives_until_ordinary_application(uncertain, monkeypatch, when):
    client, root, store, original, _ = uncertain
    result = await plan(client)
    await approve(client, result)
    manager = registry.recoveries()
    h = handle()
    path, raw = "Library/after-cutoff.md", b"# New\nThis arrived after the approved prefix.\n"

    def receive_new():
        (root / path).write_bytes(raw)
        manager.flush(None)

    if when in {"prepared", "mapping_applied", "handoff"}:
        await stage(client, result, when)
        receive_new()
    else:
        prepare = manager._prepare
        received = False

        def during_prepare(*args):
            nonlocal received
            if not received:
                receive_new()
                received = True
            return prepare(*args)

        monkeypatch.setattr(manager, "_prepare", during_prepare)
    completed = await finish(client, result)
    assert completed["completion_receipt"]
    batches = sql(store, "SELECT * FROM filesystem_inbox WHERE seq>?", (result["cutoff"],))
    assert batches and all(row["status"] == "pending" and row["payload"] for row in batches)
    assert any(record.get("path") == path for row in batches for record in json.loads(row["payload"])["records"])
    observed = h.observer
    current = registry.store(None)
    assert current.events is observed and not observed._closed and observed.journal.recovery is None
    assert current.read(original["path"])["id"] == result["files"][0]["document_id"]
    assert current.read(path)["revision"] == hashlib.sha256(raw).hexdigest()
    assert (root / path).read_bytes() == raw
    assert all(row["status"] == "applied" for row in sql(store, "SELECT status FROM filesystem_inbox WHERE seq>?", (result["cutoff"],)))


async def test_explicit_retention_preserves_identity_aliases_history_and_opaque_origin(uncertain):
    client, root, store, original, _ = uncertain
    opaque = {"doc_id": original["id"], "revision": original["revision"], "status": "invalid",
              "proof": {"format": "unknown-test-version", "opaque": ["retain", 42]}}
    with sqlite3.connect(store.db_path) as db:
        db.execute("PRAGMA recursive_triggers=ON")
        origins.put(db, original["id"], opaque)
    aliases = sql(store, "SELECT * FROM uid_aliases")
    history = sql(store, "SELECT * FROM changes")
    raw = (root / original["path"]).read_bytes()
    result = await plan(client, [{"doc": original["id"], "path": original["path"], "expected_revision": original["revision"]}])
    assert result["files"][0]["document_id"] == original["id"] and result["files"][0]["retained"]
    full = await client.get(result["plan_url"], headers=wiki_api.HEADERS)
    assert full.status_code == 200 and recovery_state.digest(full.json()["plan"]) == result["plan_hash"]
    await approve(client, result)
    await finish(client, result)
    assert registry.store(None).read(original["id"])["id"] == original["id"]
    assert sql(store, "SELECT * FROM uid_aliases") == aliases
    assert sql(store, "SELECT * FROM changes") == history
    with registry.store(None).connect() as db:
        after = origins.get(db, original["id"])
    assert after["proof"] == opaque["proof"] and after["status"] == "invalid"
    assert (root / original["path"]).read_bytes() == raw


async def test_retained_consumer_never_rebinds_to_replacement_at_old_source_path(client, tmp_path):
    root = tmp_path / "source-case"
    (root / "Library").mkdir(parents=True)
    (root / "Library/note.md").write_bytes(b"# Source\nThe original condition.\n")
    (root / "Library/consumer.md").write_bytes(source("note.md"))
    await wiki_api.configure(client, root)
    store = registry.store(None)
    original, consumer = store.read("Library/note.md"), store.read("Library/consumer.md")
    assert consumer["metadata"]["source_refs"][0]["doc"] == original["id"]
    with store.connect() as db:
        bindings = [dict(row) for row in db.execute("SELECT * FROM source_bindings")]
        db.execute("UPDATE filesystem_state SET uncertain='Unknown replacement interval' WHERE singleton=1")
    consumer_raw = (root / consumer["path"]).read_bytes()
    with (root / original["path"]).open("rb"):
        (root / original["path"]).unlink()
        replacement = b"# Replacement\nA different condition at the old path.\n"
        (root / original["path"]).write_bytes(replacement)
    result = await plan(client, [{"doc": consumer["id"], "path": consumer["path"], "expected_revision": consumer["revision"]}])
    await approve(client, result)
    await finish(client, result)
    current = registry.store(None)
    assert current.read(original["path"])["id"] != original["id"]
    ref = current.read(consumer["id"])["metadata"]["source_refs"][0]
    assert ref["doc"] == original["id"] and ref["status"] == "missing"
    for binding in bindings:
        assert sql(store, "SELECT * FROM source_bindings WHERE id=?", (binding["id"],)) == [binding]
    assert (root / consumer["path"]).read_bytes() == consumer_raw
    assert (root / original["path"]).read_bytes() == replacement


@pytest.mark.parametrize("retain_consumer", [False, True])
async def test_late_consumer_edit_keeps_source_policy_through_ordinary_handoff(client, tmp_path, retain_consumer):
    root = tmp_path / "late-source-case"
    (root / "Library").mkdir(parents=True)
    (root / "Library/source.md").write_bytes(b"# Source\nThe original condition.\n")
    (root / "Library/consumer.md").write_bytes(source("source.md"))
    await wiki_api.configure(client, root)
    store = registry.store(None)
    original, consumer = store.read("Library/source.md"), store.read("Library/consumer.md")
    with store.connect() as db:
        old_bindings = [dict(row) for row in db.execute("SELECT * FROM source_bindings")]
        db.execute("UPDATE filesystem_state SET uncertain='Unknown source replacement interval' WHERE singleton=1")
    (root / original["path"]).unlink()
    (root / original["path"]).write_bytes(b"# Replacement\nA different source at the same path.\n")
    retention = [{"doc": consumer["id"], "path": consumer["path"], "expected_revision": consumer["revision"]}] if retain_consumer else []
    result = await plan(client, retention)
    await approve(client, result)
    await stage(client, result, "mapping_applied")
    mapped = next(entry for entry in result["files"] if entry["path"] == consumer["path"])
    inode = (root / consumer["path"]).stat().st_ino
    with (root / consumer["path"]).open("ab") as target:
        target.write(b"\nA later body qualification; the source field is unchanged.\n")
    assert (root / consumer["path"]).stat().st_ino == inode
    registry.recoveries().flush(None)
    late = sql(store, "SELECT * FROM filesystem_inbox WHERE seq>?", (result["cutoff"],))
    assert late and all(row["status"] == "pending" for row in late)
    await finish(client, result)
    assert sql(store, "SELECT * FROM filesystem_recovery_audit WHERE kind='pending_after_cutoff'")
    for received in late:
        handed_off = sql(store, "SELECT * FROM filesystem_inbox WHERE seq=?", (received["seq"],))[0]
        assert handed_off == received
    current = registry.store(None)
    replacement = current.read(original["path"])
    after = current.read(consumer["path"])
    assert after["id"] == mapped["document_id"]
    ref = after["metadata"]["source_refs"][0]
    assert ref.get("doc") != replacement["id"]
    if retain_consumer:
        assert after["id"] == consumer["id"] and ref["doc"] == original["id"] and ref["status"] == "missing"
    else:
        assert after["id"] != consumer["id"] and ref["status"] == "unknown"
    assert sql(store, "SELECT path FROM document_identities WHERE id=?", (original["id"],)) == [{"path": None}]
    for binding in old_bindings:
        assert sql(store, "SELECT * FROM source_bindings WHERE id=?", (binding["id"],)) == [binding]
    for received in late:
        replayed = sql(store, "SELECT * FROM filesystem_inbox WHERE seq=?", (received["seq"],))[0]
        assert replayed["status"] == "applied"
        assert replayed["payload"] in ("", received["payload"])
        assert all(replayed[key] == received[key] for key in ("seq", "batch_id", "observer_epoch", "root_generation", "payload_hash"))


@pytest.mark.parametrize("when", ["prepared", "mapping_applied", "handoff"])
async def test_new_overflow_invalidates_approval_but_preserves_old_receipts(uncertain, when):
    client, _, store, _, _ = uncertain
    result = await plan(client)
    await approve(client, result)
    await stage(client, result, when)
    audit = sql(store, "SELECT * FROM filesystem_recovery_audit ORDER BY rowid")
    h = handle()
    with h.observer._lock:
        h.observer.persist([{"type": "overflow"}])
        h.observer.process_record({"type": "overflow"})
    registry.recoveries().flush(None)
    refused = await apply(client, result)
    assert refused.status_code in {409, 503}, refused.text
    assert sql(store, "SELECT uncertain FROM filesystem_state")[0]["uncertain"]
    assert sql(store, "SELECT * FROM filesystem_recovery_audit ORDER BY rowid")[:len(audit)] == audit
    revised = await plan(client)
    assert revised["recovery_id"] == result["recovery_id"] and revised["generation"] == result["generation"] + 1
    assert revised["plan_hash"] != result["plan_hash"] and not revised["approved"]
    assert (await apply(client, result)).status_code == 409
    await approve(client, revised)
    await finish(client, revised)


async def test_restart_reobserves_same_recovery_with_new_generation_and_never_replays_old_mapping(uncertain):
    client, _, store, original, _ = uncertain
    result = await plan(client)
    await approve(client, result)
    mapped = await stage(client, result, "mapping_applied")
    old_epoch = handle().observer.epoch
    registry.close()
    registry.resume_access()
    assert (await client.get("/api/wiki/recovery", headers=wiki_api.HEADERS)).status_code == 200
    assert (await apply(client, result)).status_code == 409
    revised = await plan(client)
    assert handle().observer.epoch != old_epoch
    assert revised["recovery_id"] == result["recovery_id"] and revised["generation"] == result["generation"] + 1
    await approve(client, revised)
    await finish(client, revised)
    mappings = sql(store, "SELECT sha256 FROM filesystem_recovery_audit WHERE kind='mapping' ORDER BY rowid")
    assert len(mappings) == 2 and mappings[0]["sha256"] == mapped["mapping_receipt"]
    assert registry.store(None).read(original["path"])["id"] == revised["files"][0]["document_id"]


@pytest.mark.parametrize("committed", [False, True])
async def test_mapping_failure_before_or_after_commit_does_not_repeat_accepted_mapping(uncertain, monkeypatch, committed):
    client, root, store, original, _ = uncertain
    result = await plan(client)
    await approve(client, result)
    raw = (root / original["path"]).read_bytes()
    manager = registry.recoveries()
    audit, reload = recovery_state.audit, manager._reload

    def fail_audit(db, rid, generation, kind, body):
        if kind == "mapping":
            raise RuntimeError("Before mapping commit")
        return audit(db, rid, generation, kind, body)

    def fail_reload(h):
        reload(h)
        raise RuntimeError("After mapping commit")

    with monkeypatch.context() as patch:
        patch.setattr(manager, "_reload", fail_reload) if committed else patch.setattr(recovery_state, "audit", fail_audit)
        with pytest.raises(RuntimeError, match="mapping commit"):
            await apply(client, result)
    row = sql(store, "SELECT * FROM filesystem_recoveries")[0]
    assert row["phase"] == ("mapping_applied" if committed else "prepared")
    assert len(sql(store, "SELECT * FROM filesystem_recovery_audit WHERE kind='mapping'")) == int(committed)
    await finish(client, result)
    assert len(sql(store, "SELECT * FROM filesystem_recovery_audit WHERE kind='mapping'")) == 1
    assert (root / original["path"]).read_bytes() == raw


async def test_derived_rebuild_failure_retains_mapping_and_continuous_reception(uncertain, monkeypatch):
    client, root, store, original, _ = uncertain
    result = await plan(client)
    await approve(client, result)
    mapped = await stage(client, result, "mapping_applied")
    h = handle()

    def fail_index(*args, **kwargs):
        (root / "Library/while-failing.md").write_bytes(b"# Received during indexing failure\n")
        raise RuntimeError("Injected derived rebuild fault")

    with monkeypatch.context() as patch:
        patch.setattr(h.store, "_index", fail_index)
        with pytest.raises(RuntimeError, match="derived rebuild fault"):
            await apply(client, result)
    registry.recoveries().flush(None)
    row = sql(store, "SELECT * FROM filesystem_recoveries")[0]
    assert row["phase"] == "mapping_applied" and row["rebuild_cursor"] == 0
    assert row["mapping_receipt"] == mapped["mapping_receipt"]
    assert not h.observer._closed and sql(store, "SELECT * FROM filesystem_inbox WHERE status='pending'")
    await finish(client, result)
    assert registry.store(None).read(original["path"])["id"] == result["files"][0]["document_id"]
    assert registry.store(None).read("Library/while-failing.md")["characters"] > 0


async def test_completion_commit_before_publish_is_retried_by_its_exact_receipt(uncertain, monkeypatch):
    client, _, store, _, _ = uncertain
    result = await plan(client)
    await approve(client, result)
    await stage(client, result, "handoff")

    def fail_publish(*args):
        raise RuntimeError("Publish response lost after completion commit")

    with monkeypatch.context() as patch:
        patch.setattr(registry, "publish_recovery", fail_publish)
        with pytest.raises(RuntimeError, match="completion commit"):
            await apply(client, result)
    receipt = sql(store, "SELECT completion_receipt FROM filesystem_recoveries")[0]["completion_receipt"]
    assert receipt and not handle().observer._closed
    repeated = await apply(client, result)
    assert repeated.status_code == 200 and repeated.json()["completion_receipt"] == receipt
    assert len(sql(store, "SELECT * FROM filesystem_recovery_audit WHERE kind='complete'")) == 1
    assert len(sql(store, "SELECT * FROM filesystem_recovery_audit WHERE kind='mapping'")) == 1


@pytest.mark.parametrize("loss", ["filesystem_recoveries", "filesystem_recovery_audit", "guard", "protocol"])
async def test_initialized_recovery_facts_are_not_rebuilt_empty(uncertain, loss):
    client, _, store, original, _ = uncertain
    await plan(client)
    with sqlite3.connect(store.db_path) as db:
        if loss == "guard":
            db.execute("DROP TRIGGER filesystem_recovery_audit_immutable")
        elif loss == "protocol":
            db.execute("DELETE FROM wiki_protocols WHERE name='filesystem_recovery'")
        else:
            db.execute("DROP TABLE " + loss)
    identities = sql(store, "SELECT * FROM document_identities")
    assert (await client.get("/api/wiki/recovery", headers=wiki_api.HEADERS)).status_code == 503
    assert (await client.post("/api/wiki/recovery/export", headers=wiki_api.HEADERS)).status_code == 503
    assert (await client.get("/api/wiki/read", params={"doc": original["id"]}, headers=wiki_api.HEADERS)).status_code == 503
    assert sql(store, "SELECT * FROM document_identities") == identities


async def test_changed_root_requires_new_observer_and_owner_plan(uncertain):
    client, root, store, original, _ = uncertain
    result = await plan(client)
    await approve(client, result)
    old_epoch = handle().observer.epoch
    root.rename(root.with_name("retained-old-root"))
    (root / "Library").mkdir(parents=True)
    raw = b"# New root\nA different document in the registered root.\n"
    (root / original["path"]).write_bytes(raw)
    refused = await apply(client, result)
    assert refused.status_code in {409, 503}
    revised = await plan(client)
    assert handle().observer.epoch != old_epoch
    assert revised["generation"] > result["generation"] and not revised["approved"]
    await approve(client, revised)
    await finish(client, revised)
    assert registry.store(None).read(original["path"])["id"] != original["id"]
    assert (root / original["path"]).read_bytes() == raw
    assert sql(store, "SELECT path FROM document_identities WHERE id=?", (original["id"],)) == [{"path": None}]


async def test_replaced_root_does_not_close_another_captured_observer_lease(uncertain):
    client, root, store, original, config = uncertain
    extra = WikiStore(root, store.state, config["vault_id"], health=store.health)
    try:
        result = await plan(client)
        old = handle().observer
        root.rename(root.with_name("old-root-with-captured-lease"))
        (root / "Library").mkdir(parents=True)
        (root / original["path"]).write_bytes(b"# New root after unknown history\n")
        refused = await client.post("/api/wiki/recovery/plan", headers=wiki_api.HEADERS, json={"retain": []})
        assert refused.status_code == 503 and "leases" in refused.text
        assert not old._closed and extra.events is old
        assert sql(store, "SELECT uncertain FROM filesystem_state")[0]["uncertain"]
        extra.close()
        revised = await plan(client)
        assert old._closed and handle().observer is not old
        assert revised["generation"] > result["generation"]
        await approve(client, revised)
        await finish(client, revised)
    finally:
        extra.close()


async def test_registration_change_invalidates_old_approval_and_requires_new_generation(uncertain):
    client, _, store, _, _ = uncertain
    result = await plan(client)
    await approve(client, result)
    config_path = store.state / "config.json"
    config = json.loads(config_path.read_text())
    config["registration_generation"] = uuid.uuid4().hex
    config_path.write_text(json.dumps(config))  # Isolated operator reconfiguration fault.
    assert (await apply(client, result)).status_code == 409
    revised = await plan(client)
    assert revised["generation"] == result["generation"] + 1 and not revised["approved"]
    await approve(client, revised)
    await finish(client, revised)


async def test_export_rejects_symlink_destination_without_touching_other_files(uncertain, tmp_path):
    client, _, store, _, _ = uncertain
    outside = tmp_path / "outside-export"
    outside.mkdir()
    (outside / "keep.txt").write_bytes(b"Preserve this unrelated file")
    (store.state / "recovery-exports").symlink_to(outside, target_is_directory=True)
    before = sql(store, "SELECT * FROM filesystem_state")
    refused = await client.post("/api/wiki/recovery/export", headers=wiki_api.HEADERS)
    assert refused.status_code == 409
    assert [entry.name for entry in outside.iterdir()] == ["keep.txt"]
    assert (outside / "keep.txt").read_bytes() == b"Preserve this unrelated file"
    assert sql(store, "SELECT * FROM filesystem_state") == before


async def test_close_joins_an_owner_plan_that_has_not_created_its_receiver_yet(uncertain, monkeypatch):
    client, _, _, _, _ = uncertain
    manager = registry.recoveries()
    entered, release, started, finished = (threading.Event() for _ in range(4))
    begin = manager._begin
    observed = []

    def paused_begin(metadata, **kwargs):
        entered.set()
        assert release.wait(5)
        receiver = begin(metadata, **kwargs)
        observed.append(receiver)
        return receiver

    def close_owner():
        started.set()
        manager.close()
        finished.set()

    monkeypatch.setattr(manager, "_begin", paused_begin)
    planning = asyncio.create_task(plan(client))
    closing = None
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        closing = asyncio.create_task(asyncio.to_thread(close_owner))
        assert await asyncio.to_thread(started.wait, 5)
        retired_early = await asyncio.to_thread(finished.wait, 0.1)
        release.set()
        await planning
        await closing
        assert not retired_early
        assert observed and not manager._handles
        assert all(receiver.observer._closed and receiver.thread is None for receiver in observed)
    finally:
        release.set()
        await asyncio.gather(planning, *([closing] if closing else []), return_exceptions=True)
        await asyncio.to_thread(manager.close)
