"""Reviewer counterexamples preserve original receipts across two databases."""

import asyncio
from types import SimpleNamespace

import pytest

from server.wiki import job_events, job_notifications, registry
from server.wiki.history import WikiHistory
from server.wiki.jobs import WikiJobs, WikiWorker
from server.wiki.store import WikiError
from server.wiki.tasks import tasks
from tests.test_wiki_job_events import enqueue
from tests.test_wiki_job_execution import disk_manager as disk_manager
from tests.test_wiki_job_execution import prepare, record_terminal
from tests.test_wiki_job_notifications import pending, session, subscribe
from tests.test_wiki_job_notifications import receiver as receiver
from tests.test_wiki_jobs import Curator, process, sapphire_delete
from tests.test_wiki_jobs import client as client
from tests.test_wiki_jobs import jobs as jobs


async def test_navigation_receipt_does_not_become_an_unstarted_creation_effect(jobs, client, disk_manager):
    from server.mcp_identity import mint
    from tests.submission import receipt_from_events
    from tests.test_wiki_supporting_navigation import addition

    WikiHistory(jobs.store).write("Library/note.md", b"# Primary\nKeep this argument.\n", None, "human", "source")
    primary = jobs.store.read("Library/note.md")
    WikiHistory(jobs.store).write("Library/index.md", b"---\ntype: Topic\n---\n# Navigation\n\nCompare the primary argument and its conditions.\n", None, "human", "navigation")
    job = jobs.maintain("cancel-navigation-only", "", "Review A and create B", [primary["path"]], None, [], "human",
                        creation={"purpose": "requested_knowledge_page", "paths": ["Library/unstarted.md"]})
    for count, item in enumerate(job["items"], 1):
        item["retry_count"] = count
    job = jobs.update(job["id"], items=job["items"])
    budget = {item["path"]: item["retry_count"] for item in job["items"]}
    entered, release = asyncio.Event(), asyncio.Event()

    class NavigationOnly(Curator):
        async def create_session(self, *args, **kwargs):
            return SimpleNamespace(id="original-worker")

        async def start_message(self, session_id, prompt, **kwargs):
            async def events():
                token = tasks.open(SimpleNamespace(id=session_id, user_id=jobs.user_id, agent_id=jobs.registration["curator_agent_id"]),
                                   "maintain", request_id=kwargs["request_id"])
                try:
                    headers = {"Authorization": "Bearer " + mint(session_id, user_id=jobs.user_id, wiki_task_id=token)}
                    original = (await client.get("/api/wiki/read", headers=headers, params={"doc": primary["id"]})).json()
                    navigation = (await client.get("/api/wiki/read", headers=headers, params={"doc": "Library/index.md"})).json()
                    added = await client.post("/api/wiki/organize", headers=headers, json=addition(original, navigation))
                    assert added.status_code == 200, added.text
                    entered.set()
                    await release.wait()
                    yield {"type": "result", "is_error": False}
                finally:
                    tasks.close(token)

            receipt = await receipt_from_events(events(), kwargs["request_id"])
            await record_terminal(self.db, kwargs["request_id"])
            return receipt

    manager = NavigationOnly(jobs)
    manager.db = disk_manager.db
    worker = WikiWorker()
    worker.manager = manager
    assert jobs.next_queued() == job["id"]
    execution = asyncio.create_task(worker.process(jobs, job["id"]))
    try:
        await asyncio.wait_for(entered.wait(), 3)
        cancelled = await jobs.cancel(job["id"], manager)
        assert cancelled["settlement_phase"] == "pending"
    finally:
        release.set()
        await asyncio.wait_for(execution, 3)
    final = jobs.get(job["id"])
    assert final["settlement_phase"] == "confirmed"
    assert {item["path"]: item["retry_count"] for item in final["items"]} == budget
    creation = next(item for item in final["items"] if item.get("kind") == "creation")
    assert len(final["receipts"]) == 1 and final["receipts"][0]["operation"] == "navigation_link"
    assert final["receipts"][0]["item_doc"] is None and not jobs.store.path(creation["path"]).exists()
    assert final["grant"]["reservations"][0]["state"] == "released"
    navigation_bytes = jobs.store.path("Library/index.md").read_bytes()
    retried = jobs.retry(job["id"])
    assert retried["status"] == "queued" and retried["grant"]["reservations"][0]["state"] == "reserved"
    # An explicit primary reread spends one attempt; re-reserving an
    # unstarted output preserves its budget. Neither count resets to zero.
    assert {item["path"]: item["retry_count"] for item in retried["items"]} == budget | {primary["path"]: 2}
    assert next(item for item in retried["items"] if item.get("kind") == "creation")["reservation_id"] == creation["reservation_id"]
    assert jobs.store.path("Library/index.md").read_bytes() == navigation_bytes


