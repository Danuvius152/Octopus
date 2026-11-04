"""Original tools/call cancellation crosses mounted MCP, REST and QuestionOwner."""

from __future__ import annotations

import asyncio

import httpx
import pytest
from mcp import ClientSession
from mcp import types as mcp_types
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import McpError

from server.config import settings
from server.deps import get_session_manager
from server.mcp_identity import revoke_run
from server.routers.questions import router
from tests.callback_api import start_callback_api
from tests.test_ask_mcp import pending_question
from tests.test_ask_mcp import served_ask as served_ask
from tests.test_question_run_scope import begin_run
from tests.test_question_run_scope import question_runtime as question_runtime


@pytest.mark.parametrize("handoff", ["waiting", "create-receipt", "queued-create", "waiter-release", "cleanup-failure", "retired-bearer"])
async def test_cancelled_mcp_call_closes_its_question_while_original_turn_is_alive(question_runtime, monkeypatch, handoff):
    manager, session, client, events, _starts = question_runtime
    token, scope = await begin_run(question_runtime)
    api = await start_callback_api(router, overrides={get_session_manager: lambda: manager}, readiness_token=token)
    monkeypatch.setattr(settings, "port", api.port)
    owner = manager._session_owners[id(session)]
    entered, release = asyncio.Event(), asyncio.Event()
    if handoff == "create-receipt":
        persist = manager._persist_message
        async def hold_receipt(session, message):
            result = await persist(session, message)
            if message.type == "question_request":
                entered.set()
                await release.wait()
            return result
        monkeypatch.setattr(manager, "_persist_message", hold_receipt)
    if handoff == "queued-create":
        create = owner.questions._create
        async def hold_create(command):
            entered.set()
            await release.wait()
            return await create(command)
        monkeypatch.setattr(owner.questions, "_create", hold_create)
        post = httpx.AsyncClient.post
        async def short_post(self, url, **kwargs):
            if url.endswith("/questions"):
                kwargs["timeout"] = .05
            return await post(self, url, **kwargs)
        monkeypatch.setattr(httpx.AsyncClient, "post", short_post)
    original = call = None
    try:
        async with httpx.AsyncClient(headers={"Authorization": f"Bearer {token}"}, timeout=10) as transport:
            async with streamable_http_client(f"http://127.0.0.1:{api.port}/mcp/ask/mcp", http_client=transport) as (read, write, get_session_id):
                async with ClientSession(read, write) as mcp:
                    initialized = await mcp.initialize()
                    rpc_id = mcp._request_id
                    metadata = {"callId": "supplied-native-call", "threadId": "supplied-thread"} if handoff == "waiting" else None
                    calling = asyncio.create_task(mcp.call_tool("user", {"questions": [{"question": "Original live receiver?"}]}, meta=metadata))
                    try:
                        if handoff in {"create-receipt", "queued-create"}:
                            await asyncio.wait_for(entered.wait(), 2)
                        async with asyncio.timeout(3):
                            while not owner.questions.mcp_calls:
                                await asyncio.sleep(.01)
                        call, = owner.questions.mcp_calls.values()
                        if handoff != "queued-create":
                            async with asyncio.timeout(3):
                                while not (questions := manager.get_pending_questions(session.id)):
                                    await asyncio.sleep(.01)
                            original, = questions
                        if handoff in {"waiter-release", "cleanup-failure"}:
                            async with asyncio.timeout(2):
                                while not original.waiters:
                                    await asyncio.sleep(.01)
                            released = original.waiters_released.wait
                            async def wait_release():
                                entered.set()
                                await release.wait()
                                if handoff == "cleanup-failure":
                                    raise RuntimeError("original waiter release unconfirmed")
                                await released()
                            monkeypatch.setattr(original.waiters_released, "wait", wait_release)
                        if handoff == "retired-bearer":
                            revoke_run(scope.run_lease_id)
                            response = await transport.delete(f"http://127.0.0.1:{api.port}/mcp/ask/mcp", headers={
                                "mcp-session-id": get_session_id(), "mcp-protocol-version": initialized.protocolVersion,
                            })
                            assert response.status_code == 200
                            calling.cancel()
                            await asyncio.gather(calling, return_exceptions=True)
                        else:
                            await mcp.send_notification(mcp_types.ClientNotification(mcp_types.CancelledNotification(
                                method="notifications/cancelled", params=mcp_types.CancelledNotificationParams(requestId=rpc_id),
                            )))
                            with pytest.raises(McpError, match="Request cancelled"):
                                await calling
                        if handoff not in {"waiting", "retired-bearer"}:
                            if handoff in {"waiter-release", "cleanup-failure"}:
                                await asyncio.wait_for(entered.wait(), 2)
                            else:
                                await asyncio.sleep(.08)
                            assert not call.task.done()
                            assert call.id in owner.questions.mcp_calls
                            assert (await mcp.list_tools()).tools  # SDK cancellation already replied.
                            release.set()
                        if handoff == "queued-create":
                            await asyncio.gather(call.task, return_exceptions=True)
                            assert call.creation.done() and call.pending is None
                            assert not manager.get_pending_questions(session.id)
                            assert not [event for event in events if event["type"] == "question_request"]
                            assert call.id not in owner.questions.mcp_calls
                            return
                        async with asyncio.timeout(2):
                            while manager.has_pending_question(session.id, original.question_id):
                                await asyncio.sleep(.01)
                        if handoff == "cleanup-failure":
                            from server.sessions.owner import SessionOwnerUnavailable
                            await asyncio.gather(owner.task, return_exceptions=True)
                            with pytest.raises(SessionOwnerUnavailable):
                                await manager.answer_question(session.id, original.question_id, [{"text": "too late"}])
                        else:
                            assert not await manager.answer_question(session.id, original.question_id, [{"text": "too late"}])
                        if handoff != "cleanup-failure":
                            assert session._run.backend.is_alive()
                            assert session._run.question_attempt_id == original.attempt_id
                        history = await client.get(f"/api/sessions/{session.id}")
                        closed = [message for message in history.json()["messages"] if message["type"] == "question_closed"]
                        assert len(closed) == 1 and closed[0]["tool_use_id"] == original.question_id
                        requested, = [message for message in history.json()["messages"] if message["type"] == "question_request"]
                        evidence = requested["tool_input"]["mcp_call"]
                        assert evidence["rpc_id"] == rpc_id and evidence["scope"]["run_lease_id"] == scope.run_lease_id
                        assert evidence["server"] == "ask" and evidence["tool"] == "user"
                        assert evidence["metadata"] == metadata
                        if handoff == "cleanup-failure":
                            await asyncio.gather(call.task, return_exceptions=True)
                            assert "MCP question call cleanup" in owner.drain_blockers()
                            with pytest.raises(RuntimeError, match="cleanup is unconfirmed"):
                                await manager.drain_turns(timeout=3)
                            with pytest.raises(RuntimeError, match="cleanup is unconfirmed"):
                                await manager.close_session_owners()
                            assert await manager.db.load_messages(session.id)
                            monkeypatch.setattr(original.waiters_released, "wait", released)
                            await call.finish("cancelled")
                        else:
                            await asyncio.gather(call.task, return_exceptions=True)
                            assert call.error is None and call.id not in owner.questions.mcp_calls
                    finally:
                        release.set()
                        calling.cancel()
                        await asyncio.gather(calling, return_exceptions=True)
                        # Keep the failing baseline teardown finite: cancelling
                        # a synchronous SDK tool otherwise retains its worker.
                        if original is not None and manager.has_pending_question(session.id, original.question_id):
                            await manager.close_pending_question(session.id, original.question_id)
    finally:
        await api.stop()


