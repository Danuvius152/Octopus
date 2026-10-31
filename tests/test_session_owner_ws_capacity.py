"""Browser answer payloads participate in the real control admission budget."""

from __future__ import annotations

import asyncio
import json

import pytest
from fastapi import FastAPI

from server.config import settings
from server.deps import get_session_manager
from server.routers.ws import router
from server.sessions import commands
from server.sessions.owner import SessionOwnerUnavailable
from server.sessions.questions import QuestionClosedError
from tests import test_ws_delivery as ws_tests

runtime = ws_tests.runtime


async def connect_browser(manager, sockets, bearer):
    """ASGI transport only; authentication and the WS endpoint remain real."""
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_session_manager] = lambda: manager
    incoming, outgoing = asyncio.Queue(), asyncio.Queue()

    async def send(message):
        await outgoing.put(message)

    scope = {
        "type": "websocket", "asgi": {"version": "3.0"}, "scheme": "ws",
        "path": "/ws", "raw_path": b"/ws", "headers": [],
        "query_string": f"token={bearer}".encode(), "subprotocols": [],
        "client": ("127.0.0.1", 1), "server": ("127.0.0.1", 8000),
    }
    await incoming.put({"type": "websocket.connect"})
    task = asyncio.create_task(app(scope, incoming.get, send))
    sockets.append((incoming, outgoing, task))
    assert (await asyncio.wait_for(outgoing.get(), 2))["type"] == "websocket.accept"
    return incoming, task


@pytest.mark.parametrize(
    ("character", "characters"), [("a", 600_000), ("答", 400_000)],
    ids=["within-budget", "oversized-singleton"],
)
async def test_ws_large_answers_wait_for_bytes_and_release_after_cancel_and_owner_failure(
    runtime, monkeypatch, tmp_path, character, characters,
):
    manager, database, bearer = runtime
    monkeypatch.setattr(settings, "ask_user_question_timeout_seconds", 60)
    agent = await database.get_system_agent()
    session = await manager.create_session(agent["id"], "WS answer capacity", str(tmp_path), backend="codex")
    question_id = await manager.create_pending_question(session.id, [{"question": "Provide the original full answer"}])
    pending, = manager.get_pending_questions(session.id)
    assert pending.timer is not None
    owner = manager._session_owners[id(session)]
    capacity_wait = owner._control_space.wait
    process_command = commands.process_command
    has_notice = database.has_delivery_notice
    entered, waiting = asyncio.Queue(), asyncio.Queue()
    read_done, release_read = asyncio.Event(), asyncio.Event()
    failure = RuntimeError("WS Owner failed after its original notice read")
    outcomes = {}
    sockets = []
    notice = poll = None

    async def observe_capacity_wait():
        waiting.put_nowait(asyncio.current_task())
        return await capacity_wait()

    async def observe_command(*args, **kwargs):
        command_id = args[1]["client_command_id"]
        entered.put_nowait((command_id, asyncio.current_task()))
        try:
            result = await process_command(*args, **kwargs)
        except BaseException as error:
            outcomes[command_id] = error
            raise
        else:
            outcomes[command_id] = result
            return result

    async def hold_then_fail_notice(session_id, delivery_id):
        result = await has_notice(session_id, delivery_id)
        if session_id == session.id and delivery_id == "ws-byte-fault":
            read_done.set()
            await release_read.wait()
            raise failure
        return result

    monkeypatch.setattr(owner._control_space, "wait", observe_capacity_wait)
    monkeypatch.setattr(commands, "process_command", observe_command)
    monkeypatch.setattr(database, "has_delivery_notice", hold_then_fail_notice)

    async def send(browser, command_id):
        payload = {
            "type": "answer_question", "session_id": session.id,
            "client_command_id": command_id, "request_id": None, "attempt_id": None,
            "question_id": question_id, "answers": [{"text": character * characters}],
        }
        encoded = json.dumps(payload, ensure_ascii=False)
        assert len(encoded.encode("utf-8")) > 600_000
        await browser[0].put({"type": "websocket.receive", "text": encoded})
        assert await asyncio.wait_for(entered.get(), 2) == (command_id, browser[1])

    try:
        first = await connect_browser(manager, sockets, bearer)
        cancelled = await connect_browser(manager, sockets, bearer)
        surviving = await connect_browser(manager, sockets, bearer)
        poll = asyncio.create_task(manager.wait_for_question_answer(session.id, question_id, timeout=10))
        notice = asyncio.create_task(manager.retain_result_notice(
            session.id, "ws-byte-fault", "The unconfirmed original append",
        ))
        await asyncio.wait_for(read_done.wait(), 2)

        # Even the 1.2 MiB answer may own an otherwise empty control entry.
        # The next answer must wait on bytes, with only one of 256 slots used.
        await send(first, "owned-large-answer")
        assert waiting.empty() and not first[1].done()
        await send(cancelled, "unadmitted-cancelled-answer")
        assert await asyncio.wait_for(waiting.get(), 2) is cancelled[1]
        cancelled[1].cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(cancelled[1], 2)
        assert isinstance(outcomes["unadmitted-cancelled-answer"], asyncio.CancelledError)

        await send(surviving, "unadmitted-surviving-answer")
        assert await asyncio.wait_for(waiting.get(), 2) is surviving[1]
        assert "owned-large-answer" not in outcomes
        assert "unadmitted-surviving-answer" not in outcomes
        assert not poll.done() and not release_read.is_set()

        release_read.set()
        with pytest.raises(SessionOwnerUnavailable):
            await asyncio.wait_for(notice, 2)
        await asyncio.wait_for(asyncio.gather(first[1], surviving[1]), 3)
        assert isinstance(outcomes["owned-large-answer"], SessionOwnerUnavailable)
        assert isinstance(outcomes["unadmitted-surviving-answer"], SessionOwnerUnavailable)
        with pytest.raises(QuestionClosedError):
            await asyncio.wait_for(poll, 2)
        await asyncio.wait_for(manager.close_session_owners(), 3)
        assert owner.task.done() and owner.task.exception() is failure
        assert owner.publisher.task.done() and owner.publisher.task.exception() is None
        assert pending.timer.done()
        assert manager.get_pending_questions(session.id) == ()
        assert not await has_notice(session.id, "ws-byte-fault")
        assert [row["type"] for row in await database.load_messages(session.id)] == ["question_request"]
        print("ws-control-byte-evidence", {
            "answer_utf8_bytes": len((character * characters).encode("utf-8")), "owned_commands": 1,
            "unadmitted_cancelled": True, "unadmitted_failed": True,
            "original_owner_ended": owner.task.done(), "original_publisher_joined": owner.publisher.task.done(),
        })
    finally:
        release_read.set()
        if notice is not None:
            await asyncio.gather(notice, return_exceptions=True)
        for _incoming, _outgoing, task in sockets:
            if not task.done():
                task.cancel()
        await asyncio.gather(*(task for _incoming, _outgoing, task in sockets), return_exceptions=True)
        if poll is not None:
            await asyncio.gather(poll, return_exceptions=True)
        await asyncio.wait_for(manager.close_session_owners(), 3)
