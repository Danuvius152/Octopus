"""Frozen synthetic sources and actual returned ranges define evaluation evidence."""

from tests.wiki_baseline import build_corpus, coverage, questions
from tests.wiki_query_evidence import source_manifest


def test_frozen_corpus_has_two_separate_question_sets_and_reproducible_sources(tmp_path):
    first, second = tmp_path / 'first', tmp_path / 'second'
    build_corpus(first)
    build_corpus(second)
    assert source_manifest(first) == source_manifest(second)
    assert len(source_manifest(first)) == 100
    cases = questions()
    assert len(cases) == 16 and len({case['id'] for case in cases}) == 16
    for split in ('development', 'holdout'):
        selected = [case for case in cases if case['split'] == split]
        assert {case['kind'] for case in selected} == {
            'personal', 'known_report', 'broad', 'comparison', 'unknown_name',
            'same_title', 'alias', 'reread',
        }
    for case in cases:
        for source in case['sources']:
            text = (first / source['path']).read_text()
            assert all(quote in text for quote in source['passages'])


def test_only_actual_same_revision_returned_text_counts_towards_full_reading():
    text = '甲乙😀丁戊己'
    calls = [
        {'operation': 'search', 'response': {'items': [{'path': 'note.md', 'snippet': text}]}},
        {'operation': 'read', 'response': {'path': 'note.md', 'revision': 'old', 'start': 0, 'end': 6, 'content': text}},
        {'operation': 'read', 'response': {'path': 'note.md', 'revision': 'current', 'start': 0, 'end': 3, 'content': text[:3]}},
        {'operation': 'read-many', 'body': {'items': [{'doc': 'note.md', 'start': 3, 'length': 3}]},
         'response': {'documents': [{'id': 'doc', 'path': 'note.md', 'revision': 'current'}],
                      'ranges': [{'doc': 'doc', 'start': 3, 'end': 4, 'content': '丁'}],
                      'response_budget_exhausted': True}},
    ]
    assert coverage(calls, 'note.md', 'current', text) == {
        'covered_characters': 4, 'duplicate_characters': 0, 'missing': [[4, 6]], 'complete': False,
    }
    calls.append({'operation': 'read', 'response': {
        'path': 'note.md', 'revision': 'current', 'start': 3, 'end': 6, 'content': text[3:],
    }})
    assert coverage(calls, 'note.md', 'current', text) == {
        'covered_characters': 6, 'duplicate_characters': 1, 'missing': [], 'complete': True,
    }


def test_invalid_content_cannot_grant_a_read_range():
    trace = [{'operation': 'read', 'response': {
        'path': 'note.md', 'revision': 'current', 'start': 0, 'end': 3, 'content': 'wrong',
    }}]
    assert not coverage(trace, 'note.md', 'current', '原始文')['complete']
