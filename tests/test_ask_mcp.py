"""Public ask behavior through mounted MCP and the original REST question."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from server.config import settings
from server.deps import get_session_manager
from server.routers.questions import router
from tests.callback_api import start_callback_api
from tests.test_question_run_scope import begin_run
from tests.test_question_run_scope import question_runtime as question_runtime


@pytest.fixture
async def served_ask(question_runtime, monkeypatch):
    manager, session, client, _events, _starts = question_runtime
    token, _scope = await begin_run(question_runtime)
    api = await start_callback_api(router, overrides={get_session_manager: lambda: manager}, readiness_token=token)
    monkeypatch.setattr(settings, "port", api.port)
    try:
        @asynccontextmanager
        async def connect():
            async with httpx.AsyncClient(headers={"Authorization": f"Bearer {token}"}, timeout=10) as transport, streamable_http_client(
                f"http://127.0.0.1:{api.port}/mcp/ask/mcp", http_client=transport,
            ) as (read, write, _):
                async with ClientSession(read, write) as mcp:
                    await mcp.initialize()
                    try:
                        yield mcp, manager, session, client
                    finally:
                        for pending in manager.get_pending_questions(session.id):
                            await manager.close_pending_question(session.id, pending.question_id)
        yield connect
    finally:
        await api.stop()


async def pending_question(manager, session):
    async with asyncio.timeout(3):
        while not (pending := manager.get_pending_questions(session.id)):
            await asyncio.sleep(.01)
    question, = pending
    return question


async def test_poll_window_expiry_keeps_the_original_question_until_its_answer(served_ask, monkeypatch):
    from server.mcp_servers import ask

    async with served_ask() as (mcp, manager, session, client):
        monkeypatch.setattr(ask, "_PER_POLL_TIMEOUT", 1)
        calling = asyncio.create_task(mcp.call_tool("user", {"questions": [{"question": "How should we proceed?"}]}))
        original = await pending_question(manager, session)
        assert original.deadline is None  # Config 0, rather than an MCP-local 30-minute budget.
        await asyncio.sleep(2.1)
        assert manager.get_pending_questions(session.id) == (original,) and not calling.done()
        response = await client.post(f"/api/sessions/{session.id}/questions/{original.question_id}/answer",
                                     json={"answers": [{"text": "Use the existing framework"}]})
        assert response.status_code == 200
        result = await calling
        assert "Use the existing framework" in result.content[0].text and not result.isError
        structured = await client.get(f"/api/sessions/{session.id}/questions/{original.question_id}/answer?structured=true")
        assert structured.json()["resolution"]["disposition"] == "answered"


@pytest.mark.parametrize("failure", ["transport", "server"])
async def test_repeated_communication_failure_closes_once_without_assuming_an_answer(served_ask, monkeypatch, failure):
    async with served_ask() as (mcp, manager, session, client):
        get = httpx.AsyncClient.get
        attempts = []
        async def fail_poll(self, url, **kwargs):
            if str(url).startswith("http://127.0.0.1:") and str(url).endswith("/answer"):
                attempts.append(url)
                if failure == "transport":
                    raise httpx.ConnectError("connection refused")
                return httpx.Response(503)
            return await get(self, url, **kwargs)
        monkeypatch.setattr(httpx.AsyncClient, "get", fail_poll)
        result = await mcp.call_tool("user", {"questions": [{"question": "Approve the action?"}]})
        assert len(attempts) == 3
        answer = result.content[0].text
        assert result.isError
        assert "Error: communication" in answer and "autonomously" not in answer.lower()
        assert manager.get_pending_questions(session.id) == ()
        history = await manager.db.load_messages(session.id)
        closed, = [message for message in history if message["type"] == "question_closed"]
        assert closed["tool_input"]["reason"] == "communication_failure"
        reread = await client.get(f"/api/sessions/{session.id}/questions/{closed['tool_use_id']}/answer?structured=true")
        assert reread.status_code == 200 and reread.json()["resolution"]["disposition"] == "closed"
        assert reread.json()["resolution"]["answers"] is None
        unknown = await client.get(f"/api/sessions/{session.id}/questions/unknown/answer?structured=true")
        assert unknown.status_code == 404


@pytest.mark.parametrize("response", [
    httpx.Response(404, text="unknown question"), httpx.Response(401, text="invalid authority"),
    httpx.Response(200, text="malformed JSON"), httpx.Response(200, json={"answer": "Unverified answer"}),
])
async def test_unknown_authority_and_invalid_responses_remain_tool_errors(served_ask, monkeypatch, response):
    async with served_ask() as (mcp, manager, session, client):
        get = httpx.AsyncClient.get
        async def bad_poll(self, url, **kwargs):
            if str(url).startswith("http://127.0.0.1:") and str(url).endswith("/answer"):
                return response
            return await get(self, url, **kwargs)
        monkeypatch.setattr(httpx.AsyncClient, "get", bad_poll)
        result = await mcp.call_tool("user", {"questions": [{"question": "Approval?"}]})
        assert result.isError and "Error" in result.content[0].text
        assert not manager.get_pending_questions(session.id)
