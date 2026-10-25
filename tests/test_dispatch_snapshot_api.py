"""The current dispatch contract through live and archived session details."""

from server.session_manager import session_manager
from tests.test_api import client as client


async def test_live_detail_exposes_only_the_current_dispatch_snapshot(client):
    agent = await session_manager.db.get_system_agent()
    created = await client.post('/api/sessions', headers=client.headers, json={
        'name': 'Current dispatch', 'agent_id': agent['id'],
    })
    assert created.status_code == 201
    session_id = created.json()['id']
    response = await client.get(f'/api/sessions/{session_id}', headers=client.headers)
    assert response.status_code == 200
    detail = response.json()
    assert 'pending_queue' not in detail
    assert detail['dispatch_state']['generation']
    assert detail['dispatch_state']['items'] == []
    assert detail['dispatch_state']['paused'] is False
    assert 'recording_failure' not in detail['dispatch_state']


async def test_archived_detail_remains_readable_without_a_live_dispatch_owner(client):
    agent = await session_manager.db.get_system_agent()
    created = await client.post('/api/sessions', headers=client.headers, json={
        'name': 'Archived dispatch', 'agent_id': agent['id'],
    })
    session_id = created.json()['id']
    archived = await client.post(f'/api/sessions/{session_id}/archive?replace=false', headers=client.headers)
    assert archived.status_code == 204
    assert session_manager.get_session(session_id) is None
    for _ in range(2):
        response = await client.get(f'/api/sessions/{session_id}', headers=client.headers)
        assert response.status_code == 200
        detail = response.json()
        assert detail['archived'] is True
        assert detail['dispatch_state'] is None
        assert 'pending_queue' not in detail
        assert session_manager.get_session(session_id) is None
