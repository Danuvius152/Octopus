"""Unsupported actions share the generic refusal and cannot control a run."""

import asyncio
import json

import pytest

from tests.test_ws_delivery import (
    connection,
    hold_model_runs,
    receipt_frame,
    start_held_request,
)
from tests.test_ws_delivery import runtime as runtime


@pytest.mark.parametrize('operation', ['approve_tool', 'deny_tool', 'unknown_action'])
async def test_unsupported_command_needs_no_execution_target_and_keeps_replay_rules(
    runtime, monkeypatch, tmp_path, operation,
):
    manager, db, bearer = runtime
    agent = await db.get_system_agent()
    session = await manager.create_session(agent['id'], 'Unsupported command', str(tmp_path), backend='codex')
    starts = hold_model_runs(manager, monkeypatch)
    await start_held_request(manager, session.id, 'Keep the current request running')
    running = await asyncio.wait_for(starts.get(), 2)
    question = await manager.create_pending_question(session.id, [
        {'question': 'Current question', 'options': [{'label': 'Keep waiting'}]},
    ])
    before = manager.dispatch_snapshot(session.id)
    payload = {'type': operation, 'session_id': session.id, 'client_command_id': 'unsupported-command'}
    try:
        async with connection(manager, bearer) as browser:
            async def send(data):
                await browser[0].put({'type': 'websocket.receive', 'text': json.dumps(data)})
                return await receipt_frame(browser[1], 'command_receipt')

            first = await send(payload)
            assert first['accepted'] is False and first['code'] == 'invalid_command'
            assert first['message'] == 'Unsupported command'
            assert await send(payload) == first
            conflict = await send({**payload, 'reason': 'Changed body'})
            assert conflict['accepted'] is False and conflict['code'] == 'command_conflict'
            assert manager.dispatch_snapshot(session.id) == before
            assert manager.has_pending_question(session.id, question)
            assert not running.stopped.is_set()
            assert starts.empty()
    finally:
        await manager.delete_session(session.id)


@pytest.mark.parametrize('operation', ['approve_tool', 'deny_tool', 'unknown_action'])
async def test_unknown_wire_action_is_refused_without_session_side_effects_or_socket_loss(
    runtime, tmp_path, operation,
):
    manager, db, bearer = runtime
    agent = await db.get_system_agent()
    session = await manager.create_session(agent['id'], 'Unsupported wire action', str(tmp_path), backend='codex')
    question = await manager.create_pending_question(session.id, [
        {'question': 'Current question', 'options': [{'label': 'Keep waiting'}]},
    ])
    before = manager.dispatch_snapshot(session.id)
    try:
        async with connection(manager, bearer) as browser:
            for target in ('owned', 'missing', 'foreign'):
                session.user_id = 'another-account' if target == 'foreign' else agent['user_id']
                payload = {'type': operation, 'session_id': 'missing-session' if target == 'missing' else session.id,
                           'tool_use_id': 'old-tool'}
                await browser[0].put({'type': 'websocket.receive', 'text': json.dumps(payload)})
                response = await receipt_frame(browser[1], 'error')
                assert response['message'] == 'Unsupported client message type'
                assert manager.dispatch_snapshot(session.id) == before
                assert manager.has_pending_question(session.id, question)
            await browser[0].put({'type': 'websocket.receive', 'text': 'invalid json'})
            assert (await receipt_frame(browser[1], 'error'))['message'] == 'Invalid JSON'
    finally:
        session.user_id = agent['user_id']
        await manager.delete_session(session.id)


@pytest.mark.parametrize('operation', ['approve_tool', 'deny_tool', 'unknown_action'])
async def test_unsupported_commands_keep_foreign_and_missing_sessions_indistinguishable(
    runtime, tmp_path, operation,
):
    manager, db, bearer = runtime
    agent = await db.get_system_agent()
    session = await manager.create_session(agent['id'], 'Private command', str(tmp_path), backend='codex')
    question = await manager.create_pending_question(session.id, [
        {'question': 'Private question', 'options': [{'label': 'Keep waiting'}]},
    ])
    before = manager.dispatch_snapshot(session.id)
    session.user_id = 'another-account'
    try:
        async with connection(manager, bearer) as browser:
            for target in (session.id, 'missing-session'):
                payload = {'type': operation, 'session_id': target, 'client_command_id': 'unowned-command'}
                await browser[0].put({'type': 'websocket.receive', 'text': json.dumps(payload)})
                refusal = await receipt_frame(browser[1], 'command_receipt')
                assert refusal['accepted'] is False and refusal['code'] == 'not_found'
                assert refusal['message'] == 'Session not found'
                assert manager.dispatch_snapshot(session.id) == before
                assert manager.has_pending_question(session.id, question)
    finally:
        session.user_id = agent['user_id']
        await manager.delete_session(session.id)
