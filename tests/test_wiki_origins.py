"""Opaque origin state survives schema upgrades, writer crashes and recovery.

The synthetic proof payloads deliberately use an unknown format. These tests
exercise the compatible state owner, not byte reconstruction or R08 reuse.
"""

import contextlib
import json
import sqlite3
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from server.wiki import origins, schema
from server.wiki.history import WikiHistory, atomic
from server.wiki.store import WikiError, WikiStore, revision


@pytest.fixture
def history(tmp_path):
    root = tmp_path / "vault"
    root.mkdir()
    store = WikiStore(root, tmp_path / "state", str(uuid.uuid4()))
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

    def crash_document_replace(path, content):
        if path == history.store.path("note.md"):
            crash()
        atomic(path, content)

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
    with history.locked():
        pass
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
        history.store.path("note.md").rename(history.store.path(destination))
        entry = {"id": uuid.uuid4().hex, "before": before, "paths": ["note.md", destination],
                 "actor": "human", "reason": "rename"}
        history.finish_external(entry)
        history.finish_external(entry)  # deterministic journal replay
    assert history.store.read(doc["id"])["path"] == destination
    assert history.store.path(destination).read_bytes() == raw
    assert current_origin(history, doc["id"]) == state
    with history.store.connect() as db:
        assert db.execute("SELECT count(*) FROM changes WHERE reason='rename'").fetchone()[0] == 2


def test_legacy_external_exact_move_preserves_current_origin(history):
    doc, state, raw, _ = opaque_origin(history)
    with history.locked():
        before = history.inventory()
        history.store.path("note.md").rename(history.store.path("renamed.md"))
        history.record_external(before, history.inventory(), "human", "legacy rename")
        history.record_external(before, history.inventory(), "human", "legacy rename")
    assert history.store.read(doc["id"])["path"] == "renamed.md"
    assert history.store.path("renamed.md").read_bytes() == raw
    assert current_origin(history, doc["id"]) == state


def external_journal(history, entry):
    journal = history.state / "external-change.json"
    atomic(journal, json.dumps(entry).encode())
    return journal


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
def test_legacy_journal_of_unobserved_edit_and_move_preserves_invalid_payload(history, destination):
    doc, state, raw, _ = opaque_origin(history)
    edited = raw + b"Unindexed human edit before a legacy operation.\n"
    history.store.path("note.md").write_bytes(edited)
    with history.locked():
        before = history.inventory()
        history.store.path("note.md").rename(history.store.path(destination))
        external_journal(history, {"version": 1, "before": before,
                                   "actor": "human", "reason": "legacy rename"})
    history.write("unrelated.md", b"New work.", None, "human", "create")
    current = current_origin(history, doc["id"])
    assert current["status"] == "invalid" and current["revision"] == revision(edited)
    assert current["proof"] == state["proof"]
    assert history.store.read(doc["id"])["path"] == destination
    assert history.store.path(destination).read_bytes() == edited


@pytest.mark.parametrize("destination", ["a-moved.md", "z-moved.md"])
@pytest.mark.parametrize("old_journal", [False, True])
def test_unobserved_human_edit_then_move_recovers_without_blocking_other_writes(
    history, destination, old_journal,
):
    doc, state, raw, _ = opaque_origin(history)
    edited = raw + b"An ordinary human correction.\n"
    history.store.path("note.md").write_bytes(edited)
    with history.locked():
        before = history.capture_external(["note.md", destination])
        if old_journal:
            before["note.md"]["origin_state"] = state
        entry = {"version": 2, "id": uuid.uuid4().hex, "before": before,
                 "paths": ["note.md", destination], "actor": "human", "reason": "rename"}
        external_journal(history, entry)
        history.store.path("note.md").rename(history.store.path(destination))
    history.write("unrelated.md", b"An unrelated write remains possible.", None, "human", "create")
    with history.locked():
        history.finish_external(entry)
        history.finish_external(entry)
    current = current_origin(history, doc["id"])
    assert current["status"] == "invalid" and current["revision"] == revision(edited)
    assert current["proof"] == state["proof"]
    assert history.store.read(doc["id"])["path"] == destination
    assert history.store.path(destination).read_bytes() == edited
    assert not (history.state / "external-change.json").exists()
    with history.store.connect() as db:
        rows = db.execute("SELECT r.document_id FROM changes c JOIN change_retention r ON r.change_id=c.id WHERE reason='rename'").fetchall()
        assert len(rows) == 2 and {row[0] for row in rows} == {doc["id"]}


