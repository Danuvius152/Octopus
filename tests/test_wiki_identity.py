"""Deleted source IDs cannot authorize replacement files; explicit restore can undo deletion."""

import uuid

import pytest

from server.wiki.curation import WikiCuration
from server.wiki.history import WikiHistory
from server.wiki.jobs import WikiJobs
from server.wiki.registry import registry
from server.wiki.store import WikiError, WikiStore


@pytest.fixture
def history(tmp_path):
    root = tmp_path / "vault"
    root.mkdir()
    return WikiHistory(WikiStore(root, tmp_path / "state", str(uuid.uuid4())))


def owned_note(history):
    created = history.write("note.md", b"# Note\nOriginal judgment.\n", None, "human", "create")
    organized = WikiCuration(history.store).organize(
        "note.md", created["revision"], "Only the original judgment is established.",
        ["identity"], [], [], "human", "organize", aliases=["Identity note"],
    )
    return organized, history.store.read("note.md")


def assert_deleted(history, identifier):
    with history.store.connect() as db:
        assert db.execute("SELECT path FROM document_identities WHERE id=?", (identifier,)).fetchone()[0] is None
        assert db.execute("SELECT 1 FROM managed_notes WHERE path='note.md'").fetchone() is None
    with pytest.raises(WikiError) as error:
        history.store.read(identifier)
    assert error.value.status == 404


def test_observed_direct_deletion_replacement_does_not_inherit_identity_or_ownership(history):
    _, original = owned_note(history)
    raw = history.store.path("note.md").read_bytes()
    history.store.path("note.md").unlink()
    history.store.refresh()
    assert_deleted(history, original["id"])
    history.store.path("note.md").write_bytes(raw)
    replacement = history.store.read("note.md")
    assert replacement["id"] != original["id"]
    assert replacement["revision"] == original["revision"]
    assert replacement["metadata"].get("author") != "Weber 整理"


def test_missing_durable_identity_detaches_when_derived_rows_were_removed(history):
    _, original = owned_note(history)
    with history.store.connect() as db:
        db.execute("DELETE FROM documents")
    history.store.path("note.md").unlink()
    history.store.refresh()
    assert_deleted(history, original["id"])


def test_deleted_id_does_not_fall_back_to_a_file_named_with_that_id(history):
    original = history.write("note.md", b"source", None, "human", "create")
    identifier = history.store.read("note.md")["id"]
    history.write("note.md", None, original["revision"], "human", "delete")
    history.store.path(identifier).write_bytes(b"a different source named after the ID")
    history.store.refresh()  # extensionless file is intentionally not discovered
    with pytest.raises(WikiError) as error:
        history.store.read(identifier)
    assert error.value.status == 404


def test_direct_rename_preserves_current_identity_and_owned_sections(history):
    _, original = owned_note(history)
    history.store.path("note.md").rename(history.store.path("renamed.md"))
    history.store.refresh()
    moved = history.store.read(original["id"])
    assert moved["path"] == "renamed.md"
    assert moved["revision"] == original["revision"]
    assert moved["metadata"]["author"] == "Weber 整理"


def test_history_delete_and_explicit_restore_preserve_the_restored_identity(history):
    organized, original = owned_note(history)
    removed = history.write("note.md", None, original["revision"], "human", "delete")
    assert_deleted(history, original["id"])
    restored = history.restore(removed["change_id"], None, "before", "human")
    current = history.store.read("note.md")
    assert current["id"] == original["id"]
    assert current["revision"] == original["revision"]
    assert current["metadata"]["author"] == "Weber 整理"
    assert restored["change_id"] not in {organized["change_id"], removed["change_id"]}
    with history.store.connect() as db:
        assert db.execute("SELECT document_id FROM change_retention WHERE change_id=?", (restored["change_id"],)).fetchone()[0] == original["id"]


def test_restoring_an_old_path_cannot_move_an_active_identity_back_from_its_new_path(history):
    organized, original = owned_note(history)
    history.store.path("note.md").rename(history.store.path("renamed.md"))
    history.store.refresh()
    history.restore(organized["change_id"], None, "after", "human")
    assert history.store.read(original["id"])["path"] == "renamed.md"
    assert history.store.read("note.md")["id"] != original["id"]
    assert history.store.read("renamed.md")["revision"] == original["revision"]


def test_recovered_delete_detaches_before_a_same_hash_creation_receipt_can_be_reused(history, monkeypatch):
    (history.store.root / "Library").mkdir()
    raw = b"# Note\nexact material\n"
    created = history.write("Library/note.md", raw, None, "human", "create")
    original = history.store.read("Library/note.md")
    history.record("Library/note.md", raw, None, "human", "delete", "prepared")
    history.store.path("Library/note.md").unlink()
    with history.locked():
        pass
    history.store.path("Library/note.md").write_bytes(raw)
    assert history.store.read("Library/note.md")["id"] != original["id"]
    # No model is called: the old creation receipt fails at the enqueue gate.
    monkeypatch.setattr(registry, "store", lambda _user_id: history.store)
    monkeypatch.setattr(registry, "state", lambda _user_id: history.state)
    monkeypatch.setattr(registry, "config", lambda _user_id: {
        "root": str(history.store.root), "vault_id": history.store.vault_id,
        "curator_agent_id": "curator", "app_id": None,
    })
    jobs = WikiJobs(None)
    with pytest.raises(WikiError, match="different document identity"):
        jobs.maintain("old-receipt", "", "organize", ["Library/note.md"], None, [], "human", {"Library/note.md": created["change_id"]})
