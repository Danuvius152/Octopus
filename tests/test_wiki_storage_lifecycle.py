"""A storage generation ends before an outer handler can admit another write."""

import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from server.config import settings
from server.service_health import ServiceHealth
from server.wiki import storage
from server.wiki.events import VaultEvents
from server.wiki.recovery import Metadata
from server.wiki.registry import WikiRegistry
from server.wiki.store import WikiError
from tests.test_service_runtime import installation_settings
from tests.test_wiki_storage import registered as registered_fixture
from tests.test_wiki_storage import registration_process

registered = registered_fixture


@pytest.mark.parametrize("journal", ["delete", "wal"])
def test_connection_confirms_extra_without_changing_the_existing_journal(tmp_path, journal):
    path = tmp_path / "ledger.sqlite3"
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA journal_mode=" + journal).fetchone()[0] == journal
        db.execute("CREATE TABLE facts(id INTEGER PRIMARY KEY)")
    with storage.connect(path, ServiceHealth()) as db:
        assert type(db) is sqlite3.Connection
        assert db.execute("PRAGMA synchronous").fetchone()[0] == 3
        assert db.execute("PRAGMA journal_mode").fetchone()[0] == journal


def test_confirmed_rollback_preserves_domain_and_body_read_errors(tmp_path):
    path = tmp_path / "ledger.sqlite3"
    health = ServiceHealth()
    with storage.connect(path, health, create=True) as db:
        db.execute("CREATE TABLE facts(id INTEGER PRIMARY KEY)")
        db.execute("INSERT INTO facts VALUES(1)")
    for failure in (WikiError("revision changed", 409), OSError("body root offline")):
        with pytest.raises(type(failure), match=str(failure)):
            with storage.connect(path, health) as db:
                db.execute("INSERT INTO facts VALUES(2)")
                raise failure
    with pytest.raises(sqlite3.IntegrityError):
        with storage.connect(path, health) as db:
            db.execute("INSERT INTO facts VALUES(1)")
    with storage.connect(path, health) as db:
        assert [row[0] for row in db.execute("SELECT id FROM facts")] == [1]
    assert health.cause is None


def test_busy_before_begin_is_a_local_refusal(tmp_path):
    path = tmp_path / "ledger.sqlite3"
    health = ServiceHealth()
    with storage.connect(path, health, create=True) as db:
        db.execute("CREATE TABLE facts(id INTEGER PRIMARY KEY)")
    lock = sqlite3.connect(path)
    try:
        lock.execute("BEGIN IMMEDIATE")
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            with storage.connect(path, health, timeout=0) as db:
                db.execute("BEGIN IMMEDIATE")
        assert health.cause is None
    finally:
        lock.rollback()
        lock.close()


def configuration_contention_process(config_file, marker, *, end_fault=None):
    return registration_process(config_file, marker, """
from server.wiki import storage
registry.configure(None,root,'curator')
registry.close()
health=registry.health
path=state/'index.sqlite3'
blocker=sqlite3.connect(path)
assert blocker.execute('PRAGMA journal_mode').fetchone()[0]=='delete'
blocker.execute('BEGIN EXCLUSIVE')
original=sqlite3.connect
end_fault=END_FAULT
def record(event):
    with marker.with_suffix('.trace').open('a') as output:
        output.write(json.dumps(event)+'\\n')
class ObservedConnection(sqlite3.Connection):
    def execute(self,sql,*args,**kwargs):
        try:
            return super().execute(sql,*args,**kwargs)
        except sqlite3.Error as error:
            record({'operation':'sql_error','sql':sql,'code':error.sqlite_errorcode,
                'in_transaction':self.in_transaction,'sqlite_version':sqlite3.sqlite_version})
            raise
    def rollback(self):
        record({'operation':'rollback'})
        if end_fault=='rollback': raise OSError('configuration rejection rollback is unconfirmed')
        super().rollback()
        record({'operation':'rollback_confirmed'})
    def close(self):
        record({'operation':'close'})
        if end_fault=='close': raise OSError('configuration rejection close is unconfirmed')
        super().close()
        record({'operation':'close_confirmed'})
def observed(*args,**kwargs):
    kwargs['factory']=ObservedConnection
    return original(*args,**kwargs)
sqlite3.connect=observed
try:
    with storage.connect(path,health,timeout=0):
        marker.with_suffix('.body').touch()
except sqlite3.OperationalError as error:
    assert error.sqlite_errorcode & 0xff==sqlite3.SQLITE_BUSY
else:
    raise AssertionError('An exclusive writer did not reject configuration')
assert health.cause is None
blocker.rollback()
blocker.close()
with storage.connect(path,health,timeout=0) as db:
    assert db.execute('PRAGMA synchronous').fetchone()[0]==3
    db.execute("INSERT INTO app_trash(trash_id,path) VALUES('configuration-retry','Library/retry.md')")
with storage.connect(path,health,write=False) as db:
    retained=db.execute("SELECT path FROM app_trash WHERE trash_id='configuration-retry'").fetchone()[0]
marker.write_text(json.dumps({'path':retained,'healthy':health.cause is None}))
""".replace("END_FAULT", repr(end_fault)))


