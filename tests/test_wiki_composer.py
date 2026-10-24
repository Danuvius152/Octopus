"""Composer feedback and cancellation must agree with the trusted WS boundary."""

import json
from pathlib import Path

import pytest

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
def test_legacy_off_cancels_maintenance_and_keeps_shared_lookup(case):
    assert explicit_mode(case["message"], "off") == "read"


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
    accepted = await submit_composer(runtime, monkeypatch, tmp_path, message, 'off')
    assert accepted['content'] == message and accepted['wiki_mode'] == 'read'


@pytest.mark.parametrize("command", ["/wiki维护", "/wiki 维护"])
@pytest.mark.parametrize("selection", [None, "off", "read"])
async def test_websocket_chinese_maintenance_uses_trusted_intent_and_respects_cancellation(
    runtime, monkeypatch, tmp_path, command, selection,
):
    message = command + " Library/projects/current.md"
    accepted = await submit_composer(runtime, monkeypatch, tmp_path, message, selection)
    assert accepted['content'] == message
    assert accepted['wiki_mode'] == ('maintain' if selection is None else 'read')
