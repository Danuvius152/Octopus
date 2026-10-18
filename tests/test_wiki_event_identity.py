"""Observed filesystem lifecycles cannot transfer a deleted note's grants."""

import os
import struct
import subprocess
import sys
import uuid
from collections import OrderedDict
from types import SimpleNamespace

import pytest

from server.config import settings
from server.mcp_identity import mint, verify
from server.wiki import origins
from server.wiki.curation import WikiCuration
from server.wiki.events import FileChanges, FileEvent, VaultEvents
from server.wiki.history import WikiHistory
from server.wiki.jobs import WikiJobs
from server.wiki.registry import registry
from server.wiki.store import WikiError, WikiStore
from server.wiki.tasks import tasks


@pytest.fixture
def store(tmp_path):
    root = tmp_path / "vault"
    (root / "Library").mkdir(parents=True)
    store = WikiStore(root, tmp_path / "state", str(uuid.uuid4()))
    assert store.events.mode == "inotify"
    yield store
    store.close()


def note(store, path="Library/note.md", raw=b"# Note\nOriginal judgment.\n"):
    file = store.root / path
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_bytes(raw)
    store.refresh()  # Installs watches on new subdirectories as well.
    return store.read(path)


def retired(store, original):
    for identifier in (original["id"], original["metadata"]["uid"]):
        with pytest.raises(WikiError) as failure:
            store.read(identifier)
        assert failure.value.status == 404
    with store.connect() as db:
        identity = db.execute("SELECT path,file_key FROM document_identities WHERE id=?", (original["id"],)).fetchone()
        assert tuple(identity) == (None, "")


@pytest.mark.parametrize("synchronize", ["synchronize", "refresh"])
@pytest.mark.parametrize("same_bytes", [False, True])
def test_unlink_recreate_retires_identity_before_indexing(store, synchronize, same_bytes):
    original = note(store)
    path = store.path(original["path"])
    raw = path.read_bytes()
    with path.open("rb"):
        old_inode = path.stat().st_ino
        path.unlink()
        path.write_bytes(raw if same_bytes else b"# Replacement\nA different judgment.\n")
        assert path.stat().st_ino != old_inode
        getattr(store, synchronize)()
    replacement = store.read(original["path"])
    assert replacement["id"] != original["id"]
    assert (replacement["revision"] == original["revision"]) == same_bytes
    retired(store, original)


@pytest.fixture
def assigned_store(tmp_path, monkeypatch):
    root = tmp_path / "vault"
    (root / "Library").mkdir(parents=True)
    monkeypatch.setattr(settings, "wiki_dir", str(tmp_path / "state"))
    monkeypatch.setattr(settings, "users_root", str(tmp_path / "users"))
    monkeypatch.setattr(registry, "_stores", OrderedDict())
    monkeypatch.setattr(registry, "_retired_stores", {})
    config = registry.configure(None, root, "synthetic-curator")
    store = registry.store(None)
    original = note(store)
    jobs = WikiJobs(None)
    job = jobs.maintain("event-replacement", "", "organize", [original["path"]], None, [], "human")
    session = SimpleNamespace(id="synthetic-worker", user_id=None, agent_id=config["curator_agent_id"])
    jobs.update(job["id"], status="running", session_id=session.id)
    tasks.assign(session.id, job["id"], job["items"])
    token = tasks.open(session, "maintain")
    task = tasks.call(tasks.active[token], verify(mint(session.id, wiki_task_id=token)))
    tasks.record_read(task, original)
    yield store, original, task
    tasks.close(token)
    tasks.unassign(session.id)
    registry.close()


