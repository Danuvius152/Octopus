"""Schema 9 event ledgers survive upgrade, reopen and derived-index repair."""

import contextlib
import sqlite3
from types import SimpleNamespace

import pytest

from server.wiki import schema
from server.wiki.store import WikiError


@pytest.fixture
def database(tmp_path):
    path = tmp_path / "index.sqlite3"

    @contextlib.contextmanager
    def connect():
        db = sqlite3.connect(path)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def unobserved(*args):
        raise AssertionError("Schema migration must not inspect vault files")

    store = SimpleNamespace(connect=connect, observed_path=unobserved)
    schema.initialize(store)
    return store


def snapshot(store):
    with store.connect() as db:
        names = [row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        return {name: [tuple(row) for row in db.execute('SELECT * FROM "' + name + '"')] for name in names}


def seed_schema8(database):
    """Retain the actual older table shapes, including durable ledger rows."""
    with database.connect() as db:
        db.execute("DROP TABLE filesystem_inbox")
        db.execute("DROP TABLE filesystem_state")
        db.execute("DELETE FROM wiki_schema WHERE version=9")
        db.execute("INSERT INTO documents VALUES('doc','note.md','revision','Note','md','Source body','[]','good','{}','signature')")
        db.execute("INSERT INTO document_identities VALUES('doc','note.md','inode-key')")
        db.execute("INSERT INTO managed_notes VALUES('note.md','base','revision','[]','{}')")
        db.execute("INSERT INTO uid_aliases VALUES('urn:uuid:registered','doc')")
        db.execute("INSERT INTO changes VALUES('change','note.md','old','revision','human','keep history','applied','old-time')")
        db.execute("INSERT INTO change_management VALUES('change',NULL,'{}')")
        db.execute("INSERT INTO change_retention VALUES('change','doc',1,1,NULL)")
        db.execute("INSERT INTO material_origins VALUES('doc','{\"opaque\":true}')")
        db.execute("INSERT INTO change_material_origins VALUES('change',NULL,'{\"opaque\":true}')")
        db.execute("INSERT INTO operation_receipts VALUES('receipt','job','doc','doc','note.md','old','revision','organize','retain','change','old-time','{}')")
    return snapshot(database)


def seed_pending(database):
    with database.connect() as db:
        db.execute("INSERT INTO filesystem_state VALUES(1,1,'/synthetic/root','vault',1,0,NULL,NULL)")
        db.execute("INSERT INTO filesystem_inbox VALUES(1,'batch','epoch','generation','{\"ordered\":[\"delete\"]}','immutable-hash','pending','old-time')")


def test_schema8_migration_is_additive_idempotent_and_does_not_bind_a_vault(database):
    before = seed_schema8(database)
    schema.initialize(database)
    first = snapshot(database)
    assert first["filesystem_state"] == []
    assert first["filesystem_inbox"] == []
    assert first["wiki_schema"][:-1] == before["wiki_schema"]
    assert first["wiki_schema"][-1][0] == 9
    assert {name: first[name] for name in before if name != "wiki_schema"} == {
        name: rows for name, rows in before.items() if name != "wiki_schema"
    }
    schema.initialize(database)
    assert snapshot(database) == first


def test_existing_event_tables_are_not_recreated_when_migration_marker_is_absent(database):
    seed_pending(database)
    with database.connect() as db:
        db.execute("DELETE FROM wiki_schema WHERE version=9")
    before = snapshot(database)
    schema.initialize(database)
    after = snapshot(database)
    assert after["filesystem_state"] == before["filesystem_state"]
    assert after["filesystem_inbox"] == before["filesystem_inbox"]
    assert after["wiki_schema"][-1][0] == 9


@pytest.mark.parametrize("version", [8, 9])
@pytest.mark.parametrize("missing", ["filesystem_state", "filesystem_inbox"])
def test_initialized_schema_does_not_rebuild_a_lost_event_ledger(database, missing, version):
    seed_pending(database)
    with database.connect() as db:
        if version == 8:
            db.execute("DELETE FROM wiki_schema WHERE version=9")
        db.execute("DROP TABLE " + missing)
    before = snapshot(database)
    with pytest.raises(WikiError, match="Filesystem-event ledger.*retained") as error:
        schema.initialize(database)
    assert error.value.status == 503
    assert snapshot(database) == before


@pytest.mark.parametrize("version", [8, 9])
@pytest.mark.parametrize("broken", ["column", "generated", "unique", "singleton", "status"])
def test_incompatible_existing_event_shape_is_retained_and_rejected(database, version, broken):
    with database.connect() as db:
        if version == 8:
            db.execute("DELETE FROM wiki_schema WHERE version=9")
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
    with pytest.raises(WikiError, match="Filesystem-event ledger.*retained") as error:
        schema.initialize(database)
    assert error.value.status == 503
    assert snapshot(database) == before


def test_derived_search_repair_preserves_pending_events_and_old_durable_rows(database):
    seed_schema8(database)
    schema.initialize(database)
    seed_pending(database)
    with database.connect() as db:
        db.execute("DROP TABLE search_fts")
    before = snapshot(database)
    schema.initialize(database)
    after = snapshot(database)
    for table in ("filesystem_state", "filesystem_inbox", "document_identities", "managed_notes", "changes", "operation_receipts", "material_origins"):
        assert after[table] == before[table]
    assert "search_fts" in after


def test_schema8_framework_guard_refuses_schema9_without_touching_pending(database, monkeypatch):
    seed_pending(database)
    before = snapshot(database)
    # Exercise the real initializer's max-version guard with the old release's
    # supported version; do not reproduce a second copy of its implementation.
    monkeypatch.setattr(schema, "VERSION", 8)
    with pytest.raises(WikiError, match="newer than this framework.*retained") as error:
        schema.initialize(database)
    assert error.value.status == 503
    assert snapshot(database) == before


def test_schema9_migration_failure_rolls_back_new_tables_and_marker(database):
    before = seed_schema8(database)

    @contextlib.contextmanager
    def interrupted():
        with database.connect() as db:
            db.set_authorizer(lambda action, table, *args: sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_INSERT and table == "wiki_schema" else sqlite3.SQLITE_OK)
            yield db

    with pytest.raises(sqlite3.DatabaseError):
        schema.initialize(SimpleNamespace(connect=interrupted))
    assert snapshot(database) == before
