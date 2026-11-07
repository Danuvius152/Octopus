"""The original command owns its commit after a public waiter leaves."""

from __future__ import annotations

import asyncio

import pytest

from server.sessions.owner import SessionMailboxFull, SessionOwnerUnavailable
from server.sessions.questions import QuestionClosedError
from tests import test_session_owner_publication as publication_tests

notices = publication_tests.notices


async def test_cancelled_notice_waiter_detaches_while_the_original_sql_is_pending(notices, monkeypatch):
    manager, session, client = notices
    database = manager.db
    entered, release, received = asyncio.Event(), asyncio.Event(), asyncio.Event()
    execute = database.conn.execute

    async def held_insert(sql, parameters=()):
        if sql.lstrip().startswith("INSERT INTO messages"):
            entered.set()
            await release.wait()
        return await execute(sql, parameters)

    async def observe(event):
        if event.get("type") == "delivery_notice":
            received.set()

    monkeypatch.setattr(database.conn, "execute", held_insert)
    manager.on_broadcast("original-notice", observe)
    waiter = asyncio.create_task(manager.retain_result_notice(
        session.id, "original-command", "Original confirmed material",
    ))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        waiter.cancel()
        done, _ = await asyncio.wait({waiter}, timeout=.15)
        assert waiter in done, "The original SQL command was still owned by its cancelled waiter"
        with pytest.raises(asyncio.CancelledError):
            await waiter
        release.set()
        await asyncio.wait_for(received.wait(), 2)
        detail = await client.get(f"/api/sessions/{session.id}")
        assert detail.status_code == 200
        assert [row["content"] for row in detail.json()["messages"]] == ["Original confirmed material"]
        assert await manager.retain_result_notice(session.id, "original-command", "Do not replace it")
        detail = await client.get(f"/api/sessions/{session.id}")
        assert [row["content"] for row in detail.json()["messages"]] == ["Original confirmed material"]
    finally:
        release.set()
        await asyncio.gather(waiter, return_exceptions=True)


@pytest.mark.parametrize("with_context", [False, True])
async def test_owner_failure_wakes_original_question_poll_and_refuses_new_commands(notices, monkeypatch, with_context):
    manager, session, client = notices
    if with_context:
        await manager.update_session_configuration(session.id, {"model": "context-fixture"}, session.user_id)
        assert session.context_state is not None and session.context_state.validity == "stale"
    qid = await manager.create_pending_question(session.id, [{"question": "Original question"}])
    assert qid is not None
    poll = asyncio.create_task(manager.wait_for_question_answer(session.id, qid, timeout=3))
    has_notice = manager.db.has_delivery_notice

    async def programming_failure_after_read(session_id, delivery_id):
        await has_notice(session_id, delivery_id)
        raise RuntimeError("Injected owner failure after a confirmed read")

    monkeypatch.setattr(manager.db, "has_delivery_notice", programming_failure_after_read)
    try:
        with pytest.raises(SessionOwnerUnavailable):
            await manager.retain_result_notice(session.id, "owner-fault", "Undelivered material")
        with pytest.raises(QuestionClosedError):
            await asyncio.wait_for(poll, 2)
        with pytest.raises(SessionOwnerUnavailable):
            await manager.retain_result_notice(session.id, "new-command", "Do not restart the owner")
        assert manager.get_pending_questions(session.id) == ()
        detail = await asyncio.wait_for(client.get(f"/api/sessions/{session.id}"), 2)
        assert detail.status_code == 200
        assert [row["type"] for row in detail.json()["messages"]] == ["question_request"]
        assert detail.json()["context_pending"] is with_context
    finally:
        await asyncio.gather(poll, return_exceptions=True)


async def test_delete_joins_the_original_notice_command_before_removing_its_row(notices, monkeypatch):
    manager, session, client = notices
    entered, release, delivered = asyncio.Event(), asyncio.Event(), asyncio.Event()
    has_notice = manager.db.has_delivery_notice

    async def held_actual_read(session_id, delivery_id):
        result = await has_notice(session_id, delivery_id)
        entered.set()
        await release.wait()
        return result

    async def observe(event):
        if event.get("type") == "delivery_notice":
            assert event["content"] == "Original admitted material"
            delivered.set()

    monkeypatch.setattr(manager.db, "has_delivery_notice", held_actual_read)
    manager.on_broadcast("captured-material", observe)
    notice = asyncio.create_task(manager.retain_result_notice(
        session.id, "before-delete", "Original admitted material",
    ))
    deletion = None
    try:
        await asyncio.wait_for(entered.wait(), 2)
        deletion = asyncio.create_task(client.delete(f"/api/sessions/{session.id}"))
        done, _ = await asyncio.wait({deletion}, timeout=.15)
        assert not done, "Deletion removed the row before its original notice command finished"
        release.set()
        assert await asyncio.wait_for(notice, 2)
        assert (await asyncio.wait_for(deletion, 2)).status_code == 204
        await asyncio.wait_for(delivered.wait(), 2)
        assert (await client.get(f"/api/sessions/{session.id}")).status_code == 404
    finally:
        release.set()
        await asyncio.gather(notice, *([deletion] if deletion else []), return_exceptions=True)


