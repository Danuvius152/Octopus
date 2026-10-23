"""Question creation belongs to a selected model run, never a signed claim alone."""

from __future__ import annotations

import asyncio
import sys
from dataclasses import replace

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from server.config import settings
from server.database import Database
from server.deps import get_session_manager
from server.harness import get_harness
from server.mcp_identity import McpScope, mint, scope_is_live, verify
from server.routers import questions, sessions
from server.sessions import SessionManager

QUESTIONS = [{"question": "Choose", "options": [{"label": "A"}, {"label": "B"}]}]
HUMAN_HEADERS = {"Authorization": "Bearer changeme"}


@pytest.fixture
async def question_runtime(monkeypatch, tmp_path, short_process_shutdown):
    database = Database(":memory:")
    await database.initialize()
    manager = SessionManager()
    await manager.initialize(database)
    agent = await database.get_system_agent()
    session = await manager.create_session(agent["id"], "Question scope", str(tmp_path), backend="codex")
    monkeypatch.setattr(settings, "ask_user_question_timeout_seconds", 0)
    events = []
    starts = asyncio.Queue()

    def argv(context):
        starts.put_nowait(context)
        code = "import json,time\n"
        code += "print(json.dumps({'type':'thread.started','thread_id':'question-thread'}),flush=True)\n"
        code += "print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':'Ready.'}}),flush=True)\n"
        code += "time.sleep(30)\n"
        return [sys.executable, "-u", "-c", code], {"cwd": context.working_dir}

    harness = get_harness("codex")
    monkeypatch.setattr(harness, "profile", replace(harness.profile, build_turn_argv=argv, prepare_resume=None))

    async def receive(event):
        events.append(event)

    manager.on_broadcast("question-scope", receive)
    app = FastAPI()
    app.include_router(questions.router)
    app.include_router(sessions.router)
    app.dependency_overrides[get_session_manager] = lambda: manager
    async with AsyncClient(transport=ASGITransport(app), base_url="http://test") as client:
        try:
            yield manager, session, client, events, starts
        finally:
            await manager.interrupt(session.id)
            await manager.await_session_execution(session.id, timeout=5)
            await manager.stop_all_held_processes()
            await database.close()


async def test_signed_scope_without_selected_run_cannot_create_question(question_runtime):
    _, session, client, events, _ = question_runtime
    response = await client.post(
        f"/api/sessions/{session.id}/questions",
        headers={"Authorization": f"Bearer {mint(session.id)}"},
        json={"questions": QUESTIONS},
    )
    assert response.status_code == 400
    assert response.json() == {"detail": "Model run is no longer active"}
    assert not [event for event in events if event["type"] == "question_request"]
    detail = await client.get(f"/api/sessions/{session.id}", headers=HUMAN_HEADERS)
    assert detail.json()["pending_questions"] == []
    assert not [message for message in detail.json()["messages"] if message["type"] == "question_request"]


async def begin_run(runtime, *, work_id="question-work", source="delegation"):
    manager, session, _, events, starts = runtime
    before = len(events)
    await manager.start_message(session.id, "Wait for a decision", source=source, work_id=work_id)
    context = await asyncio.wait_for(starts.get(), 5)
    async with asyncio.timeout(5):
        while not any(event["type"] == "assistant_text" for event in events[before:]):
            await asyncio.sleep(0.01)
    token = next(entry.credential for entry in context.mcp_servers if entry.key == "ask")
    return token, verify(token)


async def test_live_run_question_has_captured_work_in_history_and_broadcast(question_runtime):
    manager, session, client, events, _ = question_runtime
    token, scope = await begin_run(question_runtime)
    response = await client.post(
        f"/api/sessions/{session.id}/questions",
        headers={"Authorization": f"Bearer {token}"}, json={"questions": QUESTIONS},
    )
    assert response.status_code == 201
    question_id = response.json()["question_id"]
    event = next(event for event in events if event.get("question_id") == question_id)
    assert event["work_id"] == "question-work"
    assert event["request_id"] and event["attempt_id"]
    detail = await client.get(f"/api/sessions/{session.id}", headers=HUMAN_HEADERS)
    history = next(message for message in detail.json()["messages"] if message["tool_use_id"] == question_id)
    assert history["tool_input"]["work_id"] == "question-work"
    assert history["tool_input"]["request_id"] == event["request_id"]
    assert await manager.create_pending_question(session.id, QUESTIONS, scope=scope)


