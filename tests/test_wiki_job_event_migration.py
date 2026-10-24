"""Current notification facts survive supported preparation; older formats refuse."""

import json
import sqlite3
from contextlib import closing

import pytest

from server.wiki import job_events, job_items, schema
from server.wiki.store import WikiError, WikiStore
from tests.test_wiki_format_admission import registered_account as registered_account
from tests.test_wiki_format_preparation import facts, retained_baseline


@pytest.fixture
async def baseline(registered_account):
    root, state, registration, owner = registered_account
    retained_baseline(root, state, registration, owner, with_jobs=True)
    return root, state, registration


@pytest.mark.parametrize("version", [10, 11])
async def test_prebaseline_notifications_are_retained_without_adoption_or_fabricated_receipts(baseline, version):
    root, state, registration = baseline
    path = state / "index.sqlite3"
    with closing(sqlite3.connect(path)) as db:
        db.execute("DELETE FROM wiki_schema")
        db.execute("INSERT INTO wiki_schema VALUES(?,'original-retained-time')", (version,))
        db.commit()
        original = facts(db)
    database_bytes, config_bytes = path.read_bytes(), (state / "config.json").read_bytes()
    body = (root / "source.md").read_bytes()
    for _ in range(2):
        with pytest.raises(WikiError) as refused:
            WikiStore(root, state, registration["vault_id"], defer_recovery=True)
        assert refused.value.status == 503
        assert path.read_bytes() == database_bytes
        assert (state / "config.json").read_bytes() == config_bytes
        assert (root / "source.md").read_bytes() == body
        with closing(sqlite3.connect(path)) as db:
            assert facts(db) == original


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
    with pytest.raises(WikiError) as refused:
        WikiStore(root, state, registration["vault_id"], defer_recovery=True)
    assert refused.value.status == 503
    assert path.read_bytes() == retained
    with closing(sqlite3.connect(path)) as db:
        assert list(db.execute("SELECT * FROM jobs ORDER BY id")) == original_jobs
        assert db.execute("SELECT 1 FROM sqlite_schema WHERE name='search_fts'").fetchone() is None


async def test_schema12_forward_preparation_keeps_events_and_legacy_item_evidence_on_reentry(baseline):
    root, state, registration = baseline
    path = state / "index.sqlite3"
    legacy = {"doc": "original", "status": "attention", "recording_failure": {
        "attempt": 1, "session_id": "old-session", "request_id": "old-request", "opaque": ["original", 7]},
        "execution_unknown": {"retained": True}}
    raw = json.dumps([legacy])
    with closing(sqlite3.connect(path)) as db:
        db.execute("PRAGMA recursive_triggers=ON")
        db.execute("""INSERT INTO jobs(id,request_key,payload_hash,payload,status,items,session_id,created_at,updated_at)
                      VALUES('old-evidence','old-evidence','hash','{}','cancelled',?,'old-session','old-time','old-time')""", (raw,))
        db.execute("INSERT INTO job_requests VALUES('old-evidence','hash','old-evidence')")
        db.commit()
        original = facts(db)
    with closing(WikiStore(root, state, registration["vault_id"], defer_recovery=True)) as store:
        for _ in range(2):
            schema.initialize(store)
            with store.connect() as db:
                assert facts(db) == original
                assert db.execute("SELECT items FROM jobs WHERE id='old-evidence'").fetchone()[0] == raw
                assert job_events.metadata(db, "old-evidence")["settlement_phase"] == "attention"
                item = job_items.decode(raw)[0]
                assert item["version"] == 1 and item["status"] == "unknown"
                assert item["legacy_evidence"]["recording_failure"] == legacy["recording_failure"]
