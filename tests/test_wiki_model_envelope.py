"""The actual Wiki MCP result must fit even when HTTP metadata is very large."""

import json
from types import SimpleNamespace

import pytest
from mcp.types import CallToolRequest, CallToolRequestParams, CallToolResult

from server.mcp_identity import mint, reset_current_scope, set_current_scope, verify
from server.mcp_servers import wiki
from server.wiki.tasks import tasks


@pytest.fixture
def authorized_task():
    session = SimpleNamespace(id="model-envelope", user_id=None, agent_id="fixture-agent")
    task_id = tasks.open(session, "read")
    context = set_current_scope(verify(mint(session.id, wiki_task_id=task_id)))
    try:
        yield
    finally:
        tasks.close(task_id)
        reset_current_scope(context)


async def invoke(tool, arguments):
    request = CallToolRequest(method="tools/call", params=CallToolRequestParams(name=tool, arguments=arguments))
    actual = await wiki.mcp._mcp_server.request_handlers[CallToolRequest](request)
    assert isinstance(actual.root, CallToolResult)
    return actual.root


@pytest.mark.parametrize("tool,arguments,payload", [
    ("search", {"query": "资料", "view": "compact"}, {
        "items": [{"id": "source-id", "path": "Library/source.md",
                   "revision": "a" * 64, "title": "甲" * 36000}],
        "total": 1, "next_offset": None, "next_cursor": None,
    }),
    ("search", {"query": "资料", "view": "full"}, {
        "items": [{"id": "source-id", "path": "Library/source.md",
                   "revision": "a" * 64, "metadata": {
                       "body_sections": [{"title": "章😀" * 18000}],
                   }}],
        "total": 1, "next_offset": None, "next_cursor": None,
    }),
    ("list_documents", {"directory": "Library", "view": "compact"}, {
        "items": [{"id": "source-id", "title": "甲" * 36000}],
        "total": 1, "next_offset": None, "next_cursor": None,
    }),
    ("list_documents", {"directory": "Library", "view": "full"}, {
        "items": [{"id": "source-id", "metadata": {"description": "甲" * 36000}}],
        "total": 1, "next_offset": None, "next_cursor": None,
    }),
    ("graph", {"doc": "source-id"}, {
        "nodes": [{"id": "source-id", "title": "甲" * 36000}],
        "edges": [], "total": 0, "next_offset": None, "next_cursor": None,
    }),
    ("maintenance_status", {"doc": "source-id"}, {
        "ready": True, "items": [{"doc": "source-id", "error": "故障😀" * 18000}],
        "total": 1, "next_cursor": None,
    }),
    ("read", {"doc": "source-id", "view": "full"}, {
        "id": "source-id", "revision": "a" * 64,
        "start": 0, "end": 24000, "next_start": None, "content": "甲😀" * 12000,
    }),
    ("read_many", {"items": [{"doc": "source-id"}], "view": "full"}, {
        "items": [{"doc": "source-id", "start": 0, "end": 24000}],
        "ranges": [{"content": "甲😀" * 12000}],
    }),
    ("blocks", {"doc": "source-id"}, {
        "id": "source-id", "blocks": [{"id": "0", "content": "甲😀" * 12000}],
        "total": 1, "next_offset": None,
    }),
], ids=["search-compact", "search-full", "documents-compact", "documents-full",
        "graph", "maintenance", "read-full", "read-many-full", "raw-blocks"])
async def test_final_dispatch_enforces_budget_even_if_an_upstream_projection_is_missed(
    monkeypatch, authorized_task, tool, arguments, payload,
):
    # Exercise the registered tool and installed SDK converter. The synthetic
    # upstream result deliberately skips service fitting: a final guard must
    # still protect clients when a future endpoint misses its projection.
    monkeypatch.setattr(wiki, "call", lambda *args, **kwargs: payload)
    actual = await invoke(tool, arguments)
    assert len(actual.model_dump_json(by_alias=True, exclude_none=True).encode()) <= 32768
    value = json.loads(actual.content[0].text)
    if value.get("code") == "response_too_large":
        assert value.get("error") and value.get("hint")
    else:
        assert value


@pytest.mark.parametrize("request_key", ["exact-request-key", "巨大键😀" * 10000])
async def test_jobs_export_decision_uses_wrapped_bytes_and_retains_artifact_with_large_key(monkeypatch, authorized_task, request_key):
    from server.wiki import evidence

    payload = {
        "id": "job-id", "request_key": request_key, "status": "running",
        "session_id": "worker-id", "created_at": "2026-10-17T00:00:00+00:00",
        "updated_at": "2026-10-17T00:00:00+00:00", "items": [], "receipts": [],
        "payload": {"request": {"scope": 'escaped\\"' * 3500}}, "error": None,
    }
    if request_key == "exact-request-key":
        assert len(json.dumps(payload, ensure_ascii=False).encode()) < 48 * 1024
    exports = []

    def capture(scope, result):
        exports.append(result)
        return {"path": "/synthetic/private/jobs.json", "bytes": 12345,
                "sha256": "a" * 64, "format": "application/json"}

    monkeypatch.setattr(wiki, "call", lambda *args, **kwargs: payload)
    monkeypatch.setattr(evidence, "capture_jobs", capture)
    actual = await invoke("jobs", {"job_id": "job-id", "view": "full"})
    assert len(actual.model_dump_json(by_alias=True, exclude_none=True).encode()) <= 32768
    value = json.loads(actual.content[0].text)
    assert exports == [payload]
    assert value["summary"]["id"] == "job-id"
    if request_key == "exact-request-key":
        assert value["summary"]["request_key"] == request_key
    else:
        assert value["summary"]["summary_omitted"] and "request_key" not in value["summary"]
    assert value["artifact"]["sha256"] == "a" * 64


async def test_large_invalid_argument_is_bounded_under_the_actual_fastmcp_validation_mode(authorized_task):
    # FastMCP 1.30 sets low-level validate_input=False. Its Pydantic argument
    # error already abbreviates this particular value; retain that control.
    actual = await invoke("search", {"query": "source", "limit": "x" * 33000})
    assert actual.isError
    assert len(actual.model_dump_json(by_alias=True, exclude_none=True).encode()) <= 32768


async def test_sdk_exception_result_is_bounded_outside_the_tool_return_converter(monkeypatch, authorized_task):
    def fail(*args, **kwargs):
        raise RuntimeError("failure details 😀" * 3000)

    monkeypatch.setattr(wiki, "call", fail)
    actual = await invoke("search", {"query": "source"})
    assert actual.isError
    assert len(actual.model_dump_json(by_alias=True, exclude_none=True).encode()) <= 32768


def test_wiki_dictionary_tools_pin_the_pre_receipt_unstructured_output_contract():
    assert all(tool.output_schema is None for tool in wiki.mcp._tool_manager._tools.values())
