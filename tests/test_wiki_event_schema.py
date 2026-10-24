"""Supported preparation retains event authority and installs jobs atomically."""


import pytest

from server.config import settings
from server.storage_verification import verify_storage
from server.wiki import schema
from server.wiki.store import WikiError, WikiStore
from tests.test_wiki_format_admission import registered_account as registered_account


@pytest.fixture
async def database(registered_account):
    root, state, registration, _ = registered_account
    store = WikiStore(root, state, registration["vault_id"], defer_recovery=True)
    try:
        yield store
    finally:
        store.close()


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
    with pytest.raises((WikiError, RuntimeError)):
        verify_storage(settings)
    assert snapshot(database) == before


@pytest.mark.parametrize("missing", ["filesystem_state", "filesystem_inbox"])
def test_initialized_schema_does_not_rebuild_a_lost_event_ledger(database, missing):
    seed_pending(database)
    with database.connect() as db:
        db.execute("DROP TABLE " + missing)
    before = snapshot(database)
    retained = database.db_path.read_bytes()
    with pytest.raises((WikiError, RuntimeError)):
        verify_storage(settings)
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
    with pytest.raises((WikiError, RuntimeError)):
        verify_storage(settings)
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


@pytest.mark.parametrize("version", [0, 14, 999])
def test_unsupported_format_retains_pending_events_without_backfill(database, version):
    seed_pending(database)
    with database.connect() as db:
        db.execute(f"PRAGMA user_version={version}")
    before = snapshot(database)
    retained = database.db_path.read_bytes()
    with pytest.raises((WikiError, RuntimeError)):
        verify_storage(settings)
    assert snapshot(database) == before
    assert database.db_path.read_bytes() == retained
