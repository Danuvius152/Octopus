"""Full control admission keeps waiter cancellation separate from ownership."""

from __future__ import annotations

import asyncio
import json

import pytest

from server.config import settings
from server.sessions.owner import SessionOwnerUnavailable
from server.sessions.questions import QuestionClosedError
from tests import test_question_lifecycle as question_tests

question_host = question_tests.question_host
HEADERS, QUESTIONS = question_tests.HEADERS, question_tests.QUESTIONS


@pytest.mark.parametrize("cancelled_kind", ["runtime", "question"])
async def test_full_control_capacity_releases_original_waiters_after_owner_failure(
    question_host, monkeypatch, cancelled_kind,
):
    manager, session, client, events = question_host
    monkeypatch.setattr(settings, "ask_user_question_timeout_seconds", 60)
    await manager.interrupt(session.id)
    receipt = await manager.start_message(
        session.id, "Keep the original unstarted return", source="bg",
        request_id="original-control-capacity-input",
    )
    assert receipt.accepted
    question_id = await manager.create_pending_question(session.id, QUESTIONS)
    pending, = manager.get_pending_questions(session.id)
    assert pending.question_id == question_id
    original_question_timer = pending.timer
    assert original_question_timer is not None

    # These are the original resource references. The observers below forward
    # their actual waits/joins and never change a queue, budget or reply.
    owner = manager._session_owners[id(session)]
    original_owner_task, original_publisher_task = owner.task, owner.publisher.task
    capacity_wait = owner._control_space.wait
    question_wait = pending.ready.wait
    publisher_close = owner.publisher.close
    capacity_entered = asyncio.Queue()
    polling, publication_held = asyncio.Event(), asyncio.Event()
    release_publication, release_read = asyncio.Event(), asyncio.Event()
    read_finished, publisher_joined, publisher_closing = (
        asyncio.Event() for _ in range(3)
    )
    drained, drain_started = asyncio.Event(), asyncio.Event()
    failure = RuntimeError("Original Owner programming failure after its real read")
    delivered = []

    async def observe_capacity_wait():
        capacity_entered.put_nowait(asyncio.current_task())
        return await capacity_wait()

    async def observe_question_wait():
        polling.set()
        return await question_wait()

    async def observe_publisher_close():
        publisher_closing.set()
        await publisher_close()
        publisher_joined.set()

    async def hold_original_publication(event):
        if (event.get("session_id") == session.id
                and event.get("type") == "delivery_notice"
                and event["tool_input"]["delivery_id"] == "original-prefix"):
            publication_held.set()
            await release_publication.wait()

    async def receive_original_publication(event):
        if event.get("session_id") == session.id and event.get("type") == "delivery_notice":
            delivered.append(event["tool_input"]["delivery_id"])

    has_notice = manager.db.has_delivery_notice

    async def fail_after_original_read(session_id, delivery_id):
        result = await has_notice(session_id, delivery_id)
        if session_id == session.id and delivery_id == "control-capacity-fault":
            read_finished.set()
            await release_read.wait()
            raise failure
        return result

    async def control(kind):
        if kind == "runtime":
            return await manager.interrupt(session.id)
        return await manager.answer_question(session.id, question_id, [{"selected": ["B"]}])

    async def drain_original_owners():
        drain_started.set()
        await manager.close_session_owners()
        drained.set()

    monkeypatch.setattr(owner._control_space, "wait", observe_capacity_wait)
    monkeypatch.setattr(pending.ready, "wait", observe_question_wait)
    monkeypatch.setattr(owner.publisher, "close", observe_publisher_close)
    monkeypatch.setattr(manager.db, "has_delivery_notice", fail_after_original_read)
    manager.on_broadcast("hold-control-capacity-prefix", hold_original_publication)
    manager.on_broadcast("receive-control-capacity-prefix", receive_original_publication)
    poll = asyncio.create_task(manager.wait_for_question_answer(session.id, question_id, timeout=10))
    original_completion = asyncio.create_task(receipt.wait())
    owned_controls = []
    cancelled_waiter = surviving_waiter = faulting_notice = draining = None
    try:
        await asyncio.wait_for(polling.wait(), 2)
        assert await manager.retain_result_notice(session.id, "original-prefix", "Keep the original prefix")
        await asyncio.wait_for(publication_held.wait(), 2)
        faulting_notice = asyncio.create_task(manager.retain_result_notice(
            session.id, "control-capacity-fault", "This append was never confirmed",
        ))
        await asyncio.wait_for(read_finished.wait(), 2)

        # Count capacity is256 for the reserved control entry. The original
        # SQL command prevents consumption; neither Submit nor output is full.
        owned_controls = [asyncio.create_task(
            control("runtime" if index % 2 == 0 else "question"),
            name=f"original-owned-control-{index}",
        ) for index in range(256)]
        cancelled_waiter = asyncio.create_task(control(cancelled_kind), name="unadmitted-cancelled-control")
        assert await asyncio.wait_for(capacity_entered.get(), 2) is cancelled_waiter
        assert all(not task.done() for task in owned_controls)
        assert capacity_entered.empty()

        cancelled_waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(cancelled_waiter, 2)
        assert not release_read.is_set() and not faulting_notice.done()
        assert all(not task.done() for task in owned_controls)

        remaining_kind = "question" if cancelled_kind == "runtime" else "runtime"
        surviving_waiter = asyncio.create_task(control(remaining_kind), name="unadmitted-failed-control")
        assert await asyncio.wait_for(capacity_entered.get(), 2) is surviving_waiter
        assert capacity_entered.empty()
        snapshot = manager.dispatch_snapshot(session.id)
        assert snapshot["paused"] and snapshot["active_request_id"] is None
        assert [item["request_id"] for item in snapshot["items"]] == [receipt.request_id]
        assert not original_completion.done() and not poll.done()

        release_read.set()
        outcomes = await asyncio.wait_for(asyncio.gather(
            faulting_notice, *owned_controls, surviving_waiter, original_completion, poll,
            return_exceptions=True,
        ), 3)
        assert all(isinstance(error, SessionOwnerUnavailable) for error in outcomes[:-2])
        completion = outcomes[-2]
        assert completion.state == "unavailable" and completion.outcome is None
        assert completion.notification_state == "unavailable"
        assert isinstance(outcomes[-1], QuestionClosedError)
        assert cancelled_waiter.cancelled()
        assert original_owner_task.done() and original_owner_task.exception() is failure
        assert manager.get_pending_questions(session.id) == ()
        assert not [event for event in events if event.get("type") in {"question_answer", "result"}]
        with pytest.raises(SessionOwnerUnavailable):
            await manager.interrupt(session.id)
        with pytest.raises(SessionOwnerUnavailable):
            await manager.answer_question(session.id, question_id, [{"selected": ["A"]}])

        await asyncio.wait_for(publisher_closing.wait(), 2)
        assert not original_publisher_task.done() and not publisher_joined.is_set()
        draining = asyncio.create_task(drain_original_owners())
        await asyncio.wait_for(drain_started.wait(), 2)
        assert not draining.done() and not drained.is_set()
        assert not release_publication.is_set() and delivered == []
        release_publication.set()
        await asyncio.wait_for(draining, 3)
        assert drained.is_set() and publisher_joined.is_set()
        assert original_publisher_task.done() and original_publisher_task.exception() is None
        assert original_question_timer.done() and original_question_timer.cancelled()
        assert delivered == ["original-prefix"]
        assert await manager.drain_turns(timeout=3) == 0

        detail = await client.get(f"/api/sessions/{session.id}", headers=HEADERS)
        assert detail.status_code == 200 and detail.json()["pending_questions"] == []
        assert [row["type"] for row in detail.json()["messages"]] == ["question_request", "delivery_notice"]
        assert not await has_notice(session.id, "control-capacity-fault")
        snapshot = manager.dispatch_snapshot(session.id)
        assert [item["request_id"] for item in snapshot["items"]] == [receipt.request_id]
        print("Actual control-capacity evidence: " + json.dumps({
            "cancelled_waiter_kind": cancelled_kind, "surviving_waiter_kind": remaining_kind,
            "owned_controls": len(owned_controls), "owned_runtime_controls": 128,
            "owned_question_controls": 128, "unadmitted_waiters": 2,
            "owned_control_failures": sum(isinstance(error, SessionOwnerUnavailable) for error in outcomes[1:257]),
            "original_receipt_unavailable": isinstance(outcomes[-2], SessionOwnerUnavailable),
            "original_poll_closed": isinstance(outcomes[-1], QuestionClosedError),
            "original_question_timer_cancelled": original_question_timer.cancelled(),
            "owner_programming_failure_preserved": original_owner_task.exception() is failure,
            "original_publication_delivered": delivered,
            "original_owner_ended": original_owner_task.done(),
            "original_publisher_joined": original_publisher_task.done(),
        }), flush=True)
    finally:
        release_read.set()
        release_publication.set()
        waiting = [faulting_notice, *owned_controls, cancelled_waiter, surviving_waiter,
                   original_completion, poll, draining]
        for task in waiting:
            if task is not None and not task.done():
                task.cancel()
        await asyncio.wait_for(asyncio.gather(
            *(task for task in waiting if task is not None), return_exceptions=True,
        ), 3)
        await asyncio.wait_for(manager.close_session_owners(), 3)
