"""Registration and drain faults fail closed without losing owned resources."""

import asyncio
import importlib
import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from server import deps
from server.config import settings
from server.database import Database
from server.main import app
from server.routers import wiki as wiki_router
from server.users import UserManager
from server.wiki.registry import WikiRegistry
from server.wiki.store import WikiError
from tests.test_service_runtime import installation_settings
from tests.test_wiki_storage import registration_process

registry_module = importlib.import_module("server.wiki.registry")


@pytest.fixture
def registered(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "wiki_dir", str(tmp_path / "state"))
    root = tmp_path / "vault"
    root.mkdir()
    (root / "note.md").write_text("# Source\nPreserved words.\n")
    registry = WikiRegistry()
    config = registry.configure(None, root, "curator-a")
    try:
        yield registry, root, config
    finally:
        registry.close()


@pytest.mark.parametrize("last_user", ["borrow", "operation"])
@pytest.mark.parametrize("drain", ["block", "close"])
def test_last_user_cleanup_failure_wakes_drain_and_retains_resource(
    registered, monkeypatch, request, last_user, drain
):
    registry, _, _ = registered
    store = registry.store(None)
    events = store.events
    real_close = events.close
    real_wait = store._lifecycle.wait
    entered, release, waiting = threading.Event(), threading.Event(), threading.Event()

    def cleanup():
        # The injected failure precedes descriptor cleanup. Confirm that known
        # resource explicitly even when an assertion fails on the old code.
        real_close()
        events._release_error = None
        registry.close()

    request.addfinalizer(cleanup)

    def close_failure():
        raise OSError("synthetic owned observer close failure")

    def wait(timeout=None):
        waiting.set()
        return real_wait(timeout)

    def hold():
        borrow = store.borrow()
        lease = borrow.__enter__()
        if last_user == "operation":
            with store.operation(lease):
                borrow.__exit__(None, None, None)
                entered.set()
                assert release.wait(5)
        else:
            try:
                entered.set()
                assert release.wait(5)
            finally:
                borrow.__exit__(None, None, None)

    with monkeypatch.context() as patch:
        patch.setattr(events, "close", close_failure)
        patch.setattr(store._lifecycle, "wait", wait)
        with ThreadPoolExecutor(2) as pool:
            using = pool.submit(hold)
            assert entered.wait(2)
            draining = pool.submit(registry.block, None) if drain == "block" else pool.submit(registry.close)
            try:
                assert waiting.wait(2)
                release.set()
                with pytest.raises(OSError, match="owned observer"):
                    using.result(timeout=2)
                with pytest.raises(RuntimeError, match="cleanup remains unconfirmed"):
                    draining.result(timeout=2)
                assert store._users == 0 and store._retired
                assert events._references == 0 and not store._events_released
                assert store in registry._retired_stores
                assert None not in registry._drained
                with pytest.raises(WikiError) as unavailable:
                    registry.store(None)
                assert unavailable.value.status == 503
            finally:
                release.set()
                # Bound the failure on the pre-fix code too: never leave its
                # known missing notification hanging a test executor.
                with store._lifecycle:
                    store._lifecycle.notify_all()


def test_same_value_retry_confirms_file_and_directory_durability(tmp_path):
    config, config_file = installation_settings(tmp_path / "installation")
    marker = tmp_path / "sync-attempt"
    generations = []
    for _attempt in range(2):
        result = registration_process(config_file, marker, """
import stat
if not (state/'config.json').exists():
    first=registry.configure(None,root,'curator-a')
    marker.with_suffix('.first').write_text(json.dumps(first))
fsync=os.fsync
def synchronize(descriptor):
    path=Path(os.readlink(f'/proc/self/fd/{descriptor}'))
    if path==state or path.parent==state:
        kind='directory' if stat.S_ISDIR(os.fstat(descriptor).st_mode) else 'file'
        with marker.with_suffix('.syncs').open('a') as output: output.write(kind+'\\n')
        if path==state: raise OSError('synthetic directory durability failure')
    return fsync(descriptor)
os.fsync=synchronize
try:
    registry.configure(None,root,'curator-b')
except Exception:
    pass
marker.touch()
""")
        assert result.returncode == 70, result.stderr
        assert not marker.exists()
        generations.append(json.loads((Path(config.wiki_dir) / "config.json").read_text())["registration_generation"])
    confirmed = registration_process(config_file, marker, """
import stat
fsync=os.fsync
def synchronize(descriptor):
    path=Path(os.readlink(f'/proc/self/fd/{descriptor}'))
    if path==state or path.parent==state:
        kind='directory' if stat.S_ISDIR(os.fstat(descriptor).st_mode) else 'file'
        with marker.with_suffix('.syncs').open('a') as output: output.write(kind+'\\n')
    return fsync(descriptor)
os.fsync=synchronize
result=registry.configure(None,root,'curator-b')
marker.write_text(json.dumps(result))
registry.close()
""")
    assert confirmed.returncode == 0, confirmed.stderr
    generations.append(json.loads(marker.read_text())["registration_generation"])
    first = json.loads(marker.with_suffix(".first").read_text())
    assert len(set(generations)) == 1
    assert generations[0] != first["registration_generation"]
    assert marker.with_suffix(".syncs").read_text().splitlines().count("directory") == 3
    assert marker.with_suffix(".syncs").read_text().splitlines().count("file") >= 3


