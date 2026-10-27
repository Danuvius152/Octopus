"""Search and delivered passages share coordinates in the unchanged source."""

import hashlib

import pytest

from server.wiki import read_response
from tests.test_wiki_api import client as client
from tests.test_wiki_api import configure


@pytest.mark.parametrize("newlines", [("\n",) * 5, ("\r\n",) * 5, ("\r",) * 5, ("\r", "\r\n", "\n", "\r", "\r\n")])
async def test_search_single_and_batch_read_keep_raw_line_coordinates(client, tmp_path, newlines):
    lines = ["# Source", "intro", "## Claim", "quartz evidence", "tail"]
    text = "".join(line + newline for line, newline in zip(lines, newlines, strict=True))
    root = tmp_path / "vault"
    root.mkdir()
    (root / "note.md").write_bytes(text.encode())
    await configure(client, root)
    revision = hashlib.sha256(text.encode()).hexdigest()
    starts = [0]
    for line, newline in zip(lines, newlines, strict=True):
        starts.append(starts[-1] + len(line) + len(newline))
    found = (await client.get("/api/wiki/search", params={"query": "quartz", "view": "full"})).json()
    section = found["items"][0]["section"]
    assert (section["start"], section["line_start"], section["line_end"]) == (starts[2], 3, 6)
    located = (await client.get("/api/wiki/read", params={"doc": "note.md", "location": "L3-L4", "view": "compact"})).json()
    assert located["content"] == text[starts[2]:starts[4]]
    assert (located["line_start"], located["line_end"]) == (3, 5)
    for start, end, first, last in [(starts[2], starts[4], 3, 5), (len(text), len(text), 6, 6)]:
        params = {"doc": "note.md", "start": start, "length": max(1, end - start), "view": "compact"}
        single = (await client.get("/api/wiki/read", params=params)).json()
        batch = (await client.post("/api/wiki/read-many", json={
            "items": [{key: value for key, value in params.items() if key != "view"}], "view": "compact",
        })).json()
        for passage in (single, batch["ranges"][0]):
            assert passage["content"] == text[start:end]
            assert (passage["line_start"], passage["line_end"]) == (first, last)
            assert (passage["start"], passage["end"], passage["revision"]) == (start, end, revision)
    assert (root / "note.md").read_bytes() == text.encode()


@pytest.mark.parametrize("batch", [False, True])
async def test_crlf_split_and_clipped_responses_increment_only_after_complete_newline(client, tmp_path, monkeypatch, batch):
    text = "# Note\r\n" + "甲\r\n乙\r丙\n" * 400
    root = tmp_path / "vault"
    root.mkdir()
    (root / "note.md").write_bytes(text.encode())
    await configure(client, root)
    # Each boundary of the first CRLF, including an empty read at EOF.
    for start, length, expected in [(6, 1, (1, 1)), (7, 1, (1, 2)), (8, 1, (2, 2)), (len(text), 1, (1202, 1202))]:
        single = (await client.get("/api/wiki/read", params={"doc": "note.md", "start": start, "length": length, "view": "compact"})).json()
        assert (single["line_start"], single["line_end"]) == expected
    monkeypatch.setattr(read_response, "COMPACT_READ_MAX_BYTES", 3200)
    seen = []
    from server.wiki.tasks import tasks

    record = tasks.record_read

    def observe(task, document):
        seen.append((document["start"], document["end"]))
        record(task, document)

    monkeypatch.setattr(tasks, "record_read", observe)
    if batch:
        data = (await client.post("/api/wiki/read-many", json={"items": [{"doc": "note.md"}], "view": "compact"})).json()
        passage = data["ranges"][0]
        assert data["items"][0]["next_start"] == passage["end"]
    else:
        data = (await client.get("/api/wiki/read", params={"doc": "note.md", "view": "compact"})).json()
        passage = data
        assert data["next_start"] == data["end"]
    assert data["response_budget_exhausted"]
    assert seen == [(passage["start"], passage["end"])]
    assert passage["content"] == text[passage["start"]:passage["end"]]
    # An independently enumerated mapping: CRLF completes after LF; lone CR/LF completes immediately.
    ends = [8] + [8 + index * 7 + offset for index in range(400) for offset in (3, 5, 7)]
    assert passage["line_end"] == 1 + sum(end <= passage["end"] for end in ends)


def test_existing_cr_index_is_rebuilt_without_revising_material(tmp_path):
    import uuid

    from server.wiki.store import WikiStore

    root, state = tmp_path / "vault", tmp_path / "state"
    root.mkdir()
    text = b"# Source\rintro\r## Claim\rquartz evidence\r"
    (root / "note.md").write_bytes(text)
    vault_id = str(uuid.uuid4())
    original = WikiStore(root, state, vault_id, create=True)
    document = original.read("note.md")
    with original.connect() as db:
        db.execute("UPDATE search_documents SET version='sections-v9'")
        db.execute("UPDATE search_sections SET line_start=1,line_end=1")
    original.close()
    reopened = WikiStore(root, state, vault_id)
    try:
        found = reopened.search("quartz")["items"][0]
        assert (found["section"]["line_start"], found["section"]["line_end"]) == (3, 5)
        assert found["id"] == document["id"] and found["revision"] == document["revision"]
        assert (root / "note.md").read_bytes() == text
    finally:
        reopened.close()