async def test_stop_revokes_old_question_scope_without_poisoning_new_request(question_runtime):
    manager, session, client, events, _ = question_runtime
    old_token, old_scope = await begin_run(question_runtime)
    old_question = await manager.create_pending_question(session.id, QUESTIONS, scope=old_scope)
    await manager.interrupt(session.id)
    assert not manager.has_pending_question(session.id, old_question)
    new_token, _ = await begin_run(question_runtime, work_id="new-question-work", source="human")
    before = len([event for event in events if event["type"] == "question_request"])
    stale = await client.post(
        f"/api/sessions/{session.id}/questions",
        headers={"Authorization": f"Bearer {old_token}"}, json={"questions": QUESTIONS},
    )
    assert stale.status_code == 401
    with pytest.raises(ValueError, match="Model run is no longer active"):
        await manager.create_pending_question(session.id, QUESTIONS, scope=old_scope)
    assert len([event for event in events if event["type"] == "question_request"]) == before
    current = await client.post(
        f"/api/sessions/{session.id}/questions",
        headers={"Authorization": f"Bearer {new_token}"}, json={"questions": QUESTIONS},
    )
    assert current.status_code == 201
    assert next(event for event in events if event.get("question_id") == current.json()["question_id"])["work_id"] == "new-question-work"


@pytest.mark.parametrize("invalid_claim", ["unsigned_run", "wrong_work", "wrong_user", "wrong_session"])
async def test_manager_and_rest_recheck_invalid_run_owner(question_runtime, invalid_claim):
    manager, session, client, events, _ = question_runtime
    _, live = await begin_run(question_runtime)
    replacement = {
        "unsigned_run": {"run_lease_id": "not-a-selected-run"},
        "wrong_work": {"work_id": "unrelated-work"},
        "wrong_user": {"user_id": "another-account"},
        "wrong_session": {"session_id": "another-session"},
    }[invalid_claim]
    invalid = replace(live, **replacement)
    token = mint(
        invalid.session_id, user_id=invalid.user_id,
        run_lease_id=invalid.run_lease_id, work_id=invalid.work_id,
    )
    response = await client.post(
        f"/api/sessions/{session.id}/questions",
        headers={"Authorization": f"Bearer {token}"}, json={"questions": QUESTIONS},
    )
    assert response.status_code == 401
    with pytest.raises(ValueError, match="Model run is no longer active"):
        await manager.create_pending_question(session.id, QUESTIONS, scope=invalid)
    assert not [event for event in events if event["type"] == "question_request"]


async def test_live_lease_for_previous_account_owner_cannot_create_question(question_runtime):
    manager, session, client, events, _ = question_runtime
    token, scope = await begin_run(question_runtime)
    assert await manager.adopt_orphan_sessions("new-account-owner") == 1
    assert not scope_is_live(scope)  # Adoption retires the previous owner's authority.
    response = await client.post(
        f"/api/sessions/{session.id}/questions",
        headers={"Authorization": f"Bearer {token}"}, json={"questions": QUESTIONS},
    )
    assert response.status_code == 401
    with pytest.raises(ValueError, match="Model run is no longer active"):
        await manager.create_pending_question(session.id, QUESTIONS, scope=scope)
    assert not [event for event in events if event["type"] == "question_request"]


async def test_idle_held_process_cannot_open_a_new_question(question_runtime, monkeypatch):
    manager, session, client, events, starts = question_runtime
    root = await manager.db.get_system_agent()
    held = await manager.create_session(root["id"], "Held questions", session.working_dir, backend="claude-code")

    def argv(context):
        starts.put_nowait(context)
        code = "import json,sys\n"
        code += "for line in sys.stdin:\n"
        code += " print(json.dumps({'type':'system','subtype':'init','session_id':'held-thread'}),flush=True)\n"
        code += " print(json.dumps({'type':'result','subtype':'success','session_id':'held-thread','result':'Done.','is_error':False}),flush=True)\n"
        return [sys.executable, "-u", "-c", code], {"cwd": context.working_dir}

    harness = get_harness("claude-code")
    monkeypatch.setattr(harness, "profile", replace(harness.profile, build_turn_argv=argv, prepare_resume=None))
    await manager.start_message(held.id, "Finish")
    context = await asyncio.wait_for(starts.get(), 5)
    async with asyncio.timeout(5):
        while not any(event["type"] == "result" and event["session_id"] == held.id for event in events):
            await asyncio.sleep(0.01)
    token = next(entry.credential for entry in context.mcp_servers if entry.key == "ask")
    scope = verify(token)
    assert scope_is_live(scope)
    response = await client.post(
        f"/api/sessions/{held.id}/questions",
        headers={"Authorization": f"Bearer {token}"}, json={"questions": QUESTIONS},
    )
    assert response.status_code == 400
    with pytest.raises(ValueError, match="Model run is no longer active"):
        await manager.create_pending_question(held.id, QUESTIONS, scope=scope)
    assert not [event for event in events if event["type"] == "question_request"]


async def test_human_and_internal_question_creation_remain_compatible(question_runtime):
    manager, session, client, _, _ = question_runtime
    response = await client.post(
        f"/api/sessions/{session.id}/questions", headers=HUMAN_HEADERS,
        json={"questions": QUESTIONS},
    )
    assert response.status_code == 201
    assert await manager.create_pending_question(session.id, QUESTIONS)
    with pytest.raises(ValueError, match="Model run is no longer active"):
        await manager.create_pending_question(session.id, QUESTIONS, scope=McpScope(session.id))
