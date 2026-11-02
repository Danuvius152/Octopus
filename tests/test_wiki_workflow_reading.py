"""Worker decisions and delivery reuse must not manufacture reading evidence."""

import json

import pytest

from server.wiki.registry import registry
from server.wiki.store import WikiError
from server.wiki.tasks import tasks
from tests import wiki_api as wiki_api
from tests.test_wiki_write_gates import active_worker

client = wiki_api.client


async def prepare(client, tmp_path):
    root = tmp_path / 'vault'
    root.mkdir()
    (root / 'source.md').write_text('# Source\n\nIntro.\n\n## Claim\n\nA claim with a necessary condition.\n')
    config = await wiki_api.configure(client, root)
    source = (await client.get('/api/wiki/read', params={'doc': 'source.md'})).json()
    (root / 'target.md').write_text(
        '---\nsources:\n' + ''.join(
            f'  - resource: source.md\n    revision: {source["revision"]}\n    location: {location}\n'
            for location in ('L1', 'L3', '#claim')
        ) + '---\n# Target\n\nCompare the three distinct occurrences.\n'
    )
    return root, config, source


async def test_compact_source_details_continue_without_body_evidence_and_expire_on_source_change(client, tmp_path):
    root, config, source = await prepare(client, tmp_path)
    async with active_worker(config, ['target.md'], client=client) as headers:
        initial = await client.get('/api/wiki/read', headers=headers,
                                   params={'doc': 'target.md', 'view': 'compact', 'length': 1, 'source_limit': 1})
        assert initial.status_code == 200, initial.text
        value = initial.json()
        page = value['sources']
        assert page['total'] == 3 and len(page['items']) == 1 and not page['complete']
        assert page['items'][0]['doc'] == source['id']
        assert page['items'][0]['current_revision'] == source['revision']
        current = next(task for task in tasks.active.values() if task.job_id)
        before = {key: list(ranges) for key, ranges in current.reads.items()}
        continued = await client.get('/api/wiki/read', headers=headers, params={
            'doc': value['id'], 'source_cursor': page['next_cursor'], 'source_limit': 1,
        })
        assert continued.status_code == 200, continued.text
        assert 'content' not in continued.json()
        assert current.reads == before
        second = continued.json()['sources']
        assert second['items'][0]['ordinal'] == 1 and second['next_cursor']
        (root / 'source.md').write_text((root / 'source.md').read_text() + '\nA real source change.\n')
        changed = await client.get('/api/wiki/read', headers=headers, params={
            'doc': value['id'], 'source_cursor': second['next_cursor'],
        })
        assert changed.status_code == 409 and 'dependencies changed' in changed.text
        assert current.reads == before
        (root / 'target.md').write_text('# Target\n\nThe source list was removed by the owner.\n')
        removed = await client.get('/api/wiki/read', headers=headers, params={
            'doc': value['id'], 'source_cursor': second['next_cursor'],
        })
        assert removed.status_code == 409 and 'restart' in removed.text
        assert current.reads == before