@pytest.mark.parametrize("barrier", ["close-persist", "waiter-release"])
async def test_mcp_cancellation_cannot_interrupt_cleanup_already_started_by_communication_failure(served_ask, monkeypatch, barrier):
    async with served_ask() as (mcp, manager, session, _client):
        fail_poll, entered, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        get = httpx.AsyncClient.get
        async def refuse_poll(self, url, **kwargs):
            if str(url).endswith("/answer"):
                await fail_poll.wait()
                return httpx.Response(401, text="original observer refused")
            return await get(self, url, **kwargs)
        monkeypatch.setattr(httpx.AsyncClient, "get", refuse_poll)
        rpc_id = mcp._request_id
        calling = asyncio.create_task(mcp.call_tool("user", {"questions": [{"question": "Already cleaning up?"}]}))
        pending = await pending_question(manager, session)
        owner = manager._session_owners[id(session)]
        call, = owner.questions.mcp_calls.values()
        backend = session._run.backend
        if barrier == "close-persist":
            persist = manager._persist_message
            async def hold_close(session, message):
                if message.type == "question_closed":
                    entered.set()
                    await release.wait()
                return await persist(session, message)
            monkeypatch.setattr(manager, "_persist_message", hold_close)
        else:
            released = pending.waiters_released.wait
            async def hold_release():
                entered.set()
                await release.wait()
                await released()
            monkeypatch.setattr(pending.waiters_released, "wait", hold_release)
        try:
            fail_poll.set()
            await asyncio.wait_for(entered.wait(), 2)
            assert call.cancelled and call.task.cancelling() == 0
            await mcp.send_notification(mcp_types.ClientNotification(mcp_types.CancelledNotification(
                method="notifications/cancelled", params=mcp_types.CancelledNotificationParams(requestId=rpc_id),
            )))
            with pytest.raises(McpError, match="Request cancelled"):
                await calling
            done, _ = await asyncio.wait({call.task}, timeout=.05)
            assert not done and call.error is None
            release.set()
            await asyncio.gather(call.task, return_exceptions=True)
            assert call.error is None and call.id not in owner.questions.mcp_calls
            assert not owner.failed and backend.is_alive()
            rows = await manager.db.load_messages(session.id)
            closed, = [row for row in rows if row["type"] == "question_closed"]
            assert closed["tool_use_id"] == pending.question_id
            assert closed["tool_input"]["reason"] == "communication_failure"
        finally:
            release.set()
            calling.cancel()
            await asyncio.gather(calling, call.task, return_exceptions=True)
            if call.error is not None:
                await asyncio.gather(owner.task, return_exceptions=True)
                await call.finish("communication_failure")