@pytest.mark.parametrize("with_context", [False, True])
async def test_command_capacity_refuses_before_acceptance_and_keeps_owned_commands(notices, monkeypatch, with_context):
    manager, session, client = notices
    if with_context:
        await manager.update_session_configuration(session.id, {"model": "context-fixture"}, session.user_id)
        assert session.context_state is not None and session.context_state.validity == "stale"
    entered, release = asyncio.Event(), asyncio.Event()
    has_notice = manager.db.has_delivery_notice

    async def hold_first_read(session_id, delivery_id):
        result = await has_notice(session_id, delivery_id)
        if delivery_id == "first":
            entered.set()
            await release.wait()
        return result

    monkeypatch.setattr(manager.db, "has_delivery_notice", hold_first_read)
    first = asyncio.create_task(manager.retain_result_notice(session.id, "first", "Original first material"))
    waiting = []
    try:
        await asyncio.wait_for(entered.wait(), 2)
        waiting = [asyncio.create_task(manager.retain_result_notice(
            session.id, f"queued-{index}", f"Original queued material {index}",
        )) for index in range(257)]
        done, _ = await asyncio.wait(waiting, timeout=.15)
        assert len(done) == 1
        refused = next(iter(done))
        with pytest.raises(SessionMailboxFull, match="command capacity"):
            await refused
        detail = await asyncio.wait_for(client.get(f"/api/sessions/{session.id}"), 2)
        assert detail.status_code == 200 and detail.json()["message_count"] == 0
        assert detail.json()["context_pending"] is False
        release.set()
        assert await asyncio.wait_for(first, 3)
        accepted = [task for task in waiting if task is not refused]
        assert all(await asyncio.wait_for(asyncio.gather(*accepted), 5))
        detail = await client.get(f"/api/sessions/{session.id}")
        assert detail.status_code == 200 and detail.json()["message_count"] == 257
        assert all(row["content"] != "Original queued material 256" for row in detail.json()["messages"])
        assert await manager.retain_result_notice(session.id, "queued-256", "Original queued material 256")
        detail = await client.get(f"/api/sessions/{session.id}")
        assert detail.json()["message_count"] == 258
        assert detail.json()["messages"][-1]["content"] == "Original queued material 256"
    finally:
        release.set()
        await asyncio.gather(first, *waiting, return_exceptions=True)


async def test_archive_from_subscriber_does_not_join_its_own_publication(notices):
    manager, session, client = notices
    entered, release = asyncio.Event(), asyncio.Event()
    observed = []

    async def archive_original(event):
        if event.get("type") == "delivery_notice":
            observed.append("first")
            entered.set()
            await release.wait()
            await manager.archive_session(session.id, replace=False)
            archived = await manager.load_archived_session_detail(session.id)
            assert [row.content for row in archived.messages] == ["Keep original material"]
            observed.append("archive returned")

    async def receive_original(event):
        if event.get("type") == "delivery_notice":
            observed.append("second")

    manager.on_broadcast("archive-original", archive_original)
    manager.on_broadcast("receive-original", receive_original)
    assert await manager.retain_result_notice(session.id, "archive-parent", "Keep original material")
    await asyncio.wait_for(entered.wait(), 2)
    detail = await client.get(f"/api/sessions/{session.id}")
    assert detail.status_code == 200
    assert detail.json()["messages"][0]["content"] == "Keep original material"
    closing = asyncio.create_task(manager.db.close())
    try:
        done, _ = await asyncio.wait({closing}, timeout=.15)
        assert not done, "Database close overtook its captured publication callback"
        release.set()
        await asyncio.wait_for(closing, 3)
        assert observed == ["first", "archive returned", "second"]
        assert manager.get_session(session.id) is None
    finally:
        release.set()
        await asyncio.gather(closing, return_exceptions=True)


