"""Current notification facts survive supported preparation; older formats refuse."""

import sqlite3
from contextlib import closing

import pytest

from server.config import settings
from server.storage_verification import verify_storage
from server.wiki import job_events
from server.wiki.store import WikiError
from tests.test_wiki_format_admission import registered_account as registered_account
from tests.wiki_current_storage import facts


@pytest.fixture
async def baseline(registered_account):
    root, state, registration, owner = registered_account
    return root, state, registration


@pytest.mark.parametrize("missing", [*job_events.TABLES, "marker", "guard"])
async def test_lost_initialized_notifications_are_not_recreated_during_index_repair(baseline, missing):
    root, state, registration = baseline
    path = state / "index.sqlite3"
    with closing(sqlite3.connect(path)) as db:
        original_jobs = list(db.execute("SELECT * FROM jobs ORDER BY id"))
        db.execute("DROP TABLE search_fts")
        if missing == "marker":
            db.execute("DELETE FROM wiki_protocols WHERE name='wiki_job_events'")
        elif missing == "guard":
            db.execute("DROP TRIGGER wiki_job_insert_state")
        else:
            db.execute("DROP TABLE " + missing)
        db.commit()
    retained = path.read_bytes()
    with pytest.raises((WikiError, RuntimeError)):
        verify_storage(settings)
    assert path.read_bytes() == retained
    with closing(sqlite3.connect(path)) as db:
        assert list(db.execute("SELECT * FROM jobs ORDER BY id")) == original_jobs
        assert db.execute("SELECT 1 FROM sqlite_schema WHERE name='search_fts'").fetchone() is None


async def test_changed_current_emitter_is_rejected_before_any_derived_repair(baseline):
    root, state, registration = baseline
    path = state / "index.sqlite3"
    with closing(sqlite3.connect(path)) as db:
        declaration = db.execute("SELECT sql FROM sqlite_schema WHERE name='wiki_job_state_emit_insert'").fetchone()[0]
        assert "'progress'" in declaration
        db.execute("DROP TRIGGER wiki_job_state_emit_insert")
        db.execute(declaration.replace("'progress'", "'untrusted_progress'"))
        db.execute("DROP TABLE maintenance_projection")
        db.commit()
        original = facts(db)
    retained, config = path.read_bytes(), (state / "config.json").read_bytes()
    body = (root / "source.md").read_bytes()
    with pytest.raises((WikiError, RuntimeError)):
        verify_storage(settings)
    assert path.read_bytes() == retained
    assert (state / "config.json").read_bytes() == config
    assert (root / "source.md").read_bytes() == body
    with closing(sqlite3.connect(path)) as db:
        assert facts(db) == original
        assert db.execute("SELECT 1 FROM sqlite_schema WHERE name='maintenance_projection'").fetchone() is None
