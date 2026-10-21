"""Registration generations and construction writers obey owner drain boundaries."""

import asyncio
import importlib
import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from server.config import settings
from server.wiki import schema, source_bindings
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
    (root / "note.md").write_text("# Original\nPreserved words.\n")
    registry = WikiRegistry()
    config = registry.configure(None, root, "curator-a", "app-a")
    try:
        yield registry, root, config
    finally:
        registry.close()


@pytest.mark.parametrize("field", ["curator_agent_id", "app_id"])
def test_registration_round_trip_cannot_revive_its_old_generation(registered, field):
    registry, root, first = registered
    same = registry.configure(None, root, "curator-a", "app-a")
    assert same["registration_generation"] == first["registration_generation"]
    second = registry.configure(None, root, "curator-b" if field == "curator_agent_id" else "curator-a",
                                "app-b" if field == "app_id" else "app-a")
    third = registry.configure(None, root, "curator-a", "app-a")
    assert len({first["registration_generation"], second["registration_generation"], third["registration_generation"]}) == 3
    registry.close()
    assert registry.config(None) == third
    assert registry.store(None).vault_id == first["vault_id"]


@pytest.mark.parametrize("boundary", ["before_constructor", "before_publication"])
@pytest.mark.parametrize("action", ["block", "close"])
def test_block_waits_for_candidate_cleanup_before_owner_state_can_move(registered, monkeypatch, boundary, action):
    registry, _, _ = registered
    registry.close()
    real_store = registry_module.WikiStore
    entered, release, blocking = threading.Event(), threading.Event(), threading.Event()
    candidates = []

    def construction(*args, **kwargs):
        if boundary == "before_constructor":
            entered.set()
            assert release.wait(5)
        candidate = real_store(*args, **kwargs)
        candidates.append(candidate)
        if boundary == "before_publication":
            entered.set()
            assert release.wait(5)
        return candidate

    monkeypatch.setattr(registry_module, "WikiStore", construction)

    def block():
        blocking.set()
        registry.block(None) if action == "block" else registry.close()

    with ThreadPoolExecutor(2) as pool:
        creating = pool.submit(registry.store, None)
        assert entered.wait(3)
        draining = pool.submit(block)
        try:
            assert blocking.wait(3)
            assert not draining.done()
        finally:
            release.set()
        try:
            creating.result(timeout=5)
        except WikiError as error:
            assert error.status == 503
        draining.result(timeout=5)
    assert all(store._events_released and store._retired for store in candidates)
    if action == "block":
        with pytest.raises(WikiError):
            registry.store(None)
        registry.unblock(None)
    current = registry.store(None)
    assert current not in candidates or not current._retired
    assert current.search("Preserved")["items"]


def test_same_generation_constructors_publish_one_winner_and_release_loser(registered, monkeypatch):
    registry, _, _ = registered
    registry.close()
    real_store = registry_module.WikiStore
    built = threading.Barrier(2)
    first_built = threading.Event()
    candidates = []

    def construction(*args, **kwargs):
        candidate = real_store(*args, **kwargs)
        candidates.append(candidate)
        first_built.set()
        built.wait(timeout=5)
        return candidate

    monkeypatch.setattr(registry_module, "WikiStore", construction)
    with ThreadPoolExecutor(2) as pool:
        futures = [pool.submit(registry.store, None)]
        # The first claim has completed, but its candidate is not published.
        # Race publication rather than the separately retryable range claim.
        assert first_built.wait(3)
        futures.append(pool.submit(registry.store, None))
        results = [future.result(timeout=7) for future in futures]
    assert results[0] is results[1]
    assert len(candidates) == 2
    assert sum(candidate._events_released for candidate in candidates) == 1
    assert not results[0]._events_released
    assert not registry._constructing


def test_constructor_failure_releases_observer_before_leaving_tracking(registered, monkeypatch):
    registry, _, _ = registered
    registry.close()
    initialize = schema.initialize
    failed = []

    def interrupted(store):
        initialize(store)
        failed.append(store)
        raise RuntimeError("synthetic post-schema failure")

    monkeypatch.setattr(schema, "initialize", interrupted)
    with pytest.raises(RuntimeError, match="post-schema"):
        registry.store(None)
    assert failed[0]._events_released
    assert not registry._constructing
    registry.block(None)
    registry.unblock(None)