@pytest.mark.parametrize("window", ["acked", "main_committed", "not_received"])
async def test_owned_notice_replays_its_original_subscription_identity(jobs, receiver, window):
    db = receiver.db
    config, owner = jobs.registration, jobs.user_id
    job = enqueue(jobs)
    note = jobs.store.read(job["items"][0]["path"])
    await session(db, owner=owner)
    subscriber = subscribe(jobs, job)
    original = pending(jobs)[0]
    frames = []

    async def broadcast(frame, *, user_id):
        frames.append((frame, user_id))

    manager = SimpleNamespace(db=db, _broadcast=broadcast)
    if window == "acked":
        await job_notifications.dispatch(jobs, manager)
    elif window == "main_committed":
        await db.receive_wiki_job_notice(config["vault_id"], owner, original)
    raw = jobs.store.path(note["path"]).read_bytes()
    current = WikiJobs(owner)
    await job_notifications.dispatch(current, manager)
    cards = await db.wiki_job_cards("original", owner)
    assert len(cards) == 1 and cards[0]["job_id"] == job["id"]
    assert cards[0]["subscriber_id"] == subscriber
    assert cards[0]["origin_request_id"] == original["request_id"]
    assert await db.wiki_job_cards("original", None) == []
    cursor = await db.conn.execute("SELECT event_id,subscriber_id,user_id FROM wiki_job_notifications")
    assert [tuple(row) for row in await cursor.fetchall()] == [(original["event_id"], subscriber, owner)]
    assert pending(current) == []
    assert jobs.store.path(note["path"]).read_bytes() == raw
    with pytest.raises(WikiError, match="account"):
        await db.receive_wiki_job_notice(config["vault_id"], "unrelated-owner", original)


async def test_unavailable_old_batch_does_not_starve_a_later_valid_notice(jobs, receiver, monkeypatch):
    first = enqueue(jobs)
    for number in range(55):
        subscribe(jobs, first, f"unavailable-{number}")
    following = jobs.maintain("later-notice", "", "Review again", [first["items"][0]["path"]], None, [], "human")
    await session(receiver.db, owner=jobs.user_id)
    subscribe(jobs, following)
    receive = receiver.db.receive_wiki_job_notice

    async def unavailable_prefix(vault_id, user_id, delivery):
        if delivery["job_id"] == first["id"]:
            raise OSError("Keep this exact delivery pending")
        return await receive(vault_id, user_id, delivery)

    monkeypatch.setattr(receiver.db, "receive_wiki_job_notice", unavailable_prefix)
    for _ in range(4):
        try:
            await job_notifications.dispatch(WikiJobs(jobs.user_id), receiver)
        except OSError:
            pass
    assert [card["job_id"] for card in await receiver.db.wiki_job_cards("original", jobs.user_id)] == [following["id"]]
    with jobs.store.connect() as db:
        assert len(job_events.pending(db, limit=100)) == 55


@pytest.mark.parametrize("applied", [False, True])
async def test_saved_outputs_exclude_non_applied_proposal_but_keep_applied_cancel(jobs, receiver, monkeypatch, applied):
    import server.wiki.history as history_module

    job = enqueue(jobs)
    target = jobs.store.path(job["items"][0]["path"])
    original = target.read_bytes()
    atomic = history_module.atomic

    def before_replace(path, raw, **kwargs):
        if path == target:
            raise RuntimeError("Stop before replacing the live note")
        return atomic(path, raw, **kwargs)

    with monkeypatch.context() as patch:
        if not applied:
            patch.setattr(history_module, "atomic", before_replace)
        with pytest.raises(asyncio.CancelledError if applied else RuntimeError):
            await process(jobs, job, Curator(jobs, crash=applied))
    current = jobs.get(job["id"])
    item = current["items"][0]
    assert jobs.recover_publication(job["id"], item)
    assert item["status"] == ("done" if applied else "attention")
    jobs.update(job["id"], items=current["items"])
    jobs.mark_cancelled(job["id"])
    await session(receiver.db, owner=jobs.user_id)
    subscribe(jobs, job)
    await job_notifications.dispatch(jobs, receiver)
    card = (await receiver.db.wiki_job_cards("original", jobs.user_id))[0]
    assert card["effects"]["recorded"] == 1
    assert card["effects"]["applied"] == int(applied)
    assert card["outcome_count"] == int(applied)
    assert bool(card["outcome_refs"]) == applied
    assert (target.read_bytes() != original) == applied
    assert jobs.get(job["id"])["items"][0]["output_revision"] == item["output_revision"]


