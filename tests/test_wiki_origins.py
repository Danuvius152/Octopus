"""Opaque origin state survives schema upgrades, writer crashes and recovery.

The synthetic proof payloads deliberately use an unknown format. These tests
exercise the compatible state owner, not byte reconstruction or R08 reuse.
"""

import json
import sqlite3
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from server.wiki import origins, schema
from server.wiki.history import WikiHistory, atomic
from server.wiki.store import WikiError, WikiStore, revision


@pytest.fixture
def history(tmp_path):
    root = tmp_path / "vault"
    root.mkdir()
    store = WikiStore(root, tmp_path / "state", str(uuid.uuid4()), create=True)
    try:
        yield WikiHistory(store)
    finally:
        store.close()


def opaque_origin(history, path="note.md"):
    raw = b"# Source\r\n\r\nThe original condition remains.\r\n"
    receipt = history.write(path, raw, None, "human", "create")
    doc = history.store.read(path)
    state = {
        "version": 1, "doc_id": doc["id"], "revision": doc["revision"],
        "status": "valid", "proof": {"version": 739, "opaque": ["private YAML fragment", {"future": True}]},
    }
    with history.locked(), history.store.connect() as db:
        origins.put(db, doc["id"], state)
    return doc, state, raw, receipt


def current_origin(history, doc_id):
    with history.store.connect() as db:
        return origins.get(db, doc_id)


def test_unrecognized_proof_invalidates_on_write_and_restores_exact_state(history):
    doc, state, raw, _ = opaque_origin(history)
    changed = history.write("note.md", raw + b"New condition.\n", doc["revision"], "human", "edit")
    invalid = current_origin(history, doc["id"])
    assert invalid["status"] == "invalid" and invalid["revision"] == changed["revision"]
    assert invalid["proof"] == state["proof"]
    with history.store.connect() as db:
        assert origins.change_state(db, changed["change_id"], "before") == state
        assert origins.change_state(db, changed["change_id"], "after") == invalid
    restored = history.restore(changed["change_id"], changed["revision"], "before", "human")
    assert restored["revision"] == doc["revision"]
    assert current_origin(history, doc["id"]) == state
    assert history.store.path("note.md").read_bytes() == raw
    history.restore(changed["change_id"], doc["revision"], "after", "human")
    assert current_origin(history, doc["id"]) == invalid


def test_identical_byte_write_keeps_opaque_claim_without_interpreting_it(history):
    doc, state, raw, _ = opaque_origin(history)
    history.write("note.md", raw, doc["revision"], "human", "save unchanged")
    assert current_origin(history, doc["id"]) == state


@pytest.mark.parametrize("window", ["prepared", "replaced", "applied"])
def test_crash_windows_recover_file_and_origin_as_one_change(history, monkeypatch, window):
    import server.wiki.history as history_module

    doc, state, raw, _ = opaque_origin(history)
    changed_raw = raw + b"Actual edit.\n"
    atomic = history_module.atomic
    status = history.status
    index = history.store._index

    def crash(*args, **kwargs):
        raise RuntimeError("injected crash window")

    def crash_document_replace(path, content, **kwargs):
        if path == history.store.path("note.md"):
            crash()
        atomic(path, content, **kwargs)

    if window == "prepared":
        monkeypatch.setattr(history_module, "atomic", crash_document_replace)
    elif window == "replaced":
        monkeypatch.setattr(history, "status", crash)
    else:
        monkeypatch.setattr(history.store, "_index", crash)
    with pytest.raises(RuntimeError, match="injected"):
        history.write("note.md", changed_raw, doc["revision"], "human", "edit")
    monkeypatch.setattr(history_module, "atomic", atomic)
    monkeypatch.setattr(history, "status", status)
    monkeypatch.setattr(history.store, "_index", index)
    history.recover()
    history.store.refresh()
    if window == "prepared":
        assert history.store.path("note.md").read_bytes() == raw
        assert current_origin(history, doc["id"]) == state
        expected_status = "interrupted"
    else:
        assert history.store.path("note.md").read_bytes() == changed_raw
        current = current_origin(history, doc["id"])
        assert current["status"] == "invalid" and current["revision"] == revision(changed_raw)
        assert current["proof"] == state["proof"]
        expected_status = "applied"
    with history.store.connect() as db:
        records = db.execute("SELECT id,status FROM changes WHERE reason='edit'").fetchall()
        assert len(records) == 1 and records[0]["status"] == expected_status
        assert db.execute("SELECT count(*) FROM change_material_origins WHERE change_id=?", (records[0]["id"],)).fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM operation_receipts WHERE change_id=?", (records[0]["id"],)).fetchone()[0] == 1


