"""Consumers select the completed attempt without dropping its text blocks."""

from unittest.mock import AsyncMock

from server.app_agent import AppAgentManager, _Turn
from server.delegations import DelegationManager, DelegationRunState


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


async def test_delegation_recovery_discards_only_failed_attempt():
    manager = DelegationManager()
    rec = DelegationRunState("child", "parent", "agent", "Worker", "review")
    manager._records["child"] = rec
    manager._inject_terminal = AsyncMock()
    async def emit(kind, attempt, **fields):
        await manager._on_broadcast({"type": kind, "session_id": "child", "work_id": rec.work_id,
                                     "request_id": "r1", "attempt_id": attempt, **fields})
    await emit("assistant_text", "a1", content="obsolete")
    await emit("result", "a1", retrying=True, is_error=True)
    await emit("assistant_text", "a2", content="correct one ")
    await emit("assistant_text", "a2", content="correct two")
    await emit("result", "a2", is_error=False)
    assert "".join(rec.captured_text) == "correct one correct two"
    assert rec.state == "completed"
