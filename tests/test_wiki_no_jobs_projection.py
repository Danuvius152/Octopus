"""Disposable job projections cannot prove that a missing job authority existed."""

import sqlite3
from contextlib import closing

import pytest

from server.wiki.store import WikiStore
from tests.test_wiki_format_admission import registered_account as registered_account
from tests.test_wiki_format_preparation import facts, retained_baseline


@pytest.mark.parametrize("damage", ["missing_job_column", "stale_members"])
async def test_no_jobs_baseline_repairs_known_projection_without_inventing_durable_history(registered_account, damage):
    root, state, registration, owner = registered_account
    retained_baseline(root, state, registration, owner, with_jobs=False)
    path = state / "index.sqlite3"
    with closing(sqlite3.connect(path)) as db:
        original = facts(db)
        nonce = db.execute("SELECT rebuild_nonce FROM maintenance_input_state").fetchone()[0]
        assert db.execute("SELECT 1 FROM sqlite_schema WHERE name='jobs'").fetchone() is None
        if damage == "missing_job_column":
            db.execute("DROP TABLE maintenance_job_items")
            db.execute("CREATE TABLE maintenance_job_items(ordinal INTEGER PRIMARY KEY)")
        else:
            identifier = db.execute("SELECT id FROM documents WHERE path='source.md'").fetchone()[0]
            db.execute("INSERT INTO maintenance_job_members VALUES('stale-derived-job',?)", (identifier,))
        db.commit()
    config_bytes = (state / "config.json").read_bytes()
    body_bytes = (root / "source.md").read_bytes()
    with closing(WikiStore(root, state, registration["vault_id"], defer_recovery=True)) as store:
        with store.connect() as db:
            assert db.execute("SELECT max(version) FROM wiki_schema").fetchone()[0] == 13
            after = facts(db)
            for name, rows in original.items():
                expected = sorted([*rows, ("maintenance_jobs", 1)], key=repr) if name == "wiki_protocols" else rows
                assert after[name] == expected
            assert db.execute("SELECT rebuild_nonce FROM maintenance_input_state").fetchone()[0] != nonce
            for table in ("jobs", "job_requests", "maintenance_job_items", "maintenance_job_members"):
                assert db.execute('SELECT count(*) FROM "' + table + '"').fetchone()[0] == 0
    assert (state / "config.json").read_bytes() == config_bytes
    assert (root / "source.md").read_bytes() == body_bytes
