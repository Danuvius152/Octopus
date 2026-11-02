"""Original-request return boundaries across admission, reuse and recovery."""

import asyncio
import json
import threading
from contextlib import contextmanager

from server.mcp_identity import McpScope
from server.session_manager import SessionManager
from server.wiki import job_events, job_notifications, job_returns
from server.wiki.history import WikiHistory
from server.wiki.maintenance_contract import actor_for
from server.wiki.registry import registry
from server.wiki.tasks import tasks
from tests.test_wiki_completion_return import complete, finish_original, submitted
from tests.test_wiki_completion_return import returning as returning
from tests.wiki_job_runtime import client as client
from tests.wiki_job_runtime import jobs as jobs


async def return_inputs(manager, session_id):
    cursor = await manager.db.conn.execute(
        "SELECT request_id,payload FROM client_submissions WHERE session_id=? "
        "AND json_extract(payload,'$.source')='wiki' ORDER BY rowid", (session_id,),
    )
    return [(row[0], json.loads(row[1])) for row in await cursor.fetchall()]


async def wait_return(manager, session_id, payload):
    existing = await manager.source_receipt("wiki", payload["delivery_id"], session_id=session_id)
    assert existing is not None
    completion = await asyncio.wait_for(existing[1].wait(), 5)
    assert completion.recording_state == "committed" and completion.outcome.status == "success"


async def live_task(returning):
    ready = asyncio.Event()

    async def observe(_event):
        if returning.original._run.wiki_task_id:
            ready.set()

    manager = returning.manager
    manager.on_broadcast("wiki-admission-return-boundary", observe)
    try:
        if returning.original._run.wiki_task_id:
            ready.set()
        await asyncio.wait_for(ready.wait(), 5)
        token = returning.original._run.wiki_task_id
        task = tasks.active[token]
        assert task.request_id == returning.receipt.request_id
        return task, McpScope(task.session_id, user_id=task.user_id, wiki_task_id=token)
    finally:
        manager.remove_broadcast("wiki-admission-return-boundary")


async def test_parent_terminal_waits_for_the_last_authorized_admission_transaction(jobs, returning, monkeypatch):
    task, scope = await live_task(returning)
    first = submitted(jobs, returning, "first")
    complete(jobs, first)
    await job_notifications.dispatch(jobs, returning.manager)
    path = "Library/late.md"
    WikiHistory(jobs.store).write(path, b"# Late\nKeep this condition.\n", None, "human", "Create late input")
    entered, release, group_waiting = threading.Event(), threading.Event(), threading.Event()
    subscribe = jobs.subscribe

    def pause_final_admission(db, job_id, requester):
        result = subscribe(db, job_id, requester)
        if job_id != first["id"] and requester.request_id == returning.receipt.request_id:
            entered.set()
            assert release.wait(10), "Final admission barrier was not released"
        return result

    monkeypatch.setattr(jobs, "subscribe", pause_final_admission)
    writer = asyncio.create_task(asyncio.to_thread(
        jobs.maintain, "late", "", "Review selected note", [path], None, [], actor_for(task),
        selection_task=task, mcp_scope=scope,
    ))
    dispatcher = None
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        await finish_original(returning)
        assert not any(value.request_id == task.request_id for value in tasks.active.values())
        account_lock, group = registry._account_lock, job_returns._group
        in_group = threading.local()

        @contextmanager
        def observed_lock(lock):
            group_waiting.set()
            with lock:
                yield

        def observed_account_lock(owner):
            lock = account_lock(owner)
            return observed_lock(lock) if getattr(in_group, "active", False) else lock

        def observed_group(*args):
            in_group.active = True
            try:
                return group(*args)
            finally:
                in_group.active = False

        monkeypatch.setattr(registry, "_account_lock", observed_account_lock)
        monkeypatch.setattr(job_returns, "_group", observed_group)
        dispatcher = asyncio.create_task(job_notifications.dispatch(jobs, returning.manager))
        assert await asyncio.to_thread(group_waiting.wait, 5)
        assert not dispatcher.done()
        release.set()
        late = await asyncio.wait_for(writer, 5)
        await asyncio.wait_for(dispatcher, 5)
        assert await return_inputs(returning.manager, returning.original.id) == []
        complete(jobs, late)
        await job_notifications.dispatch(jobs, returning.manager)
        replies = await return_inputs(returning.manager, returning.original.id)
        assert len(replies) == 1
        summary = replies[0][1]["content"]
        assert first["id"] in summary and late["id"] in summary
        await wait_return(returning.manager, returning.original.id, replies[0][1])
    finally:
        release.set()
        await asyncio.gather(writer, *([dispatcher] if dispatcher else []), return_exceptions=True)


