"""Original-entry delivery across the real Wiki and main database commits."""

import json
import sqlite3
from contextlib import closing
from types import SimpleNamespace

import pytest

from server.database import Database
from server.mcp_identity import mint
from server.wiki import job_events, job_notifications
from server.wiki.history import WikiHistory
from server.wiki.store import WikiError
from server.wiki.tasks import tasks
from tests import test_wiki_api as wiki_api
from tests.test_wiki_job_events import enqueue
from tests.test_wiki_jobs import client as client
from tests.test_wiki_jobs import jobs as jobs


@pytest.fixture
async def receiver(tmp_path, jobs):
    path = tmp_path / "main.db"
    # The second receiver is a cold copy of the real current account, not a
    # second owner or an account-free database containing somebody else's rows.
    with closing(sqlite3.connect(path, check_same_thread=False)) as target:
        await wiki_api.session_manager.db.conn.backup(target)
    database = Database(str(path))
    await database.initialize()
    frames = []

    async def broadcast(event, *, user_id):
        frames.append((event, user_id))

    manager = SimpleNamespace(db=database, _broadcast=broadcast, frames=frames, get_session=lambda _sid: None,
                              _wiki_dispatch_locks={})
    try:
        yield manager
    finally:
        await manager.db.close()


async def session(database, sid="original", *, owner):
    await database.save_session(sid, "Original entry", "/isolated", "time", user_id=owner)


def subscribe(jobs, job, sid="original", *, request_id="original-request", client_id=""):
    with jobs.store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        return job_events.subscribe(db, job["id"], job_events.Requester(jobs.user_id, "chat", sid, request_id, client_id))


def pending(jobs):
    with jobs.store.connect() as db:
        return job_events.pending(db)


async def test_main_commit_precedes_ack_and_replay_survives_receiver_restart(jobs, receiver, monkeypatch):
    job = enqueue(jobs)
    await session(receiver.db, owner=jobs.user_id)
    subscribe(jobs, job)
    original_ack = job_notifications._acknowledge

    def lost_ack(*args):
        raise OSError("injected Wiki ACK failure after main commit")

    monkeypatch.setattr(job_notifications, "_acknowledge", lost_ack)
    with pytest.raises(OSError, match="ACK failure"):
        await job_notifications.dispatch(jobs, receiver)
    assert len(pending(jobs)) == 1
    assert (await receiver.db.wiki_job_cards("original", jobs.user_id))[0]["job_id"] == job["id"]
    assert len(receiver.frames) == 1
    path = receiver.db._db_path
    await receiver.db.close()
    receiver.db = Database(path)
    await receiver.db.initialize()
    monkeypatch.setattr(job_notifications, "_acknowledge", original_ack)
    assert await job_notifications.dispatch(jobs, receiver) == 1
    assert pending(jobs) == []
    cursor = await receiver.db.conn.execute("SELECT count(*) FROM wiki_job_notifications")
    assert (await cursor.fetchone())[0] == 1
    assert len(await receiver.db.wiki_job_cards("original", jobs.user_id)) == 1


async def test_before_main_commit_failure_keeps_original_delivery_pending(jobs, receiver, monkeypatch):
    job = enqueue(jobs)
    await session(receiver.db, owner=jobs.user_id)
    subscribe(jobs, job)
    receive = receiver.db.receive_wiki_job_notice

    async def unavailable(*args):
        raise OSError("main receive not committed")

    monkeypatch.setattr(receiver.db, "receive_wiki_job_notice", unavailable)
    with pytest.raises(OSError, match="not committed"):
        await job_notifications.dispatch(jobs, receiver)
    assert len(pending(jobs)) == 1
    assert await receiver.db.wiki_job_cards("original", jobs.user_id) == []
    assert receiver.frames == []
    monkeypatch.setattr(receiver.db, "receive_wiki_job_notice", receive)
    await job_notifications.dispatch(jobs, receiver)
    assert len(await receiver.db.wiki_job_cards("original", jobs.user_id)) == 1


async def test_old_version_cannot_overwrite_completed_card_and_archive_keeps_it(jobs, receiver):
    job = enqueue(jobs)
    await session(receiver.db, owner=jobs.user_id)
    subscribe(jobs, job)
    jobs.update(job["id"], status="completed")
    deliveries = pending(jobs)
    assert len(deliveries) == 2
    vault = jobs.registration["vault_id"]
    for delivery in reversed(deliveries):
        await receiver.db.receive_wiki_job_notice(vault, jobs.user_id, delivery)
    cards = await receiver.db.wiki_job_cards("original", jobs.user_id)
    assert len(cards) == 1 and cards[0]["status"] == "completed"
    await receiver.db.conn.execute("UPDATE sessions SET archived=1 WHERE id='original'")
    await receiver.db.conn.commit()
    assert await receiver.db.wiki_job_cards("original", jobs.user_id) == cards


