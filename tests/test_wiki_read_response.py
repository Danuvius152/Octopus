"""Model payload bounds must preserve evidence, continuation and write gates."""

import json

import pytest
from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult

from server.mcp_servers import wiki as wiki_tools
from server.wiki import read_response
from server.wiki.tasks import tasks
from tests import wiki_api as wiki_tests
from tests.wiki_job_runtime import active_worker

client = wiki_tests.client


async def setup_note(client, tmp_path, text):
    root = tmp_path / "vault"
    (root / "Library").mkdir(parents=True)
    (root / "Library/note.md").write_bytes(text.encode())
    return root, await wiki_tests.configure(client, root)


async def test_measured_bytes_match_actual_fastmcp_dict_conversion():
    payload = {"content": '中文😀\r\n\\"\x00', "items": [{"end": 10}], "response_bytes": 123}
    mcp = FastMCP("read-sizing")

    @mcp.tool()
    def fixture() -> dict:
        return payload

    text = await mcp.call_tool("fixture", {})
    assert isinstance(text, list)
    actual = CallToolResult(content=text)
    assert len(actual.model_dump_json(by_alias=True, exclude_none=True).encode()) == read_response.model_result_bytes(payload)
    assert json.loads(text[0].text) == payload and actual.structuredContent is None


async def test_compact_omits_large_structure_but_full_http_default_is_compatible(client, tmp_path):
    text = "# 资料\n\n" + "## 小节\n\n理由与条件。\n\n" * 600
    await setup_note(client, tmp_path, text)
    params = {"doc": "Library/note.md", "start": 30, "length": 80}
    full = (await client.get("/api/wiki/read", headers=client.headers, params=params)).json()
    compact = (await client.get("/api/wiki/read", headers=client.headers, params=params | {"view": "compact"})).json()
    explicit = (await client.get("/api/wiki/read", headers=client.headers, params=params | {"view": "full"})).json()
    assert len(full["metadata"]["body_sections"]) >= 600
    assert "metadata" not in compact and "pages" not in compact
    assert compact["content"] == full["content"] == explicit["content"] == text[30:110]
    for key in ("id", "path", "revision", "start", "end", "line_start", "line_end", "next_start", "stale"):
        assert compact[key] == full[key] == explicit[key]
    assert "view" not in full and compact["status"] == "ok"
    assert compact["response_bytes"] == read_response.model_result_bytes(compact) <= 32768
    assert len(json.dumps(compact).encode()) < len(json.dumps(full).encode()) / 5


@pytest.mark.parametrize("batch", [False, True])
async def test_byte_clipping_records_only_actual_unicode_prefix_and_can_finish_maintenance(client, tmp_path, monkeypatch, batch):
    original = '\ufeff# 观点\r\n\r\n' + '甲😀e\u0301\\"\t条件与理由。\r\n' * 1500
    root, config = await setup_note(client, tmp_path, original)
    # The existing UTF-8-sig reader omits the BOM from character coordinates;
    # source byte preservation is a separate invariant.
    text = original.removeprefix('\ufeff')
    seen = []
    record = tasks.record_read

    def observe(task, document):
        seen.append((document["start"], document["end"], document["characters"]))
        record(task, document)

    monkeypatch.setattr(tasks, "record_read", observe)
    async with active_worker(config, ["Library/note.md"], client=client) as worker:
        headers = worker.headers
        start, revision, passages = 0, None, []
        while True:
            if batch:
                response = await client.post("/api/wiki/read-many", headers=headers, json={
                    "view": "compact", "budget_chars": 24000,
                    "items": [{"doc": "Library/note.md", "start": start, "length": 24000, "expected_revision": revision}],
                })
            else:
                params = {"doc": "Library/note.md", "start": start, "length": 24000, "view": "compact"}
                if revision:
                    params["expected_revision"] = revision
                response = await client.get("/api/wiki/read", headers=headers, params=params)
            assert response.status_code == 200, response.text
            data = response.json()
            item = data["items"][0] if batch else data
            passage = data["ranges"][0] if batch else data
            revision = item["revision"]
            assert data["response_bytes"] == read_response.model_result_bytes(data) <= 32768
            assert passage["content"] == text[start:passage["end"]]
            assert seen[-1] == (start, passage["end"], len(text))
            assert passage["line_start"] == text[:start].count("\n") + 1
            assert passage["line_end"] == text[:passage["end"]].count("\n") + 1
            passages.append(passage["content"])
            if start == 0:
                assert data["response_budget_exhausted"] and item["status"] == "partial"
                rejected = await client.post("/api/wiki/organize", headers=headers, json={
                    "path": "Library/note.md", "expected_revision": revision,
                    "key_note": "条件及理由均保留。", "reason": "check full reading guard",
                })
                assert rejected.status_code == 422 and "full current note" in rejected.text
                assert (root / "Library/note.md").read_bytes() == original.encode()
            following = item["next_start"]
            if following is None:
                break
            assert following == passage["end"] > start
            start = following
        assert "".join(passages) == text
        accepted = await client.post("/api/wiki/organize", headers=headers, json={
            "path": "Library/note.md", "expected_revision": revision,
            "key_note": "条件及理由均保留。", "reason": "read guard now satisfied",
        })
        assert accepted.status_code == 200, accepted.text


