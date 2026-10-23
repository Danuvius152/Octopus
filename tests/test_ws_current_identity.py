"""External browser actions require the current correlated protocol."""

import asyncio
import json

import pytest

from server.config import settings
from tests.test_ws_delivery import (
    connection,
    frame,
    hold_model_runs,
    receipt_frame,
    start_held_request,
)
from tests.test_ws_delivery import runtime as runtime

MISSING = object()
INVALID_IDENTITIES = [
    pytest.param(MISSING, id='missing'), pytest.param(None, id='null'),
    pytest.param('', id='empty'), pytest.param(42, id='number'),
    pytest.param(True, id='boolean'), pytest.param('x' * 129, id='overlong'),
]


@pytest.mark.parametrize('identity', INVALID_IDENTITIES)
async def test_send_without_valid_client_identity_has_no_dispatch_effect(runtime, monkeypatch, tmp_path, identity):
    manager, db = runtime
    agent = await db.get_system_agent()
    session = await manager.create_session(agent['id'], 'Current identity', str(tmp_path), backend='codex')
    starts = hold_model_runs(manager, monkeypatch)
    before = manager.dispatch_snapshot(session.id)
    try:
        async with connection(manager, settings.auth_token) as browser:
            payload = {
                'type': 'send_message', 'session_id': session.id, 'content': 'Must not execute without identity',
            }
            if identity is not MISSING:
                payload['client_request_id'] = identity
            await browser[0].put({'type': 'websocket.receive', 'text': json.dumps(payload)})
            refusal = await frame(browser[1])
            assert refusal['type'] == 'error'
            assert 'identity' in refusal['message'].lower()
            assert manager.dispatch_snapshot(session.id) == before
            assert starts.empty()
    finally:
        await manager.interrupt(session.id)
        await manager.await_session_execution(session.id, timeout=2)


async def send_control(browser, payload):
    await browser[0].put({'type': 'websocket.receive', 'text': json.dumps(payload)})
    return await receipt_frame(browser[1], 'command_receipt')


async def test_current_submission_identity_replays_conflicts_and_keeps_new_ids_distinct(runtime, monkeypatch, tmp_path):
    manager, db = runtime
    agent = await db.get_system_agent()
    session = await manager.create_session(agent['id'], 'Distinct inputs', str(tmp_path), backend='codex')
    starts = hold_model_runs(manager, monkeypatch)
    await start_held_request(manager, session.id, 'Unsteerable original', wiki_mode='read')
    await asyncio.wait_for(starts.get(), 2)
    payload = {'type': 'send_message', 'session_id': session.id, 'content': 'Identical text', 'client_request_id': 'x' * 128}
    try:
        async with connection(manager, settings.auth_token) as browser:
            async def send(data):
                await browser[0].put({'type': 'websocket.receive', 'text': json.dumps(data)})
                return await receipt_frame(browser[1], 'submission')

            first = await send(payload)
            replay = await send(payload)
            conflict = await send({**payload, 'content': 'Changed input'})
            second = await send({**payload, 'client_request_id': 'new-input'})
            assert first['accepted'] is True and replay['request_id'] == first['request_id']
            assert conflict['accepted'] is False and conflict['code'] == 'submission_conflict'
            assert second['accepted'] is True and second['request_id'] != first['request_id']
            assert [item['request_id'] for item in manager.dispatch_snapshot(session.id)['items']] == [
                first['request_id'], second['request_id'],
            ]
            assert starts.empty()
    finally:
        await manager.delete_session(session.id)


@pytest.mark.parametrize('operation', ['send_message', 'interrupt', 'answer_question'])
async def test_identity_refusal_for_unknown_session_preserves_the_socket(runtime, operation):
    manager, _ = runtime
    async with connection(manager, settings.auth_token) as browser:
        await browser[0].put({'type': 'websocket.receive', 'text': json.dumps({
            'type': operation, 'session_id': 'unknown', 'content': 'Unaccepted input',
        })})
        refusal = await frame(browser[1])
        assert refusal['type'] == 'error' and 'identity' in refusal['message'].lower()
        await browser[0].put({'type': 'websocket.receive', 'text': 'invalid json'})
        assert (await frame(browser[1]))['message'] == 'Invalid JSON'


@pytest.mark.parametrize('missing', ['request_id', 'attempt_id'])
async def test_idle_stop_requires_explicit_null_targets(runtime, tmp_path, missing):
    manager, db = runtime
    agent = await db.get_system_agent()
    session = await manager.create_session(agent['id'], 'Idle identity', str(tmp_path), backend='codex')
    before = manager.dispatch_snapshot(session.id)
    payload = {'type': 'interrupt', 'session_id': session.id, 'client_command_id': 'missing-target',
               'request_id': None, 'attempt_id': None, 'generation': before['generation'], 'revision': before['revision']}
    del payload[missing]
    async with connection(manager, settings.auth_token) as browser:
        refusal = await send_control(browser, payload)
        assert refusal['accepted'] is False and refusal['code'] == 'stale_command'
        assert manager.dispatch_snapshot(session.id) == before
        accepted = await send_control(browser, {**payload, missing: None, 'client_command_id': 'explicit-idle-target'})
        assert accepted['accepted'] is True
        assert manager.dispatch_snapshot(session.id)['paused'] is True


