"""Dispatch behavior through the public session APIs and harness boundary."""

from __future__ import annotations

import asyncio

import httpx
import pytest
from fastapi import FastAPI

from server.database import Database
from server.deps import get_session_manager, scope_user_id
from server.harness import HarnessEvent
from server.routers.sessions import router
from server.session_manager import ForkError, SessionManager
from tests.fake_run import FakeRunBase


@pytest.fixture
async def dispatch(monkeypatch, request):
    manager = SessionManager()
    database = Database(":memory:")
    await database.initialize()
    await manager.initialize(database)
    agent = await database.get_system_agent()
    session = await manager.create_session(agent["id"], "Dispatch")
    started: asyncio.Queue[str] = asyncio.Queue()
    holds = {name: asyncio.Event() for name in ("first", "correction held")}
    holds.update({"frame entered": asyncio.Event(), "frame": asyncio.Event()})

    class Run(FakeRunBase):
        reusable = bool(getattr(request, "param", False))

        async def start(self, prompt, *args, **kwargs):
            self.prompt = prompt

        async def send_turn(self, prompt):
            self.prompt = prompt

        async def send_user_frame(self, prompt):
            holds["frame entered"].set()
            await holds["frame"].wait()

        async def stream(self):
            started.put_nowait(self.prompt)
            yield HarnessEvent(type="session_started", session_id="context")
            if self.prompt in holds:
                await holds[self.prompt].wait()
            yield HarnessEvent(type="result", session_id="context", is_error=False)

        async def stop(self):
            pass

        async def interrupt(self):
            pass

    monkeypatch.setattr(manager, "_make_run", lambda *a, **k: Run())
    try:
        yield manager, session, started, holds
    finally:
        await manager.stop_all_held_processes()
        await database.close()


async def test_stop_preserves_backlog_without_starting_next_message(dispatch):
    manager, session, started, _ = dispatch
    await manager.start_message(session.id, "first")
    assert await asyncio.wait_for(started.get(), 2) == "first"
    await manager.start_message(session.id, "old queued instruction")
    await manager.interrupt(session.id)
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(started.get(), 0.05)


async def test_correction_runs_before_returns_and_leaves_dispatch_paused(dispatch):
    manager, session, started, _ = dispatch
    await manager.start_message(session.id, "first")
    assert await asyncio.wait_for(started.get(), 2) == "first"
    await manager.start_message(session.id, "old queued instruction")
    await manager.interrupt(session.id)
    await manager.start_message(session.id, "background return", source="bg")
    await manager.start_message(session.id, "corrected direction", steerable=True)
    assert await asyncio.wait_for(started.get(), 2) == "corrected direction"
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(started.get(), 0.05)
    await manager.resume_dispatch(session.id)
    assert await asyncio.wait_for(started.get(), 2) == "old queued instruction"
    assert await asyncio.wait_for(started.get(), 2) == "background return"


async def test_queue_snapshot_keeps_identity_and_rejects_duplicate_delivery(dispatch):
    manager, session, started, _ = dispatch
    await manager.interrupt(session.id)
    await manager.start_message(session.id, "return one", source="bg", delivery_id="bg:one")
    before = manager.dispatch_snapshot(session.id)
    await manager.start_message(session.id, "return one", source="bg", delivery_id="bg:one")
    after = manager.dispatch_snapshot(session.id)
    assert before == after
    assert after["paused"] is True
    assert len(after["items"]) == 1
    assert after["items"][0]["source"] == "bg"
    assert after["items"][0]["request_id"]
    assert after["generation"]
    await manager.start_message(session.id, "return two", source="delegation", delivery_id="agent:two")
    newest = manager.dispatch_snapshot(session.id)
    assert newest["revision"] > after["revision"]
    assert newest["items"][0] == after["items"][0]
    assert newest["items"][1]["source"] == "delegation"
    assert started.empty()


async def test_stop_captures_current_request_before_delayed_broadcast(dispatch):
    manager, session, started, holds = dispatch
    await manager.start_message(session.id, "first")
    assert await asyncio.wait_for(started.get(), 2) == "first"
    paused = asyncio.Event()
    release = asyncio.Event()

    async def slow_client(event):
        if event["type"] == "dispatch_state" and event["dispatch_state"]["paused"]:
            if not paused.is_set():
                paused.set()
                await release.wait()

    manager.on_broadcast("slow-client", slow_client)
    stopping = asyncio.create_task(manager.interrupt(session.id))
    await asyncio.wait_for(paused.wait(), 2)
    holds["first"].set()
    await manager.start_message(session.id, "correction held", steerable=True)
    assert await asyncio.wait_for(started.get(), 2) == "correction held"
    release.set()
    await stopping
    await asyncio.sleep(0.05)
    assert manager.get_session(session.id).status.value == "running"
    holds["correction held"].set()


