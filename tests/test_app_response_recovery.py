"""Finite HTTP observation recovers the same native execution and question."""

from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from importlib.resources import files

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from server import app_agent as app_agent_module
from server.agent_manager import AgentManager
from server.app_agent import AppAgentManager
from server.applications import ApplicationManager, app_scope_token
from server.config import settings
from server.deps import get_session_manager
from server.routers import applications, questions
from server.users import UserManager
from tests.native_app_server import (
    ROOT,
    inject,
    item,
    question_request,
    terminal,
    until,
)
from tests.native_app_server import native_runtime as native_runtime
from tests.session_runtime import wait_dispatch_idle


@pytest.fixture
async def native_app_http(native_runtime, tmp_path, monkeypatch):
    manager, database, session, mode, starts, events = native_runtime
    mode[0] = "control_output"
    monkeypatch.setattr(settings, "applications_dir", str(tmp_path / "apps"))
    monkeypatch.setattr(settings, "turn_idle_timeout_seconds", 0)
    await AgentManager(database).update_agent(session.agent_id, user_id=session.user_id, backend="codex")
    apps = ApplicationManager()
    apps.bind(session_mgr=manager, db=database)
    agent = AppAgentManager()
    agent.bind(session_mgr=manager, db=database, app_mgr=apps)
    path = tmp_path / "apps" / "recover"
    path.mkdir(parents=True)
    await database.save_application(app_id="recover", name="Recover", agent_id=session.agent_id, app_dir=str(path),
        entrypoint="index.html", status="ready", created_at="now", updated_at="now", user_id=session.user_id)
    monkeypatch.setattr(applications, "_manager", apps)
    monkeypatch.setattr(applications, "app_agent_manager", agent)
    app = FastAPI()
    app.include_router(applications.static_router)
    app.include_router(questions.router)
    app.dependency_overrides[get_session_manager] = lambda: manager
    bearer = await UserManager(database).issue_token(session.user_id)
    starts.clear()
    try:
        async with AsyncClient(transport=ASGITransport(app), base_url="http://test",
            headers={"X-Octopus-App-Token": app_scope_token("recover")}) as client:
            yield client, agent, manager, database, starts, events, bearer
    finally:
        for original in tuple(manager.sessions.values()):
            if original.app_id == "recover":
                await manager.interrupt(original.id)
                await manager.await_session_execution(original.id, timeout=5)
        agent.shutdown()
        apps.shutdown()


@pytest.fixture
def response_clock(monkeypatch):
    expired = asyncio.Event()

    class Clock:
        def __getattr__(self, name):
            return getattr(asyncio, name)

        async def wait_for(self, awaitable, timeout):
            original = asyncio.create_task(awaitable)
            timer = asyncio.create_task(expired.wait())
            try:
                done, _ = await asyncio.wait((original, timer), return_when=asyncio.FIRST_COMPLETED)
                if original in done:
                    return original.result()
                raise TimeoutError
            finally:
                timer.cancel()
                if not original.done():
                    original.cancel()
                await asyncio.gather(original, timer, return_exceptions=True)

    monkeypatch.setattr(app_agent_module, "asyncio", Clock())
    return expired


@pytest.mark.parametrize("delivery", ["ask", "chat"])
async def test_http_response_wait_ends_without_ending_the_original_native_question(
    native_app_http, tmp_path, response_clock, delivery,
):
    client, agent, manager, database, starts, _events, bearer = native_app_http
    question_delivered = asyncio.Event()

    async def delivered(event):
        if event["type"] == "question_request":
            question_delivered.set()

    manager.on_broadcast("response-question-delivered", delivered)
    observing = asyncio.create_task(client.post(f"/apps/recover/agent/{delivery}", json={"message": "Wait for a decision"}))
    try:
        await until(lambda: any(session.app_id == "recover" and session._run.backend is not None
                    and session._run.backend.active_turn is not None for session in manager.sessions.values()))
        original = next(session for session in manager.sessions.values() if session.app_id == "recover")
        inject(tmp_path, question_request("response-wait"))
        await until(lambda: manager.get_pending_questions(original.id))
        question, = manager.get_pending_questions(original.id)
        await asyncio.wait_for(question_delivered.wait(), 3)
        response_clock.set()
        response = await asyncio.wait_for(observing, 3)
        if delivery == "ask":
            assert response.status_code == 504
            recovery = response.json()["detail"]
        else:
            assert response.status_code == 200
            frames = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
            assert any(frame["type"] == "question" for frame in frames)
            recovery = frames[-1]
            assert recovery["type"] == "error"
        assert recovery["code"] == "response_wait_timeout" and recovery["operation_terminal"] is False
        assert recovery["conversation_id"] == original.id and recovery["request_id"] == question.request_id
        assert recovery["state"] == "running" and recovery["waiting_for_input"] is True
        assert manager.has_pending_question(original.id, question.question_id)
        assert original._run.backend.is_alive()
        answer = await client.post(f"/api/sessions/{original.id}/questions/{question.question_id}/answer",
            headers={"Authorization": f"Bearer {bearer}"}, json={"answers": [{"text": "The user's decision"}]})
        assert answer.status_code == 200
        inject(tmp_path, item(ROOT, "turn-1", "recovered", "Recovered original output"), terminal())
        await wait_dispatch_idle(manager, original.id)
        status = (await client.get(recovery["status_url"])).json()
        assert status["state"] == "completed" and status["reply"] == "Recovered original output"
        assert status["request_id"] == question.request_id
        assert len(starts) == 1
        assert (await database.get_question_result(original.id, question.question_id))["resolution"]["disposition"] == "answered"
    finally:
        response_clock.set()
        await asyncio.gather(observing, return_exceptions=True)
        manager.remove_broadcast("response-question-delivered")