@pytest.fixture
async def http_boundary(tmp_path, monkeypatch):
    from server.wiki import maintenance_observation

    monkeypatch.setattr(settings, "wiki_dir", str(tmp_path / "legacy-wiki"))
    registry = WikiRegistry()
    monkeypatch.setattr(wiki_router, "registry", registry)
    monkeypatch.setattr(maintenance_observation, "registry", registry)
    db = Database(":memory:")
    await db.initialize()
    users = UserManager(db)
    deps.set_user_manager(users)
    user = await users.create_user(username="reader", password="test-password")
    owner = user["id"]
    token = await users.issue_token(owner)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            yield registry, owner, client, {"Authorization": "Bearer " + token}
    finally:
        registry.close()
        await db.close()


async def test_unregistered_http_queries_return_404_without_creating_state(http_boundary):
    registry, owner, client, headers = http_boundary
    state = registry.state(owner)
    assert not state.exists()
    routes = [("search", {"query": "missing"}), ("read", {"doc": "note.md"}),
              ("maintenance-status", {})]
    for route, params in routes:
        response = await client.get("/api/wiki/" + route, params=params, headers=headers)
        assert response.status_code == 404, response.text
        assert not state.exists()
    registry.begin_transition(owner)
    for route, params in routes:
        response = await client.get("/api/wiki/" + route, params=params, headers=headers)
        assert response.status_code == 503, response.text
        assert not state.exists()


@pytest.mark.parametrize("missing", [("registration_protocol",), ("registration_generation",),
                                    ("registration_protocol", "registration_generation")])
async def test_initialized_config_field_loss_is_rejected_without_rewriting(
    http_boundary, tmp_path, missing, monkeypatch
):
    registry, owner, client, headers = http_boundary
    root = tmp_path / "vault"
    root.mkdir()
    config = registry.configure(owner, root, "curator")
    path = registry.state(owner) / "config.json"
    path.write_text(json.dumps({key: value for key, value in config.items() if key not in missing}))
    original = path.read_bytes()
    for current in (registry, WikiRegistry()):
        monkeypatch.setattr(wiki_router, "registry", current)
        try:
            with pytest.raises(WikiError) as invalid:
                current.config(owner)
            assert invalid.value.status == 503
            response = await client.get("/api/wiki/config", headers=headers)
            assert response.status_code == 503, response.text
            assert path.read_bytes() == original
        finally:
            current.close()


async def test_start_cannot_reopen_while_stop_is_waiting_to_begin_close(registered, monkeypatch):
    registry, _, _ = registered
    entered, release = threading.Event(), threading.Event()
    close = registry.close

    def paused_close():
        entered.set()
        assert release.wait(5)
        close()

    monkeypatch.setattr(registry, "close", paused_close)
    stopping = asyncio.create_task(registry.stop())
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        with pytest.raises(WikiError) as not_drained:
            await registry.start()
        assert not_drained.value.status == 503
        with pytest.raises(WikiError):
            registry.resume_access()
        assert registry._task is None and registry._stopped
    finally:
        release.set()
        await stopping
        await registry.stop()
    await registry.start()
    await registry.stop()


async def test_a_start_interrupted_by_stop_cannot_publish_its_background_task(registered, monkeypatch):
    registry, _, _ = registered
    prepared, release = asyncio.Event(), asyncio.Event()
    reconcile = registry.reconcile

    async def paused_reconcile(**kwargs):
        result = await reconcile(**kwargs)
        prepared.set()
        await release.wait()
        return result

    monkeypatch.setattr(registry, "reconcile", paused_reconcile)
    starting = asyncio.create_task(registry.start())
    try:
        await asyncio.wait_for(prepared.wait(), 2)
        await registry.stop()
        release.set()
        with pytest.raises(WikiError) as revoked:
            await starting
        assert revoked.value.status == 503
        assert registry._task is None and registry._stopped
    finally:
        release.set()
        await asyncio.gather(starting, return_exceptions=True)
        await registry.stop()


