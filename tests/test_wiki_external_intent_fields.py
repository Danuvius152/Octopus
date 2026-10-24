"""Operation-specific intent fields are checked before any recovery effect."""

import json
import uuid

import pytest

from server.wiki.history import atomic
from server.wiki.store import WikiError
from tests.test_wiki_external_journal_format import history as history


def durable_facts(history):
    with history.store.connect() as db:
        return {name: [tuple(row) for row in db.execute(f"SELECT * FROM {name} ORDER BY rowid")]
                for name in ("changes", "change_retention", "document_identities", "material_origins",
                             "source_binding_observations", "source_binding_head")}


@pytest.mark.parametrize("malformed", [
    "delete-missing-root", "delete-null-root", "delete-empty-root", "delete-list-root",
    "restore-list", "restore-string", "restore-missing-path", "restore-null-path",
    "restore-empty-path", "restore-escape-path", "restore-nontext-path", "restore-list-path",
    "restore-numeric-id", "restore-empty-id", "restore-missing-trash-id",
])
def test_malformed_operation_is_refused_before_policy_write_or_synchronization(history, malformed):
    original = b"# Original\nOriginal preimage and identity.\n"
    history.write("note.md", original, None, "human", "create")
    journal = history.state / "external-change.json"
    with history.locked():
        entry = {"version": 2, "source_binding_protocol": 1, "id": uuid.uuid4().hex,
                 "before": history.capture_external(["note.md"]), "paths": ["note.md"],
                 "actor": "app:sapphire", "reason": "delete" if malformed.startswith("delete") else "restore",
                 "trash_root": str(history.state / "nonexistent-trash")}
        if malformed.startswith("delete"):
            if malformed == "delete-missing-root":
                del entry["trash_root"]
            else:
                entry["trash_root"] = {"delete-null-root": None, "delete-empty-root": "", "delete-list-root": []}[malformed]
        else:
            restored = {"path": "note.md", "document_id": None}
            if malformed == "restore-list":
                restored = []
            elif malformed == "restore-string":
                restored = "invalid row"
            elif malformed == "restore-missing-path":
                del restored["path"]
            elif malformed == "restore-missing-trash-id":
                restored["document_id"] = entry["before"]["note.md"]["document_id"]
            elif malformed.endswith("-path"):
                restored["path"] = {"restore-null-path": None, "restore-empty-path": "",
                                    "restore-escape-path": "../outside.md", "restore-nontext-path": "image.png",
                                    "restore-list-path": []}[malformed]
            else:
                restored["document_id"] = 7 if malformed == "restore-numeric-id" else ""
            entry["restored"] = restored
        raw = json.dumps(entry, indent=2).encode()
        atomic(journal, raw)
        history.store.path("note.md").unlink()
    facts = durable_facts(history)
    blobs = {path.name: path.read_bytes() for path in history.blobs.iterdir()}

    with pytest.raises(WikiError, match="external.*format") as failure:
        history.recover()

    assert failure.value.status == 503
    assert journal.read_bytes() == raw
    assert not history.store.path("note.md").exists()
    assert durable_facts(history) == facts
    assert {path.name: path.read_bytes() for path in history.blobs.iterdir()} == blobs


def test_current_delete_needs_no_existing_trash_directory(history):
    original = b"# Deleted\nOriginal deletion bytes.\n"
    history.write("note.md", original, None, "human", "create")
    journal = history.state / "external-change.json"
    with history.locked():
        trash_root = history.state / "nonexistent-trash"
        assert not trash_root.exists()
        entry = {"version": 2, "source_binding_protocol": 1, "id": uuid.uuid4().hex,
                 "before": history.capture_external(["note.md"]), "paths": ["note.md"],
                 "actor": "app:sapphire", "reason": "delete", "trash_root": str(trash_root)}
        atomic(journal, json.dumps(entry).encode())
        history.store.path("note.md").unlink()
    history.recover()
    assert not journal.exists() and not trash_root.exists()
    assert not history.store.path("note.md").exists()
    assert any(row["reason"] == "delete" for row in history.history("note.md")["items"])


@pytest.mark.parametrize("restored", [None, {"path": "note.md", "document_id": None}])
def test_current_restore_preserves_nullable_document_identity(history, restored):
    raw = b"# Returned\nCurrent returned bytes without an old document identity.\n"
    journal = history.state / "external-change.json"
    with history.locked():
        entry = {"version": 2, "source_binding_protocol": 1, "id": uuid.uuid4().hex,
                 "before": history.capture_external(["note.md"]), "paths": ["note.md"],
                 "actor": "app:sapphire", "reason": "restore", "restored": restored}
        atomic(journal, json.dumps(entry).encode())
        history.store.path("note.md").write_bytes(raw)
    history.recover()
    assert not journal.exists() and history.store.path("note.md").read_bytes() == raw
    assert history.store.read("note.md")["content"] == raw.decode()