def test_same_bytes_replacement_rejects_old_task_full_read_and_final_write(assigned_store):
    store, original, task = assigned_store
    path = store.path(original["path"])
    raw = path.read_bytes()
    with path.open("rb"):
        path.unlink()
        path.write_bytes(raw)
        with pytest.raises(WikiError, match="identity was deleted or replaced") as failure:
            tasks.require_path(task, store, original["path"])
        assert failure.value.status == 403
        with pytest.raises(WikiError, match="full current note") as failure:
            tasks.require_read(task, store, original["path"], original["revision"])
        assert failure.value.status == 422
        with pytest.raises(WikiError) as failure:
            WikiCuration(store).organize(original["path"], original["revision"], "An obsolete assignment.", [], [], [], "curator", "test", task=task)
        assert failure.value.status == 403
    assert path.read_bytes() == raw
    retired(store, original)


@pytest.mark.parametrize("directory", [False, True])
@pytest.mark.parametrize("delete_first", [False, True])
def test_move_and_delete_order_controls_which_binding_survives(store, directory, delete_first):
    source_path = "Library/a/note.md" if directory else "Library/a.md"
    target_path = "Library/b/note.md" if directory else "Library/b.md"
    source = note(store, source_path)
    target = note(store, target_path) if delete_first else None
    old = store.root / ("Library/a" if directory else source_path)
    new = store.root / ("Library/b" if directory else target_path)
    if delete_first:
        store.path(target_path).unlink()
        if directory:
            new.rmdir()
        old.rename(new)
    else:
        old.rename(new)
        store.path(target_path).unlink()
        if directory:
            new.rmdir()
            new.mkdir()
        store.path(target_path).write_bytes(b"# Replacement\n")
    store.synchronize()
    current = store.read(target_path)
    if delete_first:
        assert current["id"] == source["id"]
        retired(store, target)
    else:
        assert current["id"] != source["id"]
        retired(store, source)


@pytest.mark.parametrize("temporary", ["save.tmp", ".hidden-save.md"])
def test_atomic_save_preserves_identity_and_owned_sections(store, temporary):
    original = note(store)
    WikiCuration(store).organize(original["path"], original["revision"], "Original judgment only.", ["owned"], [], [], "human", "test")
    original = store.read(original["path"])
    path = store.path(original["path"])
    raw = path.read_bytes() + b"An atomic edit outside managed sections.\n"
    temp = path.parent / temporary
    temp.write_bytes(raw)
    os.replace(temp, path)
    store.synchronize()
    current = store.read(original["id"])
    assert current["id"] == original["id"]
    assert "owned" in current["metadata"]["tags"]
    assert path.read_bytes() == raw


def test_directory_delete_recreate_retires_children_without_neighbor_prefix(store):
    original = note(store, "Library/a/note.md")
    neighbor = note(store, "Library/ab/note.md")
    path = store.path(original["path"])
    path.unlink()
    path.parent.rmdir()
    path.parent.mkdir()
    path.write_bytes(b"# Replacement\n")
    store.refresh()
    assert store.read(original["path"])["id"] != original["id"]
    assert store.read(neighbor["id"])["path"] == neighbor["path"]
    retired(store, original)


@pytest.mark.parametrize("intermediate", ["Library/b.md", "Library/.temporary.md"])
def test_matched_rename_chain_preserves_identity_without_indexing_temporary_paths(store, intermediate):
    original = note(store, "Library/a.md")
    middle = store.root / intermediate
    store.path(original["path"]).rename(middle)
    middle.rename(store.root / "Library/c.md")
    store.refresh()
    assert store.read(original["id"])["path"] == "Library/c.md"
    with store.connect() as db:
        assert [row[0] for row in db.execute("SELECT path FROM documents")] == ["Library/c.md"]


def test_later_symlink_at_intermediate_move_path_cannot_retire_final_binding(store, tmp_path):
    original = note(store, "Library/a.md")
    middle = store.root / "Library/b.md"
    store.path(original["path"]).rename(middle)
    middle.rename(store.root / "Library/c.md")
    middle.symlink_to(tmp_path / "outside.md")
    store.synchronize()
    assert store.read(original["id"])["path"] == "Library/c.md"
    with store.connect() as db:
        assert db.execute("SELECT 1 FROM documents WHERE path='Library/b.md'").fetchone() is None