def test_configuration_busy_closes_without_yield_and_the_same_health_can_retry(tmp_path):
    _config, config_file = installation_settings(tmp_path / "installation")
    marker = tmp_path / "configuration-retry"
    result = configuration_contention_process(config_file, marker)
    trace = [json.loads(line) for line in marker.with_suffix(".trace").read_text().splitlines()]
    assert trace[0]["sql"] == "PRAGMA synchronous=EXTRA", result.stderr
    assert trace[0]["code"] & 0xFF == sqlite3.SQLITE_BUSY
    assert trace[0]["in_transaction"] is False
    assert not marker.with_suffix(".body").exists()
    assert result.returncode == 0, result.stderr
    assert [event["operation"] for event in trace[:5]] == [
        "sql_error", "rollback", "rollback_confirmed", "close", "close_confirmed",
    ]
    assert json.loads(marker.read_text()) == {"path": "Library/retry.md", "healthy": True}


@pytest.mark.parametrize("end_fault", ["rollback", "close"])
def test_configuration_busy_cannot_return_when_rollback_or_close_is_unconfirmed(tmp_path, end_fault):
    _config, config_file = installation_settings(tmp_path / "installation")
    marker = tmp_path / "unconfirmed-configuration-rejection"
    result = configuration_contention_process(config_file, marker, end_fault=end_fault)
    trace = [json.loads(line) for line in marker.with_suffix(".trace").read_text().splitlines()]
    assert trace[0]["sql"] == "PRAGMA synchronous=EXTRA", result.stderr
    assert trace[0]["code"] & 0xFF == sqlite3.SQLITE_BUSY
    assert trace[0]["in_transaction"] is False
    expected = ["sql_error", "rollback"]
    if end_fault == "close":
        expected.extend(["rollback_confirmed", "close"])
    assert [event["operation"] for event in trace] == expected
    assert not marker.with_suffix(".body").exists()
    assert result.returncode == 70, result.stderr
    assert not marker.exists()


@pytest.mark.parametrize("fault", ["open", "configuration", "configuration_sql_error", "pragma_mismatch", "rollback", "rollback_after",
                                  "close", "close_after", "metadata_sql", "access_sql", "worker_sql"])