async def test_idle_stop_captured_before_a_completed_turn_cannot_pause_the_new_idle_state(runtime, monkeypatch, tmp_path):
    manager, db = runtime
    agent = await db.get_system_agent()
    session = await manager.create_session(agent['id'], 'Idle cycle', str(tmp_path), backend='codex')
    before = manager.dispatch_snapshot(session.id)
    starts = hold_model_runs(manager, monkeypatch)
    receipt, _ = await start_held_request(manager, session.id, 'One intervening turn')
    run = await asyncio.wait_for(starts.get(), 2)
    await run.stop()
    await asyncio.wait_for(receipt.wait(), 2)
    await manager.await_session_execution(session.id, timeout=2)
    current = manager.dispatch_snapshot(session.id)
    assert current['active_request_id'] is None and current['paused'] is False
    assert current['revision'] > before['revision'] and current['generation'] == before['generation']
    async with connection(manager, settings.auth_token) as browser:
        payload = {'type': 'interrupt', 'session_id': session.id, 'client_command_id': 'late-idle-stop',
                   'request_id': None, 'attempt_id': None, 'generation': before['generation'], 'revision': before['revision']}
        refusal = await send_control(browser, payload)
        assert refusal['accepted'] is False and refusal['code'] == 'stale_command'
        assert manager.dispatch_snapshot(session.id) == current
        accepted = await send_control(browser, {**payload, 'client_command_id': 'current-idle-stop', 'revision': current['revision']})
        assert accepted['accepted'] is True


async def test_correlated_question_requires_the_complete_answer_batch(runtime, monkeypatch, tmp_path):
    manager, db = runtime
    agent = await db.get_system_agent()
    session = await manager.create_session(agent['id'], 'Whole answers', str(tmp_path), backend='codex')
    starts = hold_model_runs(manager, monkeypatch)
    receipt, scope = await start_held_request(manager, session.id, 'Answer both questions')
    await asyncio.wait_for(starts.get(), 2)
    question = await manager.create_pending_question(session.id, [{'question': 'First?'}, {'question': 'Second?'}])
    payload = {'type': 'answer_question', 'session_id': session.id, 'question_id': question,
               'request_id': receipt.request_id, 'attempt_id': scope['attempt_id']}
    try:
        async with connection(manager, settings.auth_token) as browser:
            for number, answers in enumerate(([{'text': 'first'}], [{'text': 'first'}, {}])):
                refusal = await send_control(browser, {**payload, 'client_command_id': f'incomplete-{number}', 'answers': answers})
                assert refusal['accepted'] is False
                assert manager.has_pending_question(session.id, question)
            accepted = await send_control(browser, {**payload, 'client_command_id': 'complete',
                                                    'answers': [{'text': 'first'}, {'text': 'second'}]})
            assert accepted['accepted'] is True
            answer = await manager.wait_for_question_answer(session.id, question, timeout=.1)
            assert 'first' in answer and 'second' in answer
    finally:
        await manager.interrupt(session.id)
        await asyncio.wait_for(receipt.wait(), 2)
        await manager.await_session_execution(session.id, timeout=2)


@pytest.mark.parametrize('operation', ['interrupt', 'answer_question'])
@pytest.mark.parametrize('identity', INVALID_IDENTITIES)
async def test_control_without_valid_command_identity_preserves_the_current_execution(
    runtime, monkeypatch, tmp_path, operation, identity,
):
    manager, db = runtime
    agent = await db.get_system_agent()
    session = await manager.create_session(agent['id'], 'Scoped control', str(tmp_path), backend='codex')
    starts = hold_model_runs(manager, monkeypatch)
    receipt, scope = await start_held_request(manager, session.id, 'Keep the original execution')
    await asyncio.wait_for(starts.get(), 2)
    question_id = await manager.create_pending_question(session.id, [{'question': 'Choose', 'options': [{'label': 'A'}]}])
    before = manager.dispatch_snapshot(session.id)
    try:
        async with connection(manager, settings.auth_token) as browser:
            payload = {
                'type': operation, 'session_id': session.id,
                'request_id': receipt.request_id, 'attempt_id': scope['attempt_id'],
                'generation': before['generation'], 'revision': before['revision'],
                'question_id': question_id, 'answers': [{'selected': ['A']}],
            }
            if identity is not MISSING:
                payload['client_command_id'] = identity
            await browser[0].put({'type': 'websocket.receive', 'text': json.dumps(payload)})
            refusal = await frame(browser[1])
            assert refusal['type'] == 'error'
            assert 'identity' in refusal['message'].lower()
            assert manager.dispatch_snapshot(session.id) == before
            assert manager.has_pending_question(session.id, question_id)
            assert starts.empty()
    finally:
        await manager.interrupt(session.id)
        await asyncio.wait_for(receipt.wait(), 2)
        await manager.await_session_execution(session.id, timeout=2)