async def test_client_module_is_the_actual_app_authenticated_source(native_app_http):
    client, *_ = native_app_http
    response = await client.get("/apps/recover/agent/client.js")
    assert response.status_code == 200 and "javascript" in response.headers["content-type"]
    assert response.text == files("server").joinpath("app_agent_client.js").read_text(encoding="utf-8")
    assert response.headers["cache-control"] == "no-store"
    assert (await client.get("/apps/recover/agent/client.js", headers={"X-Octopus-App-Token": "invalid"})).status_code == 401


@pytest.mark.parametrize("delivery", ["ask", "chat"])
@pytest.mark.parametrize("observation", ["committed", "storage-unavailable", "lookup-timeout", "invalid-reference", "notification-failed"])
async def test_wait_receipt_survives_committed_result_and_observation_failure_windows(
    native_app_http, tmp_path, response_clock, monkeypatch, delivery, observation,
):
    client, agent, manager, database, starts, _events, _bearer = native_app_http
    entered, release = asyncio.Event(), asyncio.Event()
    record, read = database.record_terminal, database.read_session_result

    async def hold_notification(event):
        if event.get("terminal"):
            entered.set()
            await release.wait()
            if observation == "notification-failed":
                raise OSError("Original required notification failed")

    manager.on_broadcast("original-result-delivery", hold_notification)
    if observation == "invalid-reference":
        async def corrupt_reference(**kwargs):
            # Fault at the storage boundary: the real reader must reject this
            # persisted reference instead of borrowing adjacent output.
            content = deepcopy(kwargs["content"])
            content["final_answer"]["message_seqs"] = [kwargs["seq"] + 1]
            return await record(**{**kwargs, "content": content})
        monkeypatch.setattr(database, "record_terminal", corrupt_reference)
    observing = asyncio.create_task(client.post(f"/apps/recover/agent/{delivery}", json={"message": "Finish once"}))
    try:
        await until(lambda: any(session.app_id == "recover" and session._run.backend is not None
                    and session._run.backend.active_turn is not None for session in manager.sessions.values()))
        original = next(session for session in manager.sessions.values() if session.app_id == "recover")
        request_id = original._run.request.request_id
        inject(tmp_path, item(ROOT, "turn-1", "saved-before-response", "Saved before response expiry"), terminal())
        await asyncio.wait_for(entered.wait(), 3)
        if observation in {"storage-unavailable", "lookup-timeout"}:
            async def unavailable(*args):
                if observation == "lookup-timeout":
                    await asyncio.Event().wait()
                raise OSError("Original status storage unavailable")
            monkeypatch.setattr(database, "read_session_result", unavailable)
            monkeypatch.setattr(app_agent_module, "_STATUS_READ_TIMEOUT_SECONDS", .01)
        response_clock.set()
        response = await asyncio.wait_for(observing, 3)
        recovery = response.json()["detail"] if delivery == "ask" else [
            json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")][-1]
        assert recovery["code"] == "response_wait_timeout" and recovery["operation_terminal"] is False
        assert recovery["conversation_id"] == original.id and recovery["request_id"] == request_id
        assert recovery["state"] == ("unknown" if observation in {"storage-unavailable", "lookup-timeout", "invalid-reference"} else "completed")
        assert recovery["waiting_for_input"] is False
        if recovery["state"] == "unknown":
            assert recovery["observation_error"]
        assert agent.list_conversations("recover")[0]["running"], "The original notification still owns its execution slot"
        monkeypatch.setattr(database, "read_session_result", read)
        status = await client.get(recovery["status_url"])
        assert status.status_code == (409 if observation == "invalid-reference" else 200)
        release.set()
        await wait_dispatch_idle(manager, original.id)
        assert len(starts) == 1
        if observation == "notification-failed":
            assert (await client.get(recovery["status_url"])).json()["state"] == "completed"
    finally:
        release.set()
        response_clock.set()
        await asyncio.gather(observing, return_exceptions=True)
        manager.remove_broadcast("original-result-delivery")
