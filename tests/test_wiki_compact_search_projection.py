"""Compact navigation keeps original chapter coordinates and indexed evidence."""

import hashlib
import json
import sqlite3
from contextlib import contextmanager

import pytest

from server.wiki import search_index
from server.wiki.store import WikiError
from tests.test_wiki_commonmark_locations import store as store
from tests.wiki_cost_probe import work_counts


@pytest.mark.parametrize('query,spelling', [
    ('strasse', 'Straße'), ('ffi', 'ﬃ'), ('café', 'cafe\u0301'), ('fullwidth', 'ＦＵＬＬＷＩＤＴＨ'),
])
@pytest.mark.parametrize('newline,bom', [('\n', ''), ('\r\n', ''), ('\n', '\ufeff'), ('\r\n', '\ufeff')])
def test_normalized_body_match_outside_preview_keeps_literal_section_origin(store, query, spelling, newline, bom):
    prefix = ('---\ndescription: Unrelated short description\n---\n# Article\n\n'
              + 'Prelude 😀 e\u0301 before the evidence.\n' * 15 + '\n')
    chapter = '## Key note\n\n' + 'Unrelated context retained. ' * 45 + spelling + ' is required.\n\n'
    text = (prefix + chapter).replace('\n', newline)
    raw = (bom + text).encode()
    (store.root / 'unicode.md').write_bytes(raw)
    result = store.search(query, view='compact')
    hit, = result['items']
    origin = len(prefix.replace('\n', newline))
    assert hit['revision'] == hashlib.sha256(raw).hexdigest()
    assert hit['matched_fields'] == ['body']
    # None of these normalized spellings occurs literally in the first window.
    # A body-only snippet-window projection would lose the real body match.
    assert spelling not in hit['snippet']
    assert hit['snippet'] == text[origin:origin + 240]
    assert hit['snippet_start'] == origin
    section, = hit['sections']
    assert section['section_role'] == 'key_note'
    assert (section['start'], section['end']) == (origin, len(text))
    assert hit['matched_section_count'] == 1


@pytest.mark.parametrize('nul_location', ['earlier_chapter', 'matching_chapter'])
def test_accepted_nul_text_keeps_content_and_coordinates_after_the_nul(store, nul_location):
    prefix = '# Article\n\nBefore\0the evidence.\n\n' if nul_location == 'earlier_chapter' else '# Article\n\nBefore the evidence.\n\n'
    chapter = '## Evidence\n\n' + ('Literal\0separator.\n' if nul_location == 'matching_chapter' else '') + 'Tailneedle is current evidence.\n'
    text = prefix + chapter
    (store.root / 'nul.md').write_bytes(text.encode())
    hit, = store.search('Tailneedle', view='compact')['items']
    assert hit['matched_fields'] == ['body']
    assert hit['snippet'] == chapter
    assert hit['snippet_start'] == len(prefix)
    assert (hit['sections'][0]['start'], hit['sections'][0]['end']) == (len(prefix), len(text))


def test_maximum_page_keeps_rank_order_under_a_real_sqlite_variable_limit(store, monkeypatch):
    for number in range(51):
        (store.root / f'note-{number:02}.md').write_text(f'# Note {number}\n\nPageboundmarker is evidence.\n')
    store.refresh()
    connect = store.connect

    @contextmanager
    def limited():
        with connect() as db:
            assert db.getlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER) >= 200
            db.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 200)
            yield db

    monkeypatch.setattr(store, 'connect', limited)
    full = store.search('Pageboundmarker', view='full', limit=50)
    first = store.search('Pageboundmarker', view='compact', limit=50)
    assert [item['id'] for item in first['items']] == [item['id'] for item in full['items']]
    assert first['total'] == full['total'] == 51 and first['next_offset'] == 50
    for compact, complete in zip(first['items'], full['items'], strict=True):
        assert compact['revision'] == complete['revision']
        assert compact['snippet'] == complete['snippet'][:240]
        assert compact['snippet_start'] == complete['snippet_start']
    last = store.search('Pageboundmarker', view='compact', limit=50, cursor=first['next_cursor'])
    assert len(last['items']) == 1 and last['total'] == 51 and last['next_cursor'] is None
    assert last['items'][0]['id'] not in {item['id'] for item in first['items']}
    empty = store.search('Pageboundmarker', view='compact', limit=50, offset=51)
    assert empty['items'] == [] and empty['total'] == 51 and empty['next_offset'] is None


