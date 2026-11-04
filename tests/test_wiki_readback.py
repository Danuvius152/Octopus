"""Reading obligations follow real writer transforms, not origin.generated."""

import json

from server.wiki import material_proof, readback
from tests import test_wiki_legacy_metadata as legacy
from tests import wiki_api as wiki_api
from tests.test_wiki_grant_v2 import assigned
from tests.test_wiki_supporting_navigation import setup_notes
from tests.wiki_job_runtime import active_worker

client = wiki_api.client
legacy_notes = legacy.notes


def covers(ranges, position):
    return any(first <= position < last for first, last in ranges)


def test_metadata_only_transform_has_no_new_body_readback():
    before = '---\ntags: [old]\n---\n# Original\n\nKeep every argument.\n'
    trace = material_proof.Trace(before)
    trace.metadata(before.replace('[old]', '[new]'))
    mapped = readback.transition(trace.before, trace.raw, trace)
    assert mapped['semantic'] == [] and mapped['metadata'] == {'tags': ['new']}
    assert mapped['copies']


def test_deleting_a_trailing_condition_still_has_a_final_semantic_join():
    before = '# Claim\n\nThe claim holds only under condition A.\n'
    start = before.index(' only under condition A.')
    # Exercise a deletion without any newly generated words.
    deletion = material_proof.Trace(before)
    deletion.edit([(start, len(before), '')])
    assert readback.transition(deletion.before, deletion.raw, deletion)['semantic']


async def test_restructure_without_new_words_still_requires_final_context_readback(client, tmp_path):
    root, jobs, job, config, _ = await setup_notes(client, tmp_path)
    path = root / 'Library/requested.md'
    path.write_text('# Review\n\n## Condition\n\nOnly use the claim under condition A.\n\n## Claim\n\nThis claim depends on the preceding condition.\n')
    # Select the current article, not the earlier fixture revision.
    jobs.update(job['id'], items=[dict(job['items'][0], revision=jobs.store.read('Library/requested.md')['revision'])])
    job = jobs.get(job['id'])
    async with assigned(job, config, client=client) as (headers, _):
        current = (await client.get('/api/wiki/read', headers=headers, params={'doc': 'Library/requested.md'})).json()
        parts = (await client.get('/api/wiki/blocks', headers=headers, params={'doc': current['id']})).json()['blocks']
        order = [part['id'] for part in parts]
        order[-1], order[2] = order[2], order[-1]
        result = await client.post('/api/wiki/restructure', headers=headers, json={
            'path': current['path'], 'expected_revision': current['revision'],
            'order': order, 'additions': [], 'reason': 'Check the effect of changed condition placement',
        })
        assert result.status_code == 200, result.text
        plan = result.json()['readback']
        final = path.read_text()
        assert plan['required'] and plan['revision'] == result.json()['revision']
        assert covers(plan['ranges'], final.index('Only use the claim'))
        assert covers(plan['ranges'], final.index('## Claim'))


async def test_successive_sparse_changes_keep_both_final_readback_regions(client, tmp_path):
    root, jobs, job, config, _ = await setup_notes(client, tmp_path)
    async with assigned(job, config, client=client) as (headers, _):
        current = (await client.get('/api/wiki/read', headers=headers, params={'doc': 'Library/requested.md'})).json()
        for marker in ('FIRST_ADDENDUM', 'SECOND_ADDENDUM'):
            blocks = (await client.get('/api/wiki/blocks', headers=headers, params={'doc': current['id'], 'expected_revision': current['revision']})).json()['blocks']
            last = blocks[-1]
            position = len(last['content'])
            result = await client.post('/api/wiki/revise', headers=headers, json={
                'doc': current['id'], 'expected_revision': current['revision'], 'operation': 'patch',
                'reason': 'Add a separate supported condition', 'edits': [{
                    'locator': last['locator'], 'expected_block_hash': last['sha256'],
                    'start': position, 'end': position, 'expected_text': '',
                    'replacement': '\n\n' + marker + '\n', 'kind': 'addition',
                }],
            })
            assert result.status_code == 200, result.text
            current['revision'] = result.json()['revision']
        plan = result.json()['readback']
        final = (root / current['path']).read_text()
        assert covers(plan['ranges'], final.index('FIRST_ADDENDUM'))
        assert covers(plan['ranges'], final.index('SECOND_ADDENDUM'))
        with jobs.store.connect() as db:
            receipt = db.execute('SELECT details FROM operation_receipts WHERE id=?', (result.json()['receipt_id'],)).fetchone()
        details = json.loads(receipt['details'])
        assert details['readback_transition']['copies']
        assert details['readback']['revision'] == current['revision']
        # An unchanged acknowledgement cannot erase previous semantic work.
        await client.get('/api/wiki/read', headers=headers, params={'doc': current['id'], 'expected_revision': current['revision']})
        unchanged = await client.post('/api/wiki/revise', headers=headers, json={
            'doc': current['id'], 'expected_revision': current['revision'], 'operation': 'no_change',
            'reason': 'Both addenda remain part of this final article',
        })
        assert unchanged.status_code == 200 and unchanged.json()['readback']['required']


async def test_new_job_metadata_refresh_copies_old_key_note_and_reports_effective_tags(client, legacy_notes):
    root, store, config = legacy_notes
    await legacy.initially_organize(client)
    old_summary = next(part['raw'] for part in legacy.managed(store)[1] if part['role'] == 'key_note')
    async with active_worker(config, [legacy.NOTE], client=client) as worker:
        headers = worker.headers
        result = await legacy.organize(client, headers=headers, tags=['worker-refresh'])
        assert result.status_code == 200, result.text
        plan = result.json()['readback']
        assert not plan['required'], plan
        assert plan['metadata']['tags'] == ['native-tag', 'worker-refresh']
        assert old_summary in (root / legacy.NOTE).read_bytes().decode()
        # This job subsequently writes prose. A metadata successor must keep
        # that real semantic obligation while copying the newly retained prose.
        changed = await legacy.organize(client, headers=headers, key_note='A new explanation with its necessary condition.')
        assert changed.status_code == 200 and changed.json()['readback']['required']
        successor = await legacy.organize(client, headers=headers, tags=['later-refresh'])
        assert successor.status_code == 200 and successor.json()['readback']['required']
        final = (root / legacy.NOTE).read_bytes().decode()
        assert covers(successor.json()['readback']['ranges'], final.index('A new explanation'))
