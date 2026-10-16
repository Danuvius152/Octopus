"""Observed deletions survive observer release and index/ack failures."""

import gc
import json
import sqlite3
import uuid
import weakref

import pytest

from server.wiki.curation import WikiCuration
from server.wiki.store import WikiError, WikiStore


@pytest.fixture
def vault(tmp_path):
    root = tmp_path / "vault"
    (root / "Library").mkdir(parents=True)
    state = tmp_path / "state"
    vault_id = str(uuid.uuid4())
    stores = []

    def open_store():
        store = WikiStore(root, state, vault_id)
        assert store.events.mode == "inotify"
        stores.append(weakref.ref(store))
        return store

    yield root, open_store
    for reference in stores:
        store = reference()
        if store is not None:
            store.close()


def original_note(store):
    path = store.root / "Library/note.md"
    path.write_bytes(b"# Original\nSource judgment.\n")
    store.refresh()
    return path, store.read("Library/note.md")


def source_consumer(store, source):
    path = store.root / "Library/consumer.md"
    path.write_bytes(b"# Consumer\n")
    store.refresh()
    consumer = store.read("Library/consumer.md")
    WikiCuration(store).organize(
        consumer["path"], consumer["revision"], "A source-backed judgment.",
        [], [], [{"doc": source["id"], "revision": source["revision"], "location": "L1"}],
        "human", "test",
    )
    return store.read(consumer["id"])


def assert_retired(store, original):
    for identifier in (original["id"], original["metadata"]["uid"]):
        with pytest.raises(WikiError) as failure:
            store.read(identifier)
        assert failure.value.status == 404


@pytest.mark.parametrize("synchronize", ["synchronize", "refresh"])
@pytest.mark.parametrize("release", ["close", "gc"])
def test_rolled_back_deletion_survives_last_store_release(vault, monkeypatch, synchronize, release):
    root, open_store = vault
    store = open_store()
    path, original = original_note(store)
    consumer = source_consumer(store, original)
    consumer_raw = (root / consumer["path"]).read_bytes()
    replacement_raw = b"# Replacement\nA separate source judgment.\n"
    with path.open("rb"):
        old_inode = path.stat().st_ino
        path.unlink()
        path.write_bytes(replacement_raw)
        assert path.stat().st_ino != old_inode
        index = store._index

        def fail_after_index(*args, **kwargs):
            index(*args, **kwargs)
            raise OSError("synthetic failure after indexing replacement")

        with monkeypatch.context() as patch:
            patch.setattr(store, "_index", fail_after_index)
            with pytest.raises(WikiError) as failure:
                getattr(store, synchronize)()
            assert failure.value.status == 503
        # The failure rolled back both the retirement and replacement index.
        with store.connect() as db:
            assert db.execute("SELECT path FROM document_identities WHERE id=?", (original["id"],)).fetchone()[0] == original["path"]
        if release == "close":
            store.close()
        else:
            reference = weakref.ref(store)
            del index, failure, fail_after_index, patch
            del store
            gc.collect()
            assert reference() is None
        store = open_store()
        store.refresh()
        replacement = store.read(original["path"])
    assert replacement["id"] != original["id"]
    assert_retired(store, original)
    current_consumer = store.read(consumer["id"])
    assert current_consumer["stale"]
    assert any(ref["doc"] == original["id"] and ref["status"] == "missing" for ref in current_consumer["metadata"]["source_refs"])
    assert path.read_bytes() == replacement_raw
    assert (root / consumer["path"]).read_bytes() == consumer_raw


@pytest.mark.parametrize("recovery", ["synchronize", "reopen"])
def test_committed_replacement_is_not_retired_again_after_ack_failure(vault, monkeypatch, recovery):
    _root, open_store = vault
    store = open_store()
    path, original = original_note(store)
    replacement_raw = b"# Replacement\nAlready committed bytes.\n"
    with path.open("rb"):
        old_inode = path.stat().st_ino
        path.unlink()
        path.write_bytes(replacement_raw)
        assert path.stat().st_ino != old_inode

        def fail_acknowledgement():
            raise RuntimeError("synthetic fault after index commit, before memory acknowledgement")

        with monkeypatch.context() as patch:
            patch.setattr(store, "acknowledge_changes", fail_acknowledgement)
            with pytest.raises(RuntimeError, match="after index commit"):
                store.synchronize()
        with store.connect() as db:
            committed_id = db.execute("SELECT id FROM documents WHERE path=?", (original["path"],)).fetchone()[0]
            assert committed_id != original["id"]
        if recovery == "synchronize":
            store.synchronize()
            assert store.read(original["path"])["id"] == committed_id
        store.close()
        store = open_store()
        store.refresh()
    assert store.read(original["path"])["id"] == committed_id
    assert_retired(store, original)
    assert path.read_bytes() == replacement_raw


def test_observed_delete_survives_last_close_while_another_move_is_pending(vault, tmp_path, monkeypatch):
    root, open_store = vault
    store = open_store()
    path, original = original_note(store)
    moved = root / "Library/moving.md"
    moved.write_bytes(b"# Moving\n")
    store.refresh()
    replacement_raw = b"# Replacement\nNot part of the unmatched move.\n"
    monkeypatch.setattr("server.wiki.events.time.monotonic", lambda: 10)
    with path.open("rb"):
        old_inode = path.stat().st_ino
        path.unlink()
        path.write_bytes(replacement_raw)
        assert path.stat().st_ino != old_inode
        moved.rename(tmp_path / "outside.md")
        with pytest.raises(WikiError) as failure:
            store.synchronize()
        assert failure.value.status == 503
        store.close()
        # A half move from a closed FD cannot be paired with a new observer.
        # Keep every received record and deny access rather than scan terminal
        # bytes under old identities or guess that the move was a deletion.
        for _attempt in range(2):
            with pytest.raises(WikiError, match="uncertain") as failure:
                open_store()
            assert failure.value.status == 503
        with sqlite3.connect(store.db_path) as db:
            payloads = [json.loads(row[0]) for row in db.execute("SELECT payload FROM filesystem_inbox WHERE status='pending'")]
            records = [record for payload in payloads for record in payload["records"]]
            assert any(record.get("path") == original["path"] and record.get("mask", 0) & 0x200 for record in records)
            assert any(record.get("cookie") and record.get("mask", 0) & 0x40 for record in records)
            assert db.execute("SELECT uncertain FROM filesystem_state WHERE singleton=1").fetchone()[0]
    assert path.read_bytes() == replacement_raw
    assert (tmp_path / "outside.md").read_bytes() == b"# Moving\n"


def test_unfinished_reception_is_never_reported_ready(vault):
    _root, open_store = vault
    store = open_store()
    path, original = original_note(store)
    raw = path.read_bytes()
    store.events.journal.arm(store.events.epoch)
    status = store.index_state()
    assert status["index_status"] == "unavailable"
    assert status["pending_changes"] > 0
    assert any("reception" in error["error"] for error in status["index_errors"])
    with pytest.raises(WikiError) as failure:
        store.read(original["id"])
    assert failure.value.status == 503
    assert path.read_bytes() == raw
