"""Diagnostic measurement preserves real SQLite rows and counts delivered bytes."""

import itertools
import time
from pathlib import Path

import pytest

from server.service_health import ServiceHealth
from server.wiki import storage
from tests import test_wiki_phase1_baseline as baseline
from tests.wiki_cost_probe import CostProbe, work_counts


def test_diagnostics_count_returned_utf8_and_blobs_and_keep_row_access(tmp_path):
    with work_counts([tmp_path]) as counts:
        with storage.connect(tmp_path / 'cost.db', ServiceHealth(), create=True) as db:
            row = db.execute("SELECT ? AS text, ? AS bytes", ('甲😀', b'abc')).fetchone()
            assert row['text'] == row[0] == '甲😀' and row['bytes'] == b'abc'
    assert counts['sqlite_rows'] == 1
    assert counts['sqlite_text_bytes'] == 7
    assert counts['sqlite_blob_bytes'] == 3
    assert counts['query_plans'] and any('CONSTANT ROW' in detail for detail in counts['query_plans'][0]['plan'])


@pytest.mark.parametrize('samples,p50,p95', [([1, 2, 3, 4, 20], 3, 20), ([1, 2, 3, 4, 5, 20], 3, 20)])
def test_sample_percentiles_are_reported_without_discarding_raw_samples(samples, p50, p95):
    report = {'cases': []}
    probe = CostProbe(report, lambda: None, 'synthetic timing')
    row = probe.start('known samples', {})
    row['samples'] = [{'wall_ms': value} for value in samples]
    probe.finish(row)
    assert row['p50_ms'] == p50 and row['p95_ms'] == p95
    assert len(row['samples']) == len(samples) and row['status'] == 'passed'


@pytest.mark.parametrize('milliseconds', [1, 10])
def test_reference_uses_original_limits_when_identical_pages_get_slower(monkeypatch, milliseconds):
    probe = CostProbe({'cases': []}, lambda: None, 'frozen page')
    original = {'cases': [{'test': 'frozen page', 'name': 'compact', 'dimensions': {'documents': 100},
                           'prospective_tolerance': {'p50_upper_ms': 1.5, 'p95_upper_ms': 1.5}}]}
    ticks = itertools.count(0, milliseconds / 1000)
    monkeypatch.setattr(time, 'perf_counter', lambda: next(ticks))
    page = {'items': [{'path': 'known.md'}], 'total': 1}

    def check(actual):
        assert actual == page

    def candidate():
        return baseline.measure(probe, 'compact', lambda: page, check, reference=original,
                                dimensions={'documents': 100}, diagnose=False)

    if milliseconds > 1.5:
        with pytest.raises(AssertionError, match='baseline'):
            candidate()
        assert probe.report['cases'][0]['status'] == 'failed'
    else:
        assert candidate()['status'] == 'passed'
    assert 'prospective_tolerance' not in probe.report['cases'][0]
    assert original['cases'][0]['prospective_tolerance']['p95_upper_ms'] == 1.5


@pytest.mark.wiki_perf
def test_diagnostic_reference_rejects_same_count_with_changed_explanation(tmp_path, monkeypatch):
    original = CostProbe({'cases': []}, lambda: None, 'diagnostic sample')
    baseline.test_diagnostic_growth(tmp_path / 'original', original, 3, reference=None)
    write = Path.write_text

    def change_error(path, text, *args, **kwargs):
        if path.name.startswith('error-'):
            text = text.replace('tags: [broken', 'aliases: {broken')
        return write(path, text, *args, **kwargs)

    monkeypatch.setattr(Path, 'write_text', change_error)
    candidate = CostProbe({'cases': []}, lambda: None, 'diagnostic sample')
    with pytest.raises(AssertionError, match='Frozen page differs'):
        baseline.test_diagnostic_growth(tmp_path / 'candidate', candidate, 3, reference=original.report)


@pytest.mark.wiki_perf
def test_rebuilt_baseline_keeps_identity_tie_order_and_diagnostic_samples(tmp_path):
    pages = []
    for name in ('first', 'second'):
        with baseline.corpus_store(tmp_path / name) as store:
            for number in range(5):
                (store.root / f'tie-{number}.md').write_text('# Equal rank\n\nStabletier is evidence.\n')
                (store.root / f'error-{number}.md').write_text('---\ntags: [broken\n---\n# Invalid metadata\n')
            store.refresh()
            # Remove observation time only: identities, UID spellings, ordering
            # and error samples must survive an independent rebuild unchanged.
            pages.append(baseline.canonical([
                store.search('Stabletier', view='compact', limit=3),
                store.search('Stabletier', view='full', limit=3),
                store.documents(view='full', limit=3),
            ], {}))
    assert pages[0] == pages[1]


def test_known_path_recall_keeps_other_answer_sources_separate():
    case = {'query': 'Library/Me/information.md',
            'sources': [{'path': 'Library/Me/information.md'}, {'path': 'Library/Me/边界.md'}]}
    assert baseline.retrieval_targets(case) == {'Library/Me/information.md'}
    case['query'] = 'personal evidence'
    assert baseline.retrieval_targets(case) == {'Library/Me/information.md', 'Library/Me/边界.md'}
