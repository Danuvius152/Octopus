"""Input admission never infers readiness from an assigned database pointer."""

import asyncio

import pytest

from server.database import Database
from server.session_manager import Session, SessionManager
from server.sessions.client_submissions import submission_status, submit_client_message
from tests.accounts import create_account
from tests.fake_run import FakeRunBase


@pytest.fixture
async def not_ready(tmp_path):
    database = Database(":memory:")
    await database.initialize()
    await create_account(database, extra_roots=[tmp_path])
    agent = await database.get_system_agent()
    await database.save_session("waiting", "Waiting", str(tmp_path), "now", agent_id=agent["id"], user_id=agent["user_id"])
    manager = SessionManager()
    session = Session(id="waiting", name="Waiting", working_dir=str(tmp_path), agent_id=agent["id"], user_id=agent["user_id"])
    session._dispatch_paused = True
    manager.sessions[session.id] = session
    try:
        yield manager, database, session
    finally:
        await manager.drain_turns()
        await manager.close_session_owners()
        await database.close()


@pytest.mark.parametrize("source", ["human", "schedule", "bg", "research", "delegation", "wiki", "app"])
async def test_all_sources_refuse_execution_before_initialization(not_ready, source):
    manager, database, session = not_ready
    with pytest.raises(ValueError) as refused:
        await manager.start_message(session.id, "Must remain unaccepted", source=source, request_id="unaccepted")
    assert getattr(refused.value, "code", None) == "service_not_ready"
    assert "uninitialized" in str(refused.value)
    assert manager.dispatch_snapshot(session.id)["items"] == []
    assert await database.get_submission_for_request(session.id, "unaccepted") is None


@pytest.mark.parametrize("phase", ["recover_client_submissions", "load_sessions", "_recover_incomplete_forks"])
async def test_input_is_refused_at_each_initialization_barrier(not_ready, monkeypatch, phase):
    manager, database, session = not_ready
    entered, release = asyncio.Event(), asyncio.Event()
    target = manager if phase.startswith("_") else database
    original = getattr(target, phase)
    async def held(*args, **kwargs):
        result = await original(*args, **kwargs)
        entered.set()
        await release.wait()
        return result
    monkeypatch.setattr(target, phase, held)
    initializing = asyncio.create_task(manager.initialize(database))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        assert manager.db is database
        reply = await submit_client_message(manager, session.id, "Wait until ready", [], "retryable", wiki_mode=None)
        assert reply["accepted"] is False and reply["code"] == "service_not_ready"
        assert "initializing" in reply["message"] and "state" not in reply
        assert await database.get_client_submission(session.id, "retryable") is None
        assert manager.dispatch_snapshot(session.id)["items"] == []
    finally:
        release.set()
        await initializing


async def test_failed_recovery_does_not_open_admission_or_claim_nonexistence(not_ready, monkeypatch):
    manager, database, session = not_ready
    async def fail_recovery():
        raise RuntimeError("Recovery failed")
    monkeypatch.setattr(manager, "_recover_incomplete_forks", fail_recovery)
    with pytest.raises(RuntimeError, match="Recovery failed"):
        await manager.initialize(database)
    reply = await submit_client_message(manager, session.id, "Not saved", [], "refused", wiki_mode=None)
    assert reply["accepted"] is False and reply["code"] == "service_not_ready"
    assert "failed" in reply["message"] and "state" not in reply
    with pytest.raises(ValueError, match="failed"):
        await submission_status(manager, session.id, "refused")
    with pytest.raises(ValueError, match="failed"):
        await manager.source_receipt("bg", "bg:unconfirmed", session_id=session.id)
    assert await database.get_client_submission(session.id, "refused") is None


async def test_constructor_listeners_and_empty_shutdown_need_no_database():
    manager = SessionManager()
    manager.on_broadcast("listener", lambda event: None)
    assert manager.list_sessions() == [] and manager.get_session("missing") is None
    assert await manager.drain_turns() == 0
    await manager.close_session_owners()