def test_storage_faults_exit_before_sql_wrapping_or_an_outer_handler(tmp_path, fault):
    config, config_file = installation_settings(tmp_path / "installation")
    marker = tmp_path / "admitted-after-failure"
    operation = """
import threading
from server.wiki.recovery import Metadata
registry.configure(None,root,'curator')
store=registry.store(None)
original=sqlite3.connect
fault=FAULT
def broken():
    marker.with_suffix('.fault-started').touch()
    error=sqlite3.OperationalError('synthetic physical storage failure')
    error.sqlite_errorcode=sqlite3.SQLITE_IOERR
    raise error
class FaultyConnection(sqlite3.Connection):
    def execute(self,sql,*args,**kwargs):
        if fault=='configuration' and sql=='PRAGMA synchronous=EXTRA': broken()
        if fault=='configuration_sql_error' and sql=='PRAGMA synchronous=EXTRA':
            marker.with_suffix('.fault-started').touch()
            return super().execute('SELECT * FROM missing_configuration_table')
        if fault=='pragma_mismatch' and sql=='PRAGMA synchronous=EXTRA':
            marker.with_suffix('.fault-started').touch()
            return super().execute('PRAGMA synchronous=NORMAL')
        if fault=='metadata_sql' and sql.startswith('SELECT max(version)'): broken()
        if fault=='access_sql' and sql.startswith('SELECT * FROM filesystem_recoveries'): broken()
        if fault=='worker_sql' and sql=='SELECT physical_fault': broken()
        return super().execute(sql,*args,**kwargs)
    def rollback(self):
        if fault=='rollback': broken()
        if fault=='rollback_after':
            super().rollback()
            broken()
        return super().rollback()
    def close(self):
        if fault=='close': broken()
        if fault=='close_after':
            super().close()
            broken()
        return super().close()
def connect(*args,**kwargs):
    if fault=='open': broken()
    kwargs['factory']=FaultyConnection
    return original(*args,**kwargs)
sqlite3.connect=connect
def run():
    try:
        context=Metadata(registry,None).connect(write=True) if fault=='metadata_sql' else store.connect()
        with context as db:
            if fault=='worker_sql': db.execute('SELECT physical_fault')
            if fault in {'rollback','rollback_after','close','close_after'}:
                db.execute("INSERT INTO app_trash(trash_id,path) VALUES('end-proof','Library/end.md')")
            if fault in {'rollback','rollback_after'}: raise ValueError('ordinary domain rejection')
    except Exception:
        pass
    marker.touch()
if fault=='worker_sql':
    worker=threading.Thread(target=run)
    worker.start()
    worker.join()
else:
    run()
""".replace("FAULT", repr(fault))
    result = registration_process(config_file, marker, operation)
    assert marker.with_suffix(".fault-started").is_file(), result.stderr
    assert result.returncode == 70, result.stderr
    assert not marker.exists()
    with sqlite3.connect(Path(config.wiki_dir) / "index.sqlite3") as db:
        assert db.execute("SELECT count(*) FROM filesystem_state").fetchone()[0] == 1
        if fault in {"rollback", "rollback_after", "close", "close_after"}:
            assert db.execute("SELECT count(*) FROM app_trash WHERE trash_id='end-proof'").fetchone()[0] == int(fault.startswith("close"))


@pytest.mark.parametrize("fault", ["full", "notadb", "busy_in_transaction"])
def test_real_sqlite_failures_do_not_become_a_recoverable_tool_error(tmp_path, fault):
    config, config_file = installation_settings(tmp_path / "installation")
    marker = tmp_path / "admitted-after-failure"
    operation = """
from server.wiki import storage
registry.configure(None,root,'curator')
store=registry.store(None)
fault=FAULT
try:
    if fault=='notadb':
        registry.close()
        store.db_path.write_bytes(b'corrupt authoritative bytes retained')
        marker.with_suffix('.fault-started').touch()
        registry.store(None)
    elif fault=='full':
        with storage.connect(store.db_path,store.health) as db:
            count=db.execute('PRAGMA page_count').fetchone()[0]
            db.execute('PRAGMA max_page_count='+str(count))
            marker.with_suffix('.fault-started').touch()
            db.execute("INSERT INTO app_trash(trash_id,path) VALUES('full-proof',zeroblob(1024*1024))")
    else:
        blocker=sqlite3.connect(store.db_path)
        blocker.execute('BEGIN IMMEDIATE')
        with storage.connect(store.db_path,store.health,timeout=0) as db:
            db.execute('BEGIN')
            marker.with_suffix('.fault-started').touch()
            db.execute("INSERT INTO app_trash(trash_id,path) VALUES('busy-proof','Library/busy.md')")
except Exception:
    pass
marker.touch()
""".replace("FAULT", repr(fault))
    failed = registration_process(config_file, marker, operation)
    assert marker.with_suffix(".fault-started").is_file(), failed.stderr
    assert failed.returncode == 70, failed.stderr
    assert not marker.exists()
    if fault == "notadb":
        assert (Path(config.wiki_dir) / "index.sqlite3").read_bytes() == b"corrupt authoritative bytes retained"
    else:
        with sqlite3.connect(Path(config.wiki_dir) / "index.sqlite3") as db:
            assert db.execute("SELECT count(*) FROM app_trash WHERE trash_id IN ('full-proof','busy-proof')").fetchone()[0] == 0


