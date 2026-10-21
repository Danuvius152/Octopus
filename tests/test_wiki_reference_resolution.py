"""URI boundaries and indexed link validation agree on the actual target."""

import contextlib
import json
import uuid
from urllib.parse import quote

import pytest

from server.wiki.curation import WikiCuration
from server.wiki.history import WikiHistory
from server.wiki.store import WikiStore


@pytest.fixture
def store(tmp_path):
    root = tmp_path / "vault"
    root.mkdir()
    instance = WikiStore(root, tmp_path / "state", uuid.uuid4().hex, create=True)
    try:
        yield instance
    finally:
        instance.close()


@pytest.mark.parametrize("filename", ["a#b.md", "a%23b.md", "a?b.md", "甲 😀.md"])
def test_encoded_link_selects_exact_filename_in_validation_and_graph(store, filename):
    target = store.root / filename
    target.write_text("# Target\n\n## Condition %23\nEvidence.\n")
    href = quote(filename, safe="") + "#" + quote("Condition %23", safe="")
    content = f"# Source\n\n[proof]({href})\n"
    store.refresh()
    expected = store.read(filename)["id"]
    WikiCuration(store).validate_links("source.md", content)
    WikiHistory(store).write("source.md", content.encode(), None, "human", "create")
    edge = store.graph("source.md")["edges"][0]
    assert edge["to"] == expected and edge["status"] == "resolved"
    assert edge["location"] == "Condition %23"


def test_source_doc_wins_over_literal_punctuation_display_path(store):
    history = WikiHistory(store)
    history.write("Library/a#b.md", b"# A\nEvidence.\n", None, "human", "create")
    checked = store.read("Library/a#b.md")
    result = WikiCuration(store).publish(
        "Library/consumer.md", "# Consumer\nOriginal argument.\n", "concept",
        [{"doc": checked["id"], "revision": checked["revision"], "location": "L1-L2"}],
        None, "human", "publish",
    )
    consumer = store.read(result["path"])
    assert consumer["metadata"]["sources"][0]["doc"] == checked["id"]
    assert consumer["metadata"]["source_refs"][0]["doc"] == checked["id"]


def test_validation_connection_count_does_not_grow_with_unrelated_notes(store, monkeypatch):
    (store.root / "target.md").write_text("---\naliases: [A target]\n---\n# Target\nEvidence.\n")
    store.refresh()
    connections, queries = [], []
    original = store.connect

    @contextlib.contextmanager
    def measured():
        connections.append(True)
        with original() as db:
            db.set_trace_callback(queries.append)
            yield db

    monkeypatch.setattr(store, "connect", measured)
    WikiCuration(store).validate_links("source.md", "[[A target]]")
    baseline = len(connections)
    for index in range(30):
        (store.root / f"unrelated-{index}.md").write_text(f"# Unrelated {index}\nText.\n")
    store.refresh()
    connections.clear()
    queries.clear()
    WikiCuration(store).validate_links("source.md", "[[A target]]")
    assert len(connections) == baseline
    assert any("wiki_lookup_names" in query for query in queries)
    assert not any("SELECT path,title,metadata FROM documents" in query for query in queries)
    assert not any("SELECT * FROM managed_notes" in query for query in queries)


def test_candidate_alias_is_temporary_and_existing_broken_link_does_not_block_edit(store):
    history = WikiHistory(store)
    before = "# Before\n\n[[never existed]]\n\nOld paragraph.\n"
    history.write("note.md", before.encode(), None, "human", "create")
    candidate = "---\naliases: [Temporary name]\n---\n" + before.replace("Old paragraph.", "New paragraph.\n\n[[Temporary name]]")
    WikiCuration(store).validate_links("note.md", candidate, candidate=candidate)
    with store.connect() as db:
        row = db.execute("SELECT metadata FROM documents WHERE path='note.md'").fetchone()
        assert "Temporary name" not in json.loads(row[0]).get("aliases", [])