@pytest.mark.parametrize("fail_after_drain", [False, True])
async def test_failed_owner_cleanup_drains_an_owner_created_by_its_original_subscriber(
    notices, monkeypatch, fail_after_drain,
):
    manager, original, _client = notices
    derived = await manager.create_session(original.agent_id)
    entered, release = asyncio.Event(), asyncio.Event()
    derived_entered, derived_release = asyncio.Event(), asyncio.Event()
    failing, release_failure = asyncio.Event(), asyncio.Event()
    completed = []
    has_notice = manager.db.has_delivery_notice

    async def original_subscriber(event):
        if event.get("type") != "delivery_notice":
            return
        if event["session_id"] == original.id:
            entered.set()
            await release.wait()
            assert await manager.retain_result_notice(derived.id, "derived", "Original derived material")
        elif event["session_id"] == derived.id:
            derived_entered.set()
            await derived_release.wait()
            completed.append(event["content"])

    async def failure_after_read(session_id, delivery_id):
        result = await has_notice(session_id, delivery_id)
        if delivery_id == "fail-original":
            failing.set()
            await release_failure.wait()
            raise RuntimeError("Original owner failed while its subscriber was waiting")
        return result

    manager.on_broadcast("derive-during-failure-cleanup", original_subscriber)
    assert await manager.retain_result_notice(original.id, "original", "Original material")
    await asyncio.wait_for(entered.wait(), 2)
    monkeypatch.setattr(manager.db, "has_delivery_notice", failure_after_read)
    closing = None
    failure = asyncio.create_task(manager.retain_result_notice(
        original.id, "fail-original", "Unconfirmed material",
    ))
    try:
        await asyncio.wait_for(failing.wait(), 2)
        if not fail_after_drain:
            release_failure.set()
            with pytest.raises(SessionOwnerUnavailable):
                await failure
        closing = asyncio.create_task(manager.close_session_owners())
        done, _ = await asyncio.wait({closing}, timeout=.05)
        assert not done
        if fail_after_drain:
            release_failure.set()
            with pytest.raises(SessionOwnerUnavailable):
                await failure
            done, _ = await asyncio.wait({closing}, timeout=.05)
            assert not done, "Drain returned before the failed owner's original cleanup retired"
        release.set()
        await asyncio.wait_for(derived_entered.wait(), 2)
        done, _ = await asyncio.wait({closing}, timeout=.15)
        assert not done, "Owner drain missed an owner created by original cleanup's subscriber"
        derived_release.set()
        await asyncio.wait_for(closing, 2)
        assert completed == ["Original derived material"]
    finally:
        release_failure.set()
        release.set()
        derived_release.set()
        await asyncio.gather(failure, *([closing] if closing is not None else []), return_exceptions=True)


async def test_cancelled_auto_archive_keeps_the_original_notice_owner_available(notices, monkeypatch):
    manager, original, client = notices
    session = await manager.create_session(original.agent_id, origin="schedule")
    entered, release = asyncio.Event(), asyncio.Event()
    archived, archive_release = asyncio.Event(), asyncio.Event()
    update = manager.db.update_session_field

    async def hold_original_prefix(event):
        if event.get("type") == "delivery_notice" and event["session_id"] == session.id:
            entered.set()
            await release.wait()

    async def hold_confirmed_archive(session_id, **fields):
        await update(session_id, **fields)
        if session_id == session.id and fields.get("archived") is True:
            archived.set()
            await archive_release.wait()

    manager.on_broadcast("hold-original-notice-prefix", hold_original_prefix)
    assert await manager.retain_result_notice(session.id, "original", "Original retained material")
    await asyncio.wait_for(entered.wait(), 2)
    monkeypatch.setattr(manager.db, "update_session_field", hold_confirmed_archive)
    archiving = asyncio.create_task(manager.auto_archive_scheduled_session(session.id))
    try:
        await asyncio.wait_for(archived.wait(), 2)
        with pytest.raises(SessionOwnerUnavailable, match="archive is in progress"):
            await manager.retain_result_notice(session.id, "after-cancel", "Material after cancelled archive")
        await manager.interrupt(session.id)
        archive_release.set()
        assert await asyncio.wait_for(archiving, 2) is False
        detail = await client.get(f"/api/sessions/{session.id}")
        assert detail.status_code == 200 and detail.json()["archived"] is False
        assert await manager.retain_result_notice(session.id, "after-cancel", "Material after cancelled archive")
        release.set()
        await asyncio.wait_for(manager.close_session_owners(), 2)
        detail = await client.get(f"/api/sessions/{session.id}")
        assert [row["content"] for row in detail.json()["messages"] if row["type"] == "delivery_notice"] == [
            "Original retained material", "Material after cancelled archive",
        ]
    finally:
        archive_release.set()
        release.set()
        await asyncio.gather(archiving, return_exceptions=True)


async def test_owner_failure_preserves_an_already_confirmed_question_answer(notices, monkeypatch):
    manager, session, _client = notices
    question = await manager.create_pending_question(session.id, [{"question": "Keep the confirmed answer"}])
    answer = [{"selected": [], "text": "The original confirmed answer"}]
    assert await manager.answer_question(session.id, question, answer)
    expected = "Q: Keep the confirmed answer\nA: The original confirmed answer"
    assert await manager.wait_for_question_answer(session.id, question, timeout=1) == expected
    has_notice = manager.db.has_delivery_notice

    async def failure_after_read(session_id, delivery_id):
        await has_notice(session_id, delivery_id)
        raise RuntimeError("Original owner failed after an answer was confirmed")

    monkeypatch.setattr(manager.db, "has_delivery_notice", failure_after_read)
    with pytest.raises(SessionOwnerUnavailable):
        await manager.retain_result_notice(session.id, "fail-after-answer", "Unconfirmed material")
    assert await manager.wait_for_question_answer(session.id, question, timeout=1) == expected