@pytest.mark.parametrize("finish_chain", [False, True])
def test_unsupported_external_name_cannot_block_the_vault(store, finish_chain):
    original = note(store, "Library/a.md")
    other = note(store, "Library/unrelated.md")
    unsupported = store.root / "Library/bad\\name.md"
    store.path(original["path"]).rename(unsupported)
    if finish_chain:
        unsupported.rename(store.root / "Library/c.md")
    store.refresh()
    assert store.read(other["id"])["path"] == other["path"]
    if finish_chain:
        assert store.read(original["id"])["path"] == "Library/c.md"
    else:
        retired(store, original)


def test_directory_move_out_releases_watches_before_new_directory_binding(store, tmp_path, monkeypatch):
    original = note(store, "Library/a/note.md")
    outside = tmp_path / "outside-a"
    (store.root / "Library/a").rename(outside)
    (store.root / "Library/a").mkdir()
    store.path(original["path"]).write_bytes(b"# Replacement\n")
    monkeypatch.setattr("server.wiki.events.time.monotonic", lambda: 10)
    with pytest.raises(WikiError) as failure:
        store.synchronize()
    assert failure.value.status == 503
    monkeypatch.setattr("server.wiki.events.time.monotonic", lambda: 12)
    store.synchronize()
    replacement = store.read(original["path"])
    assert replacement["id"] != original["id"]
    (outside / "note.md").unlink()
    store.synchronize()
    assert store.read(replacement["id"])["path"] == original["path"]


@pytest.mark.parametrize("synchronize", ["synchronize", "refresh"])
def test_observed_root_move_ends_old_generation_and_watches_the_new_root(store, tmp_path, synchronize):
    original = note(store)
    store.root.rename(tmp_path / "outside-root")
    store.root.mkdir()
    (store.root / "Library").mkdir()
    path = store.root / original["path"]
    path.write_bytes(b"# Replacement\n")
    getattr(store, synchronize)()
    replacement = store.read(original["path"])
    assert replacement["id"] != original["id"]
    retired(store, original)
    with path.open("rb"):
        path.unlink()
        path.write_bytes(b"# Another replacement\n")
        store.synchronize()
    assert store.read(original["path"])["id"] != replacement["id"]


def test_overlapping_store_leases_share_events_and_do_not_retire_successor_twice(store):
    original = note(store)
    with store.borrow() as lease:
        replacement_store = WikiStore(store.root, store.state, store.vault_id)
        try:
            assert replacement_store.events is store.events
            store.retire()
            path = store.root / original["path"]
            with path.open("rb"):
                path.unlink()
                path.write_bytes(b"# Replacement\n")
                replacement = replacement_store.read(original["path"])
                with store.operation(lease):
                    assert store.read(original["path"])["id"] == replacement["id"]
            retired(replacement_store, original)
        finally:
            replacement_store.close()


def test_uncommitted_events_are_shared_with_replacement_store(store, monkeypatch):
    original = note(store)
    replacement_store = WikiStore(store.root, store.state, store.vault_id)
    try:
        path = store.path(original["path"])
        with path.open("rb"):
            path.unlink()
            path.write_bytes(b"# Replacement\n")
            with monkeypatch.context() as patch:
                patch.setattr(store, "_index", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("synthetic failed transaction")))
                with pytest.raises(WikiError):
                    store.synchronize()
            store.close()
            replacement = replacement_store.read(original["path"])
            assert replacement["id"] != original["id"]
            retired(replacement_store, original)
    finally:
        replacement_store.close()