async def test_location_batch_reuses_only_explicit_current_task_delivery(client, tmp_path):
    _, config, source = await prepare(client, tmp_path)
    request = {'doc': source['id'], 'expected_revision': source['revision'], 'location': '#claim'}
    async with active_worker(config, ['target.md'], client=client) as headers:
        original = await client.post('/api/wiki/read-many', headers=headers, json={'items': [request, request], 'view': 'compact'})
        assert original.status_code == 200, original.text
        first = original.json()
        assert len(first['ranges']) == 1
        assert first['ranges'][0]['content'] == '## Claim\n\nA claim with a necessary condition.\n'
        guide = await client.get('/api/wiki/guide', headers=headers, params={'module': 'maintenance'})
        assert guide.status_code == 200, guide.text
        assert any(item['doc'] == source['id'] for item in guide.json()['reading']['documents'])
        repeated = await client.post('/api/wiki/read-many', headers=headers, json={
            'items': [request], 'reuse_delivered': True, 'view': 'compact',
        })
        assert repeated.status_code == 200, repeated.text
        second = repeated.json()
        assert second['items'][0]['status'] == 'already_delivered'
        assert second['characters_returned'] == 0 and second['ranges'] == []
        # A deliberate context-recovery read still returns the real passage.
        recovered = await client.post('/api/wiki/read-many', headers=headers, json={'items': [request], 'view': 'compact'})
        assert recovered.status_code == 200 and recovered.json()['characters_returned'] == first['characters_returned']
        task = next(task for task in tasks.active.values() if task.job_id)
        task.reads.clear()
        fresh = await client.post('/api/wiki/read-many', headers=headers, json={
            'items': [request], 'reuse_delivered': True, 'view': 'compact',
        })
        assert fresh.status_code == 200 and fresh.json()['ranges']


async def test_location_pagination_never_claims_unreturned_source_text(client, tmp_path):
    _, config, source = await prepare(client, tmp_path)
    async with active_worker(config, ['target.md'], client=client) as headers:
        result = await client.get('/api/wiki/read', headers=headers, params={
            'doc': source['id'], 'expected_revision': source['revision'], 'location': '#claim', 'length': 8,
        })
        assert result.status_code == 200, result.text
        first = result.json()
        assert first['content'] == '## Claim' and first['next_start'] == first['end']
        task = next(task for task in tasks.active.values() if task.job_id)
        store = registry.store(task.user_id)
        with pytest.raises(WikiError, match='cited current passage'):
            tasks.require_source_read(task, store, {'doc': source['id'], 'revision': source['revision'], 'location': '#claim'})
        last = await client.get('/api/wiki/read', headers=headers, params={
            'doc': source['id'], 'expected_revision': source['revision'], 'location': '#claim', 'start': first['next_start'],
        })
        assert last.status_code == 200 and last.json()['next_start'] is None
        tasks.require_source_read(task, store, {'doc': source['id'], 'revision': source['revision'], 'location': '#claim'})
        invalid = await client.get('/api/wiki/read', headers=headers, params={'doc': source['id'], 'location': '#absent'})
        assert invalid.status_code == 422


async def test_actual_raw_source_passage_satisfies_citation_without_a_second_delivery(client, tmp_path):
    _, config, source = await prepare(client, tmp_path)
    async with active_worker(config, ['target.md'], client=client) as headers:
        reference = {'doc': source['id'], 'revision': source['revision'], 'location': '#claim'}
        first = await client.get('/api/wiki/blocks', headers=headers, params={
            'doc': source['id'], 'expected_revision': source['revision'], 'offset': 2, 'limit': 2, 'budget_chars': 8,
        })
        assert first.status_code == 200, first.text
        current = first.json()
        task = next(task for task in tasks.active.values() if task.job_id)
        store = registry.store(task.user_id)
        with pytest.raises(WikiError, match='cited current passage'):
            tasks.require_source_read(task, store, reference)
        remaining = await client.get('/api/wiki/blocks', headers=headers, params={
            'doc': source['id'], 'expected_revision': source['revision'], 'offset': current['next_offset'],
            'start': current['next_start'], 'limit': 2,
        })
        assert remaining.status_code == 200, remaining.text
        tasks.require_source_read(task, store, reference)
        reused = await client.post('/api/wiki/read-many', headers=headers, json={
            'items': [reference], 'view': 'compact', 'reuse_delivered': True,
        })
        assert reused.status_code == 200 and reused.json()['items'][0]['status'] == 'already_delivered'
        assert reused.json()['characters_returned'] == 0


