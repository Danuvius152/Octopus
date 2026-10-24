"""Current notification replay preserves named column semantics."""

import json
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from server.database import Database
from server.wiki import job_notifications
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






async def test_reordered_notice_columns_preserve_cards_and_exact_replay(jobs, receiver, monkeypatch):
    job = enqueue(jobs)
    await session(receiver.db, owner=jobs.user_id)
    subscribe(jobs, job)
    path = Path(receiver.db._db_path)
    await receiver.db.close()
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
