"""A cancelled broadcast caller still owns cancellation of its own operation."""

import asyncio

import pytest

from server.sessions import SessionManager


@pytest.mark.parametrize("required", [False, True])
async def test_external_cancellation_of_broadcast_caller_is_not_observer_failure(required):
    manager = SessionManager()
    entered = asyncio.Event()
    observed = []

    async def blocked(event):
        entered.set()
        await asyncio.Event().wait()

    async def later(event):
        observed.append(event)

    manager.on_broadcast("blocked", blocked, required=required)
    manager.on_broadcast("later", later)
    caller = asyncio.create_task(manager._broadcast({"type": "control-operation"}))
    await asyncio.wait_for(entered.wait(), 1)
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller
    assert observed == []


@pytest.mark.parametrize("failure", [RuntimeError, asyncio.CancelledError])
async def test_required_failure_stops_the_original_ordered_business_prefix(failure):
    manager = SessionManager()
    observed = []

    async def unavailable(event):
        raise failure("The required original consumer failed")

    async def forward_question(event):
        observed.append(event)

    manager.on_broadcast("required-original", unavailable, required=True)
    manager.on_broadcast("required-forward", forward_question, required=True)
    with pytest.raises(failure):
        await manager._broadcast({"type": "question_request"})
    assert observed == []
