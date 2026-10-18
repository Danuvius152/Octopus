"""Omitted legacy fields retain owned bytes and recorded evidence identities."""

import json
import re
from types import SimpleNamespace

import pytest

from server.mcp_identity import mint
from server.wiki.history import WikiHistory
from server.wiki.jobs import WikiJobs
from server.wiki.registry import registry
from server.wiki.sections import original_text
from server.wiki.tasks import tasks
from tests import test_wiki_api as wiki_api

client = wiki_api.client
HEADERS = wiki_api.HEADERS
NOTE = "Library/note.md"
SOURCE = "Library/source.md"
NATIVE = "\ufeff---\r\ntags: [native-tag]\r\naliases: [Native alias]\r\n---\r\n# Note\r\nUser sentence with #native-body.\r\n"


@pytest.fixture
async def notes(client, tmp_path):
    root = tmp_path / "vault"
    (root / "Library").mkdir(parents=True)
    (root / NOTE).write_bytes(NATIVE.encode())
    (root / SOURCE).write_text("# Source\n\n## Evidence\nOnly under condition A.\n")
    config = await wiki_api.configure(client, root)
    return root, registry.store(None), config


async def read(client, path=NOTE, *, headers=HEADERS, view="full"):
    response = await client.get(
        "/api/wiki/read", params={"doc": path, "view": view}, headers=headers
    )
    assert response.status_code == 200, response.text
    return response.json()


def managed(store):
    with store.connect() as db:
        row = db.execute("SELECT * FROM managed_notes WHERE path=?", (NOTE,)).fetchone()
    return ({}, []) if row is None else (
        json.loads(row["metadata"]), json.loads(row["regions"])
    )


def provenance(regions):
    return [json.loads(re.search(r"<!-- octopus-section (.*?) -->", part["raw"])[1])
            for part in regions if part["role"] == "key_note"][0]


def citation(document):
    return {"doc": document["id"], "revision": document["revision"], "location": "L3-L4"}


async def organize(client, *, headers=HEADERS, **fields):
    current = await read(client, headers=headers)
    return await client.post("/api/wiki/organize", headers=headers, json={
        "path": NOTE, "expected_revision": current["revision"], "reason": "metadata regression",
        **fields,
    })


async def initially_organize(client, *, modern=False):
    source = await read(client, SOURCE)
    response = await organize(
        client, key_note="Initial evidence with its condition.",
        tags=["native-tag", "managed-tag"] if modern else ["managed-tag"],
        sources=[citation(source)], **({"frontmatter_patch": {}} if modern else {}),
    )
    assert response.status_code == 200, response.text
    return source


@pytest.mark.parametrize("modern", [False, True], ids=["legacy", "patch"])
@pytest.mark.parametrize("mode", ["omitted", "null", "empty", "replace"])
async def test_organize_metadata_three_states(client, notes, modern, mode):
    root, store, _ = notes
    source = await initially_organize(client, modern=modern)
    old_metadata, old_regions = managed(store)
    fields = {"frontmatter_patch": {}} if modern else {}
    if mode == "null":
        fields |= {"tags": None, "sources": None}
    elif mode == "empty":
        fields |= {"tags": [], "sources": []}
    elif mode == "replace":
        fields |= {"tags": ["replacement"], "sources": [citation(source) | {"location": "L4"}]}
    response = await organize(client, **fields)
    assert response.status_code == 200, response.text
    full = await read(client)
    assert "User sentence with #native-body." in (root / NOTE).read_text(encoding="utf-8-sig")
    if mode in {"omitted", "null"}:
        assert full["metadata"]["tags"] == ["native-tag", "managed-tag"]
        assert full["metadata"]["source_refs"][0]["location"] == "L3-L4"
        if not modern:
            metadata, regions = managed(store)
            assert metadata["sources"] == old_metadata["sources"]
            assert regions == old_regions
    elif mode == "empty":
        assert full["metadata"]["tags"] == ([] if modern else ["native-tag"])
        assert full["metadata"]["sources"] == []
        assert full["metadata"]["source_refs"] == []
    else:
        assert full["metadata"]["tags"] == (["replacement"] if modern else ["native-tag", "replacement"])
        assert full["metadata"]["source_refs"][0]["location"] == "L4"
    if not modern:
        _, regions = managed(store)
        assert original_text((root / NOTE).read_bytes().decode(), regions) == NATIVE


