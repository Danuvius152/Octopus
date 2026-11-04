"""Actual loopback HTTP, SDK dispatch, pagination and read authorization agree."""

import asyncio
import copy
import hashlib
import json
from urllib.parse import urlsplit

import pytest
from mcp.types import CallToolRequest, CallToolRequestParams

from server.mcp_identity import reset_current_scope, set_current_scope, verify
from server.mcp_result import model_result_bytes, result_bytes
from server.mcp_servers import wiki
from server.wiki.curation import WikiCuration
from server.wiki.jobs import WikiJobs
from server.wiki.registry import registry
from server.wiki.store import WikiError
from server.wiki.tasks import tasks
from tests import wiki_api as api
from tests.wiki_job_runtime import active_worker

client = api.client


def bridge(client, monkeypatch, headers):
    loop = asyncio.get_running_loop()
    monkeypatch.setattr(wiki._host, "headers", lambda: headers)

    def request(method, url, **kwargs):
        response = client.request(method, urlsplit(url).path, headers=headers,
                                  params=kwargs.get("params"), json=kwargs.get("json"))
        return asyncio.run_coroutine_threadsafe(response, loop).result(timeout=20)

    monkeypatch.setattr(wiki.httpx, "request", request)


async def invoke(name, arguments, headers):
    context = set_current_scope(verify(headers["Authorization"].removeprefix("Bearer ")))

    async def dispatch():
        request = CallToolRequest(method="tools/call", params=CallToolRequestParams(name=name, arguments=arguments))
        return (await wiki.mcp._mcp_server.request_handlers[CallToolRequest](request)).root

    try:
        actual = await asyncio.to_thread(lambda: asyncio.run(dispatch()))
    finally:
        reset_current_scope(context)
    assert result_bytes(actual) <= 32768
    return json.loads(actual.content[0].text)


async def configure(client, tmp_path, files):
    root = tmp_path / "vault"
    (root / "Library").mkdir(parents=True)
    for name, content in files.items():
        (root / name).write_bytes(content.encode())
    return root, await api.configure(client, root)


async def test_navigation_http_shape_and_shortened_final_pages_keep_every_identity(client, tmp_path, monkeypatch):
    # Keep multibyte truncation and overlong titles without turning pagination
    # correctness into a million-character Chinese tokenization workload.
    # One title exercises an individually oversized HTTP field; every title
    # still exceeds the model projection limit and every page keeps its rows.
    files = {f"Library/note{index:02}.md": f"# shared {index:02} " + "中文😀" * 100 + ("x" * 36000 if index == 0 else "") +
             "\n\nA distinct paragraph " + str(index) + "说明" * 400 + "\n\n[[note00]]\n" for index in range(36)}
    _, config = await configure(client, tmp_path, files)
    grants = []
    monkeypatch.setattr(tasks, "record_read", lambda *args: grants.append(args))
    async with active_worker(config, ["Library/note00.md"], client=client) as worker:
        headers = worker.headers
        bridge(client, monkeypatch, headers)
        for name, route, arguments in [
            ("search", "search", {"query": "shared", "intent": "lookup", "view": "compact"}),
            ("search", "search", {"query": "shared", "intent": "lookup", "view": "full"}),
            ("list_documents", "documents", {"directory": "Library", "view": "compact"}),
            ("list_documents", "documents", {"directory": "Library", "view": "full"}),
        ]:
            human = (await client.get("/api/wiki/" + route, headers=client.headers, params=arguments | {"limit": 50})).json()
            expected = [item["id"] for item in human["items"]]
            assert len(expected) == 36 and len(human["items"][0]["title"]) > 36000
            seen, cursor, clipped = [], None, False
            for _ in range(20):
                value = await invoke(name, arguments | {"limit": 36, **({"cursor": cursor} if cursor else {})}, headers)
                assert "code" not in value, value
                assert value["response_bytes"] == model_result_bytes(value)
                assert value["returned_count"] == len(value["items"]) > 0
                clipped |= len(value["items"]) < 36
                for item in value["items"]:
                    assert len(item["title"]) <= 240 and item["title_truncated"]
                    seen.append(item["id"])
                cursor = value["next_cursor"]
                if cursor is None:
                    assert value["next_offset"] is None
                    break
                assert value["next_offset"] == len(seen)
            else:
                pytest.fail("pagination did not advance")
            assert seen == expected and clipped, (name, arguments, len(seen))
        human_graph = (await client.get("/api/wiki/graph", params={"doc": "Library/note00.md"}, headers=client.headers)).json()
        seen, cursor = [], None
        while True:
            value = await invoke("graph", {"doc": "Library/note00.md", "limit": 36, **({"cursor": cursor} if cursor else {})}, headers)
            assert "code" not in value, value
            nodes = {node["id"] for node in value["nodes"]}
            assert all(edge["from"] in nodes and edge["to"] in nodes for edge in value["edges"])
            seen.extend(value["edges"])
            cursor = value["next_cursor"]
            if cursor is None:
                break
        assert seen == human_graph["edges"] and grants == []
        expected, cursor = [], None
        while True:
            human = (await client.get("/api/wiki/maintenance-status", params={"limit": 50, **({"cursor": cursor} if cursor else {})}, headers=client.headers)).json()
            expected.extend(item["current"]["id"] for item in human["items"])
            cursor = human["next_cursor"]
            if cursor is None:
                break
        seen, cursor = [], None
        while True:
            value = await invoke("maintenance_status", {"limit": 50, **({"cursor": cursor} if cursor else {})}, headers)
            assert value["ready"] and value["returned_count"] == len(value["items"])
            seen.extend(item["current"]["id"] for item in value["items"])
            cursor = value["next_cursor"]
            if cursor is None:
                break
            assert value["next_offset"] == len(seen)
        assert seen == expected and len(seen) == 36 and grants == []


