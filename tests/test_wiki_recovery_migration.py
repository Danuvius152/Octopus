"""An unreleased recovery protocol installs atomically over real schema 10."""

import contextlib
import sqlite3
from types import SimpleNamespace

import pytest

from server.wiki import recovery_state, schema
from server.wiki.store import WikiError
from tests.test_wiki_job_event_migration import database as database
from tests.test_wiki_job_event_migration import old_jobs


def test_failed_recovery_migration_keeps_old_jobs_and_both_protocols_uninstalled(database):
    before = old_jobs(database)

    @contextlib.contextmanager
    def interrupted():
        with database.connect() as db:
            db.set_authorizer(lambda action, table, *args: sqlite3.SQLITE_DENY
                              if action == sqlite3.SQLITE_CREATE_TABLE and table == "filesystem_recovery_audit"
                              else sqlite3.SQLITE_OK)
            yield db

    with pytest.raises(sqlite3.DatabaseError):
        schema.initialize(SimpleNamespace(connect=interrupted))
    with database.connect() as db:
        assert [tuple(row) for row in db.execute("SELECT * FROM jobs ORDER BY id")] == before
        assert db.execute("SELECT 1 FROM wiki_schema WHERE version=11").fetchone() is None
        assert not db.execute("SELECT 1 FROM wiki_protocols WHERE name IN ('filesystem_recovery','wiki_job_events')").fetchone()
    schema.initialize(database)
    with database.connect() as db:
        recovery_state.require(db)
        assert not db.execute("SELECT * FROM filesystem_recoveries").fetchone()
        assert [tuple(row) for row in db.execute("SELECT * FROM jobs ORDER BY id")] == before


@pytest.mark.parametrize("loss", [*recovery_state.TABLES, "guard", "protocol"])
def test_index_repair_does_not_recreate_lost_recovery_facts(database, loss):
    before = old_jobs(database)
    schema.initialize(database)
    with database.connect() as db:
        db.execute("DROP TABLE search_fts")
        if loss == "guard":
            db.execute("DROP TRIGGER filesystem_recovery_update_guard")
        elif loss == "protocol":
            db.execute("DELETE FROM wiki_protocols WHERE name='filesystem_recovery'")
        else:
            db.execute("DROP TABLE " + loss)
    with pytest.raises(WikiError) as failed:
        schema.initialize(database)
    assert failed.value.status == 503
    with database.connect() as db:
        assert [tuple(row) for row in db.execute("SELECT * FROM jobs ORDER BY id")] == before
        assert db.execute("SELECT 1 FROM sqlite_master WHERE name='search_fts'").fetchone() is None