@pytest.mark.parametrize("restart", [False, True])
async def test_framework_ingest_cancellation_settles_without_preparing_or_regenerating(jobs, restart, monkeypatch):
    raw = "# Accepted material\nPreserve every supplied condition.\n"
    job = jobs.maintain("cancel-framework-ingest", raw, "Organize", [], "Library/ingested.md", [], "human")
    assert jobs.next_queued() == job["id"]
    entered, release = asyncio.Event(), asyncio.Event()

    class Paused(Curator):
        async def create_session(self, *args, **kwargs):
            entered.set()
            await release.wait()
            return await super().create_session(*args, **kwargs)

        async def start_message(self, *args, **kwargs):
            raise AssertionError("Framework ingestion recovery never admits a model")

    manager = Paused(jobs)
    worker = WikiWorker()
    worker.manager = manager
    running = asyncio.create_task(worker.process(jobs, job["id"]))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        assert (await jobs.cancel(job["id"], manager))["settlement_phase"] == "pending"
        original = jobs.get(job["id"])
        assert original["receipts"] and all(receipt["operation"] == "ingest" for receipt in original["receipts"])
        indexed = jobs.store.require_indexed

        def index_unavailable(*args, **kwargs):
            raise WikiError("Injected derived indexing fault after cancellation", 503)

        if restart:
            monkeypatch.setattr(jobs.store, "require_indexed", index_unavailable)
        release.set()
        await running
        if restart:
            assert jobs.get(job["id"])["settlement_phase"] == "attention"
            monkeypatch.setattr(jobs.store, "require_indexed", indexed)
            registry.close()
            jobs = WikiJobs(jobs.user_id)
            await jobs.settle_cancelled(job["id"], manager)
        current = jobs.get(job["id"])
        assert current["settlement_phase"] == "confirmed", current
        assert current["receipts"] == original["receipts"]
        assert all(item["worker_request"]["disposition"] == "not_admitted" for item in current["items"])
        assert current["items"][0]["index_complete"] is True
        assert current["items"][0].get("retry_count", 0) == 0
        assert jobs.store.path("Library/ingested.md").read_text() == raw
        assert manager.turns == 0
        retried = jobs.retry(job["id"])
        assert retried["status"] == "queued"
        assert retried["items"][0].get("retry_count", 0) == 0
    finally:
        release.set()
        await asyncio.gather(running, return_exceptions=True)


@pytest.mark.parametrize("window", ["applied", "prepared", "identity_replaced", "terminal_missing"])
async def test_cancellation_checks_original_effects_before_confirmation(jobs, disk_manager, monkeypatch, window):
    import server.wiki.history as history_module

    job, reference = prepare(jobs, enqueue(jobs))
    path = job["items"][0]["path"]
    target = jobs.store.path(path)
    atomic = history_module.atomic

    def before_replace(path, raw, **kwargs):
        if path == target:
            raise RuntimeError("Original proposal has no applied proof")
        return atomic(path, raw, **kwargs)

    tasks.assign(reference["session_id"], job["id"], job["items"], registration=jobs.registration,
                 request_id=reference["request_id"], attempt=reference["attempt"])
    try:
        with monkeypatch.context() as patch:
            if window == "prepared":
                patch.setattr(history_module, "atomic", before_replace)
            try:
                async for _ in Curator(jobs)._events(reference["session_id"], "Original output", request_id=reference["request_id"]):
                    pass
            except RuntimeError:
                assert window == "prepared"
    finally:
        tasks.unassign(reference["session_id"])
    if window == "identity_replaced":
        raw = target.read_bytes()
        sapphire_delete(WikiHistory(jobs.store), path)
        WikiHistory(jobs.store).write(path, raw, None, "human", "A distinct replacement")
        assert jobs.store.read(path)["id"] != job["items"][0]["doc"]
    if window != "terminal_missing":
        await record_terminal(disk_manager.db)
    with jobs.store.connect() as db:
        db.execute("UPDATE job_publications SET indexed=0 WHERE job_id=?", (job["id"],))
    original = jobs.get(job["id"])
    raw = target.read_bytes()
    # This fixture represents the startup boundary after its previous writers
    # have been retired. A missing runtime object alone supplies no proof.
    jobs.recover_jobs(previous_writers_retired=True)
    await jobs.cancel(job["id"], disk_manager)
    await jobs.settle_cancelled(job["id"], disk_manager)
    current = jobs.get(job["id"])
    assert current["settlement_phase"] == ("confirmed" if window == "applied" else "attention")
    # The status is joined from the recovered change, not a receipt column.
    assert [{key: value for key, value in receipt.items() if key != "status"} for receipt in current["receipts"]] == [
        {key: value for key, value in receipt.items() if key != "status"} for receipt in original["receipts"]]
    assert target.read_bytes() == raw and disk_manager.turns == disk_manager.sessions == 0
    assert current["items"][0].get("retry_count", 0) == 0
    if window == "applied":
        assert current["items"][0]["index_complete"] is True
        with jobs.store.connect() as db:
            assert db.execute("SELECT indexed FROM job_publications WHERE job_id=?", (job["id"],)).fetchone()[0] == 1
    else:
        with pytest.raises(WikiError, match="Confirm the original cancellation"):
            jobs.retry(job["id"])
        with pytest.raises(WikiError, match="Confirm the original cancellation"):
            jobs.readmit(job["id"], [])