async def test_archive_cannot_retire_original_owner_before_failed_mcp_cleanup_is_observed(served_ask, monkeypatch):
    async with served_ask() as (mcp, manager, session, client):
        rpc_id = mcp._request_id
        calling = asyncio.create_task(mcp.call_tool("user", {"questions": [{"question": "Retirement boundary?"}]}))
        pending = await pending_question(manager, session)
        owner = manager._session_owners[id(session)]
        call, = owner.questions.mcp_calls.values()
        entered, release = asyncio.Event(), asyncio.Event()
        released = pending.waiters_released.wait
        async def fail_release():
            entered.set()
            await release.wait()
            raise RuntimeError("original waiter release unconfirmed after archive began")
        monkeypatch.setattr(pending.waiters_released, "wait", fail_release)
        archive = None
        try:
            await mcp.send_notification(mcp_types.ClientNotification(mcp_types.CancelledNotification(
                method="notifications/cancelled", params=mcp_types.CancelledNotificationParams(requestId=rpc_id),
            )))
            with pytest.raises(McpError, match="Request cancelled"):
                await calling
            await asyncio.wait_for(entered.wait(), 2)
            archive = asyncio.create_task(manager.archive_session(session.id, replace=False))
            await asyncio.wait({archive}, timeout=.2)
            assert manager._session_owners.get(id(session)) is owner and not owner.task.done(), (
                "archive retired the original owner while MCP cleanup was unconfirmed")
            release.set()
            await asyncio.gather(call.task, archive, return_exceptions=True)
            assert manager._session_owners.get(id(session)) is owner and call.error is not None
            with pytest.raises(RuntimeError, match="cleanup is unconfirmed"):
                await manager.close_session_owners()
        finally:
            release.set()
            if archive is not None:
                await asyncio.gather(archive, return_exceptions=True)
            monkeypatch.setattr(pending.waiters_released, "wait", released)
            await call.finish("cancelled")