def test_origin_journal_failure_rolls_back_change_receipt_and_identity(history, monkeypatch):
    def crash(*args, **kwargs):
        raise RuntimeError("origin journal failed")

    monkeypatch.setattr(origins, "record", crash)
    with pytest.raises(RuntimeError, match="origin journal failed"):
        history.write("new.md", b"not published", None, "human", "create")
    assert not history.store.path("new.md").exists()
    with history.store.connect() as db:
        assert db.execute("SELECT count(*) FROM changes").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM operation_receipts").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM document_identities WHERE path='new.md'").fetchone()[0] == 0


def test_wrong_origin_identity_is_rejected_before_file_or_journal_commit(history):
    doc, state, raw, _ = opaque_origin(history)
    with history.locked(), pytest.raises(WikiError, match="identity/revision"):
        history.write_locked(
            "note.md", raw + b"unpublished", doc["revision"], "worker", "bad binding",
            origin_state=state | {"doc_id": "another identity", "revision": revision(raw + b"unpublished")},
        )
    assert history.store.path("note.md").read_bytes() == raw
    assert current_origin(history, doc["id"]) == state
    assert all(item["reason"] != "bad binding" for item in history.history("note.md")["items"])


def test_external_edit_and_same_path_replacement_do_not_inherit_a_valid_claim(history):
    doc, state, raw, _ = opaque_origin(history)
    path = history.store.path("note.md")
    path.write_bytes(raw + b"external")
    history.store.refresh()
    invalid = current_origin(history, doc["id"])
    assert invalid["status"] == "invalid" and invalid["proof"] == state["proof"]
    path.unlink()
    history.store.refresh()
    assert current_origin(history, doc["id"])["revision"] is None
    path.write_bytes(raw)
    history.store.refresh()
    replacement = history.store.read("note.md")
    assert replacement["id"] != doc["id"]
    assert current_origin(history, replacement["id"]) is None


def test_preexisting_unobserved_external_edit_restores_bytes_without_certifying_old_proof(history):
    doc, state, raw, _ = opaque_origin(history)
    external = raw + b"Unindexed human edit.\n"
    history.store.path("note.md").write_bytes(external)
    changed = history.write("note.md", external + b"Next edit.\n", revision(external), "human", "edit")
    history.restore(changed["change_id"], changed["revision"], "before", "human")
    assert history.store.path("note.md").read_bytes() == external
    restored = current_origin(history, doc["id"])
    assert restored["status"] == "invalid" and restored["revision"] == revision(external)
    assert restored["proof"] == state["proof"]


@pytest.mark.parametrize("destination", ["a-moved.md", "z-moved.md"])
def test_external_journal_move_preserves_origin_in_either_path_sort_order(history, destination):
    doc, state, raw, _ = opaque_origin(history)
    with history.locked():
        before = history.capture_external(["note.md", destination])
        entry = {"version": 2, "source_binding_protocol": 1,
                 "id": uuid.uuid4().hex, "before": before, "paths": ["note.md", destination],
                 "actor": "human", "reason": "rename"}
        journal = external_journal(history, entry)
        history.store.path("note.md").rename(history.store.path(destination))
        history.finish_external(entry)
        history.finish_external(entry)  # deterministic journal replay
        history.recover()
    assert history.store.read(doc["id"])["path"] == destination
    assert history.store.path(destination).read_bytes() == raw
    assert current_origin(history, doc["id"]) == state
    assert not journal.exists()
    with history.store.connect() as db:
        assert db.execute("SELECT count(*) FROM changes WHERE reason='rename'").fetchone()[0] == 2


def external_journal(history, entry):
    journal = history.state / "external-change.json"
    atomic(journal, json.dumps(entry).encode())
    return journal


