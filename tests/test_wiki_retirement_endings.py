"""Old task retirement distinguishes verified endings from unknown effects."""

import json
import sqlite3
from contextlib import closing

import pytest

from server.wiki.store import WikiStore
from tests.test_wiki_format_admission import registered_account as registered_account
from tests.test_wiki_retirement_boundaries import old_history as old_history
from tests.test_wiki_retirement_eligibility import insert_job, original_execution


@pytest.mark.parametrize("job_status,item_status,terminal,recording,retired,removed", [
    ("failed", "failed", "failed", "committed", True, True),
    ("completed_with_attention", "excluded", "failed", "committed", True, True),
    ("cancelled", "done", "failed", "committed", True, True),
    ("failed", "failed", "failed", "unconfirmed", True, False),
    ("failed", "failed", "failed", "committed", False, False),
    ("cancelled", "done", "failed", "committed", False, False),
    ("completed_with_attention", "attention", "success", "committed", True, False),
])
async def test_only_fully_verified_old_endings_retire(old_history, job_status, item_status, terminal, recording, retired, removed):
    root, state, registration, _, articles = old_history
    identifier = "verified-ending"
    reference = original_execution(identifier) | {"recording_state": recording, "retired": retired}
    reference["terminal"] = reference["terminal"] | {"status": terminal}
    with closing(sqlite3.connect(state / "index.sqlite3")) as db:
        db.execute("PRAGMA recursive_triggers=ON")
        insert_job(db, articles[0], identifier, execution=reference, job_status=job_status, item_status=item_status)
        db.execute("UPDATE jobs SET session_id=NULL WHERE id=?", (identifier,))
        if job_status == "cancelled":
            db.execute("UPDATE wiki_job_state SET state_version=state_version+1,settlement_phase='confirmed' WHERE job_id=?", (identifier,))
        db.commit()
        receipts = list(db.execute("SELECT * FROM operation_receipts WHERE job_id=?", (identifier,)))
    with closing(WikiStore(root, state, registration["vault_id"], defer_recovery=True)) as current:
        with current.connect() as db:
            assert (db.execute("SELECT 1 FROM jobs WHERE id=?", (identifier,)).fetchone() is None) is removed
            columns = [row[1] for row in db.execute("PRAGMA table_info(operation_receipts)") if row[1] != "item_match_sequence"]
            assert list(map(tuple, db.execute("SELECT " + ",".join(columns) + " FROM operation_receipts WHERE job_id=?", (identifier,)))) == receipts
            if not removed:
                retained = json.loads(db.execute("SELECT items FROM jobs WHERE id=?", (identifier,)).fetchone()[0])[0]
                assert retained["version"] == 1 and retained["worker_request"] == reference


async def test_confirmed_never_admitted_old_attempt_zero_retires_without_fabricated_execution(old_history):
    root, state, registration, _, articles = old_history
    identifier = "never-admitted"
    with closing(sqlite3.connect(state / "index.sqlite3")) as db:
        db.execute("PRAGMA recursive_triggers=ON")
        item = insert_job(db, articles[0], identifier, job_status="queued", item_status="pending")
        item["worker_request"] = {"attempt": 0, "phase": "rejected", "disposition": "not_admitted"}
        db.execute("UPDATE jobs SET status='cancelled',items=?,session_id=NULL WHERE id=?", (json.dumps([item]), identifier))
        db.execute("UPDATE wiki_job_state SET state_version=state_version+1,settlement_phase='confirmed' WHERE job_id=?", (identifier,))
        assert db.execute("SELECT attempt FROM wiki_job_state WHERE job_id=?", (identifier,)).fetchone()[0] == 0
        assert not db.execute("SELECT 1 FROM operation_receipts WHERE job_id=?", (identifier,)).fetchone()
        db.commit()
    with closing(WikiStore(root, state, registration["vault_id"], defer_recovery=True)) as current:
        with current.connect() as db:
            assert db.execute("SELECT 1 FROM jobs WHERE id=?", (identifier,)).fetchone() is None
            assert not db.execute("SELECT 1 FROM operation_receipts WHERE job_id=?", (identifier,)).fetchone()
