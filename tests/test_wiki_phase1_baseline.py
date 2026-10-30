"""Opt-in paired measurements on frozen sources before compact projection changes."""

import asyncio
import hashlib
import json
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from server.config import settings
from server.wiki.jobs import WikiJobs, WikiWorker
from server.wiki.registry import registry
from server.wiki.store import WikiStore, revision
from tests.wiki_baseline import (
    CURRENT,
    QUESTION_FILE,
    SEED,
    UPDATED,
    build_corpus,
    questions,
)
from tests.wiki_cost_probe import NOISE_RULE
from tests.wiki_query_evidence import source_manifest

pytestmark = pytest.mark.wiki_perf
REPEAT = 14
BASELINE_VAULT_ID = str(uuid.uuid5(uuid.NAMESPACE_URL, f'octopus:wiki-engineering-baseline:{SEED}'))


@contextmanager
def corpus_store(tmp_path):
    root = tmp_path / 'vault'
    build_corpus(root)
    store = WikiStore(root, tmp_path / 'state', BASELINE_VAULT_ID, create=True)
    try:
        yield store
    finally:
        store.close()


def canonical(value, identities):
    """UUID spelling and observation time vary across rebuilt equivalent indexes."""
    if isinstance(value, dict):
        return {key: canonical(item, identities) for key, item in value.items() if key != 'index_checked_at'}
    if isinstance(value, list):
        return [canonical(item, identities) for item in value]
    if isinstance(value, str):
        for identity, path in identities.items():
            value = value.replace(identity, '@' + path)
    return value


def frozen_page(probe, store, key, page, reference):
    with store.connect() as db:
        identities = {row['id']: row['path'] for row in db.execute('SELECT id,path FROM documents')}
    normalized = canonical(page, identities)
    probe.report.setdefault('reference_pages', {})[key] = normalized
    if reference is not None:
        assert normalized == reference['reference_pages'][key], f'Frozen page differs: {key}'
    probe.save()


@pytest.fixture
def reference(request):
    configured = request.config.getoption('wiki_baseline_reference')
    return json.loads(Path(configured).read_text()) if configured else None


def measure(probe, name, operation, check, *, reference=None, repeat=REPEAT, **options):
    return probe.measure(name, operation, check, repeat=repeat, baseline=True, reference=reference, **options)


def retrieval_targets(case):
    paths = {source['path'] for source in case['sources']}
    # A known-identity lookup is one step of a task that may need other sources.
    return {case['query']} if case['query'] in paths else paths