def test_another_process_cannot_replay_events_into_an_owned_index(store):
    # The service's replacement Stores share an observer. A separate process
    # must fail before initializing or mutating the same index independently.
    code = """import sys
from pathlib import Path
from server.wiki.store import WikiError, WikiStore
try:
    store = WikiStore(Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3])
except WikiError as error:
    assert error.status == 503 and 'observer unavailable' in str(error)
    print('observer ownership refused')
else:
    store.close()
    raise AssertionError('independent observer acquired an owned index')
"""
    result = subprocess.run([sys.executable, "-c", code, str(store.root), str(store.state), store.vault_id], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "observer ownership refused"


def test_directory_rename_preserves_all_children_and_ownership(store):
    first = note(store, "Library/a/first.md")
    second = note(store, "Library/a/sub/second.md")
    WikiCuration(store).organize(first["path"], first["revision"], "Owned summary.", ["owned"], [], [], "human", "test")
    (store.root / "Library/a").rename(store.root / "Library/b")
    store.synchronize()
    assert store.read(first["id"])["path"] == "Library/b/first.md"
    assert "owned" in store.read(first["id"])["metadata"]["tags"]
    assert store.read(second["id"])["path"] == "Library/b/sub/second.md"


@pytest.mark.parametrize("synchronize", ["synchronize", "refresh"])
def test_transaction_failure_replays_consumed_lifecycle(store, monkeypatch, synchronize):
    original = note(store)
    path = store.path(original["path"])
    with path.open("rb"):
        path.unlink()
        path.write_bytes(b"# Replacement\n")
        index = store._index

        def failed_index(*args, **kwargs):
            index(*args, **kwargs)
            raise OSError("synthetic failure after indexing")

        with monkeypatch.context() as patch:
            patch.setattr(store, "_index", failed_index)
            with pytest.raises(WikiError) as failure:
                getattr(store, synchronize)()
            assert failure.value.status == 503
        with store.connect() as db:
            assert db.execute("SELECT path FROM document_identities WHERE id=?", (original["id"],)).fetchone()[0] == original["path"]
        getattr(store, synchronize)()
    assert store.read(original["path"])["id"] != original["id"]
    retired(store, original)


@pytest.mark.parametrize("restore", [False, True])
def test_journalled_delete_notifications_cannot_retire_new_or_restored_identity(store, restore):
    history = WikiHistory(store)
    created = history.write("Library/note.md", b"# Original\n", None, "human", "create")
    original = store.read(created["path"])
    removed = history.write(original["path"], None, original["revision"], "human", "delete")
    if restore:
        history.restore(removed["change_id"], None, "before", "human")
    else:
        history.write(original["path"], b"# Replacement\n", None, "human", "create")
    current = store.read(original["path"])
    store.refresh()
    assert store.read(current["id"])["path"] == original["path"]
    assert (current["id"] == original["id"]) == restore


def test_retirement_invalidates_origins_ownership_and_source_edges(store):
    original = note(store)
    WikiCuration(store).organize(original["path"], original["revision"], "An owned summary.", ["owned"], [], [], "human", "test")
    original = store.read(original["path"])
    consumer = note(store, "Library/consumer.md", b"# Consumer\n")
    WikiCuration(store).organize(consumer["path"], consumer["revision"], "A source-backed summary.", [], [], [{"doc": original["id"], "revision": original["revision"], "location": "L1"}], "human", "test")
    with store.connect() as db:
        origins.put(db, original["id"], {"doc_id": original["id"], "revision": original["revision"], "status": "valid"})
    path = store.path(original["path"])
    raw = path.read_bytes()
    with path.open("rb"):
        path.unlink()
        path.write_bytes(raw)
        store.synchronize()
    replacement = store.read(original["path"])
    assert replacement["id"] != original["id"]
    assert replacement["metadata"].get("author") != "Weber 整理"
    with store.connect() as db:
        assert db.execute("SELECT 1 FROM managed_notes WHERE path=?", (original["path"],)).fetchone() is None
        assert origins.get(db, original["id"])["status"] == "invalid"
        assert origins.get(db, replacement["id"]) is None
        dependencies = db.execute("SELECT target_doc,status FROM wiki_edges WHERE source_doc=?", (consumer["id"],)).fetchall()
        assert (original["id"], "missing") in [tuple(row) for row in dependencies]
    current_consumer = store.read(consumer["id"])
    assert current_consumer["stale"]
    assert any(ref["status"] == "missing" for ref in current_consumer["metadata"]["source_refs"])
    retired(store, original)


def test_ordered_lifecycle_is_replayed_without_terminal_move_destination(store, monkeypatch):
    original = note(store, "Library/a.md")
    (store.root / original["path"]).unlink()
    (store.root / "Library/b.md").write_bytes(b"# Replacement\n")
    store.events.drain()  # The control below supplies the observed sequence.
    batches = iter([FileChanges(paths={"Library/a.md", "Library/b.md"}, lifecycle=[
        FileEvent("move", "Library/a.md", "Library/b.md"),
        FileEvent("delete", "Library/b.md"),
        FileEvent("delete", "Library/b.md"),  # Repeated deletion is idempotent before indexing.
    ]), FileChanges(), FileChanges()])
    monkeypatch.setattr(store.events, "drain", lambda: next(batches, FileChanges()))
    store.synchronize()
    assert store.read("Library/b.md")["id"] != original["id"]
    retired(store, original)


def test_split_move_waits_for_cookie_before_later_delete_and_preserves_order(tmp_path, monkeypatch):
    root = tmp_path / "vault"
    root.mkdir()
    observer = VaultEvents(root)
    try:
        descriptor = next(key for key, path in observer._directories.items() if path == root)

        def event(mask, cookie, name):
            name = name.encode() + b"\0"
            return struct.pack("iIII", descriptor, mask, cookie, len(name)) + name

        pending = [event(0x40, 17, "a.md")]

        def read(_fd, _length):
            if pending:
                return pending.pop(0)
            raise BlockingIOError

        monkeypatch.setattr(os, "read", read)
        assert observer.drain().pending_moves
        pending.append(event(0x80, 17, "b.md") + event(0x200, 0, "b.md"))
        changed = observer.drain()
        assert not changed.pending_moves
        assert [(item.kind, item.path, item.destination) for item in changed.lifecycle] == [
            ("move", "a.md", "b.md"), ("delete", "b.md", ""),
        ]
        assert not observer.drain().lifecycle
    finally:
        observer.close()


def test_unmatched_move_expires_as_deletion_and_retains_following_events(tmp_path, monkeypatch):
    root = tmp_path / "vault"
    root.mkdir()
    observer = VaultEvents(root)
    try:
        descriptor = next(iter(observer._directories))
        raw = struct.pack("iIII", descriptor, 0x40, 18, 5) + b"a.md\0"
        pending = [raw]

        def read(_fd, _length):
            if pending:
                return pending.pop(0)
            raise BlockingIOError

        monkeypatch.setattr(os, "read", read)
        monkeypatch.setattr("server.wiki.events.time.monotonic", lambda: 10)
        assert observer.drain().pending_moves
        monkeypatch.setattr("server.wiki.events.time.monotonic", lambda: 12)
        changed = observer.drain()
        assert [(item.kind, item.path) for item in changed.lifecycle] == [("delete", "a.md")]
    finally:
        observer.close()


def test_split_hidden_intermediate_move_uses_prior_virtual_binding(tmp_path, monkeypatch):
    root = tmp_path / "vault"
    root.mkdir()
    observer = VaultEvents(root)
    try:
        descriptor = next(iter(observer._directories))

        def event(mask, cookie, name):
            name = name.encode() + b"\0"
            return struct.pack("iIII", descriptor, mask, cookie, len(name)) + name

        pending = [event(0x40, 19, "a.md") + event(0x80, 19, ".save.md") + event(0x40, 20, ".save.md")]

        def read(_fd, _length):
            if pending:
                return pending.pop(0)
            raise BlockingIOError

        monkeypatch.setattr(os, "read", read)
        assert observer.drain().pending_moves
        observer.settle_inactive({"a.md"})
        assert observer.drain().pending_moves
        pending.append(event(0x80, 20, "c.md"))
        changed = observer.drain()
        assert [(item.kind, item.path, item.destination) for item in changed.lifecycle] == [
            ("move", "a.md", ".save.md"), ("move", ".save.md", "c.md"),
        ]
    finally:
        observer.close()
