"""Main-format equivalence must preserve actual SQLite write semantics."""

import json
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path

import pytest

from server.database import Database
from server.wiki import job_notifications
from tests.main_storage import write_unversioned_baseline
from tests.test_wiki_job_notifications import (
    client as client,
)
from tests.test_wiki_job_notifications import (
    enqueue,
    pending,
    session,
    subscribe,
)
from tests.test_wiki_job_notifications import (
    jobs as jobs,
)
from tests.test_wiki_job_notifications import (
    receiver as receiver,
)

ROOT = Path(__file__).resolve().parents[1]


async def test_unversioned_test_baseline_cannot_discard_an_unrecognized_table(tmp_path):
    path = tmp_path / "unknown-fixture.db"
    database = Database(str(path))
    await database.initialize()
    await database.close()
    with closing(sqlite3.connect(path)) as seed:
        seed.execute("CREATE TABLE sqlitex_external(original TEXT)")
        seed.execute("INSERT INTO sqlitex_external VALUES('Keep this unknown fact')")
        seed.commit()
    original = path.read_bytes()
    with pytest.raises(AssertionError):
        write_unversioned_baseline(path)
    assert path.read_bytes() == original


@pytest.mark.parametrize("version", [0, 1])
@pytest.mark.parametrize("damage", ["quoted_primary_key", "quoted_default", "missing_unique"])
def test_constraint_keywords_cannot_be_replaced_by_quoted_type_or_default_tokens(tmp_path, version, damage):
    path = tmp_path / "keywords.db"
    script = (ROOT / "tests/fixtures/main-baseline-v0.sql").read_text()
    if damage == "quoted_primary_key":
        damaged = script.replace("id TEXT PRIMARY KEY,", 'id TEXT "PRIMARY" "KEY",', 1)
    elif damage == "quoted_default":
        damaged = script.replace("DEFAULT 'active'", '''DEFAULT "'active'"''', 1)
    else:
        damaged = script.replace("username TEXT NOT NULL UNIQUE COLLATE NOCASE", "username TEXT NOT NULL COLLATE NOCASE")
    assert damaged != script
    with closing(sqlite3.connect(path)) as seed:
        seed.executescript(damaged)
        if version == 1:
            seed.execute("DROP TABLE schema_migrations")
            seed.execute("PRAGMA user_version=1")
        seed.commit()
        # Check the engine's interpretation independently of our comparator.
        if damage == "quoted_primary_key":
            column = next(row for row in seed.execute("PRAGMA table_info(agents)") if row[1] == "id")
            assert column[2] == 'TEXT "PRIMARY" "KEY"' and column[5] == 0
        elif damage == "quoted_default":
            column = next(row for row in seed.execute("PRAGMA table_info(backend_credentials)") if row[1] == "status")
            assert column[4] == '''"'active'"'''
        else:
            assert not any(row[3] == "u" for row in seed.execute("PRAGMA index_list(users)"))
    original = path.read_bytes()
    result = subprocess.run([sys.executable, "-c", """
import asyncio,sys
from server.database import Database
async def main():
    database=Database(sys.argv[1])
    await database.initialize()
    await database.close()
asyncio.run(main())
""", str(path)], cwd=ROOT, capture_output=True, text=True, timeout=15)
    assert result.returncode == 70, result.stdout + result.stderr
    assert path.read_bytes() == original


@pytest.mark.parametrize("version", [0, 1])
async def test_reordered_notice_columns_preserve_cards_and_exact_replay(jobs, receiver, monkeypatch, version):
    job = enqueue(jobs)
    await session(receiver.db, owner=jobs.user_id)
    subscribe(jobs, job)
    path = Path(receiver.db._db_path)
    await receiver.db.close()
    if version == 0:
        write_unversioned_baseline(path)
    with closing(sqlite3.connect(path)) as seed:
        original = seed.execute("SELECT sql FROM sqlite_schema WHERE name='wiki_job_notifications'").fetchone()[0]
        reordered = original.replace("target TEXT NOT NULL,\n    request_id TEXT NOT NULL,",
                                     "request_id TEXT NOT NULL,\n    target TEXT NOT NULL,")
        assert reordered != original
        indexes = [row[0] for row in seed.execute(
            "SELECT sql FROM sqlite_schema WHERE type='index' AND tbl_name='wiki_job_notifications' AND sql IS NOT NULL",
        )]
        assert seed.execute("SELECT count(*) FROM wiki_job_notifications").fetchone() == (0,)
        seed.execute("DROP TABLE wiki_job_notifications")
        seed.execute(reordered)
        for sql in indexes:
            seed.execute(sql)
        seed.commit()
    receiver.db = Database(str(path))
    await receiver.db.initialize()
    acknowledge = job_notifications._acknowledge

    def lost_ack(*args):
        raise OSError("ACK lost after receipt commit")

    monkeypatch.setattr(job_notifications, "_acknowledge", lost_ack)
    with pytest.raises(OSError, match="ACK lost"):
        await job_notifications.dispatch(jobs, receiver)
    assert len(pending(jobs)) == 1
    cards = await receiver.db.wiki_job_cards("original", jobs.user_id)
    assert len(cards) == 1
    assert (cards[0]["job_id"], cards[0]["origin_request_id"]) == (job["id"], "original-request")
    await receiver.db.close()
    receiver.db = Database(str(path))
    await receiver.db.initialize()
    monkeypatch.setattr(job_notifications, "_acknowledge", acknowledge)
    assert await job_notifications.dispatch(jobs, receiver) == 1
    assert pending(jobs) == []
    assert await receiver.db.wiki_job_cards("original", jobs.user_id) == cards
    async with receiver.db.transaction(write=False) as tx:
        assert await tx.fetch_one("SELECT count(*) FROM wiki_job_notifications") == (1,)
        receipt = await tx.fetch_one("SELECT target,request_id,body FROM wiki_job_notifications")
    assert receipt[:2] == ("original", "original-request")
    assert json.loads(receipt[2])["job_id"] == job["id"]
