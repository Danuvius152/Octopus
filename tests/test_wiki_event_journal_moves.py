"""Trusted copy/unlink journal moves retain identity without hiding deletion."""

import json
import uuid

import pytest

from server.wiki.history import WikiHistory, atomic
from server.wiki.store import WikiError
from tests import test_wiki_event_identity as event_identity

store = event_identity.store


def external_journal(history, paths):
    journal = history.state / "external-change.json"
    with history.locked():
        entry = {
            "version": 2,
            "id": uuid.uuid4().hex,
            "source_binding_protocol": 1,
            "before": history.capture_external(paths),
            "paths": paths,
            "actor": "app:sapphire",
            "reason": "rename",
        }
        atomic(journal, json.dumps(entry).encode())
    return journal


def test_verified_copy_unlink_move_preserves_only_its_source_identity(store):
    source = event_identity.note(store, "Library/a.md")
    unrelated = event_identity.note(store, "Library/unrelated.md", b"# Unrelated\n")
    history = WikiHistory(store)
    journal = external_journal(history, [source["path"], "Library/b.md"])
    source_path = store.path(source["path"])
    target = store.root / "Library/b.md"
    target.write_bytes(source_path.read_bytes())
    source_path.unlink()
    unrelated_path = store.path(unrelated["path"])
    with unrelated_path.open("rb"):
        unrelated_path.unlink()
        unrelated_path.write_bytes(b"# Replacement of the unrelated note\n")
        history.recover()
    assert not journal.exists()
    assert store.read(source["id"])["path"] == "Library/b.md"
    assert store.read(unrelated["path"])["id"] != unrelated["id"]
    event_identity.retired(store, unrelated)


@pytest.mark.parametrize("ambiguity", ["source_recreated", "target_recreated", "target_moved_out"])
def test_journal_cannot_assign_old_identity_to_an_ambiguous_same_hash_copy(store, tmp_path, ambiguity):
    source = event_identity.note(store, "Library/a.md")
    history = WikiHistory(store)
    journal = external_journal(history, [source["path"], "Library/b.md"])
    path = store.path(source["path"])
    target = store.root / "Library/b.md"
    raw = path.read_bytes()
    with path.open("rb"):
        if ambiguity == "source_recreated":
            path.unlink()
            path.write_bytes(raw)
        target.write_bytes(raw)
        if ambiguity == "target_recreated":
            target.unlink()
            target.write_bytes(raw)
        elif ambiguity == "target_moved_out":
            target.rename(tmp_path / "outside.md")
            target.write_bytes(raw)
        path.unlink()
        with pytest.raises(WikiError, match="Ambiguous external copy/unlink move") as failure:
            history.recover()
        assert failure.value.status == 409
    assert journal.exists()
    assert target.read_bytes() == raw
    with store.connect() as db:
        assert db.execute("SELECT path FROM document_identities WHERE id=?", (source["id"],)).fetchone()[0] != "Library/b.md"


def test_matched_kernel_rename_does_not_resurrect_a_later_target_replacement(store):
    source = event_identity.note(store, "Library/a.md")
    history = WikiHistory(store)
    journal = external_journal(history, [source["path"], "Library/b.md"])
    path = store.path(source["path"])
    raw = path.read_bytes()
    target = store.root / "Library/b.md"
    with path.open("rb"):
        path.rename(target)
        target.unlink()
        target.write_bytes(raw)
        with pytest.raises(WikiError, match="External move identity changed"):
            history.recover()
    assert journal.exists()
    assert target.read_bytes() == raw
    with store.connect() as db:
        assert db.execute("SELECT path FROM document_identities WHERE id=?", (source["id"],)).fetchone()[0] is None