async def test_raw_delivery_does_not_reuse_offsets_when_extraction_removes_a_bom(client, tmp_path):
    root, config, _ = await prepare(client, tmp_path)
    source = root / 'source.md'
    source.write_bytes(b'\xef\xbb\xbf' + source.read_bytes())
    async with active_worker(config, ['target.md'], client=client) as headers:
        result = await client.get('/api/wiki/blocks', headers=headers, params={'doc': 'source.md', 'budget_chars': 8})
        assert result.status_code == 200, result.text
        document = result.json()
        key = (document['id'], document['path'], document['revision'])
        task = next(task for task in tasks.active.values() if task.job_id)
        assert task.raw_reads[key] == [(0, 8)]
        assert key not in task.reads


async def test_explicit_reuse_returns_only_the_next_gap_and_does_not_cross_revisions(client, tmp_path):
    root, config, source = await prepare(client, tmp_path)
    async with active_worker(config, ['target.md'], client=client) as headers:
        first = await client.get('/api/wiki/read', headers=headers, params={
            'doc': source['id'], 'location': '#claim', 'length': 8,
        })
        assert first.status_code == 200
        start = first.json()['start']
        middle = await client.get('/api/wiki/read', headers=headers, params={
            'doc': source['id'], 'location': '#claim', 'start': start + 20, 'length': 5,
        })
        assert middle.status_code == 200
        request = {'doc': source['id'], 'expected_revision': source['revision'], 'location': '#claim'}
        filled = await client.post('/api/wiki/read-many', headers=headers, json={
            'items': [request], 'reuse_delivered': True, 'view': 'compact',
        })
        assert filled.status_code == 200, filled.text
        piece = filled.json()['ranges'][0]
        assert (piece['start'], piece['end']) == (start + 8, start + 20)
        assert filled.json()['characters_returned'] == 12
        assert filled.json()['items'][0]['next_start'] == start + 25
        (root / 'source.md').write_text((root / 'source.md').read_text() + '\nA new condition.\n')
        changed = await client.post('/api/wiki/read-many', headers=headers, json={
            'items': [request], 'reuse_delivered': True, 'view': 'compact',
        })
        assert changed.status_code == 200 and changed.json()['items'][0]['status'] == 'changed'
        assert not changed.json()['ranges']


async def test_long_optional_source_label_preserves_short_action_fields(client, tmp_path):
    root, config, source = await prepare(client, tmp_path)
    target = root / 'target.md'
    target.write_text(target.read_text().replace('    location: L1\n',
                     '    location: L1\n    id: ' + json.dumps('展示标签' * 1500, ensure_ascii=False) + '\n', 1))
    async with active_worker(config, ['target.md'], client=client) as headers:
        response = await client.get('/api/wiki/read', headers=headers, params={'doc': 'target.md', 'view': 'compact'})
        assert response.status_code == 200, response.text
        value = response.json()['sources']
        entry = value['items'][0]
        assert entry['doc'] == source['id'] and entry['current_revision'] == source['revision']
        assert entry['resource'] == 'source.md' and entry['location'] == 'L1'
        assert entry['id_truncated'] and value['complete']


async def test_oversized_source_locator_has_a_real_original_declaration_recovery(client, tmp_path):
    root, config, source = await prepare(client, tmp_path)
    location = 'L' + '9' * 3000
    target = root / 'target.md'
    target.write_text(target.read_text().replace('    location: L1\n', '    location: ' + location + '\n', 1))
    async with active_worker(config, ['target.md'], client=client) as headers:
        response = await client.get('/api/wiki/read', headers=headers, params={'doc': 'target.md', 'view': 'compact'})
        assert response.status_code == 200, response.text
        page = response.json()['sources']
        entry = page['items'][0]
        assert entry['doc'] == source['id'] and entry['current_revision'] == source['revision']
        assert entry['details_omitted'] and 'location' in entry['omitted_fields']
        assert not page['complete']
        original = await client.get('/api/wiki/blocks', headers=headers, params=entry['recovery']['blocks'])
        assert original.status_code == 200, original.text
        assert location in ''.join(block['content'] for block in original.json()['blocks'])