def test_every_captured_writer_uses_the_registry_health(registered):
    registry, store = registered
    health = registry.health
    metadata = Metadata(registry, None)
    assert store.health is store.events.health is store.events.journal.health is health
    assert metadata.health is metadata.inbox.health is health
    assert registry.recoveries().health is health
    with pytest.raises(WikiError, match="generation"):
        VaultEvents.acquire(store.root, store.state, health=ServiceHealth())


def test_health_binding_requires_store_and_recovery_gate_retirement(registered):
    registry, store = registered
    old_health = registry.health
    manager = registry.recoveries()
    new_health = ServiceHealth()
    with pytest.raises(WikiError):
        registry.bind_health(new_health)
    registry.close()
    registry.bind_health(new_health)
    assert store.health is old_health and manager.health is old_health
    assert registry.health is new_health
    with pytest.raises(WikiError):
        with manager.gate(None):
            pytest.fail("The old recovery manager reopened after health binding")
    current = registry.store(None)
    assert current.health is new_health


def test_empty_recovery_handles_do_not_prove_an_active_plan_has_exited(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "wiki_dir", str(tmp_path / "state"))
    registry = WikiRegistry()
    manager = registry.recoveries()
    entered, release = threading.Event(), threading.Event()

    def plan():
        with manager.gate(None):
            entered.set()
            assert release.wait(5)

    with ThreadPoolExecutor(2) as workers:
        planning = workers.submit(plan)
        assert entered.wait(2)
        assert not manager._handles
        closing = workers.submit(registry.close)
        try:
            with pytest.raises(WikiError):
                registry.bind_health(ServiceHealth())
        finally:
            release.set()
        planning.result(timeout=2)
        closing.result(timeout=2)
    registry.bind_health(ServiceHealth())
    with pytest.raises(WikiError):
        with manager.gate(None):
            pytest.fail("A retired recovery gate reopened")


def test_unopened_recovery_gate_must_be_closed_before_health_binding():
    registry = WikiRegistry()
    manager = registry.recoveries()
    with pytest.raises(WikiError):
        registry.bind_health(ServiceHealth())
    registry.close()
    registry.bind_health(ServiceHealth())
    assert manager.is_drained()


def test_binding_waits_for_every_unpublished_constructor(registered, monkeypatch):
    from server.wiki import schema

    registry, _store = registered
    registry.close()
    entered, release = threading.Event(), threading.Event()
    initialize = schema.initialize

    def paused(store):
        entered.set()
        assert release.wait(5)
        initialize(store)

    monkeypatch.setattr(schema, "initialize", paused)
    with ThreadPoolExecutor(1) as worker:
        constructing = worker.submit(registry.store, None)
        try:
            assert entered.wait(2)
            with pytest.raises(WikiError):
                registry.bind_health(ServiceHealth())
        finally:
            release.set()
        constructing.result(timeout=2)
    registry.close()
    registry.bind_health(ServiceHealth())


def test_a_waiting_old_plan_cannot_enter_after_close_and_explicit_resume():
    registry = WikiRegistry()
    manager = registry.recoveries()
    waiting, release = threading.Event(), threading.Event()
    admitted = []
    plan_thread = None

    class PausedGate:
        def __init__(self):
            self.lock = threading.RLock()

        def __enter__(self):
            if threading.get_ident() == plan_thread:
                waiting.set()
                assert release.wait(5)
            self.lock.acquire()

        def __exit__(self, *_args):
            self.lock.release()

    manager._gates[manager.key(None)] = PausedGate()

    def plan():
        nonlocal plan_thread
        plan_thread = threading.get_ident()
        with manager.gate(None):
            admitted.append(True)

    with ThreadPoolExecutor(1) as worker:
        planning = worker.submit(plan)
        try:
            assert waiting.wait(2)
            registry.close()
            registry.resume_access()
        finally:
            release.set()
        with pytest.raises(WikiError, match="lifecycle"):
            planning.result(timeout=2)
    assert not admitted
    registry.close()


def test_offline_root_remains_a_local_metadata_diagnostic(registered):
    registry, store = registered
    original = (store.state / "config.json").read_bytes()
    offline = store.root.with_name("offline-vault")
    store.root.rename(offline)
    try:
        assert not Metadata(registry, None).snapshot()["root_available"]
        assert registry.health.cause is None
        assert (store.state / "config.json").read_bytes() == original
    finally:
        offline.rename(store.root)
