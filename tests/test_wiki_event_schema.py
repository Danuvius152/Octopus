"""Supported preparation retains event authority and installs jobs atomically."""

import json
import sqlite3
import uuid
from contextlib import closing, contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from server.service_health import ServiceHealth
from server.wiki import schema, storage
from server.wiki.history import WikiHistory
from server.wiki.store import WikiError, WikiStore


@pytest.fixture
def database(tmp_path):
    root = tmp_path / "vault"
    root.mkdir()
    store = WikiStore(root, tmp_path / "state", uuid.uuid4().hex, create=True, defer_recovery=True)
    WikiHistory(store).write("note.md", b"# Note\nSource body.\n", None, "human", "keep history")
    try:
        yield store
    finally:
        store.close()


@pytest.fixture
def without_jobs(tmp_path):
    """The independently retained pre-job format12, with its actual binding."""
    path = tmp_path / "index.sqlite3"
    root = tmp_path / "vault"
    root.mkdir()
    with closing(sqlite3.connect(path)) as db:
        db.executescript((Path(__file__).parent / "fixtures/wiki-baseline-v12.sql").read_text())
        db.execute("INSERT INTO filesystem_state(singleton,protocol,root,vault_id) VALUES(1,1,?,'vault')", (str(root),))
        db.commit()
    health = ServiceHealth()

    @contextmanager
    def connect():
        with storage.connect(path, health) as db:
            yield db

    def no_files(*args):
        raise AssertionError("Format preparation must not observe vault files")

    return SimpleNamespace(root=root, vault_id="vault", connect=connect, observed_path=no_files)


