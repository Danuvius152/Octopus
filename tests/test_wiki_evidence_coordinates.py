"""Evidence coverage uses the same unchanged code points as real Wiki reads."""

import json
from types import SimpleNamespace

import pytest

from tests.test_wiki_markdown_navigation import store as store
from tests.wiki_query_evidence import collect_evaluation


@pytest.mark.parametrize('text', ['# A\nbody\n', '# A\r\nbody\r\n', '# A\rbody\r', '# A\r\nbody\rlast\n', '\ufeff# A\r\nbody\rlast\n'])
@pytest.mark.parametrize('complete', [True, False])
async def test_actual_read_ranges_survive_snapshot_newlines(store, tmp_path, text, complete):
    (store.root / 'note.md').write_bytes(text.encode())
    expected = text.removeprefix('\ufeff')
    probe = SimpleNamespace(root=store.root, last_session_id='coordinates', last_request_id='request',
                            last_events=[], call_trace=[], usage_observations=[])

    async def ask(question):
        result = store.read('note.md', length=24000 if complete else 6)
        assert result['content'] == (expected if complete else expected[:6])
        assert result['end'] == (len(expected) if complete else 6)
        probe.call_trace.append({'operation': 'read', 'response': result})
        probe.last_events.append({'type': 'assistant_text', 'content': 'Controlled coordinate check'})
        return 'Controlled coordinate check'

    probe.ask = ask
    output = tmp_path / 'evidence'
    operation = collect_evaluation(probe, 'Read all current text', case_id='coordinates', output=output,
                                   expected_sources=[{'path': 'note.md', 'full': True}])
    if complete:
        await operation
    else:
        with pytest.raises(AssertionError, match='full reading'):
            await operation
    report = json.loads(next(output.glob('*/report.json')).read_text())
    reading, = report['source_coverage']
    assert reading['complete'] is complete
    assert reading['covered_characters'] == (len(expected) if complete else 6)
    assert reading['missing'] == ([] if complete else [[6, len(expected)]])
    assert report['execution_status'] == ('success' if complete else 'failed')
    assert report['semantic_acceptance'] == 'not_reviewed'
