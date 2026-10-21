"""Wiki cost regressions on 5d99de373921; no production vault or model is used.

Run from the repository root:
    .venv/bin/python -m pytest -p tests.conftest -q -s \
        docs/review/wiki_efficiency_repros_20261020.py --tb=short

The 400-section cases intentionally fail the bounded traversal assertion.
The 20-section controls pass. Content, revision and receipt checks run first.
"""

import json

import pytest

from server.wiki import format
from server.wiki.extract import blocks
from server.wiki.registry import registry
from tests import test_wiki_api as api
from tests.test_wiki_grant_v2 import assigned
from tests.test_wiki_maintenance_state import prepare

client = api.client


@pytest.mark.parametrize("entry", ["raw_page", "no_change"])
@pytest.mark.parametrize("count", [20, 400])
async def test_selected_operation_does_not_restart_section_scan_per_block(
    client, tmp_path, monkeypatch, entry, count
):
    text = "# Dense fixture\n\n" + "".join(
        f"## Section {number}\n\nClaim {number} keeps its necessary condition. 中文 🐙。\n\n"
        for number in range(count)
    )
    root, config, jobs = await prepare(client, tmp_path, {"Library/dense.md": text})
    original = format.parse
    section_count = len(original(text).sections)
    block_count = len(blocks(text))
    visits = 0

    class Sections(list):
        def __iter__(self):
            nonlocal visits
            for section in super().__iter__():
                visits += 1
                yield section

    def counted_parse(source):
        parsed = original(source)
        parsed.sections = Sections(parsed.sections)
        return parsed

    async def ok(method, path, **kwargs):
        response = await client.request(method, path, **kwargs)
        assert response.status_code == 200, response.text
        return response.json()

    try:
        document = await ok("GET", "/api/wiki/read", headers=api.HEADERS,
            params={"doc": "Library/dense.md", "length": 1})
        if entry == "no_change":
            job = jobs.maintain(f"dense-{count}", "", "Review this note",
                [document["path"]], None, [], "human")
            with assigned(job, config) as (headers, _):
                start, actual = 0, []
                while True:
                    part = await ok("GET", "/api/wiki/read", headers=headers, params={
                        "doc": document["id"], "expected_revision": document["revision"],
                        "start": start, "length": 24000, "view": "compact"})
                    actual.append(part["content"])
                    if part["next_start"] is None:
                        break
                    assert part["next_start"] > start
                    start = part["next_start"]
                assert "".join(actual) == text
                with monkeypatch.context() as patch:
                    patch.setattr(format, "parse", counted_parse)
                    result = await ok("POST", "/api/wiki/revise", headers=headers, json={
                        "operation": "no_change", "doc": document["id"],
                        "expected_revision": document["revision"], "reason": "Full review found no change"})
            assert not result["changed"] and result["revision"] == document["revision"]
            assert any(receipt["id"] == result["receipt_id"] for receipt in jobs.get(job["id"])["receipts"])
        else:
            with monkeypatch.context() as patch:
                patch.setattr(format, "parse", counted_parse)
                result = await ok("GET", "/api/wiki/blocks", headers=api.HEADERS, params={
                    "doc": document["id"], "expected_revision": document["revision"], "limit": 20})
            assert result["revision"] == document["revision"]
            assert result["total"] == block_count and len(result["blocks"]) == 20
            assert text.startswith("".join(part["content"] for part in result["blocks"]))
        assert (root / document["path"]).read_bytes() == text.encode()
        facts = {"entry": entry, "sections": section_count, "blocks": block_count,
            "section_candidates": visits, "traversal_budget": 8 * (section_count + block_count)}
        print(json.dumps(facts, sort_keys=True))
        # Allow several passes; reject restarting from section zero for every block.
        assert visits <= facts["traversal_budget"], facts
    finally:
        registry.close()
        registry.resume_access()
