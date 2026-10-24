"""Same-revision raw pages preserve coordinates, authorization and read coverage."""

import json
from time import perf_counter

import pytest

from server.wiki import format
from server.wiki.contracts import NoChangeRevision
from server.wiki.curation import WikiCuration
from server.wiki.revise import revise
from server.wiki.store import WikiError, revision
from tests.test_wiki_commonmark_locations import store as store
from tests.test_wiki_read_response import client as client
from tests.test_wiki_read_response import setup_note
from tests.test_wiki_write_gates import active_worker


def page_measurements(store, monkeypatch, count):
    text = "\ufeff---\r\ntitle: Raw coordinates\r\n---\r\n# Topic\r\n\r\n" + "".join(
        f"## Section {index}\r\n\r\nEvidence {index}: 甲😀e\u0301 with conditions.\r\n\r\n" for index in range(count))
    path = store.path("Library/long.md")
    path.write_bytes(text.encode())
    document = store.read("Library/long.md", length=1)
    parse = format._markdown.parse
    scanned = []

    def counted(source, *args, **kwargs):
        scanned.append(len(source))
        return parse(source, *args, **kwargs)

    monkeypatch.setattr(format._markdown, "parse", counted)
    curation = WikiCuration(store)
    measured = []
    first = None
    for phase in ("single_page", "all_pages", "no_change"):
        scanned.clear()
        started = perf_counter()
        if phase == "single_page":
            first = curation.blocks(document["id"], limit=25)
            assert first["revision"] == revision(text.encode())
        elif phase == "all_pages":
            content, offset = [], 0
            while True:
                page = curation.blocks(document["id"], expected_revision=document["revision"], offset=offset, limit=25)
                for part in page["blocks"]:
                    assert part["content"] == text[part["start"]:part["end"]]
                    assert part["sha256"] == revision(part["content"].encode())
                    if part["role"] != "metadata" and part["title"] != "Topic":
                        assert part["title"].startswith("Section ")
                    content.append(part["content"])
                if page["next_offset"] is None:
                    break
                offset = page["next_offset"]
            assert "".join(content) == text
        else:
            confirmed = revise(curation, NoChangeRevision(
                operation="no_change", doc=document["id"], expected_revision=document["revision"],
                reason="Review current evidence and conditions"), "human", None)
            assert not confirmed["changed"] and confirmed["receipt_id"] and confirmed["change_id"] is None
            assert path.read_bytes() == text.encode()
            assert confirmed["revision"] == document["revision"]
        measured.append({"sections": count, "phase": phase, "seconds": perf_counter() - started,
                         "parses": len(scanned), "parsed_characters": sum(scanned)})
    assert first is not None
    return measured


def test_raw_pagination_reuses_same_revision_coordinates_and_no_change_needs_no_parse(store, monkeypatch):
    measurements = page_measurements(store, monkeypatch, 30)
    assert [row["parses"] for row in measurements] == [1, 0, 0]


@pytest.mark.wiki_perf
@pytest.mark.parametrize("count", [100, 400, 1000])
def test_raw_page_and_no_change_cost(store, monkeypatch, count):
    print(json.dumps({"raw_page_cost": page_measurements(store, monkeypatch, count)}), flush=True)


def test_raw_coordinates_cannot_survive_a_revision_or_escape_in_a_mutable_response(store):
    path = store.path("Library/note.md")
    path.write_text("# Original\n\nFirst conditions.\n")
    curation = WikiCuration(store)
    first = curation.blocks("Library/note.md")
    first["blocks"][-1]["title"] = "Poisoned response"
    assert curation.blocks(first["id"])["blocks"][-1]["title"] == "Original"
    path.write_text("# Replacement\n\nLater conditions.\n")
    with pytest.raises(WikiError) as conflict:
        curation.blocks(first["id"], expected_revision=first["revision"])
    assert conflict.value.status == 409
    current = curation.blocks(first["id"])
    assert current["revision"] != first["revision"]
    assert current["blocks"][-1]["title"] == "Replacement"
    assert current["blocks"][-1]["locator"] != first["blocks"][-1]["locator"]


async def test_warm_raw_coordinates_grant_only_returned_text_and_require_live_authority(client, tmp_path):
    text = "# Topic\n\n" + "".join(f"## Section {index}\n\nEvidence and conditions {index}.\n\n" for index in range(30))
    root, config = await setup_note(client, tmp_path, text)
    path = "Library/note.md"
    async with active_worker(config, [path], client=client) as headers:
        first = await client.get("/api/wiki/blocks", headers=headers, params={"doc": path, "view": "locators", "limit": 3})
        assert first.status_code == 200, first.text
        body = {"operation": "no_change", "doc": first.json()["id"], "expected_revision": first.json()["revision"], "reason": "Review"}
        denied = await client.post("/api/wiki/revise", headers=headers, json=body)
        assert denied.status_code == 422
        offset, returned = 0, []
        while True:
            response = await client.get("/api/wiki/blocks", headers=headers, params={
                "doc": path, "expected_revision": body["expected_revision"], "offset": offset, "limit": 3})
            assert response.status_code == 200, response.text
            page = response.json()
            returned.extend(part["content"] for part in page["blocks"])
            if page["next_offset"] is None:
                break
            if offset == 0:
                denied = await client.post("/api/wiki/revise", headers=headers, json=body)
                assert denied.status_code == 422
            offset = page["next_offset"]
        assert "".join(returned) == text
        accepted = await client.post("/api/wiki/revise", headers=headers, json=body)
        assert accepted.status_code == 200 and accepted.json()["receipt_id"], accepted.text
    denied = await client.post("/api/wiki/revise", headers=headers, json=body)
    assert denied.status_code == 403
    assert (root / path).read_bytes() == text.encode()
