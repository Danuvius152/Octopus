"""Confirmed retirement releases Registry ownership, including the last user."""

import gc
import threading
import weakref
from concurrent.futures import ThreadPoolExecutor

import pytest

from server.config import settings
from server.wiki.registry import WikiRegistry
from server.wiki.store import WikiError


@pytest.fixture
def registered(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "wiki_dir", str(tmp_path / "state"))
    root = tmp_path / "vault"
    root.mkdir()
    (root / "note.md").write_text("# Original\nStill available.\n")
    registry = WikiRegistry()
    registry.configure(None, root, "curator-0")
    try:
        yield registry, root
    finally:
        registry.close()


@pytest.mark.parametrize("last_user", ["none", "borrow", "operation"])
def test_replacement_reclaims_only_after_last_user_and_confirmed_release(registered, last_user):
    registry, root = registered
    old = registry.store(None)
    borrow = old.borrow() if last_user != "none" else None
    operation = None
    if borrow is not None:
        lease = borrow.__enter__()
        if last_user == "operation":
            operation = old.operation(lease)
            operation.__enter__()
            borrow.__exit__(None, None, None)
    try:
        registry.configure(None, root, "curator-1")
        current = registry.store(None)
        assert current is not old and current.events is old.events
        if last_user != "none":
            assert old in registry._retired_stores and not old._events_released
            assert old._users == 1
    finally:
        if operation is not None:
            operation.__exit__(None, None, None)
        elif borrow is not None:
            borrow.__exit__(None, None, None)
    assert old._events_released and old._users == 0
    assert old not in registry._retired_stores
    assert current in registry._stores.values()
    assert current.read("note.md")["content"] == "# Original\nStill available.\n"


def test_successive_replacements_do_not_keep_released_store_objects_alive(registered):
    registry, root = registered
    previous = []
    for index in range(1, 5):
        previous.append(weakref.ref(registry.store(None)))
        registry.configure(None, root, f"curator-{index}")
    gc.collect()
    assert all(reference() is None for reference in previous)
    assert not registry._retired_stores
    assert registry.store(None).read("note.md")["id"]


def test_immediate_cleanup_failure_remains_owned_and_blocks_reopening(registered, monkeypatch):
    registry, _ = registered
    old = registry.store(None)
    observer = old.events
    real_close = observer.close

    def failed_close():
        raise OSError("synthetic owned observer close failure")

    try:
        with monkeypatch.context() as fault:
            fault.setattr(observer, "close", failed_close)
            with pytest.raises(OSError, match="owned observer"):
                registry.block(None)
            assert old in registry._retired_stores and not old._events_released
            with pytest.raises(WikiError):
                registry.unblock(None)
            with pytest.raises(WikiError):
                registry.store(None)
    finally:
        # Dispose the injected resource explicitly; production errors stay sticky.
        real_close()
        observer._release_error = None
        registry.close()


def test_last_user_reclamation_does_not_hold_lifecycle_while_waiting_for_registry(registered, monkeypatch):
    registry, root = registered
    old = registry.store(None)
    entered, release, released = threading.Event(), threading.Event(), threading.Event()
    real_release = old.release_events

    def release_events():
        real_release()
        released.set()

    monkeypatch.setattr(old, "release_events", release_events)

    def hold():
        with old.borrow():
            entered.set()
            assert release.wait(5)

    with ThreadPoolExecutor(1) as pool:
        using = pool.submit(hold)
        try:
            assert entered.wait(2)
            registry.configure(None, root, "curator-next")
            with registry._lock:
                release.set()
                assert released.wait(2)
                acquired = old._lifecycle.acquire(timeout=2)
                try:
                    assert acquired, "Retirement callback acquired Registry while still holding Store lifecycle"
                finally:
                    if acquired:
                        old._lifecycle.release()
            using.result(timeout=2)
            assert old not in registry._retired_stores
        finally:
            release.set()
