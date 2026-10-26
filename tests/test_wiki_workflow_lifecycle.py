"""Exercise economic worker tools across admission, receipt and reselection."""

import json
from types import SimpleNamespace

from server.mcp_identity import mint
from server.wiki.tasks import tasks
from tests import test_wiki_maintenance_state as maintenance
from tests.test_wiki_initial_review import target
from tests.test_wiki_jobs import Curator, process
from tests.test_wiki_maintenance_selections import select
from tests.test_wiki_source_confirmation import confirm

client = maintenance.client


async def test_shared_source_review_settles_once_without_repeat_reads_or_false_new_candidates(client, tmp_path):
    files = {'Library/source.md': '# Shared\n\nThe claim applies under condition A.\n',
             'Library/index.md': '# Conditional reasoning\n\nCompare claims and their applicability conditions.\n'}
    for name in ('a', 'b'):
        files[f'Library/{name}.md'] = (
            '---\nsources:\n  - {resource: source.md, revision: previous, location: "#shared"}\n---\n'
            f'# {name.upper()}\n\nThis explanation already states condition A.\n'
        )
    root, config, jobs = await maintenance.prepare(client, tmp_path, files)
    for name in ('a', 'b'):
        await confirm(client, config, jobs, f'Library/{name}.md', 'initial-' + name)
    originals = {path: (root / path).read_bytes() for path in files if path.endswith(('/a.md', '/b.md'))}
    source = root / 'Library/source.md'
    source.write_text(source.read_text() + '\nCondition A is still necessary.\n')
    frozen = await select(client)
    chosen = [item for item in frozen['items'] if item['path'] in originals]
    assert len(chosen) == 2
    request = {'selection_id': frozen['id'], 'ordinals': [item['ordinal'] for item in chosen]}
    admitted = await client.post('/api/wiki/maintain-batch', json=request)
    assert admitted.status_code == 200, admitted.text
    job = jobs.get(admitted.json()['jobs'][0]['id'])
    admitted_observations = {item['review']['doc']: item['review'] for item in chosen}
    # Facts can change while queued. Keep the admitted baseline while exposing
    # and checking the current source; do not pretend it is the old snapshot.
    source.write_text(source.read_text() + '\nA queued-time clarification of the same condition.\n')

    class EconomicWorker(Curator):
        async def _events(self, session_id, prompt, **kwargs):
            self.turns += 1
            session = SimpleNamespace(id=session_id, user_id=jobs.user_id, agent_id=config['curator_agent_id'])
            token = tasks.open(session, 'maintain', request_id=kwargs['request_id'])
            headers = {'Authorization': 'Bearer ' + mint(session_id, user_id=jobs.user_id, wiki_task_id=token)}
            try:
                documents, sources = [], []
                for item in job['items']:
                    context = target(prompt, item['doc'])['review_context']
                    assert context['observed']['dependency_generation'] == admitted_observations[item['doc']]['dependency_generation']
                    assert context['baseline']['id']
                    assert context['reasons'] == ['direct_sources_need_review']
                    result = await client.get('/api/wiki/read', headers=headers, params={'doc': item['doc'], 'view': 'compact'})
                    assert result.status_code == 200, result.text
                    document = result.json()
                    documents.append(document)
                    ref = document['sources']['items'][0]
                    sources.append({'doc': ref['doc'], 'expected_revision': ref['current_revision'], 'location': ref['location']})
                material = await client.post('/api/wiki/read-many', headers=headers, json={'items': sources, 'view': 'compact'})
                assert material.status_code == 200, material.text
                assert len(material.json()['ranges']) == 1
                guide = await client.get('/api/wiki/guide', headers=headers, params={'module': 'maintenance'})
                assert guide.status_code == 200
                reused = await client.post('/api/wiki/read-many', headers=headers, json={
                    'items': sources, 'reuse_delivered': True, 'view': 'compact',
                })
                assert reused.status_code == 200 and reused.json()['characters_returned'] == 0
                for document in documents:
                    ref = document['sources']['items'][0]
                    result = await client.post('/api/wiki/revise', headers=headers, json={
                        'doc': document['id'], 'expected_revision': document['revision'], 'operation': 'confirm_sources',
                        'checks': [{'resource': ref['resource'], 'doc': ref['doc'], 'revision': ref['current_revision'], 'location': ref['location']}],
                        'reason': 'The current source retains the condition already stated in this article',
                    })
                    assert result.status_code == 200, result.text
                    assert not result.json()['changed'] and not result.json()['readback']['required']
                index = await client.get('/api/wiki/blocks', headers=headers, params={'doc': 'Library/index.md', 'view': 'navigation'})
                assert index.status_code == 200, index.text
                index = index.json()
                linked = await client.post('/api/wiki/organize', headers=headers, json={
                    'path': index['path'], 'expected_revision': index['revision'], 'reason': 'Index the two checked conditional explanations',
                    'relations': [{'doc': document['id'], 'revision': document['revision'], 'location': '#' + document['title'].lower(),
                                   'explanation': 'Compare the stated applicability condition'} for document in documents],
                })
                assert linked.status_code == 200, linked.text
                assert not linked.json()['readback']['required']
                yield {'type': 'result', 'content': 'Existing claims checked against the current shared source', 'is_error': False}
            finally:
                tasks.close(token)

    worker = EconomicWorker(jobs)
    completed = await process(jobs, job, worker)
    assert completed['status'] == 'completed' and worker.turns == 1
    for path, original in originals.items():
        assert (root / path).read_bytes() == original
        state = (await maintenance.state(client, path))['items'][0]
        assert state['status'] == 'unchanged' and not state['source_warnings'] and not state['actionable']
    repeated = await client.post('/api/wiki/maintain-batch', json=request)
    assert repeated.status_code == 200, repeated.text
    assert all(item['reused'] for item in repeated.json()['selected_items'])
    assert {item['job_id'] for item in repeated.json()['selected_items']} == {job['id']}
    assert not any(item['path'] in originals for item in (await select(client))['items'])
    assert [receipt['operation'] for receipt in completed['receipts']].count('navigation_link') == 1
    # The deterministic append receipt is not either article's accepted review.
    assert len([receipt for receipt in completed['receipts'] if receipt['operation'] == 'confirm_sources']) == 2
    with jobs.store.connect() as db:
        detail = json.loads(db.execute('SELECT details FROM operation_receipts WHERE job_id=? AND operation=?',
                                      (job['id'], 'navigation_link')).fetchone()[0])
    assert detail['supporting_navigation']['target']['path'] == 'Library/index.md'