async def test_batch_overlap_errors_and_response_cutoff_keep_truthful_ranges(client, tmp_path, monkeypatch):
    text = "# 资料\n" + "甲😀\n" * 13000
    await setup_note(client, tmp_path, text)
    seen = []
    monkeypatch.setattr(tasks, "record_read", lambda task, doc: seen.append((doc["id"], doc["start"], doc["end"])))
    response = await client.post("/api/wiki/read-many", headers=client.headers, json={"view": "compact", "items": [
        {"doc": "Library/note.md", "start": 0, "length": 24000},
        {"doc": "Library/note.md", "start": 50, "length": 50},
        {"doc": "Library/note.md", "start": 23990, "length": 100},
        {"doc": "Library/note.md", "expected_revision": "0" * 64},
        {"doc": "missing.md"},
    ]})
    assert response.status_code == 200, response.text
    value = response.json()
    assert [item["status"] for item in value["items"]] == ["partial", "ok", "budget_exhausted", "changed", "not_found"]
    assert len(value["ranges"]) == len(seen) == len(value["documents"]) == 1
    piece = value["ranges"][0]
    assert seen == [(piece["doc"], piece["start"], piece["end"])]
    assert piece["content"] == text[:piece["end"]]
    assert value["characters_returned"] == len(piece["content"])
    assert value["items"][2]["next_start"] == 23990
    assert "range_id" not in value["items"][2]
    assert value["items"][0]["range_id"] == value["items"][1]["range_id"] == piece["id"]


@pytest.mark.parametrize("batch", [False, True])
async def test_unfittable_compact_envelope_does_not_grant_a_read(client, tmp_path, monkeypatch, batch):
    await setup_note(client, tmp_path, "# 内容\n不能伪造已经读完。")
    seen = []
    monkeypatch.setattr(read_response, "COMPACT_READ_MAX_BYTES", 1)
    monkeypatch.setattr(tasks, "record_read", lambda *args: seen.append(args))
    if batch:
        response = await client.post("/api/wiki/read-many", headers=client.headers, json={"view": "compact", "items": [{"doc": "Library/note.md"}]})
    else:
        response = await client.get("/api/wiki/read", headers=client.headers, params={"view": "compact", "doc": "Library/note.md"})
    assert response.status_code == 422 and "envelope" in response.text
    assert seen == []


async def test_compact_keeps_eof_and_rejects_invalid_views_and_changed_revisions(client, tmp_path):
    text = "# 当前\n" + "甲" * 200
    await setup_note(client, tmp_path, text)
    initial = (await client.get("/api/wiki/read", headers=client.headers, params={"doc": "Library/note.md", "view": "compact"})).json()
    eof = (await client.get("/api/wiki/read", headers=client.headers, params={"doc": initial["id"], "start": len(text), "view": "compact"})).json()
    assert eof["status"] == "ok" and eof["content"] == "" and eof["next_start"] is None
    assert eof["start"] == eof["end"] == len(text)
    changed = await client.get("/api/wiki/read", headers=client.headers, params={"doc": initial["id"], "expected_revision": "0" * 64, "view": "compact"})
    assert changed.status_code == 409
    invalid = await client.get("/api/wiki/read", headers=client.headers, params={"doc": initial["id"], "view": "other"})
    assert invalid.status_code == 422
    invalid = await client.post("/api/wiki/read-many", headers=client.headers, json={"items": [{"doc": initial["id"]}], "view": "other"})
    assert invalid.status_code == 422


