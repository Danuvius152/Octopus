"""Maintenance readiness follows relevant health and semantic changes."""

import pytest

from server.wiki.store import revision
from tests.test_wiki_jobs import Curator, process
from tests.test_wiki_maintenance_selections import FILES, select
from tests.test_wiki_maintenance_selections import submission as selected_submission
from tests.test_wiki_maintenance_state import client as client
from tests.test_wiki_maintenance_state import prepare, state, submission


async def test_unchanged_read_and_refresh_preserve_cursor_but_changed_content_expires_it(client, tmp_path):
    root, _, jobs = await prepare(client, tmp_path, FILES)
    first = await state(client, limit=1)
    read = await client.get("/api/wiki/read", params={"doc": "Library/a.md"}, headers=client.headers)
    assert read.status_code == 200, read.text
    jobs.store.refresh()
    second = await client.get("/api/wiki/maintenance-status", headers=client.headers,
                              params={"limit": 1, "cursor": first["next_cursor"]})
    assert second.status_code == 200, second.text
    assert second.json()["items"][0]["current"]["path"] == "Library/b.md"
    (root / "Library/b.md").write_text("# B\nChanged semantic content.\n")
    jobs.store.refresh()
    stale = await client.get("/api/wiki/maintenance-status", headers=client.headers,
                             params={"limit": 1, "cursor": first["next_cursor"]})
    assert stale.status_code == 409, stale.text


async def test_applied_inbox_for_same_bytes_preserves_the_maintenance_cursor(client, tmp_path):
    root, _, jobs = await prepare(client, tmp_path, FILES)
    first = await state(client, limit=1)
    with jobs.store.connect() as db:
        received_before = db.execute("SELECT received_seq FROM filesystem_state").fetchone()[0]
    file = root / "Library/a.md"
    original = file.read_bytes()
    file.write_bytes(original)
    jobs.store.synchronize()
    with jobs.store.connect() as db:
        received, applied = db.execute("SELECT received_seq,applied_seq FROM filesystem_state").fetchone()
    assert received > received_before and received == applied
    current = jobs.store.read("Library/a.md")
    assert current["id"] == first["items"][0]["current"]["id"]
    assert current["revision"] == revision(original)
    second = await client.get("/api/wiki/maintenance-status", headers=client.headers,
                              params={"limit": 1, "cursor": first["next_cursor"]})
    assert second.status_code == 200, second.text
    assert second.json()["items"][0]["current"]["path"] == "Library/b.md"


async def test_unrelated_bad_asset_does_not_block_checked_maintenance_or_selection(client, tmp_path):
    root, _, jobs = await prepare(client, tmp_path, FILES)
    observed = (await state(client, "Library/a.md"))["items"][0]
    frozen = await select(client, limit=1)
    (root / "Assets").mkdir()
    (root / "Assets/broken.md").write_text("---\nsources: [\n---\n# Broken asset\n")
    jobs.store.refresh()
    assert jobs.store.index_state()["index_status"] == "partial"
    current = await state(client, "Library/a.md")
    assert current["ready"] and current["items"][0]["actionable"], current
    assert current["items"][0]["review"] == observed["review"]
    selected = await select(client, limit=1)
    assert selected["items"][0]["review"] == frozen["items"][0]["review"]
    accepted = await client.post("/api/wiki/maintain", headers=client.headers,
                                 json=selected_submission(frozen, frozen["items"][0]))
    assert accepted.status_code == 200, accepted.text
    finished = await process(jobs, accepted.json(), Curator(jobs))
    assert finished["status"] == "completed"
    assert len(finished["receipts"]) == 1


@pytest.mark.parametrize("damaged", ["target", "source"])
async def test_related_bad_document_is_not_actionable_or_admitted(client, tmp_path, damaged):
    source = "# Source\nOriginal support.\n"
    target = "---\nsources:\n  - resource: source.md\n    revision: " + revision(source.encode()) + "\n---\n# A\nDerived.\n"
    root, _, jobs = await prepare(client, tmp_path, FILES | {"Library/a.md": target, "Library/source.md": source})
    observed = (await state(client, "Library/a.md"))["items"][0]
    path = "Library/a.md" if damaged == "target" else "Library/source.md"
    (root / path).write_text("---\nsources: [\n---\n# Broken relevant note\n")
    jobs.store.refresh()
    current = await state(client, "Library/a.md")
    assert current["ready"] and not current["items"][0]["actionable"], current
    rejected = await client.post("/api/wiki/maintain", headers=client.headers, json=submission(observed))
    assert rejected.status_code in {409, 503}, rejected.text
    assert jobs.list()["items"] == []


async def test_source_health_change_after_admission_prevents_worker_effects(client, tmp_path):
    source = "# Source\nOriginal support.\n"
    target = "---\nsources:\n  - resource: source.md\n    revision: " + revision(source.encode()) + "\n---\n# A\nDerived.\n"
    root, _, jobs = await prepare(client, tmp_path, FILES | {"Library/a.md": target, "Library/source.md": source})
    observed = (await state(client, "Library/a.md"))["items"][0]
    accepted = await client.post("/api/wiki/maintain", headers=client.headers, json=submission(observed))
    assert accepted.status_code == 200, accepted.text
    (root / "Library/source.md").write_text("---\nsources: [\n---\n# Broken source\n")
    jobs.store.refresh()
    original = (root / "Library/a.md").read_bytes()
    finished = await process(jobs, accepted.json(), Curator(jobs))
    assert finished["status"] == "completed_with_attention"
    assert not finished["receipts"]
    assert (root / "Library/a.md").read_bytes() == original