@pytest.mark.parametrize("old_trash", [False, True])
def test_unobserved_human_edit_then_trash_restore_retains_bytes_and_invalid_payload(
    history, tmp_path, old_trash,
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
        if old_trash:
            before["note.md"]["origin_state"] = state
        deletion = {"version": 2, "id": uuid.uuid4().hex, "before": before,
                    "paths": ["note.md"], "actor": "human", "reason": "delete",
                    "trash_root": str(trash_root)}
        history.store.path("note.md").rename(archive / "file")
        history.finish_external(deletion)
        with history.store.connect() as db:
            if old_trash:
                db.execute("UPDATE app_trash SET origin_state=?", (json.dumps(state),))
            restored = dict(db.execute("SELECT * FROM app_trash WHERE trash_id='saved'").fetchone())
        entry = {"version": 2, "id": uuid.uuid4().hex, "before": {},
                 "paths": ["note.md"], "actor": "human", "reason": "trash/restore",
                 "restored": restored, "trash_id": "saved"}
        external_journal(history, entry)
        (archive / "file").rename(history.store.path("note.md"))
    history.write("unrelated.md", b"Available after trash restore.", None, "human", "create")
    with history.locked():
        history.finish_external(entry)
        history.finish_external(entry)
    current = current_origin(history, doc["id"])
    assert current["status"] == "invalid" and current["revision"] == revision(edited)
    assert current["proof"] == state["proof"]
    assert history.store.read("note.md")["id"] == doc["id"]
    assert history.store.path("note.md").read_bytes() == edited
    with history.store.connect() as db:
        assert db.execute("SELECT count(*) FROM app_trash").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM changes WHERE reason='trash/restore'").fetchone()[0] == 1


@pytest.mark.parametrize("destination", ["a-moved.md", "z-moved.md"])
@pytest.mark.parametrize("window", ["identity", "first_record", "before_unlink"])
def test_legacy_journal_upgrade_replays_stable_origin_and_change_identity(
    history, monkeypatch, destination, window,
):
    doc, state, raw, _ = opaque_origin(history)
    with history.locked():
        before = history.inventory()
        history.store.path("note.md").rename(history.store.path(destination))
        journal = external_journal(history, {
            "version": 1, "before": before, "actor": "human", "reason": "legacy rename",
        })
    move, record, unlink = history.store.move_identity, history.record, type(journal).unlink
    injected = False

    def crash_once():
        nonlocal injected
        if not injected:
            injected = True
            raise RuntimeError("legacy recovery crash")

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
        with pytest.raises(RuntimeError, match="legacy recovery crash"):
            with history.locked():
                pass
    assert json.loads(journal.read_text())["version"] == 2
    history.write("unrelated.md", b"Recovery no longer blocks writes.", None, "human", "create")
    with history.locked():
        history.recover()
    assert current_origin(history, doc["id"]) == state
    assert history.store.read(doc["id"])["path"] == destination
    assert history.store.path(destination).read_bytes() == raw
    assert not journal.exists()
    with history.store.connect() as db:
        rows = db.execute("SELECT r.document_id FROM changes c JOIN change_retention r ON r.change_id=c.id WHERE reason LIKE 'legacy rename%'").fetchall()
        assert len(rows) == 2 and {row[0] for row in rows} == {doc["id"]}


@pytest.mark.parametrize("destination", ["a-moved.md", "z-moved.md"])
@pytest.mark.parametrize("committed", ["identity", "first_record", "complete"])
def test_preexisting_partly_committed_legacy_journal_preserves_current_payload(
    history, destination, committed,
):
    doc, state, raw, _ = opaque_origin(history)
    with history.locked():
        before = history.inventory()
        history.store.path("note.md").rename(history.store.path(destination))
        history.store.move_identity("note.md", destination)
        if committed != "identity":
            paths = sorted(["note.md", destination])
            for relative in paths if committed == "complete" else paths[:1]:
                removed = relative == "note.md"
                history.record(
                    relative, raw if removed else None, None if removed else raw,
                    "human", "legacy rename; recovered external mutation", "applied",
                    document_id=doc["id"], before_origin=state if removed else None,
                    origin_state=origins.UNCHANGED if removed else state,
                )
        external_journal(history, {"version": 1, "before": before,
                                   "actor": "human", "reason": "legacy rename"})
    history.write("unrelated.md", b"New work.", None, "human", "create")
    with history.locked():
        history.recover()
    assert current_origin(history, doc["id"]) == state
    assert history.store.read(doc["id"])["path"] == destination
    assert history.store.path(destination).read_bytes() == raw
    assert not (history.state / "external-change.json").exists()
    with history.store.connect() as db:
        rows = db.execute("SELECT r.document_id FROM changes c JOIN change_retention r ON r.change_id=c.id WHERE reason LIKE 'legacy rename%'").fetchall()
        assert {row[0] for row in rows} == {doc["id"]}


def legacy_reserved_move(history, monkeypatch, destination, window):
    """3b60 recorded each path without document_id or origin-state support."""
    doc, state, raw, _ = opaque_origin(history)
    with history.locked():
        before = history.inventory()
        history.store.path("note.md").rename(history.store.path(destination))
        history.store.move_identity("note.md", destination)
        with monkeypatch.context() as legacy:
            legacy.setattr(origins, "record", lambda *args, **kwargs: None)
            legacy.setattr(origins, "apply", lambda *args, **kwargs: None)
            for relative in sorted(["note.md", destination]):
                removed = relative == "note.md"
                history.record(
                    relative, raw if removed else None, None if removed else raw,
                    "human", "legacy rename; recovered external mutation", "applied",
                )
                if removed and window == "deletion":
                    break
        external_journal(history, {"version": 1, "before": before,
                                   "actor": "human", "reason": "legacy rename"})
        with history.store.connect() as db:
            reserved = db.execute("SELECT id,file_key FROM document_identities WHERE path='note.md'").fetchone()
            assert reserved[0] != doc["id"] and reserved[1] == ""
            assert db.execute("SELECT id FROM document_identities WHERE path=?", (destination,)).fetchone()[0] == doc["id"]
    return doc, state, raw, reserved[0]


@pytest.mark.parametrize("schema_version", [7, 8])
@pytest.mark.parametrize("destination", ["a-moved.md", "z-moved.md"])
@pytest.mark.parametrize("window", ["deletion", "complete"])
def test_legacy_deleted_path_reservation_cannot_replace_the_real_moved_identity(
    history, monkeypatch, schema_version, destination, window,
):
    doc, state, raw, reserved_id = legacy_reserved_move(history, monkeypatch, destination, window)
    upgraded = None
    active = history
    if schema_version == 7:
        root, state_dir, vault_id = downgrade_fixture_to_schema7(history)
        upgraded = WikiStore(root, state_dir, vault_id)
        active = WikiHistory(upgraded)
    try:
        active.write("unrelated.md", b"New work after legacy recovery.", None, "human", "create")
        with active.locked():
            active.recover()
        with active.store.connect() as db:
            assert db.execute("SELECT id FROM document_identities WHERE path=?", (destination,)).fetchone()[0] == doc["id"]
            assert db.execute("SELECT path FROM document_identities WHERE id=?", (reserved_id,)).fetchone()[0] is None
            # Keep the old handler's history rather than rewriting its ownership.
            assert db.execute("SELECT count(*) FROM change_retention WHERE document_id=?", (reserved_id,)).fetchone()[0] == 1
            for relative in ("note.md", destination):
                rows = db.execute("SELECT r.document_id FROM changes c JOIN change_retention r ON r.change_id=c.id WHERE c.path=? AND c.reason LIKE 'legacy rename%' ORDER BY c.rowid DESC LIMIT 1", (relative,)).fetchall()
                assert rows and rows[0][0] == doc["id"]
        for reference in (doc["id"], doc["metadata"]["uid"]):
            read = active.store.read(reference)
            assert read["id"] == doc["id"] and read["path"] == destination
            assert read["metadata"]["uid"] == doc["metadata"]["uid"]
        assert active.store.path(destination).read_bytes() == raw
        assert current_origin(active, doc["id"]) == (state if schema_version == 8 else None)
        assert not (active.state / "external-change.json").exists()
    finally:
        if upgraded:
            upgraded.close()


@pytest.mark.parametrize("destination", ["a-moved.md", "z-moved.md"])
@pytest.mark.parametrize("window", ["upgraded_journal", "first_record"])
def test_upgraded_legacy_journal_keeps_selected_identity_across_another_crash(
    history, monkeypatch, destination, window,
):
    import server.wiki.history as history_module

    doc, state, raw, reserved_id = legacy_reserved_move(history, monkeypatch, destination, "deletion")
    journal = history.state / "external-change.json"
    original_atomic, record = history_module.atomic, history.record
    with history.store.connect() as db:
        previous_changes = db.execute("SELECT count(*) FROM changes WHERE reason LIKE 'legacy rename%'").fetchone()[0]

    def after_upgrade(path, content):
        original_atomic(path, content)
        if path == journal:
            raise RuntimeError("crash after upgrading journal")

    def after_record(*args, **kwargs):
        record(*args, **kwargs)
        raise RuntimeError("crash after committing a recovery record")

    with monkeypatch.context() as interruption:
        if window == "upgraded_journal":
            interruption.setattr(history_module, "atomic", after_upgrade)
        else:
            interruption.setattr(history, "record", after_record)
        with pytest.raises(RuntimeError, match="crash after"):
            with history.locked():
                pass
    saved = json.loads(journal.read_text())
    assert saved["version"] == 2 and saved["before"]["note.md"]["document_id"] == doc["id"]
    history.write("unrelated.md", b"Recovery remains usable.", None, "human", "create")
    assert history.store.read(doc["metadata"]["uid"])["id"] == doc["id"]
    assert history.store.read(doc["id"])["path"] == destination
    assert current_origin(history, doc["id"]) == state
    assert history.store.path(destination).read_bytes() == raw
    assert not journal.exists()
    with history.store.connect() as db:
        assert db.execute("SELECT path FROM document_identities WHERE id=?", (reserved_id,)).fetchone()[0] is None
        assert db.execute("SELECT count(*) FROM changes WHERE reason LIKE 'legacy rename%'").fetchone()[0] == previous_changes + 2


@pytest.mark.parametrize("destination", ["a-moved.md", "z-moved.md"])
@pytest.mark.parametrize("unverified", ["missing_target", "changed_target_key", "reserved_has_uid", "reserved_has_origin"])
def test_legacy_reservation_recovery_requires_verified_ownership(
    history, monkeypatch, destination, unverified,
):
    doc, state, raw, reserved_id = legacy_reserved_move(history, monkeypatch, destination, "complete")
    with history.store.connect() as db:
        if unverified == "missing_target":
            db.execute("UPDATE document_identities SET path=NULL WHERE id=?", (doc["id"],))
            db.execute("DELETE FROM documents WHERE id=?", (doc["id"],))
        elif unverified == "changed_target_key":
            db.execute("UPDATE document_identities SET file_key='0:0:0' WHERE id=?", (doc["id"],))
        elif unverified == "reserved_has_uid":
            db.execute("INSERT INTO uid_aliases VALUES(?,?)", ('urn:uuid:' + str(uuid.uuid4()), reserved_id))
        else:
            origins.put(db, reserved_id, state | {"doc_id": reserved_id})
        identities = [tuple(row) for row in db.execute("SELECT * FROM document_identities ORDER BY id")]
        states = [tuple(row) for row in db.execute("SELECT * FROM material_origins ORDER BY doc_id")]
    journal = history.state / "external-change.json"
    saved = journal.read_bytes()
    with pytest.raises(WikiError, match="Ambiguous external move identity"):
        with history.locked():
            pass
    assert journal.read_bytes() == saved
    assert history.store.path(destination).read_bytes() == raw
    with history.store.connect() as db:
        assert [tuple(row) for row in db.execute("SELECT * FROM document_identities ORDER BY id")] == identities
        assert [tuple(row) for row in db.execute("SELECT * FROM material_origins ORDER BY doc_id")] == states


@pytest.mark.parametrize("destination", ["a-moved.md", "z-moved.md"])
def test_legacy_move_does_not_merge_competing_real_identities_by_content(history, destination):
    doc, state, raw, _ = opaque_origin(history)
    with history.locked():
        before = history.inventory()
        # A copied target has its own observed identity; the old logical binding
        # refers to another inode, so exact content alone cannot establish a move.
        history.store.path(destination).write_bytes(raw)
        with history.store.connect() as db:
            target = history.store._index(db, destination)
            source_key = db.execute("SELECT file_key FROM document_identities WHERE id=?", (doc["id"],)).fetchone()[0]
        history.store.path("note.md").unlink()
        entry = {"version": 1, "before": before, "actor": "human", "reason": "legacy rename"}
        journal = external_journal(history, entry)
    with pytest.raises(WikiError, match="Ambiguous external move identity") as error:
        with history.locked():
            pass
    assert error.value.status == 409
    assert json.loads(journal.read_text()) == entry
    assert history.store.path(destination).read_bytes() == raw
    with history.store.connect() as db:
        assert db.execute("SELECT id FROM document_identities WHERE path=?", (destination,)).fetchone()[0] == target["id"]
        assert db.execute("SELECT file_key FROM document_identities WHERE id=?", (doc["id"],)).fetchone()[0] == source_key
    assert current_origin(history, doc["id"]) == state


@pytest.mark.parametrize("destination", ["a-moved.md", "z-moved.md"])
def test_legacy_move_preserves_verified_source_over_a_stale_target_binding(history, destination):
    doc, state, raw, _ = opaque_origin(history)
    stale = history.write(destination, b"An unrelated previously deleted document.", None, "human", "create")
    stale_id = history.store.read(destination)["id"]
    history.store.path(destination).unlink()
    with history.locked():
        before = history.inventory()
        history.store.path("note.md").rename(history.store.path(destination))
        external_journal(history, {"version": 1, "before": before,
                                   "actor": "human", "reason": "legacy rename"})
    history.write("unrelated.md", b"New work.", None, "human", "create")
    assert history.store.read(doc["id"])["path"] == destination
    assert history.store.path(destination).read_bytes() == raw
    assert current_origin(history, doc["id"]) == state
    with history.store.connect() as db:
        assert db.execute("SELECT path FROM document_identities WHERE id=?", (stale_id,)).fetchone()[0] is None
        assert db.execute("SELECT document_id FROM change_retention WHERE change_id=?", (stale["change_id"],)).fetchone()[0] == stale_id


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


def downgrade_fixture_to_schema7(history):
    """Make the exact older table shape, retaining its real document/history rows."""
    store = history.store
    store.close()
    with sqlite3.connect(store.db_path) as db:
        for action in ("insert", "update", "delete"):
            db.execute(f"DROP TRIGGER wiki_origin_{action}")
        db.execute("DROP TABLE material_origins")
        db.execute("DROP TABLE change_material_origins")
        db.execute("ALTER TABLE app_trash DROP COLUMN origin_state")
        db.execute("DELETE FROM wiki_schema WHERE version=8")
    return store.root, store.state, store.vault_id


def test_schema7_upgrade_is_transactional_idempotent_and_does_not_backfill_proofs(history):
    history.write("note.md", b"legacy material", None, "human", "create")
    root, state, vault_id = downgrade_fixture_to_schema7(history)
    with sqlite3.connect(state / "index.sqlite3") as db:
        tables = ["documents", "document_identities", "changes", "change_management", "change_retention", "operation_receipts"]
        before = {t: db.execute("SELECT * FROM " + t).fetchall() for t in tables}
    for _ in range(2):
        upgraded = WikiStore(root, state, vault_id)
        try:
            assert upgraded.read("note.md")["content"] == "legacy material"
            with upgraded.connect() as db:
                assert db.execute("SELECT max(version) FROM wiki_schema").fetchone()[0] == 8
                assert before == {t: [tuple(r) for r in db.execute("SELECT * FROM " + t)] for t in tables}
                assert db.execute("SELECT count(*) FROM material_origins").fetchone()[0] == 0
                assert db.execute("SELECT count(*) FROM change_material_origins").fetchone()[0] == 0
        finally:
            upgraded.close()


def test_interrupted_schema8_migration_retains_schema7_and_all_old_state(history):
    history.write("note.md", b"legacy material", None, "human", "create")
    _, state, _ = downgrade_fixture_to_schema7(history)

    @contextlib.contextmanager
    def connection():
        db = sqlite3.connect(state / "index.sqlite3")
        db.row_factory = sqlite3.Row
        db.set_authorizer(lambda action, *args: sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_ALTER_TABLE else sqlite3.SQLITE_OK)
        try:
            with db:
                yield db
        finally:
            db.close()

    with pytest.raises(sqlite3.DatabaseError):
        schema.initialize(SimpleNamespace(connect=connection))
    with sqlite3.connect(state / "index.sqlite3") as db:
        assert db.execute("SELECT max(version) FROM wiki_schema").fetchone()[0] == 7
        assert db.execute("SELECT count(*) FROM changes").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM sqlite_master WHERE name IN ('material_origins','change_material_origins')").fetchone()[0] == 0
        assert "origin_state" not in {row[1] for row in db.execute("PRAGMA table_info(app_trash)")}


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
    with pytest.raises(WikiError, match="ledger is missing") as error:
        WikiStore(store.root, store.state, store.vault_id)
    assert error.value.status == 503
    assert store.path("note.md").read_bytes() == raw
    with sqlite3.connect(store.db_path) as db:
        assert db.execute("SELECT path FROM document_identities WHERE id=?", (doc["id"],)).fetchone()[0] == "note.md"
        assert db.execute("SELECT name FROM sqlite_master WHERE name=?", (missing,)).fetchone() is None
