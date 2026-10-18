"""Schema 10 work is adopted without fabricating requesters or recording proof."""

import contextlib
import sqlite3
from types import SimpleNamespace

import pytest

from server.wiki import job_events, schema
from server.wiki.jobs import WikiJobs
from server.wiki.store import WikiError
from tests.test_wiki_event_schema import database as database
from tests.wiki_schema_fixtures import remove_future_job_events


def old_jobs(database):
    with database.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        WikiJobs.initialize_ledger(db)
        remove_future_job_events(db)
        for status in ("queued", "running", "completed", "cancelled"):
            db.execute("INSERT INTO jobs VALUES(?,?,?,?,?,?,NULL,?,'old-time','old-time')",
                       (status, status, "retained-hash", '{"retained_input":true}', status, "[]", "old-session"))
    with database.connect() as db:
        return [tuple(row) for row in db.execute("SELECT * FROM jobs ORDER BY id")]


def test_schema10_adoption_preserves_work_and_only_knows_proven_cancellation(database):
    before = old_jobs(database)
    schema.initialize(database)
    with database.connect() as db:
        assert [tuple(row) for row in db.execute("SELECT * FROM jobs ORDER BY id")] == before
        assert [tuple(row) for row in db.execute("SELECT job_id,attempt,settlement_phase FROM wiki_job_state ORDER BY job_id")] == [
            ("cancelled", 1, "unconfirmed"), ("completed", 1, "none"), ("queued", 0, "none"), ("running", 1, "none"),
        ]
        assert db.execute("SELECT count(*) FROM wiki_job_outbox").fetchone()[0] == 4
        assert db.execute("SELECT count(*) FROM wiki_job_subscribers").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM wiki_job_deliveries").fetchone()[0] == 0
    schema.initialize(database)
    with database.connect() as db:
        assert db.execute("SELECT count(*) FROM wiki_job_outbox").fetchone()[0] == 4


@pytest.mark.parametrize("missing", [*job_events.TABLES, "marker", "guard"])
def test_lost_initialized_notifications_are_not_recreated_during_index_repair(database, missing):
    old_jobs(database)
    schema.initialize(database)
    with database.connect() as db:
        db.execute("DROP TABLE search_fts")
        if missing == "marker":
            db.execute("DELETE FROM wiki_protocols WHERE name='wiki_job_events'")
        elif missing == "guard":
            db.execute("DROP TRIGGER wiki_job_insert_state")
        else:
            db.execute("DROP TABLE " + missing)
    with pytest.raises(WikiError) as error:
        schema.initialize(database)
    assert error.value.status == 503
    with database.connect() as db:
        assert db.execute("SELECT count(*) FROM jobs").fetchone()[0] == 4
        assert db.execute("SELECT 1 FROM sqlite_master WHERE name='search_fts'").fetchone() is None


def test_failed_job_state_migration_rolls_back_protocol_and_old_work(database):
    before = old_jobs(database)

    @contextlib.contextmanager
    def interrupted():
        with database.connect() as db:
            db.set_authorizer(lambda action, table, *args: sqlite3.SQLITE_DENY
                              if action == sqlite3.SQLITE_INSERT and table == "wiki_job_outbox" else sqlite3.SQLITE_OK)
            yield db

    with pytest.raises(sqlite3.DatabaseError):
        schema.initialize(SimpleNamespace(connect=interrupted))
    with database.connect() as db:
        assert [tuple(row) for row in db.execute("SELECT * FROM jobs ORDER BY id")] == before
        assert db.execute("SELECT 1 FROM wiki_protocols WHERE name='wiki_job_events'").fetchone() is None
        assert db.execute("SELECT 1 FROM wiki_schema WHERE version=11").fetchone() is None
        assert not any(db.execute("SELECT 1 FROM sqlite_master WHERE name=?", (table,)).fetchone() for table in job_events.TABLES)
