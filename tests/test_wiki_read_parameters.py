"""Read bounds are the same in the model schema and real MCP/HTTP calls."""

import json

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from server.config import settings
from server.deps import get_session_manager
from server.main import _wiki_error
from server.mcp_identity import mint
from server.mcp_servers import wiki as wiki_tools
from server.routers.wiki import router
from server.wiki import WikiError
from server.wiki.tasks import tasks
from tests import test_wiki_api as wiki_api
from tests.callback_api import start_callback_api

client = wiki_api.client


async def test_read_schema_and_real_calls_share_length_bounds(client, tmp_path, monkeypatch):
    vault = tmp_path / 'vault'
    vault.mkdir()
    text = '# Unicode\n甲😀é原文\n' + 'original source text\n' * 2000
    (vault / 'source.md').write_text(text)
    config = await wiki_api.configure(client, vault)
    manager = wiki_api.session_manager
    session = await manager.create_session(config['curator_agent_id'], 'Read parameter contract', str(tmp_path))
    token = tasks.open(session, 'read')
    bearer = mint(session.id, user_id=session.user_id, wiki_task_id=token)
    api = await start_callback_api(router, overrides={get_session_manager: lambda: manager},
                                   exception_handlers={WikiError: _wiki_error}, readiness_token=bearer)
    monkeypatch.setattr(settings, 'port', api.port)
    actual_reads, forwards = [], []
    record, call = tasks.record_read, wiki_tools.call

    def record_read(task, document):
        actual_reads.append((document['id'], document['revision'], document['start'], document['end']))
        return record(task, document)

    def forward(*args, **kwargs):
        forwards.append(args)
        return call(*args, **kwargs)

    monkeypatch.setattr(tasks, 'record_read', record_read)
    monkeypatch.setattr(wiki_tools, 'call', forward)
    base = f'http://127.0.0.1:{api.port}'
    try:
        async with httpx.AsyncClient(base_url=base, headers={'Authorization': f'Bearer {bearer}'}) as http:
            async with streamable_http_client(f'{base}/mcp/wiki/mcp', http_client=http) as (read, write, _):
                async with ClientSession(read, write) as peer:
                    await peer.initialize()
                    schemas = {tool.name: tool.inputSchema for tool in (await peer.list_tools()).tools}
                    single = schemas['read']['properties']['length']
                    batch = schemas['read_many']['$defs']['ReadRequest']['properties']['length']
                    for field in (single, batch):
                        assert {key: field[key] for key in ('type', 'default', 'minimum', 'maximum')} == {
                            'type': 'integer', 'default': 16000, 'minimum': 1, 'maximum': 24000,
                        }
                    for length in (None, 1, 24000, 0, -1, 24001):
                        item = {'doc': 'source.md', 'start': 10}
                        if length is not None:
                            item['length'] = length
                        valid = length is None or 1 <= length <= 24000
                        count, sent = len(actual_reads), len(forwards)
                        single_http = await http.get('/api/wiki/read', params=item | {'view': 'compact'})
                        batch_http = await http.post('/api/wiki/read-many', json={'items': [item], 'view': 'compact'})
                        single_mcp = await peer.call_tool('read', item)
                        batch_mcp = await peer.call_tool('read_many', {'items': [item]})
                        if not valid:
                            assert single_http.status_code == batch_http.status_code == 422
                            assert single_mcp.isError and batch_mcp.isError
                            assert len(actual_reads) == count and len(forwards) == sent
                            continue
                        assert single_http.status_code == batch_http.status_code == 200
                        assert not single_mcp.isError and not batch_mcp.isError
                        one = single_http.json()
                        two = json.loads(single_mcp.content[0].text)
                        pieces = [one, two, batch_http.json()['ranges'][0], json.loads(batch_mcp.content[0].text)['ranges'][0]]
                        end = 10 + (16000 if length is None else length)
                        for piece in pieces:
                            assert (piece['start'], piece['end'], piece['content']) == (10, end, text[10:end])
                        assert two['revision'] == one['revision']
                        assert actual_reads[count:] == [(one['id'], one['revision'], 10, end)] * 4
                        assert len(forwards) == sent + 2
    finally:
        tasks.close(token)
        await api.stop()
