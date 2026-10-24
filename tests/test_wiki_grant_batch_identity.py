"""Current batches retain identities and supporting receipts stay auxiliary."""

import pytest

from server.wiki.tasks import tasks
from server.wiki.workflow import view
from tests import test_wiki_api as api
from tests.test_wiki_grant_v2 import assigned
from tests.test_wiki_supporting_navigation import addition, setup_notes

client = api.client


async def read(client, headers, doc):
    result = await client.get('/api/wiki/read', headers=headers, params={'doc': doc})
    assert result.status_code == 200, result.text
    return result.json()


@pytest.mark.parametrize('operation', ['organize', 'navigation'])
async def test_batch_keeps_its_original_identities_when_another_job_item_moves_into_its_path(client, tmp_path, operation):
    root, jobs, _, config, _ = await setup_notes(client, tmp_path)
    a, b, c, d = 'Library/requested.md', 'Library/other.md', 'Library/c.md', 'Library/d.md'
    for path in (c, d):
        (root / path).write_text('# Batch member\n\nKeep this selected text.\n')
    job = jobs.maintain('wide-finite-job', '', 'Review the four explicitly selected notes', [a, b, c, d], None, [], 'human')
    async with assigned(job, config, paths=[a, c, d], client=client) as (headers, task):
        original_a = await read(client, headers, a)
        original_b = await read(client, headers, b)
        moved_a = 'Library/moved-A.md'
        (root / a).rename(root / moved_a)
        (root / b).rename(root / a)
        jobs.store.refresh()
        current_a = await read(client, headers, moved_a)
        current_b = await read(client, headers, a)
        assert current_a['id'] == original_a['id'] and current_b['id'] == original_b['id']
        before = (root / a).read_bytes()
        if operation == 'organize':
            body = {'path': a, 'expected_revision': current_b['revision'], 'key_note': 'A summary outside this batch.', 'reason': 'Exercise only the original batch authority'}
        else:
            navigation = await read(client, headers, 'Library/index.md')
            body = addition(current_b, navigation)
        denied = await client.post('/api/wiki/organize', headers=headers, json=body)
        assert denied.status_code == 403, denied.text
        assert (root / a).read_bytes() == before
        grant = tasks.current_grant(task)
        assert {row['doc_id'] for row in grant['selected_documents']} == {item['doc'] for item in job['items'] if item['path'] in {a, c, d}}
        with jobs.store.connect() as db:
            assert db.execute('SELECT count(*) FROM operation_receipts WHERE job_id=?', (job['id'],)).fetchone()[0] == 0
        card = view(task).card
        assert moved_a in card['writable_files'] and a not in card['writable_files'] and b not in card['writable_files']
        allowed = await client.post('/api/wiki/organize', headers=headers, json={
            'path': moved_a, 'expected_revision': current_a['revision'], 'key_note': 'The selected original A remains writable after its move.', 'reason': 'Preserve the captured A identity'})
        assert allowed.status_code == 200, allowed.text
        assert (root / a).read_bytes() == before


async def test_navigation_no_change_cannot_complete_a_changed_created_navigation_page(client, tmp_path):
    root, jobs, _, config, _ = await setup_notes(client, tmp_path)
    a, b = 'Library/requested.md', 'Library/created-index.md'
    job = jobs.maintain('mixed-created-index', '', 'Review A and create a finite navigation page', [a], None, [], 'human',
                        creation={'purpose': 'requested_knowledge_page', 'paths': [b]})
    creation = next(item for item in job['items'] if item.get('kind') == 'creation')
    async with assigned(job, config, client=client) as (headers, _):
        created = await client.post('/api/wiki/publish', headers=headers, json={
            'path': b, 'expected_revision': None, 'kind': 'topic', 'content': '# Navigation\n\nKeep this original menu.\n',
            'reservation_id': creation['reservation_id'], 'reason': 'Create the requested navigation page'})
        assert created.status_code == 200, created.text
    (root / b).write_bytes((root / b).read_bytes() + b'\nA human changed the navigation conditions.\n')
    jobs.update(job['id'], status='interrupted', session_id=None)
    retried = await client.post(f"/api/wiki/jobs/{job['id']}/retry", headers=client.headers)
    assert retried.status_code == 200, retried.text
    job = retried.json()
    assert next(item for item in job['items'] if item.get('kind') == 'creation')['status'] == 'attention'
    async with assigned(job, config, paths=[a], client=client) as (headers, _):
        source = await read(client, headers, a)
        navigation = await read(client, headers, b)
        added = await client.post('/api/wiki/organize', headers=headers, json=addition(source, navigation))
        assert added.status_code == 200, added.text
        navigation = await read(client, headers, b)
        repeated = await client.post('/api/wiki/organize', headers=headers, json=addition(source, navigation))
        assert repeated.status_code == 200 and repeated.json()['changed'] is False, repeated.text
        accepted = await client.post('/api/wiki/revise', headers=headers, json={'doc': source['id'], 'expected_revision': source['revision'], 'operation': 'no_change', 'reason': 'A satisfies its own review'})
        assert accepted.status_code == 200, accepted.text
        for item in job['items']:
            if item.get('doc') == source['id']:
                assert jobs.recover_publication(job['id'], item) and item['status'] == 'done'
    before = (root / b).read_bytes()
    jobs.update(job['id'], status='completed_with_attention', session_id=None, items=job['items'])
    retry = await client.post(f"/api/wiki/jobs/{job['id']}/retry", headers=client.headers)
    assert retry.status_code == 200, retry.text
    job = retry.json()
    item = next(item for item in job['items'] if item.get('kind') == 'creation')
    assert item['status'] == 'attention' and item.get('operation') != 'navigation_link', item
    assert (root / b).read_bytes() == before
    async with assigned(job, config, paths=[b], client=client) as (headers, _):
        navigation = await read(client, headers, b)
        accepted = await client.post('/api/wiki/revise', headers=headers, json={'doc': navigation['id'], 'expected_revision': navigation['revision'], 'operation': 'no_change', 'reason': 'B itself has now been fully reviewed'})
        assert accepted.status_code == 200, accepted.text
    assert jobs.recover_creation(job['id'], item) and item['status'] == 'done'
    assert item['receipt_id'] == accepted.json()['receipt_id'] and item['operation'] == 'no_change'
    assert (root / b).read_bytes() == before
