"""Composer feedback and cancellation must agree with the trusted WS boundary."""

import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from server.deps import get_session_manager
from server.routers.sessions import router
from server.wiki.tasks import explicit_mode
from tests.test_ws_delivery import connection, hold_model_runs, receipt_frame
from tests.test_ws_delivery import runtime as runtime

CASES = json.loads(
    (Path(__file__).parent / "fixtures/wiki-command-cases.json").read_text()
)


@pytest.mark.parametrize("case", CASES)
def test_wiki_command_cases(case):
    assert explicit_mode(case["message"]) == case["mode"]


@pytest.mark.parametrize("case", CASES)
def test_read_cancels_maintenance_and_keeps_shared_lookup(case):
    assert explicit_mode(case["message"], "read") == "read"


async def submit_composer(runtime, monkeypatch, tmp_path, message, selection):
    manager, db, bearer = runtime
    agent = await db.get_system_agent()
    session = await manager.create_session(agent['id'], 'Wiki composer', str(tmp_path), backend='codex')
    hold_model_runs(manager, monkeypatch)
    payload = {'type': 'send_message', 'session_id': session.id, 'content': message,
               'client_request_id': 'wiki-composer-input'}
    if selection is not None:
        payload['wiki_mode'] = selection
    try:
        async with connection(manager, bearer) as browser:
            await browser[0].put({'type': 'websocket.receive', 'text': json.dumps(payload)})
            receipt = await receipt_frame(browser[1], 'submission')
            assert receipt['accepted'] is True, receipt
            assert receipt['client_request_id'] == 'wiki-composer-input'
            return receipt['input']
    finally:
        await manager.interrupt(session.id)
        await manager.await_session_execution(session.id, timeout=2)


async def test_websocket_cancelled_maintenance_keeps_shared_lookup(runtime, monkeypatch, tmp_path):
    message = "/wiki /wiki maintain 的用法是什么？"
    accepted = await submit_composer(runtime, monkeypatch, tmp_path, message, 'read')
    assert accepted['content'] == message and accepted['wiki_mode'] == 'read'


@pytest.mark.parametrize("transport", ["http", "ws"])
@pytest.mark.parametrize("selection", [None, "read"])
@pytest.mark.parametrize("message", ["/wiki 维护一下知识库", "/wiki 维护本地知识库", "/wiki 如何维护知识库？"])
async def test_source_only_input_preserves_raw_semantics_and_explicit_read_control(
    runtime, monkeypatch, tmp_path, transport, selection, message,
):
    if transport == "ws":
        accepted = await submit_composer(runtime, monkeypatch, tmp_path, message, selection)
    else:
        manager, db, bearer = runtime
        agent = await db.get_system_agent()
        session = await manager.create_session(agent["id"], "HTTP Wiki intent", str(tmp_path), backend="codex")
        hold_model_runs(manager, monkeypatch)
        app = FastAPI()
        app.include_router(router)
        app.dependency_overrides[get_session_manager] = lambda: manager
        payload = {"content": message, "client_request_id": "wiki-http-input"}
        if selection is not None:
            payload["wiki_mode"] = selection
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test",
                                   headers={"Authorization": "Bearer " + bearer}) as api:
                response = await api.post(f"/api/sessions/{session.id}/submissions", json=payload)
                assert response.status_code == 200, response.text
                assert response.json()["accepted"] is True
                accepted = response.json()["input"]
        finally:
            await manager.interrupt(session.id)
            await manager.await_session_execution(session.id, timeout=2)
    assert accepted["content"] == message
    assert accepted["wiki_mode"] == ("read" if selection == "read" else "auto")


@pytest.mark.parametrize("command", ["/wiki维护", "/wiki 维护"])
@pytest.mark.parametrize("selection", [None, "auto", "read"])
async def test_websocket_chinese_maintenance_uses_trusted_intent_and_respects_cancellation(
    runtime, monkeypatch, tmp_path, command, selection,
):
    message = command + " Library/projects/current.md"
    accepted = await submit_composer(runtime, monkeypatch, tmp_path, message, selection)
    assert accepted['content'] == message
    assert accepted['wiki_mode'] == ('read' if selection == 'read' else 'maintain')