async def test_cold_unknown_parent_keeps_visible_result_without_permanent_pending(jobs, returning, monkeypatch):
    job = submitted(jobs, returning, "recording-unknown")
    complete(jobs, job)
    manager, database = returning.manager, returning.manager.db
    record = database.record_terminal

    async def fail_original_record(**fields):
        if fields["request_id"] == returning.receipt.request_id:
            raise OSError("injected original terminal recording failure")
        return await record(**fields)

    monkeypatch.setattr(database, "record_terminal", fail_original_record)
    returning.gate.touch()
    completion = await asyncio.wait_for(returning.receipt.wait(), 5)
    assert completion.recording_state != "committed"
    # Join the original owner's actual resources before simulating startup;
    # unknown classification alone is never retirement evidence.
    await manager.close_session_owners()
    monkeypatch.setattr(database, "record_terminal", record)
    cold = SessionManager()
    await cold.initialize(database, previous_writers_retired=True)
    try:
        original = await database.get_submission_for_request(returning.original.id, returning.receipt.request_id)
        assert original["execution_state"] == "unknown" and original["terminal"] is None
        await job_notifications.dispatch(jobs, cold)
        with jobs.store.connect() as db:
            assert not job_events.pending(db)
            terminal = db.execute(
                "SELECT d.outcome FROM wiki_job_deliveries d JOIN wiki_job_outbox o ON o.event_id=d.event_id "
                "WHERE o.job_id=? AND json_extract(o.body,'$.status')='completed'", (job["id"],),
            ).fetchone()
            assert terminal[0] == "unavailable"
        assert await return_inputs(cold, returning.original.id) == []
        messages = await database.load_messages(returning.original.id)
        assert any(row["type"] == "delivery_notice" for row in messages)
        cards = await database.wiki_job_cards(returning.original.id, jobs.user_id)
        assert len(cards) == 1 and cards[0]["status"] == "completed"
    finally:
        await cold.drain_turns(timeout=4)
        await cold.close_session_owners()


async def test_same_chat_later_request_reusing_completed_job_receives_its_own_return(jobs, returning):
    job = submitted(jobs, returning, "shared")
    complete(jobs, job)
    await finish_original(returning)
    await job_notifications.dispatch(jobs, returning.manager)
    first = await return_inputs(returning.manager, returning.original.id)
    assert len(first) == 1
    await wait_return(returning.manager, returning.original.id, first[0][1])
    next_request = await returning.manager.start_message(returning.original.id, "Check the same selected note again")
    repeated = jobs.maintain(
        "shared", "", "Review selected note", ["Library/shared.md"], None, [], "human",
        requester=job_events.Requester(jobs.user_id, "chat", returning.original.id, next_request.request_id),
    )
    assert repeated["id"] == job["id"] and repeated["state_version"] == jobs.get(job["id"])["state_version"]
    result = await asyncio.wait_for(next_request.wait(), 5)
    assert result.recording_state == "committed"
    await job_notifications.dispatch(jobs, returning.manager)
    replies = await return_inputs(returning.manager, returning.original.id)
    assert len(replies) == 2
    assert len({payload["delivery_id"] for _, payload in replies}) == 2
    assert [json.loads(payload["content"].split("\n")[-1])["origin_request_id"] for _, payload in replies] == [
        returning.receipt.request_id, next_request.request_id,
    ]
    await wait_return(returning.manager, returning.original.id, replies[1][1])
    await job_notifications.dispatch(jobs, returning.manager)
    assert len(await return_inputs(returning.manager, returning.original.id)) == 2