def snapshot(store):
    with store.connect() as db:
        names = [row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        return {name: [tuple(row) for row in db.execute('SELECT * FROM "' + name + '"')] for name in names}


def seed_pending(database):
    with database.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute("UPDATE filesystem_state SET received_seq=1,applied_seq=0")
        db.execute("INSERT INTO filesystem_inbox VALUES(1,'batch','epoch','generation','{\"ordered\":[\"delete\"]}',"
                   "'immutable-hash','pending','old-time')")


def test_selection_schema_does_not_reference_an_uninitialized_job_ledger(without_jobs):
    from server.wiki import maintenance_selections
    from server.wiki.jobs import WikiJobs

    with without_jobs.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        maintenance_selections.require(db)
        assert db.execute("SELECT name FROM sqlite_master WHERE name='jobs'").fetchone() is None
        assert db.execute("SELECT name FROM sqlite_master WHERE name='maintenance_evidence_job_owner'").fetchone() is None
        # ALTER validates every persistent trigger, even on unrelated tables.
        db.execute("CREATE TABLE unrelated(value TEXT)")
        db.execute("ALTER TABLE unrelated RENAME COLUMN value TO renamed")
        db.execute("DROP TABLE unrelated")
        db.execute("INSERT INTO maintenance_selections VALUES('selection','user:owner','vault','generation','human','purpose','{}','time','time',1,1)")
        review = {"doc": "doc", "path": "Library/note.md", "revision": "revision", "baseline_receipt_id": None,
                  "dependency_generation": "generation", "review_key": "key"}
        db.execute("INSERT INTO maintenance_selection_items VALUES('selection',0,'doc','Library/note.md','revision',?)",
                   (json.dumps(review),))
        with pytest.raises(sqlite3.IntegrityError, match="matching job and observation"):
            db.execute("INSERT INTO job_selection_evidence VALUES('unknown','selection',0,'doc','key','time')")
        selections = list(map(tuple, db.execute("SELECT * FROM maintenance_selections")))
        items = list(map(tuple, db.execute("SELECT * FROM maintenance_selection_items")))
    # The storage owner prepares the whole old layout before business owners
    # can use the current receipt and job representations.
    schema.initialize(without_jobs)
    with without_jobs.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        maintenance_selections.require(db)
        assert list(map(tuple, db.execute("SELECT * FROM maintenance_selections"))) == selections
        assert list(map(tuple, db.execute("SELECT * FROM maintenance_selection_items"))) == items
        assert db.execute("SELECT name FROM sqlite_master WHERE name='maintenance_evidence_job_owner'").fetchone()
        with pytest.raises(sqlite3.IntegrityError, match="matching job"):
            db.execute("INSERT INTO job_selection_evidence VALUES('unknown','selection',0,'doc','key','time')")
        assert not WikiJobs.initialize_ledger(db)


def test_job_owner_rolls_back_tables_protocol_and_selection_guard_together(without_jobs, monkeypatch):
    from server.wiki import maintenance_selections

    before = snapshot(without_jobs)
    attach = maintenance_selections.attach_jobs

    def fail(db):
        attach(db)
        raise RuntimeError("Injected failure after installing the job guard")

    monkeypatch.setattr(maintenance_selections, "attach_jobs", fail)
    with pytest.raises(RuntimeError, match="Injected failure"):
        schema.initialize(without_jobs)
    assert snapshot(without_jobs) == before
    with without_jobs.connect() as db:
        maintenance_selections.require(db)
        assert db.execute("SELECT 1 FROM sqlite_master WHERE name='maintenance_evidence_job_owner'").fetchone() is None


def test_missing_initialized_selection_job_guard_is_not_recreated(database):
    from server.wiki.jobs import WikiJobs

    with database.connect() as db:
        db.execute("DROP TRIGGER maintenance_evidence_job_owner")
    before = snapshot(database)
    with database.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        with pytest.raises(WikiError, match="Selection ledger.*retained") as error:
            WikiJobs.initialize_ledger(db)
        assert error.value.status == 503
        assert db.execute("SELECT name FROM sqlite_master WHERE name='maintenance_evidence_job_owner'").fetchone() is None
    with pytest.raises(WikiError, match="retained"):
        schema.initialize(database)
    assert snapshot(database) == before


@pytest.mark.parametrize("missing", ["filesystem_state", "filesystem_inbox"])
def test_initialized_schema_does_not_rebuild_a_lost_event_ledger(database, missing):
    seed_pending(database)
    with database.connect() as db:
        db.execute("DROP TABLE " + missing)
    before = snapshot(database)
    retained = database.db_path.read_bytes()
    with pytest.raises(WikiError, match="retained") as error:
        schema.initialize(database)
    assert error.value.status == 503
    assert snapshot(database) == before
    assert database.db_path.read_bytes() == retained


@pytest.mark.parametrize("broken", ["column", "generated", "unique", "singleton", "status"])
def test_incompatible_existing_event_shape_is_retained_and_rejected(database, broken):
    seed_pending(database)
    with database.connect() as db:
        if broken == "column":
            db.execute("ALTER TABLE filesystem_inbox RENAME COLUMN payload_hash TO other_hash")
        elif broken == "generated":
            db.execute("ALTER TABLE filesystem_inbox ADD COLUMN other_hash TEXT GENERATED ALWAYS AS (payload_hash) VIRTUAL")
        else:
            table = "filesystem_state" if broken == "singleton" else "filesystem_inbox"
            sql = db.execute("SELECT sql FROM sqlite_master WHERE name=?", (table,)).fetchone()[0]
            if broken == "unique":
                sql = sql.replace("batch_id TEXT UNIQUE", "batch_id TEXT")
            elif broken == "singleton":
                sql = sql.replace(" CHECK(singleton=1)", "")
            else:
                sql = sql.replace(" CHECK(status IN ('pending','applied'))", "")
            db.execute("DROP TABLE " + table)
            db.execute(sql)
    before = snapshot(database)
    retained = database.db_path.read_bytes()
    with pytest.raises(WikiError, match="retained") as error:
        schema.initialize(database)
    assert error.value.status == 503
    assert snapshot(database) == before
    assert database.db_path.read_bytes() == retained


def test_derived_search_repair_preserves_pending_events_and_durable_rows(database):
    seed_pending(database)
    with database.connect() as db:
        db.execute("DROP TABLE search_fts")
    before = snapshot(database)
    schema.initialize(database)
    after = snapshot(database)
    for table in ("filesystem_state", "filesystem_inbox", "document_identities", "managed_notes", "changes",
                  "operation_receipts", "material_origins", "source_bindings", "source_binding_observations"):
        assert after[table] == before[table]
    assert "search_fts" in after
    schema.initialize(database)
    assert snapshot(database) == after


@pytest.mark.parametrize("version", [8, 11, 15])
def test_unsupported_format_retains_pending_events_without_backfill(database, version):
    seed_pending(database)
    with database.connect() as db:
        db.execute("DELETE FROM wiki_schema")
        db.execute("INSERT INTO wiki_schema VALUES(?,'original-time')", (version,))
    before = snapshot(database)
    retained = database.db_path.read_bytes()
    with pytest.raises(WikiError, match="retained") as error:
        schema.initialize(database)
    assert error.value.status == 503
    assert snapshot(database) == before
    assert database.db_path.read_bytes() == retained