async def test_cancelled_stop_joins_an_unpublished_constructor(registered, monkeypatch):
    registry, _, _ = registered
    registry.close()
    real_store = registry_module.WikiStore
    entered, release = threading.Event(), threading.Event()
    candidates = []

    def construction(*args, **kwargs):
        candidate = real_store(*args, **kwargs)
        candidates.append(candidate)
        entered.set()
        assert release.wait(5)
        return candidate

    monkeypatch.setattr(registry_module, "WikiStore", construction)
    creating = asyncio.create_task(asyncio.to_thread(registry.store, None))
    assert await asyncio.to_thread(entered.wait, 3)
    stopping = asyncio.create_task(registry.stop())
    await asyncio.sleep(0)
    stopping.cancel("waiter cancelled")
    release.set()
    with pytest.raises(WikiError):
        await creating
    with pytest.raises(asyncio.CancelledError):
        await stopping
    assert candidates[0]._events_released
    assert not registry._constructing
    with pytest.raises(WikiError):
        registry.store(None)


def test_migration_reopen_requires_a_real_owned_transition(registered):
    registry, _, _ = registered
    registry.block(None)
    with pytest.raises(WikiError):
        registry.reopen_for_upgrade(None, {"upgrade_id": "model-supplied"})
    with pytest.raises(WikiError):
        registry._store(None)


def test_unblock_cannot_revive_an_old_drain_after_registry_close(registered):
    registry, _, _ = registered
    registry.block(None)
    registry.close()
    with pytest.raises(WikiError, match="cleanup"):
        registry.unblock(None)
    registry.block(None)
    registry.unblock(None)
    assert registry.store(None).search("Preserved")["items"]


def test_same_value_retry_after_directory_fsync_failure_preserves_committed_nonce(tmp_path):
    config, config_file = installation_settings(tmp_path / "installation")
    marker = tmp_path / "registration.json"
    failed = registration_process(config_file, marker, """
first=registry.configure(None,root,'curator-a','app-a')
marker.with_suffix('.before').write_text(json.dumps(first))
fsync=os.fsync
def interrupted(descriptor):
    if Path(os.readlink(f'/proc/self/fd/{descriptor}'))==state:
        marker.with_suffix('.sync-started').touch()
        raise OSError('synthetic directory durability failure')
    return fsync(descriptor)
os.fsync=interrupted
try:
    registry.configure(None,root,'curator-b','app-a')
except Exception:
    pass
marker.touch()
""")
    assert marker.with_suffix(".sync-started").is_file(), failed.stderr
    assert failed.returncode == 70, failed.stderr
    assert not marker.exists()
    path = Path(config.wiki_dir) / "config.json"
    committed = json.loads(path.read_text())
    first = json.loads(marker.with_suffix(".before").read_text())
    assert committed["registration_generation"] != first["registration_generation"]
    assert committed["vault_id"] == first["vault_id"]
    retried = registration_process(config_file, marker, """
result=registry.configure(None,root,'curator-b','app-a')
marker.write_text(json.dumps(result))
registry.close()
""")
    assert retried.returncode == 0, retried.stderr
    assert json.loads(marker.read_text()) == committed


def test_initialized_registration_cannot_recreate_missing_generation(registered):
    registry, root, _ = registered
    path = registry.state(None) / "config.json"
    config = registry.config(None)
    for key in ("registration_generation", "registration_protocol"):
        config.pop(key)
    path.write_text(json.dumps(config))
    before = path.read_bytes()
    with pytest.raises(WikiError, match="protocol ledger"):
        registry.configure(None, root, "curator-a", "app-a")
    assert path.read_bytes() == before


