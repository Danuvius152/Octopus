"""Live reads must not apply filesystem events under a shared vault lock."""

import fcntl
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from server.wiki.history import WikiHistory
from tests.test_wiki_event_identity import note, retired
from tests.test_wiki_event_identity import store as store


def require_exclusive(history):
    with (history.state / "vault.lock").open("a+b") as observer:
        with pytest.raises(BlockingIOError):
            fcntl.flock(observer, fcntl.LOCK_SH | fcntl.LOCK_NB)


def test_shared_lock_cannot_silently_satisfy_exclusive_request(store):
    history = WikiHistory(store)
    with history.locked(shared=True):
        with pytest.raises(RuntimeError, match="exclusive"):
            with history.locked():
                pytest.fail("A shared lock authorized a writer")


def test_exclusive_shared_exclusive_reentry_keeps_exclusive_lock(store):
    history = WikiHistory(store)
    with history.locked(), history.locked(shared=True), history.locked():
        require_exclusive(history)


def test_transferred_exclusive_lock_allows_writer_reentry(store):
    history = WikiHistory(store)
    with (history.state / "vault.lock").open("a+b") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        with history.under_existing_lock(handle), history.locked(shared=True), history.locked():
            require_exclusive(history)


def test_shared_context_rejects_external_exclusive_transfer(store):
    history = WikiHistory(store)
    with history.locked(shared=True), (history.state / "vault.lock").open("a+b") as handle:
        with pytest.raises(RuntimeError, match="exclusive"):
            with history.under_existing_lock(handle):
                pytest.fail("A shared context accepted an exclusive transfer")


@pytest.mark.parametrize("method", ["read_snapshot", "graph"])
def test_live_read_indexing_owns_exclusive_lock(store, monkeypatch, method):
    original = note(store)
    index = store._index
    calls = []

    def indexed(*args, **kwargs):
        calls.append(True)
        require_exclusive(WikiHistory(store))
        return index(*args, **kwargs)

    monkeypatch.setattr(store, "_index", indexed)
    getattr(store, method)(original["id"])
    assert calls


def test_concurrent_graphs_do_not_acknowledge_unapplied_replacement(store, monkeypatch):
    a = note(store, "Library/a.md")
    b = note(store, "Library/b.md")
    first_read, first_lifecycle, release_first, first_finished = (threading.Event() for _ in range(4))
    second_queued = threading.Event()
    ready = threading.Barrier(2)
    local = threading.local()
    synchronize, read = store.synchronize, store.read
    lifecycle, pending = store.apply_lifecycle, store.pending_changes
    pending_receipts = []
    b_path = store.path(b["path"])

    def synchronized(*args, **kwargs):
        outer = not getattr(local, "synchronized", False)
        local.synchronized = True
        result = synchronize(*args, **kwargs)
        if outer:
            ready.wait(timeout=5)
            if local.role == "second":
                assert first_read.wait(5)
        return result

    def live_read(*args, **kwargs):
        if local.role == "first":
            store.path(a["path"]).write_bytes(b"# A changed\n")
            first_read.set()
        else:
            assert first_lifecycle.wait(5)
            b_path.unlink()
            b_path.write_bytes(b"# Replacement B\n")
        return read(*args, **kwargs)

    def applying(*args, **kwargs):
        if local.role == "first":
            first_lifecycle.set()
            assert release_first.wait(5)
        else:
            # The replacement batch must remain durable and unapplied until
            # this graph's own lifecycle transaction consumes it.
            with store.connect() as db:
                state = db.execute("SELECT received_seq,applied_seq FROM filesystem_state").fetchone()
                receipts = db.execute("SELECT seq,payload,status FROM filesystem_inbox WHERE seq>?", (state[1],)).fetchall()
                assert state[0] > state[1]
                assert receipts and all(row[1] and row[2] == "pending" for row in receipts)
                pending_receipts.extend(row[0] for row in receipts)
        return lifecycle(*args, **kwargs)

    def pending_changes(*args, **kwargs):
        result = pending(*args, **kwargs)
        if local.role == "second" and first_lifecycle.is_set():
            second_queued.set()
            assert first_finished.wait(5)
        return result

    def graph(role):
        local.role = role
        try:
            return store.graph(a["id"])
        finally:
            if role == "first":
                first_finished.set()

    with b_path.open("rb"), monkeypatch.context() as patch, ThreadPoolExecutor(max_workers=2) as pool:
        patch.setattr(store, "synchronize", synchronized)
        patch.setattr(store, "read", live_read)
        patch.setattr(store, "apply_lifecycle", applying)
        patch.setattr(store, "pending_changes", pending_changes)
        first, second = pool.submit(graph, "first"), pool.submit(graph, "second")
        try:
            assert first_lifecycle.wait(5)
            # In the broken implementation the second graph can merge its
            # batch into the first writer's queue. With EX it waits outside.
            second_queued.wait(0.2)
        finally:
            release_first.set()
        first.result(timeout=5)
        second.result(timeout=5)

    replacement = store.read(b["path"])
    assert replacement["id"] != b["id"]
    assert pending_receipts
    retired(store, b)
    with store.connect() as db:
        row = db.execute("SELECT received_seq,applied_seq FROM filesystem_state").fetchone()
        assert row[0] == row[1]
