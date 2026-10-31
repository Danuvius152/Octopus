"""Registration transactions and Store ownership through real instances."""

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from server.config import settings
from server.service_health import ServiceHealth
from server.wiki.registry import WikiRegistry
from server.wiki.store import WikiError


@pytest.fixture
def registries(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "wiki_dir", str(tmp_path / "state"))
    instances = [WikiRegistry(), WikiRegistry()]
    health = ServiceHealth()
    for registry in instances:
        registry.bind_health(health)
    roots = [tmp_path / "a", tmp_path / "b"]
    for root in roots:
        root.mkdir()
        (root / "note.md").write_text("# Source\noriginalword\n")
    try:
        yield instances, roots
    finally:
        for registry in instances:
            registry.close()


def test_independent_registry_first_registration_cannot_overwrite_another_root(registries):
    instances, roots = registries
    barrier = threading.Barrier(2)

    def configure(index):
        barrier.wait(timeout=2)
        try:
            return instances[index].configure(None, roots[index], "curator")
        except WikiError as error:
            assert error.status == 409
            return None

    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(configure, [0, 1]))
    assert sum(result is not None for result in results) == 1
    winner = next(result for result in results if result is not None)
    assert instances[0].config(None) == instances[1].config(None) == winner
    repeat = instances[0].configure(None, Path(winner["root"]), "curator")
    assert repeat["vault_id"] == winner["vault_id"]


@pytest.mark.parametrize("raw", ["invalid-json", "[]", '{"root":null}', '{"version":999}'])
def test_bad_registration_is_diagnostic_and_retains_its_original_bytes(registries, raw):
    instances, roots = registries
    state = instances[0].state(None)
    state.mkdir()
    config = state / "config.json"
    config.write_text(raw)
    with pytest.raises(WikiError, match="configuration retained") as error:
        instances[0].configure(None, roots[0], "curator")
    assert error.value.status == 503
    assert config.read_text() == raw


def test_cross_thread_operation_keeps_observer_until_both_lease_and_operation_end(registries):
    instances, roots = registries
    registry = instances[0]
    registry.configure(None, roots[0], "curator")
    store = registry.store(None)
    borrow = store.borrow()
    lease = borrow.__enter__()
    entered, release = threading.Event(), threading.Event()

    def query():
        with store.operation(lease):
            entered.set()
            assert release.wait(3)
            return store.search("originalword")

    with ThreadPoolExecutor(2) as pool:
        running = pool.submit(query)
        try:
            assert entered.wait(2)
            store.retire()
            borrow.__exit__(None, None, None)
            assert store._users == 1
            closing = pool.submit(store.close)
            assert not closing.done()
        finally:
            release.set()
        assert running.result(timeout=2)["items"]
        closing.result(timeout=2)
    assert store._users == 0
    with pytest.raises(WikiError, match="retired"):
        store.search("originalword")


def test_captured_binding_keeps_old_observer_until_exit_and_rejects_rebinding(registries):
    instances, roots = registries
    registry = instances[0]
    registry.configure(None, roots[0], "curator")
    with registry.using(None) as (store, registration):
        rebound = registry.configure(None, roots[0], "replacement")
        assert rebound["registration_generation"] != registration["registration_generation"]
        assert not store._events_released
        with pytest.raises(WikiError, match="registration changed"):
            with registry.registered(None, store, registration):
                pytest.fail("An old binding must not authorize new work")
        assert registry.store(None) is not store
    assert store._events_released


async def test_registry_cancelled_stop_finishes_thread_then_closes_observer(registries, monkeypatch):
    instances, roots = registries
    registry = instances[0]
    registry.configure(None, roots[0], "curator")
    await registry.start()
    store = registry.store(None)
    entered, release = threading.Event(), threading.Event()
    original = store.synchronize

    def synchronize():
        entered.set()
        assert release.wait(3)
        original()

    monkeypatch.setattr(store, "synchronize", synchronize)
    assert await asyncio.to_thread(entered.wait, 2)
    stopping = asyncio.create_task(registry.stop())
    await asyncio.wait_for(registry._stopping.wait(), 2)
    stopping.cancel("stop cancelled")
    await asyncio.sleep(0)
    stopping.cancel("second cancellation")
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await stopping
    assert registry._task is None
    with pytest.raises(WikiError, match="retired"):
        store.search("originalword")