@pytest.mark.parametrize("remove", ["table", "marker", "shape"])
def test_missing_initialized_protocol_is_not_rebuilt_as_an_empty_cache(registered, remove):
    registry, _, _ = registered
    store = registry.store(None)
    registry.close()
    with store.db_path.open("rb") as stream:
        assert stream.read(6) == b"SQLite"
    with sqlite3.connect(store.db_path) as db:
        if remove == "table":
            db.execute("DROP TABLE wiki_protocols")
        elif remove == "marker":
            db.execute("DELETE FROM wiki_protocols WHERE name='registration'")
        else:
            db.execute("ALTER TABLE wiki_protocols RENAME TO incompatible_protocols")
            db.execute("CREATE TABLE wiki_protocols(name TEXT,version INTEGER)")
            db.execute("INSERT INTO wiki_protocols SELECT * FROM incompatible_protocols")
    config = (registry.state(None) / "config.json").read_bytes()
    with pytest.raises(WikiError, match="protocol ledger"):
        registry.store(None)
    assert (registry.state(None) / "config.json").read_bytes() == config


def test_failed_candidate_cleanup_stays_owned_and_blocks_new_work_until_drain(registered, monkeypatch):
    registry, _, _ = registered
    registry.close()
    real_store = registry_module.WikiStore
    candidates = []

    def construction(*args, **kwargs):
        candidate = real_store(*args, **kwargs)
        candidates.append(candidate)
        close = candidate.close
        failed = False

        def interrupted():
            nonlocal failed
            if not failed:
                failed = True
                raise RuntimeError("synthetic unconfirmed candidate cleanup")
            close()

        monkeypatch.setattr(candidate, "close", interrupted)
        registry.begin_transition(None)
        return candidate

    monkeypatch.setattr(registry_module, "WikiStore", construction)
    with pytest.raises(RuntimeError, match="unconfirmed candidate cleanup"):
        registry.store(None)
    assert len(registry._constructing) == 1
    permit = next(iter(registry._constructing))
    assert permit.candidate is candidates[0] and not permit.done.is_set()
    with pytest.raises(WikiError):
        registry.store(None)
    registry.block(None)
    assert permit.done.is_set() and candidates[0]._events_released
    assert not registry._constructing


@pytest.mark.parametrize("crash", [False, True])
def test_legacy_config_upgrade_preserves_identity_and_does_not_remint_after_restart(registered, monkeypatch, crash):
    from server.wiki import maintenance_inputs
    from tests.wiki_schema_fixtures import remove_future_selections

    registry, _, config = registered
    store = registry.store(None)
    original = store.read("note.md")
    registry.close()
    with sqlite3.connect(store.db_path) as db:
        maintenance_inputs.discard_derived(db)
        remove_future_selections(db)
        for table in source_bindings.TABLES:
            db.execute("DROP TABLE " + table)
        db.execute("ALTER TABLE app_trash DROP COLUMN source_plan")
        db.execute("DROP TABLE wiki_protocols")
        db.execute("DELETE FROM wiki_schema WHERE version>=10")
    path = registry.state(None) / "config.json"
    path.write_text(json.dumps({k: v for k, v in config.items() if not k.startswith("registration_")}))
    if crash:
        with monkeypatch.context() as patch:
            patch.setattr(registry_module, "WikiStore", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("after config replacement")))
            with pytest.raises(RuntimeError, match="after config replacement"):
                registry.store(None)
    committed = registry.config(None).get("registration_generation")
    current = registry.store(None)
    assert current.read("note.md")["id"] == original["id"]
    assert current.path("note.md").read_text() == "# Original\nPreserved words.\n"
    if committed:
        assert registry.config(None)["registration_generation"] == committed
    assert registry.config(None)["vault_id"] == config["vault_id"]


async def test_stop_waits_for_a_registration_commit_before_finishing(registered, monkeypatch):
    registry, root, first = registered
    committed, release = threading.Event(), threading.Event()
    configure = registry._configure

    def paused(*args):
        result = configure(*args)
        committed.set()
        assert release.wait(5)
        return result

    monkeypatch.setattr(registry, "_configure", paused)
    writing = asyncio.create_task(asyncio.to_thread(registry.configure, None, root, "curator-b", "app-a"))
    assert await asyncio.to_thread(committed.wait, 3)
    stopping = asyncio.create_task(registry.stop())
    await asyncio.sleep(0)
    assert not stopping.done()
    release.set()
    with pytest.raises(WikiError):
        await writing
    await stopping
    assert registry.config(None)["registration_generation"] != first["registration_generation"]
    assert not registry._constructing
    with pytest.raises(WikiError):
        registry.store(None)
