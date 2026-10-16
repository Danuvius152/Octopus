"""Current document identities and human prose remain protected at write time."""

from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from server.mcp_identity import mint
from server.wiki.curation import WikiCuration
from server.wiki.history import WikiHistory
from server.wiki.jobs import WikiJobs
from server.wiki.registry import registry
from server.wiki.tasks import tasks
from tests import test_wiki_curation as wiki_tests
from tests.test_wiki_jobs import sapphire_delete

HEADERS = wiki_tests.HEADERS
client = wiki_tests.client
notes = wiki_tests.notes
read = wiki_tests.read


@contextmanager
def active_worker(config, paths):
    jobs = WikiJobs(None)
    job = jobs.maintain("write-gate", "", "organize", paths, None, [], "human")
    session = SimpleNamespace(id="write-gate-worker", user_id=None, agent_id=config["curator_agent_id"])
    jobs.update(job["id"], status="running", session_id=session.id)
    assigned = [*paths, *(path + ".md" for path in paths if not path.endswith((".md", ".markdown")))]
    tasks.assign(session.id, job["id"], assigned)
    token = tasks.open(session, "maintain")
    try:
        yield {"Authorization": f"Bearer {mint(session.id, wiki_task_id=token)}"}
    finally:
        tasks.close(token)
        tasks.unassign(session.id)


async def test_worker_publish_preserves_human_changes_to_existing_derived_page(client, notes):
    root, config = notes
    source = await read(client, "Library/source.md")
    body = {"path": "Library/concept.md", "kind": "concept", "content": "# Concept\nA derived claim.", "expected_revision": None,
            "sources": [{"doc": source["id"], "revision": source["revision"], "location": "L3-L4"}], "reason": "new concept"}
    result = await client.post("/api/wiki/publish", headers=HEADERS, json=body)
    assert result.status_code == 200, result.text
    path = root / body["path"]
    path.write_text(path.read_text() + "\n人工补充：必须保留重要条件 A。\n")
    before = path.read_bytes()
    current = await read(client, body["path"])
    with active_worker(config, [body["path"]]) as headers:
        await client.get("/api/wiki/read", params={"doc": current["id"]}, headers=headers)
        await client.get("/api/wiki/read", params={"doc": source["id"]}, headers=headers)
        body.update(content="# Replacement\nNo conditions.", expected_revision=current["revision"])
        rejected = await client.post("/api/wiki/publish", headers=headers, json=body)
        assert rejected.status_code == 403 and "creates new pages" in rejected.text
    assert path.read_bytes() == before


@pytest.mark.parametrize("operation", ["organize", "restructure"])
@pytest.mark.parametrize("replace_after_read_gate", [False, True])
async def test_assignment_rejects_same_hash_replacement_before_actual_write(client, notes, monkeypatch, operation, replace_after_read_gate):
    root, config = notes
    store = registry.store(None)
    original = await read(client, "Library/note.md")
    raw = (root / original["path"]).read_bytes()
    history = WikiHistory(store)

    def replace():
        sapphire_delete(history, original["path"])
        history.write(original["path"], raw, None, "human", "new identity")
        assert store.read(original["path"])["id"] != original["id"]

    with active_worker(config, [original["path"]]) as headers:
        await client.get("/api/wiki/read", params={"doc": original["id"]}, headers=headers)
        if replace_after_read_gate:
            original_gate = tasks.require_read
            changed = False

            def change_after_initial_gate(*args):
                nonlocal changed
                original_gate(*args)
                if not changed:
                    changed = True
                    replace()

            monkeypatch.setattr(tasks, "require_read", change_after_initial_gate)
        else:
            replace()
        body = {"path": original["path"], "expected_revision": original["revision"], "reason": "preserve scope"}
        if operation == "organize":
            body["key_note"] = "A proposed summary."
        else:
            body.update(order=[part["id"] for part in WikiCuration(store).blocks(original["path"])["blocks"]], additions=[])
        rejected = await client.post(f"/api/wiki/{operation}", headers=headers, json=body)
        assert rejected.status_code == 403 and "identity" in rejected.text, rejected.text
    assert (root / original["path"]).read_bytes() == raw