def test_trash_restore_cannot_take_an_identity_active_at_another_path(history):
    doc, state, raw, _ = opaque_origin(history, "elsewhere.md")
    returned = b"A returned file with a stale trash receipt.\n"
    with history.locked():
        entry = {
            "version": 2, "source_binding_protocol": 1,
            "id": uuid.uuid4().hex, "before": history.capture_external(["note.md"]),
            "paths": ["note.md"], "actor": "human", "reason": "trash/restore",
            "restored": {"path": "note.md", "document_id": doc["id"]},
            "trash_id": "stale",
        }
        journal = external_journal(history, entry)
        history.store.path("note.md").write_bytes(returned)
    with pytest.raises(WikiError, match="Restored document identity changed"):
        history.recover()
    assert journal.exists()
    with history.store.connect() as db:
        assert db.execute("SELECT id FROM document_identities WHERE path='elsewhere.md'").fetchone()[0] == doc["id"]
        assert db.execute("SELECT id FROM document_identities WHERE path='note.md'").fetchone()[0] != doc["id"]
    assert history.store.path("elsewhere.md").read_bytes() == raw
    assert history.store.path("note.md").read_bytes() == returned
    assert current_origin(history, doc["id"]) == state
    # A new Store performs startup recovery before it can be exposed. Join the
    # old observer first; the retained conflict must still block opening.
    history.store.close()
    with pytest.raises(WikiError, match="Restored document identity changed"):
        WikiStore(history.store.root, history.store.state, history.store.vault_id)


def test_external_capture_binds_unobserved_preimage_without_certifying_it(history):
    doc, state, raw, _ = opaque_origin(history)
    edited = raw + b"Human correction without opening the Wiki first.\n"
    history.store.path("note.md").write_bytes(edited)
    with history.locked():
        captured = history.capture_external(["note.md"])["note.md"]
    assert captured["hash"] == revision(edited)
    claim = captured["origin_state"]
    assert claim["doc_id"] == doc["id"] and claim["revision"] == captured["hash"]
    assert claim["status"] == "invalid" and claim["proof"] == state["proof"]


@pytest.mark.parametrize("destination", ["a-moved.md", "z-moved.md"])
@pytest.mark.parametrize("stale_claim", [False, True])
def test_unobserved_human_edit_then_move_recovers_without_blocking_other_writes(
    history, destination, stale_claim,
):
    doc, state, raw, _ = opaque_origin(history)
    edited = raw + b"An ordinary human correction.\n"
    history.store.path("note.md").write_bytes(edited)
    with history.locked():
        before = history.capture_external(["note.md", destination])
        if stale_claim:
            before["note.md"]["origin_state"] = state
        entry = {"version": 2, "source_binding_protocol": 1,
                 "id": uuid.uuid4().hex, "before": before,
                 "paths": ["note.md", destination], "actor": "human", "reason": "rename"}
        external_journal(history, entry)
        history.store.path("note.md").rename(history.store.path(destination))
    history.recover()
    history.write("unrelated.md", b"An unrelated write remains possible.", None, "human", "create")
    with history.locked():
        history.recover()
    current = current_origin(history, doc["id"])
    assert current["status"] == "invalid" and current["revision"] == revision(edited)
    assert current["proof"] == state["proof"]
    assert history.store.read(doc["id"])["path"] == destination
    assert history.store.path(destination).read_bytes() == edited
    assert not (history.state / "external-change.json").exists()
    with history.store.connect() as db:
        rows = db.execute("SELECT r.document_id FROM changes c JOIN change_retention r ON r.change_id=c.id WHERE reason='rename'").fetchall()
        assert len(rows) == 2 and {row[0] for row in rows} == {doc["id"]}


