"""New Markdown-only maintenance does not alter legacy text or recovery facts."""

import hashlib
import json
import uuid

import pytest

from server.wiki import materials
from server.wiki.contracts import NoChangeRevision
from server.wiki.curation import WikiCuration
from server.wiki.history import WikiHistory
from server.wiki.revise import revise
from server.wiki.store import WikiError
from tests import test_wiki_jobs as job_tests
from tests.test_wiki_api import HEADERS

client = job_tests.client
jobs = job_tests.jobs


@pytest.mark.parametrize("suffix", [".txt", ".html", ".HTM"])
async def test_domain_maintenance_rejects_non_markdown_without_changing_history(jobs, suffix):
    path = "Library/legacy" + suffix
    raw = b"# Original\nRetain original words and conditions.\n"
    history = WikiHistory(jobs.store)
    created = history.write(path, raw, None, "human", "legacy creation")
    curation = WikiCuration(jobs.store)
    attempts = [
        lambda: jobs.maintain("existing", "", "review", [path], None, [], "human"),
        lambda: curation.organize(path, created["revision"], "Summary", [], [], [], "human", "review"),
        lambda: curation.organize(path, created["revision"], "Summary", [], [], [], "human", "review", frontmatter_patch={"title": "Title"}),
        lambda: curation.restructure(path, created["revision"], [], [], "human", "review"),
        lambda: curation.publish(path, "# New", "source", [], None, "human", "review"),
        lambda: revise(curation, NoChangeRevision(operation="no_change", doc=path, expected_revision=created["revision"], reason="review"), "human", None),
    ]
    with jobs.store.connect() as db:
        counts = tuple(db.execute("SELECT (SELECT count(*) FROM changes),(SELECT count(*) FROM operation_receipts),(SELECT count(*) FROM jobs)").fetchone())
    for attempt in attempts:
        with pytest.raises(WikiError) as rejected:
            attempt()
        assert rejected.value.status == 422
        assert jobs.store.path(path).read_bytes() == raw
        assert not jobs.store.path(path + ".md").exists()
    with jobs.store.connect() as db:
        assert tuple(db.execute("SELECT (SELECT count(*) FROM changes),(SELECT count(*) FROM operation_receipts),(SELECT count(*) FROM jobs)").fetchone()) == counts
        retained = db.execute("SELECT before_available,after_available FROM change_retention WHERE change_id=?", (created["change_id"],)).fetchone()
    assert retained[1] == 1
    assert jobs.store.read(path)["revision"] == created["revision"]


@pytest.mark.parametrize("path", ["Library/note.md", "Library/note.MARKDOWN", "Library/assets/assigned.MD"])
async def test_qualified_markdown_remains_maintainable(jobs, path):
    WikiHistory(jobs.store).write(path, b"# Original\nPreserve the original.\n", None, "human", "create")
    result = jobs.maintain("markdown", "", "review", [path], None, [], "human")
    assert result["items"][0]["path"] == path and result["status"] == "queued"


async def test_raw_material_lookup_keeps_legacy_default_but_new_submission_reuses_markdown(jobs):
    raw = b"# Original\nSame raw material.\n"
    history = WikiHistory(jobs.store)
    history.write("Library/aaa.txt", raw, None, "human", "legacy original")
    history.write("Library/zzz.MARKDOWN", raw, None, "human", "Markdown original")
    note = jobs.store.read("Library/zzz.MARKDOWN", length=1)
    assert materials.find(jobs.store, raw)["path"] == "Library/aaa.txt"
    result = jobs.maintain("new", raw.decode(), "review", [], "Library/requested.md", [], "human")
    assert result["status"] == "completed"
    assert result["items"][0]["path"] == "Library/zzz.MARKDOWN"
    assert result["items"][0]["doc"] == note["id"]
    assert not jobs.store.path("Library/requested.md").exists()


@pytest.mark.parametrize("suffix", [".txt", ".html"])
async def test_ingest_skips_late_non_markdown_match(jobs, suffix):
    raw = "# Original\nExact material before queue delay.\n"
    job = jobs.maintain("late", raw, "review", [], "Library/requested.md", [], "human")
    path = "Library/late" + suffix
    WikiHistory(jobs.store).write(path, raw.encode(), None, "human", "late legacy match")
    created = jobs.ingest(job["id"])
    assert created["path"] == "Library/requested.md" and not created.get("reused")
    assert jobs.store.path(path).read_bytes() == jobs.store.path(created["path"]).read_bytes() == raw.encode()


async def test_pending_non_markdown_submission_is_not_an_alias_target(jobs):
    raw = "# Original\nSame material.\n"
    # Retain a pre-Markdown-contract pending fact; current admission correctly
    # rejects creating this old TXT target and must not be used as a fixture.
    old = {"id": uuid.uuid4().hex}
    payload = json.dumps({"kind": "submission", "material": raw, "scope": "old", "actor": "human", "target": "Library/old.txt"}, sort_keys=True)
    with jobs.store.connect() as db:
        db.execute("INSERT INTO jobs VALUES(?,?,?,?,?,'[]',NULL,NULL,'old-time','old-time')",
                   (old["id"], "legacy", hashlib.sha256(payload.encode()).hexdigest(), payload, "queued"))
    new = jobs.maintain("current", raw, "review", [], "Library/current.md", [], "human")
    assert new["id"] != old["id"] and new["status"] == "queued"
    assert jobs.ingest(new["id"])["path"] == "Library/current.md"
    with jobs.store.connect() as db:
        assert db.execute("SELECT payload FROM jobs WHERE id=?", (old["id"],)).fetchone()[0] == payload
        assert db.execute("SELECT count(*) FROM wiki_job_grants WHERE job_id=?", (old["id"],)).fetchone()[0] == 0