async def test_stop_during_queue_claim_preserves_message_that_has_not_started(dispatch):
    manager, session, started, holds = dispatch
    await manager.start_message(session.id, "first")
    assert await asyncio.wait_for(started.get(), 2) == "first"
    await manager.start_message(session.id, "claimed instruction")
    stopped = asyncio.Event()

    async def stop_on_claim(event):
        if event["type"] == "dequeued" and not stopped.is_set():
            stopped.set()
            await manager.interrupt(session.id)

    manager.on_broadcast("stop-on-claim", stop_on_claim)
    holds["first"].set()
    await asyncio.wait_for(stopped.wait(), 2)
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(started.get(), 0.05)
    assert [item["summary"] for item in manager.dispatch_snapshot(session.id)["items"]] == [
        "claimed instruction",
    ]
    await manager.resume_dispatch(session.id)
    assert await asyncio.wait_for(started.get(), 2) == "claimed instruction"


async def test_runtime_restart_has_empty_queue_and_new_generation(dispatch):
    manager, session, _, _ = dispatch
    await manager.interrupt(session.id)
    await manager.start_message(session.id, "stored only in memory", source="research")
    old_state = manager.dispatch_snapshot(session.id)
    restarted = SessionManager()
    await restarted.initialize(manager.db)
    new_state = restarted.dispatch_snapshot(session.id)
    assert new_state["generation"] != old_state["generation"]
    assert new_state["paused"] is False
    assert new_state["items"] == []


async def test_paused_session_excludes_model_quota(dispatch):
    manager, session, started, _ = dispatch
    await manager.start_message(session.id, "first")
    assert await asyncio.wait_for(started.get(), 2) == "first"
    await manager.start_message(session.id, "waiting")
    await manager.interrupt(session.id)
    await asyncio.sleep(0.05)
    assert manager.running_turns_for(session.user_id) == 0
    assert len(manager.dispatch_snapshot(session.id)["items"]) == 1


async def test_fork_does_not_bypass_pause_when_queue_is_empty(dispatch, tmp_path):
    manager, _, _, _ = dispatch
    project = tmp_path / "project"
    project.mkdir()
    agent = await manager.db.get_system_agent()
    parent = await manager.create_session(agent["id"], "Paused fork", str(project), backend="codex")
    await manager.interrupt(parent.id)
    with pytest.raises(ForkError, match="paused"):
        await manager.duplicate_session(parent.id)


async def test_concurrent_returns_are_preserved_once_across_correction_and_resume(dispatch):
    manager, session, started, _ = dispatch
    await manager.interrupt(session.id)
    await asyncio.gather(*(
        manager.start_message(session.id, f"return {i}", source="bg", delivery_id=f"bg:{i}")
        for i in range(20) for _ in range(2)
    ))
    assert len(manager.dispatch_snapshot(session.id)["items"]) == 20
    await manager.start_message(session.id, "correction")
    assert await asyncio.wait_for(started.get(), 2) == "correction"
    await manager.resume_dispatch(session.id)
    delivered = [await asyncio.wait_for(started.get(), 2) for _ in range(20)]
    assert delivered == [f"return {i}" for i in range(20)]
    assert started.empty()


async def test_http_snapshot_and_resume_are_scoped_to_session_owner(dispatch):
    manager, session, started, _ = dispatch
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_session_manager] = lambda: manager
    app.dependency_overrides[scope_user_id] = lambda: session.user_id
    await manager.interrupt(session.id)
    await manager.start_message(session.id, "report", source="research", delivery_id="research:one")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test",
        headers={"Authorization": "Bearer changeme"},
    ) as client:
        snapshot = (await client.get(f"/api/sessions/{session.id}")).json()
        assert snapshot["dispatch_state"] == manager.dispatch_snapshot(session.id)
        assert snapshot["messages"] == []
        app.dependency_overrides[scope_user_id] = lambda: "different-account"
        denied = await client.post(f"/api/sessions/{session.id}/dispatch/resume")
        assert denied.status_code == 404
        assert manager.dispatch_snapshot(session.id)["paused"] is True
        app.dependency_overrides[scope_user_id] = lambda: session.user_id
        resumed = await client.post(f"/api/sessions/{session.id}/dispatch/resume")
        assert resumed.status_code == 200 and resumed.json()["paused"] is False
        assert resumed.json()["revision"] > snapshot["dispatch_state"]["revision"]
    assert await asyncio.wait_for(started.get(), 2) == "report"