@pytest.mark.parametrize("clear", ["tags", "sources"])
async def test_legacy_fields_are_omitted_independently(client, notes, clear):
    _, store, _ = notes
    await initially_organize(client)
    before, regions = managed(store)
    result = await organize(client, **{clear: []})
    assert result.status_code == 200, result.text
    after, current_regions = managed(store)
    preserved = "sources" if clear == "tags" else "tags"
    assert after[clear] == []
    assert after[preserved] == before[preserved]
    assert next(part for part in current_regions if part["role"] == "key_note") == next(
        part for part in regions if part["role"] == "key_note"
    )
    if clear == "sources":
        assert next(part for part in current_regions if part["role"] == "tags") == next(
            part for part in regions if part["role"] == "tags"
        )


def change_source(root, store, state):
    if state == "changed":
        (root / SOURCE).write_text("# Source\n\n## Evidence\nA changed claim with condition B.\n")
    else:
        old = store.read(SOURCE)
        WikiHistory(store).write(SOURCE, None, old["revision"], "human", "remove source")
        if state == "replacement":
            WikiHistory(store).write(SOURCE, b"# Source\n\n## Evidence\nReplacement claims.\n", None, "human", "new source")


@pytest.mark.parametrize("state", ["changed", "missing", "replacement"])
async def test_omitted_sources_retain_stale_dependency_and_provenance(client, notes, state):
    root, store, _ = notes
    source = await initially_organize(client)
    old_metadata, old_regions = managed(store)
    old_provenance = provenance(old_regions)
    change_source(root, store, state)
    result = await organize(client, tags=["updated-tag"])
    assert result.status_code == 200, result.text
    metadata, regions = managed(store)
    assert metadata["sources"] == old_metadata["sources"]
    assert provenance(regions) == old_provenance
    assert original_text((root / NOTE).read_bytes().decode(), regions) == NATIVE
    full, compact = await read(client), await read(client, view="compact")
    status = "changed" if state == "changed" else "missing"
    assert full["stale"] is True and compact["stale"] is True
    assert compact["warnings"]["sources"] == {status: 1}
    ref = full["metadata"]["source_refs"][0]
    assert ref["doc"] == source["id"]
    assert ref["revision"] == source["revision"] and ref["location"] == "L3-L4"
    assert ref["status"] == status
    with store.connect() as db:
        edge = db.execute("SELECT * FROM wiki_edges WHERE relation='derived_from'").fetchone()
    assert edge["target_doc"] == source["id"] and edge["recorded_revision"] == source["revision"]
    if state == "replacement":
        assert (await read(client, SOURCE))["id"] != ref["doc"]


@pytest.mark.parametrize("state", ["changed", "missing", "replacement"])
async def test_new_key_note_does_not_recertify_retained_stale_sources(client, notes, state):
    root, store, _ = notes
    original_source = await initially_organize(client)
    change_source(root, store, state)
    before = (root / NOTE).read_bytes()
    rejected = await organize(client, key_note="New assertion from the retained evidence.")
    assert rejected.status_code == 422 and "explicit current sources" in rejected.text
    assert (root / NOTE).read_bytes() == before
    if state != "missing":
        current = await read(client, SOURCE)
        accepted = await organize(client, key_note="Explicitly checked assertion.", sources=[citation(current)])
        assert accepted.status_code == 200, accepted.text
        metadata, regions = managed(store)
        assert metadata["sources"][0]["revision"] == current["revision"]
        assert metadata["sources"][0]["doc"] == current["id"]
        assert provenance(regions)["sources"] == metadata["sources"]
        assert (await read(client))["stale"] is False
        if state == "replacement":
            assert current["id"] != original_source["id"]