def test_frozen_hundred_document_queries(tmp_path, cost_probe, reference):
    with corpus_store(tmp_path) as store:
        manifest = source_manifest(store.root)
        cost_probe.report['frozen_workload'] = {
            'seed': SEED, 'manifest': manifest,
            'manifest_sha256': hashlib.sha256(json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest(),
            'questions_sha256': hashlib.sha256(QUESTION_FILE.read_bytes()).hexdigest(),
            'noise_rule': NOISE_RULE, 'repeat': REPEAT,
            'gold': [{**case, 'sources': [{**source, 'revision': revision((store.root / source['path']).read_bytes())}
                                         for source in case['sources']]} for case in questions()],
        }
        if reference:
            assert cost_probe.report['frozen_workload'] == reference['frozen_workload']
        quality = []
        for case in questions():
            for view in ('compact', 'full'):
                options = {key: case[key] for key in ('intent', 'directory') if key in case}

                def operation(case=case, view=view, options=options):
                    return store.search(case['query'], view=view, limit=10, **options)

                page = operation()
                frozen_page(cost_probe, store, case['id'] + '/' + view, page, reference)
                expected = retrieval_targets(case)
                found = {item['path'] for item in page['items']}
                quality.append({'id': case['id'], 'view': view, 'recall_at_10': len(found & expected) / len(expected),
                                'scope': 'This indexed query; separate from complete answer-source coverage',
                                'expected': sorted(expected), 'returned': sorted(found),
                                'answer_sources': [source['path'] for source in case['sources']],
                                'source_section_hits': {item['path']: [section.get('heading_path', section.get('title')) for section in item['sections']]
                                                        for item in page['items']}})

                def equivalent(result, expected_page=page):
                    assert result == expected_page

                measure(cost_probe, case['id'] + '/' + view, operation, equivalent, roots=[store.root],
                        reference=reference, dimensions={'documents': 100, 'split': case['split'], 'kind': case['kind']})
        cost_probe.report['retrieval_quality'] = quality
        measure(cost_probe, 'synchronize_unchanged_100', store.synchronize, lambda _: None, reference=reference, roots=[store.root])
        current = store.read(CURRENT)
        measure(cost_probe, 'live_read_100', lambda: store.read(CURRENT),
                lambda page: pytest.fail('Live revision/content changed') if page != current else None,
                reference=reference, roots=[store.root])
        (store.root / CURRENT).write_text(UPDATED)
        changed = store.read(CURRENT)
        assert changed['revision'] != current['revision'] and changed['content'] == UPDATED
        assert '标定误差超过两像素' in changed['content']
        cost_probe.report['live_update'] = {'before_revision': current['revision'], 'after_revision': changed['revision']}
        cost_probe.save()


def test_long_document_compact_transfer(tmp_path, cost_probe, reference):
    root = tmp_path / 'vault'
    build_corpus(root)
    for number in range(10):
        (root / f'Library/Notes/note-{number:03}.md').write_text(
            f'# Long {number}\n\n' + ''.join(f'## Part {part}\n\nphasefocus 条件甲。\n' + '独立的测量说明；不能改变既有匹配范围。\n' * 400
                                            for part in range(12)))
    store = WikiStore(root, tmp_path / 'state', BASELINE_VAULT_ID, create=True)
    try:
        for view in ('compact', 'full'):
            for kind, query, options in (
                ('lexical', 'phasefocus', {}),
                ('exact', 'Library/Notes/note-000.md', {}),
                ('filtered_identity', 'Library/Notes/note-000.md', {'directory': 'Library/Research'}),
            ):
                def operation(view=view, query=query, options=options):
                    return store.search(query, view=view, limit=10, **options)
                page = operation()
                key = 'long/' + kind + '/' + view
                frozen_page(cost_probe, store, key, page, reference)
                measure(cost_probe, key, operation, lambda result, expected=page: require_equal(result, expected),
                        reference=reference, roots=[root], memory=True, dimensions={'documents': 100, 'long_documents': 10, 'view': view})
    finally:
        store.close()


def require_equal(actual, expected):
    assert actual == expected


@pytest.mark.parametrize('errors', [0, 3, 50, 300])
def test_diagnostic_growth(tmp_path, cost_probe, errors, reference):
    with corpus_store(tmp_path) as store:
        for number in range(errors):
            (store.root / f'error-{number:04}.md').write_text('---\ntags: [broken\n---\n# Invalid metadata\n')
        store.refresh()
        for operation in (lambda: store.search('实时成像', view='compact'), lambda: store.documents(view='compact')):
            page = operation()
            assert page['index_error_count'] == errors
            assert len(page['index_errors']) == min(errors, 3)
            assert page['index_status'] == ('partial' if errors else 'ready')
            name = ('search' if 'match_mode' in page else 'list') + '/diagnostics'
            frozen_page(cost_probe, store, f'diagnostics/{errors}/{name}', page, reference)
            measure(cost_probe, name, operation, lambda result, expected=page: require_equal(result, expected),
                    reference=reference, roots=[store.root], memory=True, dimensions={'errors': errors, 'base_documents': 100})
        full = store.documents()
        assert len(full['index_errors']) == errors
        frozen_page(cost_probe, store, f'diagnostics/{errors}/list/full', full, reference)


def test_cold_index_hundred_documents(tmp_path, cost_probe, reference):
    root = tmp_path / 'vault'
    build_corpus(root)
    sequence = []

    def build():
        path = tmp_path / f'state-{len(sequence)}'
        store = WikiStore(root, path, BASELINE_VAULT_ID, create=True)
        sequence.append(path)
        return store

    def check(store):
        try:
            assert store.documents(limit=1)['total'] == 100
        finally:
            store.close()

    row = measure(cost_probe, 'cold_index_100', build, check, reference=reference, roots=[root], repeat=6, memory=True)
    row['cold_scope'] = 'Fresh application index; OS cache retained; tokenizer first-use included if encountered.'
    cost_probe.save()


async def test_maintenance_queue_wait(tmp_path, monkeypatch, cost_probe):
    root = tmp_path / 'vault'
    build_corpus(root)
    monkeypatch.setattr(settings, 'wiki_dir', str(tmp_path / 'wiki-state'))
    registry.configure(None, root, 'curator')
    jobs = WikiJobs(None)
    document = jobs.store.read(CURRENT, length=1)
    admitted, waiting = {}, []
    for number in range(12):
        job = await asyncio.to_thread(jobs.enqueue, f'queue-{number}', {'kind': 'curation'}, [{
            'doc': document['id'], 'path': document['path'], 'revision': document['revision'], 'status': 'pending',
        }])
        admitted[job['id']] = time.perf_counter()

    worker = WikiWorker()
    worker.manager = SimpleNamespace(db=None)

    async def controlled_consumer(current_jobs, job_id):
        waiting.append({'wall_ms': (time.perf_counter() - admitted[job_id]) * 1000,
                        'queue_position': len(waiting), 'job_id': job_id})
        await asyncio.sleep(.005)
        await asyncio.to_thread(current_jobs.update, job_id, status='completed_with_attention',
                                error='Controlled queue consumer; no model or content change')

    # Keep the real queue claim, per-account serial dispatcher, JobOwner and
    # grant retirement. The consumer starts the measurement's service interval.
    monkeypatch.setattr(worker, '_process', controlled_consumer)
    before = source_manifest(root)
    async with asyncio.timeout(15):
        await worker.run(None)
    assert [sample['job_id'] for sample in waiting] == list(admitted)
    assert not worker.executing
    assert source_manifest(root) == before
    row = cost_probe.start('maintenance_queue_wait', {'documents': 100, 'queued_jobs': 12, 'consumer_delay_ms': 5})
    row['samples'] = waiting
    row['timing_scope'] = 'Admission ACK to real worker consumer entry; all twelve jobs admitted before serial dispatch. Controlled 5ms consumer, no model. Positions are a single batch, not stationary repetitions or a production percentile/SLO.'
    cost_probe.finish(row)