@pytest.mark.parametrize("stale_claim", [False, True])
def test_unobserved_human_edit_then_trash_restore_retains_bytes_and_invalid_payload(
    history, tmp_path, stale_claim,
):
    doc, state, raw, _ = opaque_origin(history)
    edited = raw + b"A human edit before deletion.\n"
    history.store.path("note.md").write_bytes(edited)
    trash_root = tmp_path / "trash"
    archive = trash_root / "saved"
    archive.mkdir(parents=True)
    (archive / "info.json").write_text(json.dumps({"path": "note.md"}))
    with history.locked():
        before = history.capture_external(["note.md"])
        if stale_claim:
            before["note.md"]["origin_state"] = state
        deletion = {"version": 2, "source_binding_protocol": 1,
                    "id": uuid.uuid4().hex, "before": before,
                    "paths": ["note.md"], "actor": "human", "reason": "delete",
                    "trash_root": str(trash_root)}
        journal = external_journal(history, deletion)
        history.store.path("note.md").rename(archive / "file")
        history.recover()
        assert not journal.exists()
        with history.store.connect() as db:
            if stale_claim:
                db.execute("UPDATE app_trash SET origin_state=?", (json.dumps(state),))
            restored = dict(db.execute("SELECT * FROM app_trash WHERE trash_id='saved'").fetchone())
        entry = {"version": 2, "source_binding_protocol": 1,
                 "id": uuid.uuid4().hex, "before": history.capture_external(["note.md"]),
                 "paths": ["note.md"], "actor": "human", "reason": "trash/restore",
                 "restored": restored, "trash_id": "saved"}
        external_journal(history, entry)
        (archive / "file").rename(history.store.path("note.md"))
    history.recover()
    history.write("unrelated.md", b"Available after trash restore.", None, "human", "create")
    with history.locked():
        history.recover()
    current = current_origin(history, doc["id"])
    assert current["status"] == "invalid" and current["revision"] == revision(edited)
    assert current["proof"] == state["proof"]
    assert history.store.read("note.md")["id"] == doc["id"]
    assert history.store.path("note.md").read_bytes() == edited
    assert not journal.exists()
    with history.store.connect() as db:
        assert db.execute("SELECT count(*) FROM app_trash").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM changes WHERE reason='trash/restore'").fetchone()[0] == 1