def test_managed_override_and_more_than_two_matching_sections_survive_projection(store):
    text = ('---\naliases: [NativeAlias]\ntags: [native]\ndescription: Native description\n---\n'
            '# Article\n\n## Key note\nAlphacondition is necessary.\n\n'
            '## Second\nBetacondition is necessary.\n\n'
            '## Third\nGammacondition is necessary.\n\n'
            '## Fourth\nDeltacondition is necessary.\n')
    (store.root / 'conditions.md').write_text(text)
    document = store.read('conditions.md')
    with store.connect() as db:
        db.execute('INSERT INTO managed_notes(path,base_hash,rendered_revision,regions,metadata) VALUES(?,?,?,?,?)',
                   ('conditions.md', document['revision'], document['revision'], '[]', json.dumps({
                       'description': 'Managed NativeAlias description', 'doc_role': 'synthesis',
                       'aliases': ['NativeAlias', 'ManagedAlias'], 'tags': ['native', 'managed'],
                   })))
    query = 'NativeAlias Alphacondition Betacondition Gammacondition Deltacondition'
    complete, = store.search(query, view='full')['items']
    hit, = store.search(query, view='compact')['items']
    assert complete['metadata']['aliases'] == ['NativeAlias', 'ManagedAlias']
    assert complete['metadata']['tags'] == ['native', 'managed']
    assert hit['description'] == 'Managed NativeAlias description'
    assert hit['doc_role'] == 'synthesis'
    assert hit['matched_fields'] == ['aliases', 'description', 'body']
    assert hit['matched_section_count'] == len(complete['sections']) == 4
    assert len(hit['sections']) == 2
    assert hit['sections'][0]['section_role'] == 'key_note'
    assert [(part['start'], part['end']) for part in hit['sections']] == [
        (part['start'], part['end']) for part in complete['sections'][:2]]
    assert hit['snippet'] == complete['snippet'][:240]
    assert hit['snippet_start'] == complete['snippet_start']


def test_page_uses_one_snapshot_when_another_index_transaction_commits(store, monkeypatch):
    with store.connect() as db:
        assert db.execute('PRAGMA journal_mode=WAL').fetchone()[0] == 'wal'
    for number in range(2):
        (store.root / f'note-{number}.md').write_text(
            f'---\ndescription: Before {number}\n---\n# Before {number}\n\nSnapshotmarker original evidence.\n')
    before = store.search('Snapshotmarker', view='compact', limit=1)
    select, replaced = search_index.select, False

    def commit_after_selection(*args, **kwargs):
        nonlocal replaced
        selected = select(*args, **kwargs)
        if not replaced:
            replaced = True
            # Commit a real new index on another WAL connection between
            # candidate selection and page projection, without mocking hits.
            for number in range(2):
                (store.root / f'note-{number}.md').write_text(
                    f'---\ndescription: After {number}\n---\n# After {number}\n\nSnapshotmarker replaced evidence.\n')
            with store.connect() as writer:
                for number in range(2):
                    store._index(writer, f'note-{number}.md')
        return selected

    monkeypatch.setattr(search_index, 'select', commit_after_selection)
    during = store.search('Snapshotmarker', view='compact', limit=1)
    assert replaced and during == before
    with pytest.raises(WikiError) as conflict:
        store.search('Snapshotmarker', view='compact', limit=1, cursor=before['next_cursor'])
    assert conflict.value.status == 409
    with pytest.raises(WikiError) as changed:
        store.read(before['items'][0]['id'], expected_revision=before['items'][0]['revision'])
    assert changed.value.status == 409


@pytest.mark.wiki_perf
def test_compact_lexical_page_does_not_transfer_each_full_document(store):
    source_bytes = 0
    for number in range(3):
        text = f'# Long {number}\n\n' + ''.join(
            f'## Evidence {section}\n\nProjectionmarker has this condition.\n' + 'Unrelated long context.\n' * 400
            for section in range(10)
        )
        (store.root / f'long-{number}.md').write_text(text)
        source_bytes += len(text.encode())
    store.refresh()
    with work_counts([store.root]) as counts:
        page = store.search('Projectionmarker', view='compact')
    assert len(page['items']) == page['total'] == 3
    assert all(item['matched_fields'] == ['body'] for item in page['items'])
    full_rows = [item['sql'] for item in counts['query_plans'] if item['sql'].startswith('SELECT d.*,')]
    assert not full_rows, f'Compact fetched {len(full_rows)} complete document rows'
    assert counts['sqlite_text_bytes'] < source_bytes / 2