def test_compact_preserves_warnings_and_marks_condensed_diagnostics():
    document = {"title": "甲" * 300, "stale": True, "metadata": {
        "frontmatter_error": "details", "source_refs": [{"status": "changed"}, {"status": "missing"}],
    }}
    value = read_response.compact_document(document)
    assert value["stale"] is True and value["title_truncated"]
    assert value["warnings"] == {"frontmatter_error": True, "sources": {"changed": 1, "missing": 1}}
    index = read_response.compact_index({"index_error_count": 10, "index_errors": [{"path": "x" * 200, "error": "y" * 200}] * 10})
    assert index["index_error_count"] == 10 and index["index_errors_truncated"]
    assert len(index["index_errors"]) == 3
    assert index["index_errors"][0]["path"] == "x" * 200
    limited = read_response.compact_document({"metadata": {"limitations": ["A" * 500, "B", "C", "D"]}})
    assert limited["warnings"]["limitations"] == ["A" * 240, "B", "C"]
    assert limited["warnings"]["limitations_count"] == 4
    assert limited["warnings"]["limitations_truncated"]


@pytest.mark.parametrize("eof_first", [False, True])
async def test_clipped_batch_keeps_merged_empty_eof_in_its_own_range(client, tmp_path, eof_first):
    text = "# 原文\n" + "甲" * 12000
    root, config = await setup_note(client, tmp_path, text)
    requests = [{"doc": "Library/note.md", "start": 0, "length": 24000},
                {"doc": "Library/note.md", "start": len(text), "length": 1}]
    if eof_first:
        requests.reverse()
    async with active_worker(config, ["Library/note.md"], client=client) as worker:
        headers = worker.headers
        response = await client.post("/api/wiki/read-many", headers=headers, json={"view": "compact", "items": requests})
        assert response.status_code == 200, response.text
        data = response.json()
        assert data["response_budget_exhausted"]
        intervals = {piece["id"]: piece for piece in data["ranges"]}
        eof = next(item for item in data["items"] if item["requested_start"] == len(text))
        assert eof["status"] == "ok" and eof["next_start"] is None
        assert eof["start"] == eof["end"] == len(text)
        assert intervals[eof["range_id"]]["content"] == ""
        assert intervals[eof["range_id"]]["line_start"] == text.count("\n") + 1
        for item in data["items"]:
            piece = intervals[item["range_id"]]
            assert piece["start"] <= item["start"] <= item["end"] <= piece["end"]
            assert item["next_start"] is None or item["next_start"] >= item["end"]
        rejected = await client.post("/api/wiki/organize", headers=headers, json={
            "path": "Library/note.md", "expected_revision": eof["revision"],
            "key_note": "不得把空EOF当成读完全文", "reason": "coverage control",
        })
        assert rejected.status_code == 422 and "full current note" in rejected.text
        assert (root / "Library/note.md").read_bytes() == text.encode()


def test_model_tools_default_to_compact_and_allow_explicit_full(monkeypatch):
    calls = []
    monkeypatch.setattr(wiki_tools, "call", lambda *args, **kwargs: calls.append((args, kwargs)) or {})
    wiki_tools.read("note.md")
    wiki_tools.read("note.md", view="full")
    wiki_tools.read_many([wiki_tools.ReadRequest(doc="note.md")])
    wiki_tools.search("a natural question", intent="question")
    wiki_tools.list_documents(directory="Library/course")
    wiki_tools.search("a natural question", view="full")
    wiki_tools.list_documents(directory="Library/course", view="full")
    assert calls[0][1]["params"]["view"] == "compact"
    assert calls[1][1]["params"]["view"] == "full"
    assert calls[2][1]["body"]["view"] == "compact"
    assert calls[3][1]["params"]["view"] == "compact"
    assert calls[4][1]["params"]["view"] == "compact"
    assert calls[5][1]["params"]["view"] == "full"
    assert calls[6][1]["params"]["view"] == "full"