async def test_second_stop_pauses_human_messages_accepted_before_it(dispatch):
    manager, session, started, _ = dispatch
    await manager.interrupt(session.id)
    await manager.start_message(session.id, "correction held")
    assert await asyncio.wait_for(started.get(), 2) == "correction held"
    await manager.start_message(session.id, "earlier correction")
    await manager.interrupt(session.id)
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(started.get(), 0.05)
    assert manager.dispatch_snapshot(session.id)["items"][0]["priority"] is False
    await manager.start_message(session.id, "latest correction")
    assert await asyncio.wait_for(started.get(), 2) == "latest correction"
    await manager.resume_dispatch(session.id)
    assert await asyncio.wait_for(started.get(), 2) == "earlier correction"


@pytest.mark.parametrize("dispatch", [True], indirect=True)
async def test_stop_releases_blocked_steering_and_preserves_undelivered_message(dispatch):
    manager, session, started, holds = dispatch
    await manager.start_message(session.id, "first")
    assert await asyncio.wait_for(started.get(), 2) == "first"
    await manager.start_message(session.id, "undelivered steer", steerable=True)
    await asyncio.wait_for(holds["frame entered"].wait(), 2)
    await manager.interrupt(session.id)
    await manager.start_message(session.id, "fresh correction", steerable=True)
    assert await asyncio.wait_for(started.get(), 1) == "fresh correction"
    assert [item["summary"] for item in manager.dispatch_snapshot(session.id)["items"]] == [
        "undelivered steer",
    ]
    await manager.resume_dispatch(session.id)
    assert await asyncio.wait_for(started.get(), 2) == "undelivered steer"


async def test_deleting_running_session_does_not_leave_failed_dispatch_task(dispatch):
    manager, session, started, _ = dispatch
    await manager.start_message(session.id, "first")
    assert await asyncio.wait_for(started.get(), 2) == "first"
    active = session._active_task
    assert await manager.delete_session(session.id)
    completed = await asyncio.gather(active, return_exceptions=True)
    assert all(value is None or isinstance(value, asyncio.CancelledError) for value in completed)


@pytest.mark.parametrize("dispatch", [True], indirect=True)
async def test_delivered_steer_keeps_human_source_and_same_live_history_identity(dispatch):
    manager, session, started, holds = dispatch
    delivered = asyncio.Event()
    events = []

    async def observe(event):
        if event["type"] == "steered":
            events.append(event)
            delivered.set()

    manager.on_broadcast("observe-steer", observe)
    await manager.start_message(session.id, "first")
    assert await asyncio.wait_for(started.get(), 2) == "first"
    prompt = "[bg-task-result] I am quoting this marker in a human correction"
    await manager.start_message(session.id, prompt, steerable=True)
    holds["frame"].set()
    await asyncio.wait_for(delivered.wait(), 2)
    rows = await manager.db.load_messages(session.id)
    stored = next(row for row in rows if row["content"] == prompt)
    context = stored["tool_input"]["request_context"]
    assert context["source"] == "human"
    assert context["request_id"]
    assert events[0]["tool_input"] == stored["tool_input"]
    assert events[0]["seq"] == stored["seq"]


async def test_paused_schedule_neither_notifies_completion_nor_archives_until_resumed(dispatch):
    manager, _, started, _ = dispatch
    notifications = []

    class Notifier:
        async def fire(self, event, user_id):
            notifications.append(event)

    manager.set_notifier_manager(Notifier())
    agent = await manager.db.get_system_agent()
    scheduled = await manager.create_session(agent["id"], "Paused schedule", origin="schedule")
    await manager.interrupt(scheduled.id)
    await manager.start_message(scheduled.id, "pending automatic return", source="bg")
    await manager.start_message(scheduled.id, "correct schedule")
    correcting = scheduled._active_task
    assert await asyncio.wait_for(started.get(), 2) == "correct schedule"
    await asyncio.wait_for(correcting, 2)
    assert manager.get_session(scheduled.id) is scheduled
    assert notifications == []
    await manager.resume_dispatch(scheduled.id)
    resumed = scheduled._active_task
    assert await asyncio.wait_for(started.get(), 2) == "pending automatic return"
    await asyncio.wait_for(resumed, 2)
    assert manager.get_session(scheduled.id) is None
    assert len(notifications) == 1