async def test_deleted_or_different_owner_entry_is_not_retargeted(jobs, receiver):
    job = enqueue(jobs)
    await session(receiver.db, owner="different-owner")
    subscribe(jobs, job)
    await job_notifications.dispatch(jobs, receiver)
    assert receiver.frames == []
    assert await receiver.db.wiki_job_cards("original", "different-owner") == []
    cursor = await receiver.db.conn.execute("SELECT outcome FROM wiki_job_notifications")
    assert (await cursor.fetchone())[0] == "unavailable"
    assert pending(jobs) == []
    assert jobs.get(job["id"])["id"] == job["id"]
    # An absent original entry also receives a durable unavailable receipt.
    subscribe(jobs, job, "deleted")
    await job_notifications.dispatch(jobs, receiver)
    assert receiver.frames == [] and pending(jobs) == []


async def test_duplicate_receive_rejects_conflicting_snapshot_and_wrong_account(jobs, receiver):
    job = enqueue(jobs)
    await session(receiver.db, owner=jobs.user_id)
    subscribe(jobs, job)
    delivery = pending(jobs)[0]
    vault = jobs.registration["vault_id"]
    await receiver.db.receive_wiki_job_notice(vault, jobs.user_id, delivery)
    tampered = json.loads(delivery["body"]) | {"status": "completed"}
    with pytest.raises(WikiError, match="conflicts"):
        await receiver.db.receive_wiki_job_notice(vault, jobs.user_id, delivery | {"body": json.dumps(tampered)})
    with pytest.raises(WikiError, match="account"):
        await receiver.db.receive_wiki_job_notice(vault, "different-owner", delivery)
    assert (await receiver.db.wiki_job_cards("original", jobs.user_id))[0]["status"] == "queued"


async def test_original_request_is_captured_at_open_and_http_replay_subscribes_new_entry(jobs, client, tmp_path):
    WikiHistory(jobs.store).write("Library/note.md", b"# Note\nKeep this claim.\n", None, "human", "create")
    manager = wiki_api.session_manager
    sessions = [await manager.create_session(jobs.registration["curator_agent_id"], name, str(tmp_path))
                for name in ("Original", "Following")]
    tokens = [tasks.open(value, "auto", request_id=f"original-{i}") for i, value in enumerate(sessions)]
    headers = [{"Authorization": f"Bearer {mint(value.id, user_id=value.user_id, wiki_task_id=token)}"}
               for value, token in zip(sessions, tokens, strict=True)]
    body = {"idempotency_key": "same-original-job", "paths": ["Library/note.md"]}
    try:
        first = await client.post("/api/wiki/maintain", headers=headers[0], json=body)
        assert first.status_code == 200, first.text
        job = jobs.update(first.json()["id"], status="completed")
        await job_notifications.dispatch(jobs, manager)
        second = await client.post("/api/wiki/maintain", headers=headers[1], json=body)
        assert second.status_code == 200, second.text
        assert second.json()["id"] == job["id"]
        assert second.json()["state_version"] == job["state_version"]
        assert len(pending(jobs)) == 1
        assert pending(jobs)[0]["request_id"] == "original-1"
        await job_notifications.dispatch(jobs, manager)
        cards = await client.get(f"/api/sessions/{sessions[1].id}/wiki-jobs", headers=client.headers)
        assert cards.status_code == 200, cards.text
        assert len(cards.json()) == 1
        assert cards.json()[0]["status"] == "completed"
        assert cards.json()[0]["origin_request_id"] == "original-1"
        assert tasks.active[tokens[1]].request_id == "original-1"
    finally:
        for token in tokens:
            tasks.close(token)


async def test_material_alias_and_completed_reuse_keep_original_subscriptions(jobs, receiver):
    await session(receiver.db, owner=jobs.user_id)
    material = "# Claim\nKeep the qualification.\n"
    origin = job_events.Requester(jobs.user_id, "chat", "original", "original-request")
    first = jobs.maintain("first", material, "Review", [], None, [], "human", requester=origin)
    aliased = jobs.maintain("alias", material, "Review", [], None, [], "human", requester=origin)
    assert first["id"] == aliased["id"]
    with jobs.store.connect() as db:
        assert db.execute("SELECT count(*) FROM wiki_job_subscribers WHERE job_id=?", (first["id"],)).fetchone()[0] == 1
    jobs.update(first["id"], status="cancelled")
    WikiHistory(jobs.store).write("Library/existing.md", material.encode(), None, "human", "known material")
    reused = jobs.maintain("reuse", material, "Review", [], None, [], "human", requester=origin)
    assert reused["status"] == "completed" and reused["items"][0]["reused"]
    await job_notifications.dispatch(jobs, receiver)
    assert {card["job_id"] for card in await receiver.db.wiki_job_cards("original", jobs.user_id)} == {first["id"], reused["id"]}
