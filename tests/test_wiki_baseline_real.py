"""Opt-in frozen questions through two explicitly different runtime entries."""

import hashlib
from copy import deepcopy
from pathlib import Path

import pytest

from tests.test_wiki_chat_real import ordinary_chat_probe as ordinary_chat_probe
from tests.test_wiki_query_real import query_probe_context
from tests.wiki_baseline import CURRENT, UPDATED
from tests.wiki_query_evidence import (
    collect_evaluation,
    copy_registered_sources,
    load_registration,
)

pytestmark = [pytest.mark.real, pytest.mark.real_codex, pytest.mark.knowledge_eval]


@pytest.fixture
async def registered_query_probe(tmp_path, monkeypatch, request, position):
    configured = request.config.getoption('wiki_query_registration')
    assert configured, '--wiki-query-registration is required before a dedicated model request'
    registration = load_registration(configured)
    root = copy_registered_sources(registration, tmp_path / 'registered-sources')
    registration = deepcopy(registration)
    registration['source_root'] = str(root)
    async with query_probe_context(tmp_path, monkeypatch, registration=registration) as probe:
        if position < len(registration['questions']) and registration['questions'][position].get('kind') == 'reread':
            updated = root / CURRENT
            original = updated.read_bytes()
            updated.write_text(UPDATED)
            probe.input_context['source_update'] = {
                'path': CURRENT, 'before': hashlib.sha256(original).hexdigest(),
                'after': hashlib.sha256(UPDATED.encode()).hexdigest(),
                'scope': 'External update in this case-owned source copy after initial binding, before its question',
            }
            item = registration['questions'][position]
            item['sources'] = [{**source, 'passages': ['标定误差超过两像素时结论不成立']} if source['path'] == CURRENT else source
                               for source in item['sources']]
        yield probe, registration


async def record_case(probe, registration, position, request):
    if position >= len(registration['questions']):
        pytest.skip('No registered question in this position')
    configured = request.config.getoption('knowledge_eval_output')
    assert configured, '--knowledge-eval-output must retain baseline evidence'
    item = registration['questions'][position]
    sources = item.get('sources')
    path = await collect_evaluation(probe, item['question'], case_id=item['id'], output=Path(configured), expected_sources=sources)
    request.node.user_properties.append(('knowledge_evidence', str(path)))


@pytest.mark.parametrize('position', range(4))
async def test_frozen_ordinary_chat(ordinary_chat_probe, position, request):
    probe, registration = ordinary_chat_probe
    await record_case(probe, registration, position, request)


@pytest.mark.parametrize('position', range(4))
async def test_frozen_dedicated_query(registered_query_probe, position, request):
    probe, registration = registered_query_probe
    await record_case(probe, registration, position, request)
