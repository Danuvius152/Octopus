"""Appending a relation preserves native evidence; omission differs from clear."""

from tests.test_wiki_maintenance_state import client as client
from tests.test_wiki_maintenance_state import prepare


async def test_adding_relation_retains_native_source_without_renewing_its_proof(client, tmp_path):
    root, _, jobs = await prepare(client, tmp_path, {
        "Library/a.md": "# A\nOriginal support.\n", "Library/b.md": "# B\nAnother support.\n",
    })
    a, b = jobs.store.read("Library/a.md"), jobs.store.read("Library/b.md")
    original = ("---\nsources:\n  - resource: a.md\n    id: native-a\n    revision: "
                + a["revision"] + "\n    location: L1-L2\n---\n# Consumer\nRetain this claim.\n")
    (root / "Library/consumer.md").write_text(original)
    document = jobs.store.read("Library/consumer.md")
    added = await client.post("/api/wiki/organize", headers=client.headers, json={
        "path": document["path"], "expected_revision": document["revision"], "reason": "add relation B", "reuse_existing_summary": True,
        "relations": [{"doc": b["id"], "revision": b["revision"], "location": "L1-L2", "explanation": "Related support"}],
    })
    assert added.status_code == 200, added.text
    read = await client.get("/api/wiki/read", headers=client.headers, params={"doc": document["id"]})
    assert read.status_code == 200, read.text
    refs = {ref["doc"]: ref for ref in read.json()["metadata"]["source_refs"]}
    assert set(refs) == {a["id"], b["id"]}
    assert refs[a["id"]]["revision"] == a["revision"]

    (root / "Library/a.md").write_text("# A\nChanged support.\n")
    current = jobs.store.read(document["id"])
    assert current["stale"]
    assert next(ref for ref in current["metadata"]["source_refs"] if ref["doc"] == a["id"])["status"] == "changed"
    omitted = await client.post("/api/wiki/organize", headers=client.headers, json={
        "path": current["path"], "expected_revision": current["revision"], "reason": "update tags only", "tags": ["kept"], "reuse_existing_summary": True,
    })
    assert omitted.status_code == 200, omitted.text
    current = jobs.store.read(document["id"])
    refs = {ref["doc"]: ref for ref in current["metadata"]["source_refs"]}
    assert set(refs) == {a["id"], b["id"]}
    assert refs[a["id"]]["revision"] == a["revision"] and refs[a["id"]]["status"] == "changed"
    cleared = await client.post("/api/wiki/organize", headers=client.headers, json={
        "path": current["path"], "expected_revision": current["revision"], "reason": "explicitly clear sources", "sources": [], "reuse_existing_summary": True,
    })
    assert cleared.status_code == 200, cleared.text
    current = jobs.store.read(document["id"])
    assert current["metadata"]["sources"] == [] and current["metadata"]["source_refs"] == []
    text = (root / "Library/consumer.md").read_text()
    assert text.startswith(original[:original.index("Retain this claim.")])
    assert text.endswith("Retain this claim.\n")