async def test_existing_paired_reading_page_cannot_be_replaced_after_assignment(client, notes):
    root, config = notes
    store = registry.store(None)
    history = WikiHistory(store)
    history.write("Library/source.txt", b"A full original source.", None, "human", "create")
    paired_path = "Library/source.txt.md"
    created = history.write(paired_path, b"# Existing legacy reading page\nPreserve this page.\n", None, "human", "legacy page")
    raw = store.path(paired_path).read_bytes()
    with active_worker(config, [paired_path]) as headers:
        await client.get("/api/wiki/read", params={"doc": "Library/source.txt"}, headers=headers)
        await client.get("/api/wiki/read", params={"doc": paired_path}, headers=headers)
        sapphire_delete(history, paired_path)
        history.write(paired_path, raw, None, "human", "replacement reading page")
        rejected = await client.post("/api/wiki/organize", headers=headers, json={"path": paired_path, "expected_revision": created["revision"], "key_note": "New summary.", "reason": "scope"})
        assert rejected.status_code == 403 and "identity" in rejected.text, rejected.text
    assert (root / paired_path).read_bytes() == raw


async def test_relation_read_coverage_cannot_transfer_to_same_hash_new_identity(client, notes):
    root, config = notes
    source = await read(client, "Library/source.md")
    target = await read(client, "Library/note.md")
    history = WikiHistory(registry.store(None))
    before = (root / target["path"]).read_bytes()
    source_raw = (root / source["path"]).read_bytes()
    with active_worker(config, [target["path"]]) as headers:
        await client.get("/api/wiki/read", params={"doc": target["id"]}, headers=headers)
        await client.get("/api/wiki/read", params={"doc": source["id"]}, headers=headers)
        sapphire_delete(history, source["path"])
        history.write(source["path"], source_raw, None, "human", "replace relation")
        replacement = await read(client, source["path"])
        assert replacement["id"] != source["id"] and replacement["revision"] == source["revision"]
        body = {"path": target["path"], "expected_revision": target["revision"], "key_note": "Original claims.", "reason": "relation",
                "relations": [{"doc": replacement["id"], "revision": replacement["revision"], "location": "L3-L4", "explanation": "The same specific claim."}]}
        rejected = await client.post("/api/wiki/organize", headers=headers, json=body)
        assert rejected.status_code == 422 and "cited current passage" in rejected.text, rejected.text
    assert (root / target["path"]).read_bytes() == before


@pytest.mark.parametrize("text", [
    "# Source\n\nOnly under condition A.\nConclusion follows. ^claim\n\nOther material.\n",
    "# Source\n\n- Only under condition A.\n\n  Conclusion follows within this list item.\n\n^claim\n",
    "# Source\n\n- Only under condition A.\n\n  Conclusion follows within this list item.\nThis is a lazy continuation.\n\n^claim\n",
])
async def test_block_relation_reads_conditions_in_earlier_lines(client, notes, text):
    root, config = notes
    (root / "Library/source.md").write_text(text)
    source = await read(client, "Library/source.md")
    target = await read(client, "Library/note.md")
    with active_worker(config, [target["path"]]) as headers:
        await client.get("/api/wiki/read", params={"doc": target["id"]}, headers=headers)
        last_line = text.index("Conclusion follows")
        await client.get("/api/wiki/read", params={"doc": source["id"], "start": last_line, "length": len(text) - last_line}, headers=headers)
        body = {"path": target["path"], "expected_revision": target["revision"], "key_note": "Original claims.", "reason": "condition-aware relation",
                "relations": [{"doc": source["id"], "revision": source["revision"], "location": "^claim", "explanation": "The claim depends on condition A."}]}
        rejected = await client.post("/api/wiki/organize", headers=headers, json=body)
        assert rejected.status_code == 422 and "cited current passage" in rejected.text, rejected.text
        await client.get("/api/wiki/read", params={"doc": source["id"]}, headers=headers)
        accepted = await client.post("/api/wiki/organize", headers=headers, json=body)
        assert accepted.status_code == 200, accepted.text
