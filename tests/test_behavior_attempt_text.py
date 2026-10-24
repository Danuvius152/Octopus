"""Consumers select the completed attempt without dropping its text blocks."""

from server.agent_manager import AgentManager
from server.app_agent import AppAgentManager, _Turn
from server.database import Database
from server.delegations import DelegationManager
from server.sessions import SessionManager
from tests.accounts import create_account
from tests.completion_models import CompletionModels


async def test_app_recovery_replaces_failed_text_and_tells_stream_consumer():
    manager = AppAgentManager()
    turn = _Turn(session_id="child", app_id="app", request_id="r1")
    manager._turns["child"] = turn
    async def emit(kind, attempt, **fields):
        await manager._on_broadcast({"type": kind, "session_id": "child", "request_id": "r1",
                                     "attempt_id": attempt, **fields})
    await emit("assistant_text", "a1", content="obsolete")
    await emit("result", "a1", retrying=True, is_error=True)
    await emit("assistant_delta", "a2", content="correct")
    await emit("assistant_text", "a2", content="correct one")
    await emit("assistant_text", "a2", content="correct two")
    await emit("result", "a2", is_error=False)
    assert turn.reply() == "correct one\n\ncorrect two"
    events = []
    while not turn.queue.empty():
        events.append(turn.queue.get_nowait())
    assert any(event["type"] == "reset" and event["attempt_id"] == "a2" for event in events)
    assert all("attempt_id" in event for event in events)


async def test_delegation_recovery_discards_only_failed_attempt(monkeypatch, tmp_path):
    database = Database(":memory:")
    await database.initialize()
    owner, _ = await create_account(database, extra_roots=[tmp_path])
    sessions, manager, models = SessionManager(), DelegationManager(), CompletionModels()
    await sessions.initialize(database)
    manager.bind(sessions, database)
    monkeypatch.setattr(sessions, "start_message", models.start_for(sessions))
    root = await database.get_system_agent()
    await AgentManager(database).create_agent(name="Worker", user_id=owner["id"])
    parent = await sessions.create_session(root["id"], "Caller", str(tmp_path))
    try:
        rec = await manager.start_delegation(parent_session_id=parent.id, agent_name="Worker", request="review")
        original = models.pending(sessions, rec.delegation_id)

        async def emit(kind, attempt, **fields):
            original.attempt_id = attempt
            await models.emit(manager, {"type": kind, "session_id": rec.delegation_id,
                "work_id": rec.work_id, "attempt_id": attempt, **fields})

        await emit("assistant_text", "a1", content="obsolete")
        await emit("result", "a1", retrying=True, is_error=True)
        await emit("assistant_text", "a2", content="correct one ")
        await emit("assistant_text", "a2", content="correct two")
        assert rec.state == "running" and not original.receipt._completion.done()
        await emit("result", "a2", is_error=False)
        assert rec.state == "completed"
        replies = models.requests[(id(sessions), parent.id)]
        assert len(replies) == 1
        rows = await database.load_messages(parent.id)
        reply = next(row["content"] for row in rows if row["role"] == "user")
        assert "correct one correct two" in reply and "obsolete" not in reply
        assert (await original.receipt.wait()).outcome.status == "success"
    finally:
        manager.shutdown()
        await database.close()