async def test_new_key_note_keeps_current_retained_citation_version(client, notes):
    _, store, _ = notes
    source = await initially_organize(client)
    old_metadata, _ = managed(store)
    refreshed = await organize(client, key_note="A different phrasing with the same current source.")
    assert refreshed.status_code == 200, refreshed.text
    metadata, regions = managed(store)
    assert metadata["sources"] == old_metadata["sources"]
    assert provenance(regions)["sources"][0]["revision"] == source["revision"]


async def test_preserved_owned_key_note_does_not_revalidate_old_missing_links(client, notes):
    root, store, _ = notes
    source = await read(client, SOURCE)
    first = await organize(client, key_note="The cited claim is in [[Library/source#Evidence]].", sources=[citation(source)])
    assert first.status_code == 200, first.text
    _, old_regions = managed(store)
    change_source(root, store, "missing")
    preserved = await organize(client, tags=["metadata-only"])
    assert preserved.status_code == 200, preserved.text
    _, regions = managed(store)
    assert next(part for part in regions if part["role"] == "key_note") == next(
        part for part in old_regions if part["role"] == "key_note"
    )
    assert (await read(client, view="compact"))["warnings"]["sources"] == {"missing": 1}
    rejected = await organize(client, key_note="A new [[Library/missing]] link.", sources=[])
    assert rejected.status_code == 422 and "resolve" in rejected.text


async def test_native_sources_are_not_masked_on_omission_or_revived_after_explicit_clear(client, notes):
    root, store, _ = notes
    source = await read(client, SOURCE)
    native = NATIVE.replace("---\r\n# Note", (
        f"sources:\r\n- resource: source.md\r\n  revision: {source['revision']}\r\n"
        "  location: L3-L4\r\n---\r\n# Note"
    ))
    (root / NOTE).write_bytes(native.encode())
    first = await organize(client, key_note="The original metadata remains the source authority.")
    assert first.status_code == 200, first.text
    metadata, regions = managed(store)
    assert "sources" not in metadata
    assert original_text((root / NOTE).read_bytes().decode(), regions) == native
    assert (await read(client))["metadata"]["source_refs"][0]["doc"] == source["id"]
    omitted = await organize(client)
    assert omitted.status_code == 200, omitted.text
    assert "sources" not in managed(store)[0]
    cleared = await organize(client, sources=[])
    assert cleared.status_code == 200, cleared.text
    assert (await read(client))["metadata"]["source_refs"] == []
    omitted_again = await organize(client)
    assert omitted_again.status_code == 200, omitted_again.text
    assert managed(store)[0]["sources"] == []
    assert (await read(client))["metadata"]["source_refs"] == []
    assert original_text((root / NOTE).read_bytes().decode(), managed(store)[1]) == native


async def test_native_stale_sources_also_reject_new_key_note_without_explicit_sources(client, notes):
    root, store, _ = notes
    source = await read(client, SOURCE)
    native = f"---\nsources:\n- resource: source.md\n  revision: {source['revision']}\n  location: L3-L4\n---\n# Note\nOriginal body.\n"
    (root / NOTE).write_text(native)
    change_source(root, store, "changed")
    rejected = await organize(client, key_note="An uncertified new assertion.")
    assert rejected.status_code == 422
    assert (root / NOTE).read_bytes() == native.encode()
    assert managed(store) == ({}, [])


