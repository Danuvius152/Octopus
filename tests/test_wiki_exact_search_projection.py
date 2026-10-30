"""Exact identity authority, filtered totals and page projection stay distinct."""

import json
import sqlite3
from contextlib import contextmanager

import pytest

from server.wiki.store import WikiError
from tests.test_wiki_markdown_navigation import store as store
from tests.test_wiki_markdown_navigation import write


@pytest.mark.parametrize('view', ['compact', 'full'])
def test_filtered_stable_identity_never_borrows_a_title_or_alias(store, view):
    write(store, 'Outside/source.md', '# Original\n\nOriginal evidence.\n')
    source = store.read('Outside/source.md')
    for index, query in enumerate([source['path'], source['id'], source['metadata']['uid'].upper()]):
        write(store, f'Inside/collision-{index}.md',
              f'---\ntitle: {json.dumps(query)}\naliases: [{json.dumps(query)}]\n---\n# Collision\n\nIndependent evidence.\n')
        result = store.search(query, intent='lookup', directory='Inside', view=view)
        assert result['items'] == [] and result['total'] == 0
        assert result['match_mode'] == 'exact' and result['query_terms'] == []
        assert result['next_cursor'] is None
    (store.root / source['path']).unlink()
    for query in [source['id'], source['metadata']['uid'].upper()]:
        result = store.search(query, intent='lookup', directory='Inside', view=view)
        assert result['items'] == [] and result['total'] == 0 and result['match_mode'] == 'exact'


@pytest.mark.parametrize('query', ['共同标题', '共享别名'])
@pytest.mark.parametrize('view', ['compact', 'full'])
def test_title_and_alias_keep_filtered_total_and_ambiguity_on_empty_pages(store, query, view):
    directory = 'Library/Scope%_ß'
    for relative in ['a-é.md', 'b-😀.md', 'c.md']:
        write(store, directory + '/' + relative, '---\ntitle: 共同标题\naliases: [共享别名]\n---\n# Article\n\nLiteral scope evidence.\n')
    for decoy in ['Library/ScopeXYß/decoy.md', directory + '_extra/decoy.md']:
        write(store, decoy, '---\ntitle: 共同标题\naliases: [共享别名]\n---\n# Decoy\n\nOutside the literal directory.\n')
    result = store.search(query, intent='lookup', directory=directory, view=view, limit=1, offset=1)
    assert result['total'] == 3 and result['match_mode'] == 'ambiguous' and result['query_terms'] == []
    assert [item['path'] for item in result['items']] == [directory + '/b-😀.md']
    assert result['next_offset'] == 2 and result['next_cursor']
    last = store.search(query, intent='lookup', directory=directory, view=view, limit=1, cursor=result['next_cursor'])
    assert last['total'] == 3 and last['match_mode'] == 'ambiguous'
    assert [item['path'] for item in last['items']] == [directory + '/c.md']
    assert last['next_cursor'] is None
    empty = store.search(query, intent='lookup', directory=directory, view=view, limit=1, offset=3)
    assert empty['items'] == [] and empty['total'] == 3 and empty['match_mode'] == 'ambiguous'
    assert empty['query_terms'] == [] and empty['next_cursor'] is None


@pytest.mark.parametrize('view', ['compact', 'full'])
def test_maximum_exact_page_preserves_total_and_expires_with_the_epoch(store, view):
    for number in range(51):
        write(store, f'note-{number:02}.md', '# Shared boundary\n\nCurrent evidence.\n')
    first = store.search('Shared boundary', intent='lookup', view=view, limit=50)
    assert first['total'] == 51 and first['match_mode'] == 'ambiguous'
    assert [item['path'] for item in first['items']] == [f'note-{number:02}.md' for number in range(50)]
    last = store.search('Shared boundary', intent='lookup', view=view, limit=50, cursor=first['next_cursor'])
    assert last['total'] == 51 and last['match_mode'] == 'ambiguous'
    assert [item['path'] for item in last['items']] == ['note-50.md']
    missing = store.search('Unrecordedterm', intent='lookup', view=view)
    assert missing['total'] == 0 and missing['items'] == []
    write(store, 'note-00.md', '# Shared boundary\n\nChanged current evidence.\n')
    with pytest.raises(WikiError) as changed_cursor:
        store.search('Shared boundary', intent='lookup', view=view, limit=50, cursor=first['next_cursor'])
    assert changed_cursor.value.status == 409
    with pytest.raises(WikiError) as changed_body:
        store.read(first['items'][0]['id'], expected_revision=first['items'][0]['revision'])
    assert changed_body.value.status == 409


@pytest.mark.wiki_perf
def test_compact_exact_returns_one_narrow_page_without_other_candidates_bodies(store, monkeypatch):
    for number in range(4):
        write(store, f'note-{number}.md', '---\ntitle: Shared long article\ndescription: Short indexed description\n---\n# Article\n\n' + 'Long original evidence.\n' * 4000)
    store.refresh()
    connect, returned = store.connect, []

    @contextmanager
    def observed():
        with connect() as db:
            def row(cursor, values):
                fields = [column[0] for column in cursor.description]
                if 'managed_metadata' in fields:
                    returned.append((fields, sum(len(value.encode()) for value in values if isinstance(value, str))))
                return sqlite3.Row(cursor, values)
            db.row_factory = row
            yield db

    monkeypatch.setattr(store, 'connect', observed)
    result = store.search('Shared long article', intent='lookup', view='compact', limit=1, offset=2)
    assert result['total'] == 4 and result['match_mode'] == 'ambiguous'
    assert [item['path'] for item in result['items']] == ['note-2.md']
    assert result['items'][0]['snippet'] == 'Short indexed description'
    assert len(returned) == 1, f'Exact search returned {len(returned)} candidate rows for a one-row page'
    fields, byte_count = returned[0]
    assert not {'text', 'pages', 'signature'}.intersection(fields)
    assert byte_count < 3000