@pytest.mark.parametrize("batch", [False, True])
async def test_full_model_reads_clip_before_grants_and_finish_exact_unicode(client, tmp_path, monkeypatch, batch):
    original = '\ufeff# Source\r\n\r\n' + '甲😀e\u0301\\"\t条件。\r\n' * 2200
    text = original.removeprefix('\ufeff')
    _, config = await configure(client, tmp_path, {"Library/note.md": original})
    async with active_worker(config, ["Library/note.md"], client=client) as worker:
        headers = worker.headers
        bridge(client, monkeypatch, headers)
        task = worker.task
        passages, start, revision = [], 0, None
        while True:
            request = {"doc": "Library/note.md", "start": start, "length": 24000,
                       **({"expected_revision": revision} if revision else {})}
            value = await invoke("read_many" if batch else "read", {"items": [request], "view": "full"} if batch else request | {"view": "full"}, headers)
            assert "code" not in value, value
            part = value["ranges"][0] if batch else value
            item = value["items"][0] if batch else value
            revision = item["revision"]
            assert part["content"] == text[start:part["end"]]
            assert part["line_end"] == text[:part["end"]].count("\n") + 1
            key = (item.get("id", part.get("doc")), "Library/note.md", revision)
            assert task.reads[key][-1] == (start, part["end"])
            if start == 0:
                assert part["end"] < len(text)
                with pytest.raises(WikiError, match="full current note"):
                    tasks.require_read(task, registry.store(task.user_id), "Library/note.md", revision)
            passages.append(part["content"])
            start = item["next_start"]
            if start is None:
                break
        assert "".join(passages) == text
        tasks.require_read(task, registry.store(task.user_id), "Library/note.md", revision)


async def test_full_metadata_refusal_retains_prior_receipts_without_granting_more(client, tmp_path, monkeypatch):
    text = "# Source\n\n" + "## Heading\n\nA paragraph.\n\n" * 600
    _, config = await configure(client, tmp_path, {"Library/note.md": text})
    async with active_worker(config, ["Library/note.md"], client=client) as worker:
        headers = worker.headers
        bridge(client, monkeypatch, headers)
        task = worker.task
        await invoke("read", {"doc": "Library/note.md", "length": 4}, headers)
        prior = copy.deepcopy(task.reads)
        value = await invoke("read", {"doc": "Library/note.md", "view": "full"}, headers)
        assert value["code"] == "response_too_large" and task.reads == prior
        value = await invoke("read_many", {"items": [{"doc": "Library/note.md"}], "view": "full"}, headers)
        assert value["code"] == "response_too_large" and task.reads == prior


