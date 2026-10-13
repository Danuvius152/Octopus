"""A signed-in Codex can wait past its native 60-second MCP default.

The callback host, question REST routes, storage and session driver are real.
Only the human answer is supplied by the test, after 65 seconds of waiting.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any

import httpx
import pytest

from server.agent_manager import AgentManager
from server.config import settings
from server.database import Database
from server.deps import get_session_manager
from server.routers import questions, sessions
from server.session_manager import SessionManager
from tests.callback_api import CallbackApi, start_callback_api

pytestmark = [pytest.mark.real, pytest.mark.real_codex]


@pytest.mark.asyncio
async def test_codex_http_question_waits_sixty_five_seconds_then_resumes(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(settings, "agents_dir", str(tmp_path / "agents"))
    monkeypatch.setattr(settings, "ask_user_question_timeout_seconds", 0)
    # Shorter than the human's wait, but enough for normal model startup.
    monkeypatch.setattr(settings, "turn_idle_timeout_seconds", 45)
    monkeypatch.setattr(settings, "turn_max_seconds", 0)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    db = Database(str(tmp_path / "questions.sqlite3"))
    manager = SessionManager()
    api: CallbackApi | None = None
    session_id: str | None = None
    answer_task: asyncio.Task[httpx.Response] | None = None
    events: list[dict[str, Any]] = []
    loop = asyncio.get_running_loop()
    final: asyncio.Future[dict[str, Any]] = loop.create_future()
    question_received: asyncio.Future[dict[str, Any]] = loop.create_future()
    # The model cannot know the selected banner before the HTTP tool returns.
    banner = f"HUMAN-BANNER-{uuid.uuid4().hex}"
    headers = {"Authorization": f"Bearer {settings.auth_token}"}
    question_seen_at: float | None = None
    answer_sent_at: float | None = None

    try:
        await db.initialize()
        await manager.initialize(db)
        agent = await AgentManager(db).create_agent(
            name="Codex question waiting acceptance",
            backend="codex",
            reasoning_effort="low",
            service_tier="default",
            mcp_servers=["ask"],
            system_prompt=(
                "A human must choose the release banner. Use mcp__ask__user "
                "when requested, wait for its answer, then use the exact "
                "human choice. Do not choose a default or call other tools."
            ),
        )
        session = await manager.create_session(
            agent["id"], "Codex delayed human answer", str(workspace), backend="codex"
        )
        session_id = session.id
        api = await start_callback_api(
            questions.router,
            sessions.router,
            overrides={get_session_manager: lambda: manager},
        )
        monkeypatch.setattr(settings, "port", api.port)

        async def answer_after_wait(question_id: str) -> httpx.Response:
            nonlocal answer_sent_at
            await asyncio.sleep(65)
            answer_sent_at = loop.time()
            async with httpx.AsyncClient(timeout=10) as human:
                response = await human.post(
                    f"http://127.0.0.1:{api.port}/api/sessions/{session_id}/questions/{question_id}/answer",
                    headers=headers,
                    json={"answers": [{"text": banner}]},
                )
            assert response.status_code == 200, response.text
            return response

        async def observe(event: dict[str, Any]) -> None:
            nonlocal answer_task, question_seen_at
            if event.get("session_id") != session_id:
                return
            events.append(event)
            if event["type"] == "question_request" and not question_received.done():
                question_seen_at = loop.time()
                question_received.set_result(event)
                # Never block the broadcast bus while the human thinks.
                answer_task = asyncio.create_task(
                    answer_after_wait(event["question_id"]), name="delayed-human-answer"
                )
            if event["type"] == "result" and not final.done():
                final.set_result(event)

        manager.on_broadcast("codex-question-acceptance", observe)
        question_payload = [{
            "question": "Which text should the release banner display?",
            "options": [{"label": "Ready"}, {"label": "Review required"}],
        }]
        prompt = (
            "I need a one-line release banner and its wording is my product "
            "decision. Before writing it, invoke mcp__ask__user exactly once "
            f"with questions={json.dumps(question_payload)}. Wait for the "
            "tool to return my answer, including an Other/free-text answer. "
            "Then reply with only the exact banner text I selected. Do not "
            "call any other tool, guess a choice, or answer before the tool returns."
        )
        async with asyncio.timeout(180):
            await manager.start_message(session_id, prompt)
            await asyncio.wait(
                [question_received, final], return_when=asyncio.FIRST_COMPLETED
            )
            assert question_received.done(), (
                f"Codex ended without calling the HTTP question tool: {events!r}"
            )
            outcome = await final
            assert outcome["content"]["status"] == "success", events
            assert answer_task is not None and question_seen_at is not None
            assert answer_sent_at is not None, (
                f"Codex ended before the delayed human answer: {events!r}"
            )
            await answer_task
            assert answer_sent_at - question_seen_at >= 65

            asked = [event for event in events if event["type"] == "question_request"]
            assert len(asked) == 1, events
            answered = [event for event in events if event["type"] == "question_answer"]
            assert len(answered) == 1 and not answered[0].get("auto"), events
            assert answered[0]["question_id"] == asked[0]["question_id"]
            assert banner in answered[0]["content"]
            assert not [event for event in events if event["type"] in {"error", "question_closed"}], events
            finals = [event for event in events if event["type"] == "result"]
            assert len(finals) == 1 and not finals[0].get("is_error"), events
            ask_calls = [event for event in events if (
                event["type"] == "tool_use" and event.get("tool") == "mcp__ask__user"
            )]
            assert len(ask_calls) == 1, events
            tool_results = [event for event in events if (
                event["type"] == "tool_result" and event.get("tool_use_id") == ask_calls[0]["tool_use_id"]
            )]
            assert len(tool_results) == 1 and not tool_results[0].get("is_error"), events
            assert banner in tool_results[0].get("output", ""), events
            assistant = "\n".join(
                event.get("content", "") for event in events if event["type"] == "assistant_text"
            )
            assert banner in assistant, events
            async with httpx.AsyncClient(timeout=10) as human:
                history = await human.get(
                    f"http://127.0.0.1:{api.port}/api/sessions/{session_id}", headers=headers
                )
            assert history.status_code == 200, history.text
            detail = history.json()
            assert detail["pending_questions"] == []
            results = [message for message in detail["messages"] if message["type"] == "result"]
            assert len(results) == 1
            assert results[0]["content"]["status"] == "success"
    finally:
        if answer_task is not None:
            answer_task.cancel()
            await asyncio.gather(answer_task, return_exceptions=True)
        if session_id is not None:
            await manager.interrupt(session_id)
        await manager.stop_all_held_processes()
        if api is not None:
            await api.stop()
        await db.close()