async def test_submission_receipt_duplicate_and_closed_are_explicit(dispatch):
    from server.mcp_identity import close_work

    manager, session, _, _ = dispatch
    await manager.interrupt(session.id)
    accepted = await manager.start_message(session.id, "return", source="bg", delivery_id="bg:receipt")
    duplicate = await manager.start_message(session.id, "return", source="bg", delivery_id="bg:receipt")
    assert accepted.disposition == "accepted"
    assert duplicate.disposition == "duplicate"
    assert duplicate.request_id == accepted.request_id
    close_work("closed-receipt-work")
    closed = await manager.start_message(session.id, "ignored", source="bg", work_id="closed-receipt-work")
    assert closed.disposition == "closed"
    assert (await closed.wait()).state == "withdrawn"
    assert len(session._pending_queue) == 1


async def test_cancelled_receipt_waiter_does_not_cancel_execution(dispatch):
    manager, session, started, holds = dispatch
    receipt = await manager.start_message(session.id, "first")
    assert await asyncio.wait_for(started.get(), 2) == "first"
    waiting = asyncio.create_task(receipt.wait())
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting
    assert session._inner_task is not None and not session._inner_task.done()
    holds["first"].set()
    completion = await asyncio.wait_for(receipt.wait(), 2)
    assert completion.state == "completed"
    assert completion.request_id == receipt.request_id
    assert completion.outcome.status == "success"


async def test_accepted_receipt_does_not_wait_for_notification(dispatch):
    manager, session, _, _ = dispatch
    entered, release = asyncio.Event(), asyncio.Event()

    async def blocked(event):
        if event["type"] == "dispatch_state":
            entered.set()
            await release.wait()

    manager.on_broadcast("blocked-admission", blocked)
    receipt = await asyncio.wait_for(manager.start_message(session.id, "fast"), .2)
    assert receipt.disposition == "accepted"
    assert session._active_task is not None
    release.set()
    assert (await asyncio.wait_for(receipt.wait(), 2)).outcome.status == "success"


async def test_withdrawn_queued_receipt_has_no_model_outcome(dispatch):
    manager, session, _, _ = dispatch
    await manager.interrupt(session.id)
    receipt = await manager.start_message(session.id, "return", source="bg", work_id="withdraw-receipt-work")
    affected = manager.close_work("withdraw-receipt-work")
    await manager.finish_work_cancellation(affected)
    completion = await asyncio.wait_for(receipt.wait(), .2)
    assert completion.state == "withdrawn"
    assert completion.outcome is None


async def test_deleted_queued_receipt_ends_wait(dispatch):
    manager, session, _, _ = dispatch
    await manager.interrupt(session.id)
    receipt = await manager.start_message(session.id, "return", source="bg")
    await manager.delete_session(session.id)
    assert (await asyncio.wait_for(receipt.wait(), .2)).state == "deleted"


@pytest.mark.parametrize("dispatch", [True], indirect=True)
async def test_inline_steer_receipt_follows_actual_logical_request(dispatch):
    manager, session, started, holds = dispatch
    delivered = asyncio.Event()

    async def notice(event):
        if event["type"] == "steered":
            delivered.set()

    manager.on_broadcast("steer-completion", notice)
    original = await manager.start_message(session.id, "first")
    assert await asyncio.wait_for(started.get(), 2) == "first"
    steer = await manager.start_message(session.id, "inline input", steerable=True)
    assert steer.disposition == "steered"
    await asyncio.wait_for(holds["frame entered"].wait(), 2)
    holds["frame"].set()
    await asyncio.wait_for(delivered.wait(), 2)
    holds["first"].set()
    completed = await asyncio.wait_for(steer.wait(), 2)
    assert completed.input_request_id == steer.request_id
    assert completed.request_id == original.request_id
    assert completed.outcome.request_id == original.request_id
    rows = await manager.db.load_messages(session.id)
    assert sum(row["type"] == "result" for row in rows) == 1