@pytest.mark.parametrize("source", ["human", "schedule", "bg", "research", "delegation", "wiki", "app"])
async def test_ready_sources_execute_only_after_the_saved_claim_commits(not_ready, monkeypatch, source):
    from server.harness import HarnessEvent

    manager, database, session = not_ready
    await manager.initialize(database)
    starts = []
    claimed, release = asyncio.Event(), asyncio.Event()
    original = database.claim_client_submission
    async def held_claim(*args, **kwargs):
        result = await original(*args, **kwargs)
        claimed.set()
        await release.wait()
        return result
    class Run(FakeRunBase):
        async def start(self, prompt, *args, **kwargs):
            starts.append(prompt)
        async def stream(self):
            yield HarnessEvent(type="result", is_error=False)
        async def stop(self):
            pass
        async def interrupt(self):
            pass
    monkeypatch.setattr(database, "claim_client_submission", held_claim)
    monkeypatch.setattr(manager, "_make_run", lambda *args, **kwargs: Run())
    receipt = await manager.start_message(session.id, "Saved original input", source=source,
                                          delivery_id=f"source-{source}", request_id="saved-input")
    try:
        await asyncio.wait_for(claimed.wait(), 2)
        row = await database.get_submission_for_request(session.id, receipt.request_id)
        assert receipt.accepted and row["execution_state"] == "running"
        assert row["payload"]["source"] == source and row["payload"]["content"] == "Saved original input"
        assert starts == []
        release.set()
        completion = await asyncio.wait_for(receipt.wait(), 3)
        assert completion.state == "completed" and completion.recording_state == "committed"
        assert starts == ["Saved original input"]
        repeated = await manager.start_message(session.id, "Saved original input", source=source, delivery_id=f"source-{source}")
        assert repeated.disposition == "duplicate" and repeated.request_id == receipt.request_id
        manager.close_admission()
        repeated = await manager.start_message(session.id, "Saved original input", source=source, delivery_id=f"source-{source}")
        assert repeated.request_id == receipt.request_id and repeated.accepted
        with pytest.raises(ValueError, match="different input"):
            await manager.start_message(session.id, "Changed content", source=source, delivery_id=f"source-{source}")
        assert len(starts) == 1
    finally:
        release.set()


async def test_unready_one_shot_and_background_return_create_no_new_session(not_ready):
    from server.bg_tasks import BgTaskRecord
    from server.knowledge import for_entry

    manager, _, session = not_ready
    context = for_entry(session, "schedule", request_id="original")
    with pytest.raises(ValueError, match="uninitialized"):
        await manager.start_one_shot({"id": "one-shot", "run_at": "now"}, "schedule", context)
    record = BgTaskRecord(
        id="task", session_id=session.id, command="printf retained", description=None,
        working_dir=session.working_dir, status="completed", exit_code=0,
        stdout="retained", stderr="", truncated=False, started_at="now", completed_at="now",
    )
    with pytest.raises(ValueError, match="uninitialized"):
        await manager.deliver_bg_result(record)
    assert list(manager.sessions) == [session.id]


async def test_stop_settles_a_started_claim_without_running_or_replaying_it(not_ready, monkeypatch):
    from server.harness import HarnessEvent

    manager, database, session = not_ready
    await manager.initialize(database)
    entered, release = asyncio.Event(), asyncio.Event()
    starts = []
    claim = database.claim_client_submission
    async def held_claim(*args, **kwargs):
        result = await claim(*args, **kwargs)
        entered.set()
        await release.wait()
        return result
    class Run(FakeRunBase):
        async def start(self, prompt, *args, **kwargs):
            starts.append(prompt)
        async def stream(self):
            yield HarnessEvent(type="result", is_error=False)
        async def stop(self):
            pass
        async def interrupt(self):
            pass
    monkeypatch.setattr(database, "claim_client_submission", held_claim)
    monkeypatch.setattr(manager, "_make_run", lambda *args, **kwargs: Run())
    receipt = await manager.start_message(session.id, "Original committed content")
    try:
        await asyncio.wait_for(entered.wait(), 2)
        await manager.interrupt(session.id)
        release.set()
        await manager.await_session_execution(session.id, timeout=2)
        snapshot = manager.dispatch_snapshot(session.id)
        assert snapshot["paused"] and snapshot["items"] == []
        assert starts == []
        completion = await asyncio.wait_for(receipt.wait(), 3)
        assert completion.state == "completed" and completion.input_request_id == receipt.request_id
        assert completion.outcome.status == "interrupted"
        await manager.resume_dispatch(session.id)
        assert starts == []
    finally:
        release.set()