async def test_historical_key_replays_its_original_non_markdown_result(jobs):
    raw = "# Historical original\nPreserve this old receipt.\n"
    WikiHistory(jobs.store).write("Library/legacy.txt", raw.encode(), None, "human", "legacy")
    legacy = jobs.store.read("Library/legacy.txt", length=1)
    job = jobs.maintain("original-key", raw, "review", [], "Library/requested.md", [], "human")
    # A stored completion from before the Markdown-only contract is a fact,
    # independent of eligibility for a new request under a different key.
    jobs.update(job["id"], status="completed", items=[{"path": "Library/legacy.txt", "doc": legacy["id"], "revision": legacy["revision"], "status": "done", "reused": True}])
    replay = jobs.maintain("original-key", raw, "review", [], "Library/requested.md", [], "human")
    assert replay["id"] == job["id"] and replay["items"][0]["path"] == "Library/legacy.txt"
    with pytest.raises(WikiError) as conflict:
        jobs.maintain("original-key", raw + "changed", "review", [], "Library/requested.md", [], "human")
    assert conflict.value.status == 409
    fresh = jobs.maintain("new-key", raw, "review", [], "Library/new.md", [], "human")
    assert fresh["status"] == "queued" and jobs.ingest(fresh["id"])["path"] == "Library/new.md"


async def test_legacy_non_markdown_item_stops_before_a_model_turn(jobs):
    raw = b"# Original\nLegacy selected file.\n"
    WikiHistory(jobs.store).write("Library/legacy.txt", raw, None, "human", "legacy")
    doc = jobs.store.read("Library/legacy.txt", length=1)
    job = jobs.enqueue("legacy-selection", {"kind": "curation", "scope": "legacy review", "new_only": False}, [{"doc": doc["id"], "path": "Library/legacy.txt", "revision": doc["revision"], "status": "pending"}])
    curator = job_tests.Curator(jobs)
    result = await job_tests.process(jobs, job, curator)
    assert result["status"] == "completed_with_attention"
    assert result["items"][0]["status"] == "attention"
    assert "Markdown" in result["items"][0]["error"]
    assert curator.turns == curator.sessions == 0
    assert jobs.store.path("Library/legacy.txt").read_bytes() == raw


async def test_implicit_legacy_curate_only_selects_markdown(jobs):
    history = WikiHistory(jobs.store)
    for path in ("Library/note.md", "Library/other.MARKDOWN", "Library/source.txt", "Library/source.html"):
        history.write(path, b"# Original\nCurrent content.\n", None, "human", "create")
    result = jobs.curate("legacy-curate", "review")
    assert {item["path"] for item in result["items"]} == {"Library/note.md", "Library/other.MARKDOWN"}


async def test_owner_http_cannot_bypass_markdown_maintenance(jobs, client):
    path = "Library/source.txt"
    raw = b"Original words and conditions.\n"
    created = WikiHistory(jobs.store).write(path, raw, None, "human", "legacy source")
    for extra in ({}, {"frontmatter_patch": {"title": "Title"}}):
        response = await client.post("/api/wiki/organize", headers=HEADERS, json={"path": path, "expected_revision": created["revision"], "key_note": "Summary", "reason": "review", **extra})
        assert response.status_code == 422 and "Markdown" in response.text
    selected = await client.post("/api/wiki/maintain", headers=HEADERS, json={"idempotency_key": "owner-selection", "paths": [path], "scope": "review"})
    assert selected.status_code == 422 and "Markdown" in selected.text
    unchanged = await client.post("/api/wiki/revise", headers=HEADERS, json={"doc": path, "expected_revision": created["revision"], "operation": "no_change", "reason": "review"})
    assert unchanged.status_code == 422 and "Markdown" in unchanged.text
    assert jobs.store.path(path).read_bytes() == raw
    assert not jobs.store.path(path + ".md").exists()


async def test_markdown_note_can_still_cite_a_read_only_html_source(jobs):
    history = WikiHistory(jobs.store)
    history.write("Library/source.html", b"<h1>Evidence</h1><p>Only under the stated condition.</p>", None, "human", "legacy source")
    history.write("Library/note.md", b"# Note\nPreserve this original.\n", None, "human", "create")
    source = jobs.store.read("Library/source.html", length=1)
    note = jobs.store.read("Library/note.md", length=1)
    source_before = jobs.store.path(source["path"]).read_bytes()
    result = WikiCuration(jobs.store).organize(note["path"], note["revision"], "A qualified claim.", [], [], [{"doc": source["id"], "revision": source["revision"], "location": "L1"}], "human", "cite read-only legacy source")
    assert result["original_preserved"] is True
    assert jobs.store.path(source["path"]).read_bytes() == source_before
    assert jobs.store.read(note["path"])["metadata"]["sources"][0]["doc"] == source["id"]