async def test_legacy_paired_page_keeps_original_evidence_version_on_omission(client, notes):
    root, _, _ = notes
    html = root / "Library/page.html"
    html.write_text("<html><body><h1>Human view</h1><p>First claim.</p></body></html>")
    source = await read(client, "Library/page.html")
    paired_path = source["path"] + ".md"
    # Represent a preserved legacy reading page, then maintain that Markdown
    # explicitly; the HTML original remains a read-only versioned source.
    (root / paired_path).write_text("# Human view\n\n[原文](page.html)\n\nFirst claim.\n")
    paired_original = await read(client, paired_path)
    first = await client.post("/api/wiki/organize", headers=HEADERS, json={
        "path": paired_path, "expected_revision": paired_original["revision"],
        "key_note": "Reading points from the human view.", "reason": "paired page",
        "sources": [{"doc": source["id"], "revision": source["revision"], "location": "L1"}],
    })
    assert first.status_code == 200, first.text
    before = await read(client, paired_path)
    html.write_text("<html><body><h1>Human view</h1><p>Changed claim.</p></body></html>")
    current = await read(client, source["path"])
    assert current["revision"] != source["revision"]
    refreshed = await client.post("/api/wiki/organize", headers=HEADERS, json={
        "path": paired_path, "expected_revision": before["revision"],
        "tags": ["metadata-only"], "reason": "paired refresh without new evidence",
    })
    assert refreshed.status_code == 200, refreshed.text
    paired = await read(client, paired_path)
    assert paired["metadata"]["sources"] == before["metadata"]["sources"]
    assert len(paired["metadata"]["source_refs"]) == 1
    assert paired["metadata"]["source_refs"][0]["revision"] == source["revision"]
    assert paired["metadata"]["source_refs"][0]["status"] == "changed"
    assert html.read_text().endswith("<p>Changed claim.</p></body></html>")


@pytest.mark.parametrize("trusted", [False, True], ids=["unowned-marker", "edited-owned-region"])
async def test_omission_cannot_claim_or_overwrite_human_changed_regions(client, notes, trusted):
    root, store, _ = notes
    if trusted:
        await initially_organize(client)
        raw = (root / NOTE).read_bytes().replace(b"#managed-tag", b"#human-edited")
    else:
        raw = NATIVE.encode() + b'\n<!-- octopus-section {"role":"tags","tags":["forged"]} -->\n#forged\n<!-- /octopus-section -->\n'
    (root / NOTE).write_bytes(raw)
    result = await organize(client, key_note=None if trusted else "New points.")
    assert result.status_code == 409, result.text
    assert (root / NOTE).read_bytes() == raw
    if not trusted:
        assert managed(store) == ({}, [])


async def test_worker_preserves_old_evidence_without_new_read_but_new_refs_keep_guards(client, notes):
    root, store, config = notes
    old_source = await initially_organize(client)
    change_source(root, store, "changed")
    jobs = WikiJobs(None)
    job = jobs.maintain("metadata regression", "", "organize", [NOTE], None, [], "human")
    session = SimpleNamespace(id="legacy-metadata-worker", user_id=None, agent_id=config["curator_agent_id"])
    jobs.update(job["id"], status="running", session_id=session.id)
    tasks.assign(session.id, job["id"], job["items"])
    token = tasks.open(session, "maintain")
    headers = {"Authorization": f"Bearer {mint(session.id, wiki_task_id=token)}"}
    try:
        preserved = await organize(client, headers=headers, tags=["worker-refresh"])
        assert preserved.status_code == 200, preserved.text
        assert managed(store)[0]["sources"][0]["revision"] == old_source["revision"]
        current_source = await read(client, SOURCE)
        unread = await organize(client, headers=headers, sources=[citation(current_source)])
        assert unread.status_code == 422 and "cited current passage" in unread.text
        await read(client, SOURCE, headers=headers)
        invalid = await organize(client, headers=headers, sources=[citation(current_source) | {"location": "L9999"}])
        assert invalid.status_code == 422 and "existing line" in invalid.text
        outdated = await organize(client, headers=headers, sources=[citation(old_source)])
        assert outdated.status_code == 409
        written = await organize(client, headers=headers, key_note="The worker read the current evidence.", sources=[citation(current_source)])
        assert written.status_code == 200, written.text
        assert managed(store)[0]["sources"][0]["revision"] == current_source["revision"]
    finally:
        tasks.close(token)
        tasks.unassign(session.id)