async def test_raw_large_block_keeps_hash_and_can_continue_without_unseen_edit_grants(client, tmp_path, monkeypatch):
    original = "\ufeff# Source\r\n\r\n" + "甲😀条件与理由。\r\n" * 6000 + "\r\nEnd.\r\n"
    root, config = await configure(client, tmp_path, {"Library/note.md": original})
    owner = (await api.session_manager.db.get_system_agent())["user_id"]
    full = WikiCuration(registry.store(owner)).blocks("Library/note.md")
    block = max(full["blocks"], key=lambda part: part["characters"])
    assert block["sha256"] == hashlib.sha256(block["content"].encode()).hexdigest()
    async with active_worker(config, ["Library/note.md"], client=client) as worker:
        headers = worker.headers
        bridge(client, monkeypatch, headers)
        task = worker.task
        cursor, passages = None, []
        while True:
            value = await invoke("blocks", {"doc": "Library/note.md", "ids": [block["id"]], "offset": 0,
                                           **({"start": cursor} if cursor is not None else {})}, headers)
            part = value["blocks"][0]
            assert (part["start"], part["end"], part["sha256"], part["locator"]) == tuple(block[key] for key in ("start", "end", "sha256", "locator"))
            assert part["content"] == original[part["content_start"]:part["content_end"]]
            assert task.raw_reads[(full["id"], full["path"], full["revision"])][-1] == (part["content_start"], part["content_end"])
            if cursor is None:
                assert value["next_offset"] == 0 and value["next_start"] > part["start"]
                position = block["characters"] - 5
                denied = await client.post("/api/wiki/revise", headers=headers, json={
                    "operation": "patch", "doc": full["id"], "expected_revision": full["revision"], "reason": "unseen suffix control",
                    "edits": [{"locator": block["locator"], "expected_block_hash": block["sha256"], "start": position, "end": position + 1,
                               "expected_text": block["content"][position:position + 1], "replacement": "Z", "kind": "correction"}],
                })
                assert denied.status_code == 422 and "Read the current raw passage" in denied.text
                assert (root / "Library/note.md").read_bytes() == original.encode()
            passages.append(part["content"])
            previous, cursor = cursor, value["next_start"]
            if value["next_offset"] is None:
                break
            assert cursor is not None and cursor > (previous or block["start"])
        assert "".join(passages) == block["content"]
        prior = copy.deepcopy(task.raw_reads)
        await invoke("blocks", {"doc": full["id"], "view": "locators"}, headers)
        assert task.raw_reads == prior
        invalid = await client.get("/api/wiki/blocks", headers=headers, params={"doc": full["id"], "ids": block["id"], "start": block["end"] + 1})
        assert invalid.status_code == 422 and task.raw_reads == prior


async def test_compact_jobs_pages_keep_exact_keys_and_minimum_oversize_is_refused(client, tmp_path, monkeypatch):
    _, config = await configure(client, tmp_path, {"Library/note.md": "# Source\nText.\n"})
    owner = (await api.session_manager.db.get_system_agent())["user_id"]
    jobs = WikiJobs(owner)
    for index in range(8):
        jobs.maintain(str(index) + "甲😀" * 1200, "", f"purpose{index}", ["Library/note.md"], None, [], "human")
    async with active_worker(config, ["Library/note.md"], client=client) as worker:
        headers = worker.headers
        bridge(client, monkeypatch, headers)
        # active_worker adds one genuine scoped job as well.
        expected = jobs.list(limit=30, view="full")["items"]
        seen, offset = [], 0
        while True:
            value = await invoke("jobs", {"limit": 30, "offset": offset}, headers)
            assert value["items"]
            seen.extend(value["items"])
            offset = value["next_offset"]
            if offset is None:
                break
            assert offset == len(seen)
        assert [(item["id"], item["request_key"]) for item in seen] == [(item["id"], item["request_key"]) for item in expected]
        job = expected[-1]
        with jobs.store.connect() as db:
            db.execute("UPDATE jobs SET request_key=? WHERE id=?", ("x" * 40000, job["id"]))
        value = await invoke("jobs", {"job_id": job["id"]}, headers)
        assert value["code"] == "response_too_large"