async def test_overlapping_stops_keep_access_closed_until_both_drains_finish(registered, monkeypatch):
    registry, _, _ = registered
    entered = [threading.Event(), threading.Event()]
    release = [threading.Event(), threading.Event()]
    count_lock = threading.Lock()
    count = 0
    close = registry.close

    def paused_close():
        nonlocal count
        with count_lock:
            index = count
            count += 1
        if index < 2:
            entered[index].set()
            assert release[index].wait(5)
        close()

    monkeypatch.setattr(registry, "close", paused_close)
    first = asyncio.create_task(registry.stop())
    second = None
    try:
        assert await asyncio.to_thread(entered[0].wait, 2)
        second = asyncio.create_task(registry.stop())
        assert await asyncio.to_thread(entered[1].wait, 2)
        release[0].set()
        await first
        assert not second.done()
        with pytest.raises(WikiError):
            registry.resume_access()
        with pytest.raises(WikiError):
            await registry.start()
    finally:
        for event in release:
            event.set()
        await asyncio.gather(*(task for task in (first, second) if task), return_exceptions=True)
    await registry.start()
    await registry.stop()


async def test_failed_stop_cannot_reopen_until_a_later_confirmed_drain(registered, monkeypatch):
    registry, _, _ = registered
    with monkeypatch.context() as patch:
        def fail():
            raise OSError("synthetic unconfirmed stop drain")

        patch.setattr(registry, "close", fail)
        with pytest.raises(OSError, match="unconfirmed stop"):
            await registry.stop()
    with pytest.raises(WikiError):
        registry.resume_access()
    with pytest.raises(WikiError):
        await registry.start()
    await registry.stop()
    await registry.start()
    await registry.stop()


async def test_concurrent_start_does_not_create_two_background_tasks(registered, monkeypatch):
    registry, _, _ = registered
    entered, release = asyncio.Event(), asyncio.Event()
    reconcile = registry.reconcile

    async def paused_reconcile(**kwargs):
        result = await reconcile(**kwargs)
        entered.set()
        await release.wait()
        return result

    monkeypatch.setattr(registry, "reconcile", paused_reconcile)
    first = asyncio.create_task(registry.start())
    try:
        await asyncio.wait_for(entered.wait(), 2)
        with pytest.raises(WikiError):
            await registry.start()
        release.set()
        await first
        task = registry._task
        await registry.start()
        assert registry._task is task
    finally:
        release.set()
        await asyncio.gather(first, return_exceptions=True)
        await registry.stop()


@pytest.mark.parametrize("phase", ["schema", "refresh"])
@pytest.mark.parametrize("drain", ["block", "close"])
def test_drain_waits_for_side_effects_inside_constructor(registered, monkeypatch, phase, drain):
    from server.wiki import schema
    from server.wiki.store import WikiStore

    registry, _, _ = registered
    previous = registry.store(None)
    registry.close()
    if phase == "refresh":
        with sqlite3.connect(previous.db_path) as db:
            db.execute("PRAGMA recursive_triggers=ON")
            db.execute("UPDATE index_state SET checked_at=NULL WHERE singleton=1")
    entered, release = threading.Event(), threading.Event()
    candidates = []
    target, name = (schema, "initialize") if phase == "schema" else (WikiStore, "refresh")
    original = getattr(target, name)

    def paused(store):
        candidates.append(store)
        entered.set()
        assert release.wait(5)
        return original(store)

    monkeypatch.setattr(target, name, paused)
    with ThreadPoolExecutor(2) as pool:
        creating = pool.submit(registry.store, None)
        try:
            assert entered.wait(2)
            draining = pool.submit(registry.block, None) if drain == "block" else pool.submit(registry.close)
            # The condition is observed under the same lock used to revoke.
            # Waiting below is bounded and only coordinates this fixture.
            deadline = threading.Event()
            for _ in range(200):
                with registry._lock:
                    revoked = all(permit.revoked for permit in registry._constructing)
                if revoked:
                    break
                deadline.wait(0.005)
            assert revoked and not draining.done()
        finally:
            release.set()
        with pytest.raises(WikiError) as cancelled:
            creating.result(timeout=3)
        assert cancelled.value.status == 503
        draining.result(timeout=3)
    assert candidates and all(store._events_released for store in candidates)
    assert not registry._constructing


@pytest.mark.parametrize("lock", ["account", "history"])
def test_cold_store_refuses_hidden_construction_under_outer_locks(registered, monkeypatch, lock):
    from server.wiki.history import WikiHistory

    registry, _, _ = registered
    store = registry.store(None)
    cold = WikiRegistry()
    context = cold._account_lock(None) if lock == "account" else WikiHistory(store).locked(shared=True)

    def forbidden(*args, **kwargs):
        raise AssertionError("A constructor ran inside an outer owner lock")

    monkeypatch.setattr(registry_module, "WikiStore", forbidden)
    with context:
        with pytest.raises(WikiError, match="Prepare") as unavailable:
            cold.store(None)
        assert unavailable.value.status == 503
    assert not cold._constructing and store.events._references == 1