@pytest.mark.parametrize("dispatch", [True], indirect=True)
async def test_failed_inline_delivery_retains_receipt_for_queued_execution(dispatch):
    manager, session, started, holds = dispatch
    original = await manager.start_message(session.id, "first")
    assert await asyncio.wait_for(started.get(), 2) == "first"
    steer = await manager.start_message(session.id, "fallback input", steerable=True)
    await asyncio.wait_for(holds["frame entered"].wait(), 2)
    await manager.interrupt(session.id)
    assert (await asyncio.wait_for(original.wait(), 2)).outcome.status == "interrupted"
    await manager.resume_dispatch(session.id)
    assert await asyncio.wait_for(started.get(), 2) == "fallback input"
    completed = await asyncio.wait_for(steer.wait(), 2)
    assert completed.request_id == steer.request_id
    assert completed.outcome.status == "success"


async def test_expired_question_delivery_finishes_without_model_outcome(dispatch):
    manager, session, started, _ = dispatch
    receipt = await manager.start_message(session.id, "answer old question", source="delegation", question_session_id="missing-child", question_id="expired")
    completion = await asyncio.wait_for(receipt.wait(), 2)
    assert completion.state == "expired"
    assert completion.outcome is None
    assert started.empty()


async def test_shutdown_withdraws_accepted_unstarted_receipts(dispatch):
    manager, session, _, _ = dispatch
    await manager.interrupt(session.id)
    receipt = await manager.start_message(session.id, "return", source="bg")
    await manager.drain_turns()
    assert (await asyncio.wait_for(receipt.wait(), 2)).state == "shutdown"
    closed = await manager.start_message(session.id, "late return", source="bg")
    assert closed.disposition == "closed"


async def test_duplicate_delivery_after_shutdown_keeps_original_request_and_completion(dispatch):
    manager, session, started, _ = dispatch
    await manager.interrupt(session.id)
    original = await manager.start_message(session.id, "return", source="bg", delivery_id="one-return")
    await manager.drain_turns()
    duplicate = await manager.start_message(session.id, "return", source="bg", delivery_id="one-return")
    assert duplicate.disposition == "duplicate"
    assert duplicate.request_id == original.request_id
    assert await duplicate.wait() == await original.wait()
    assert (await duplicate.wait()).state == "shutdown"
    assert started.empty() and not session._pending_queue


async def test_precise_request_cancel_keeps_later_input(dispatch):
    manager, session, _, _ = dispatch
    await manager.interrupt(session.id)
    older = await manager.start_message(session.id, "older", source="wiki")
    later = await manager.start_message(session.id, "later", source="wiki")
    assert await manager.cancel_request(session.id, older.request_id)
    assert (await older.wait()).state == "withdrawn"
    assert [item.request_id for item in session._pending_queue] == [later.request_id]


async def test_unconfirmed_old_backend_retirement_blocks_replacement(dispatch, monkeypatch):
    from tests.fake_run import FakeRunBase

    manager, session, _, _ = dispatch
    reaped = asyncio.Event()
    entered = asyncio.Event()
    created = []

    class OldRun(FakeRunBase):
        async def interrupt(self):
            pass

        async def stop(self):
            entered.set()
            await reaped.wait()

    session._backend = OldRun()
    monkeypatch.setattr(manager, "_make_run", lambda *args, **kwargs: created.append(True))
    try:
        with pytest.raises(RuntimeError, match="retirement remains unconfirmed"):
            await manager._acquire_backend(session, None, None, None, retirement_timeout=.01)
        assert entered.is_set()
        assert created == []
        assert len(session._backend_retirements) == 1
        cleanup = next(iter(session._backend_retirements.values()))[1]
        assert not cleanup.cancelled() and not cleanup.done()
        reaped.set()
        await cleanup
        await manager._await_backend_retirement(session)
    finally:
        reaped.set()


async def test_cancelled_acquire_waiter_keeps_old_retirement_owned(dispatch):
    from tests.fake_run import FakeRunBase

    manager, session, _, _ = dispatch
    entered, reaped = asyncio.Event(), asyncio.Event()

    class OldRun(FakeRunBase):
        async def interrupt(self):
            pass

        async def stop(self):
            entered.set()
            await reaped.wait()

    session._backend = OldRun()
    acquiring = asyncio.create_task(manager._acquire_backend(session, None, None, None))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        acquiring.cancel()
        with pytest.raises(asyncio.CancelledError):
            await acquiring
        cleanup = next(iter(session._backend_retirements.values()))[1]
        assert not cleanup.cancelled() and cleanup in manager._owned_tasks
        reaped.set()
        await cleanup
    finally:
        reaped.set()
        await asyncio.gather(acquiring, return_exceptions=True)