@pytest.mark.parametrize("destination", ["a-moved.md", "z-moved.md"])
@pytest.mark.parametrize("window", ["identity", "first_record", "before_unlink"])
def test_current_journal_replays_stable_origin_and_change_identity(
    history, monkeypatch, destination, window,
):
    doc, state, raw, _ = opaque_origin(history)
    with history.locked():
        before = history.capture_external(["note.md", destination])
        entry = {"version": 2, "source_binding_protocol": 1,
                 "id": uuid.uuid4().hex, "before": before,
                 "paths": ["note.md", destination], "actor": "human", "reason": "rename"}
        journal = external_journal(history, entry)
        history.store.path("note.md").rename(history.store.path(destination))
    expected_changes = {
        uuid.uuid5(uuid.NAMESPACE_URL, f"{entry['id']}:{path}").hex
        for path in ("note.md", destination)
    }
    move, record, unlink = history.store.move_identity, history.record, type(journal).unlink
    injected = False

    def crash_once():
        nonlocal injected
        if not injected:
            injected = True
            raise RuntimeError("external recovery crash")

    def move_then_crash(*args, **kwargs):
        move(*args, **kwargs)
        crash_once()

    def record_then_crash(*args, **kwargs):
        result = record(*args, **kwargs)
        crash_once()
        return result

    def crash_before_unlink(path, *args, **kwargs):
        if path == journal:
            crash_once()
        return unlink(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        if window == "identity":
            patch.setattr(history.store, "move_identity", move_then_crash)
        elif window == "first_record":
            patch.setattr(history, "record", record_then_crash)
        else:
            patch.setattr(type(journal), "unlink", crash_before_unlink)
        with pytest.raises(RuntimeError, match="external recovery crash"):
            history.recover()
    saved = json.loads(journal.read_text())
    assert saved["id"] == entry["id"] and saved["before"] == before
    with history.store.connect() as db:
        committed_changes = {
            row[0] for row in db.execute("SELECT id FROM changes WHERE reason='rename'")
        }
    assert committed_changes <= expected_changes
    assert len(committed_changes) == {"identity": 0, "first_record": 1, "before_unlink": 2}[window]
    history.recover()
    history.write("unrelated.md", b"Recovery no longer blocks writes.", None, "human", "create")
    with history.locked():
        history.recover()
    assert current_origin(history, doc["id"]) == state
    for reference in (doc["id"], doc["metadata"]["uid"]):
        read = history.store.read(reference)
        assert read["id"] == doc["id"] and read["path"] == destination
        assert read["metadata"]["uid"] == doc["metadata"]["uid"]
    assert history.store.path(destination).read_bytes() == raw
    assert not journal.exists()
    with history.store.connect() as db:
        rows = db.execute("SELECT c.id,r.document_id FROM changes c JOIN change_retention r ON r.change_id=c.id WHERE reason='rename'").fetchall()
        assert len(rows) == 2 and {row[0] for row in rows} == expected_changes
        assert {row[1] for row in rows} == {doc["id"]}


def test_current_origin_survives_derived_rebuild_and_history_expiration(history):
    doc, state, raw, _ = opaque_origin(history)
    changed = history.write("note.md", raw + b"change", doc["revision"], "human", "edit")
    current = current_origin(history, doc["id"])
    history.store.rebuild_search()
    assert current_origin(history, doc["id"]) == current
    now = datetime.now(UTC)
    with history.store.connect() as db:
        db.execute("UPDATE changes SET created_at=?", ((now - timedelta(days=31)).isoformat(),))
    history.cleanup(now)
    with history.store.connect() as db:
        assert db.execute("SELECT before_state,after_state FROM change_material_origins WHERE change_id=?", (changed["change_id"],)).fetchone()[:] == (None, None)
    assert current_origin(history, doc["id"]) == current
    with pytest.raises(WikiError) as error:
        history.restore(changed["change_id"], changed["revision"], "before", "human")
    assert error.value.status == 410
    assert current["proof"] == state["proof"]


def test_private_origin_state_never_appears_in_document_or_public_history(history):
    doc, state, raw, _ = opaque_origin(history)
    history.write("note.md", raw, doc["revision"], "human", "unchanged")
    result = {"read": history.store.read(doc["id"]),
              "search": history.store.search("condition"),
              "documents": history.store.documents(), "history": history.history("note.md")}
    assert "private YAML fragment" not in json.dumps(result)
    assert current_origin(history, doc["id"])["proof"] == state["proof"]


def test_current_reopen_preserves_rows_without_synthesizing_origin_proofs(history):
    history.write("note.md", b"legacy material", None, "human", "create")
    store = history.store
    store.close()
    with sqlite3.connect(store.db_path) as db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN IMMEDIATE")
        tables = ["documents", "document_identities", "changes", "change_management", "change_retention", "operation_receipts",
                  "material_origins", "change_material_origins"]
        before = {t: list(map(tuple, db.execute("SELECT * FROM " + t))) for t in tables}
        assert db.execute("SELECT count(*) FROM material_origins").fetchone()[0] == 0
    for _ in range(2):
        upgraded = WikiStore(store.root, store.state, store.vault_id)
        try:
            assert upgraded.read("note.md")["content"] == "legacy material"
            with upgraded.connect() as db:
                assert db.execute("SELECT max(version) FROM wiki_schema").fetchone()[0] == schema.VERSION
                assert before == {t: [tuple(r) for r in db.execute("SELECT * FROM " + t)] for t in tables}
                assert db.execute("SELECT count(*) FROM material_origins").fetchone()[0] == 0
                assert all(value is None or json.loads(value) is None
                           for row in db.execute("SELECT before_state,after_state FROM change_material_origins") for value in row)
        finally:
            upgraded.close()


def test_invalid_private_json_is_retained_and_reported(history):
    doc, _, _, _ = opaque_origin(history)
    with history.store.connect() as db:
        db.execute("UPDATE material_origins SET state='broken' WHERE doc_id=?", (doc["id"],))
        with pytest.raises(WikiError, match="retained"):
            origins.get(db, doc["id"])
        assert db.execute("SELECT state FROM material_origins WHERE doc_id=?", (doc["id"],)).fetchone()[0] == "broken"


@pytest.mark.parametrize("missing", ["material_origins", "change_material_origins"])
def test_lost_durable_origin_table_is_reported_not_rebuilt_empty(history, missing):
    doc, _, raw, _ = opaque_origin(history)
    store = history.store
    store.close()
    with sqlite3.connect(store.db_path) as db:
        db.execute("DROP TABLE " + missing)
    before = store.db_path.read_bytes()
    with pytest.raises(WikiError, match="retained") as error:
        WikiStore(store.root, store.state, store.vault_id, health=store.health)
    assert error.value.status == 503
    assert store.path("note.md").read_bytes() == raw
    assert store.db_path.read_bytes() == before
    with sqlite3.connect(store.db_path) as db:
        assert db.execute("SELECT path FROM document_identities WHERE id=?", (doc["id"],)).fetchone()[0] == "note.md"
        assert db.execute("SELECT name FROM sqlite_master WHERE name=?", (missing,)).fetchone() is None
