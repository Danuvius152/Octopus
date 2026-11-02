"""A direct read returns body and dependency evidence from one version."""

from pathlib import Path

import pytest

from server.wiki.store import WikiError, revision
from tests.test_wiki_source_bindings import history as history
from tests.test_wiki_source_bindings import source


@pytest.mark.parametrize(("name", "original", "extracted", "position", "line"), [
    ("note.md", "\ufeff# H\r\n甲😀\nTail", "# H\r\n甲😀\nTail", 8, 3),
    ("plain.txt", "# H\r\n甲😀\nTail", "# H\r\n甲😀\nTail", 5, 2),
    ("source.html", "<p>甲😀</p>", "甲😀", 1, 1),
])
def test_full_private_snapshot_keeps_one_version_without_widening_public_delivery(history, name, original, extracted, position, line):
    history.store.path(name).write_bytes(original.encode())
    value = history.store.read_snapshot(name, length=1)
    assert value.raw == original.encode() and value.original_text == original
    assert value.text == extracted and value.document["revision"] == revision(value.raw)
    assert value.lines.line(position) == line
    assert value.document["content"] == extracted[:1]
    with pytest.raises(TypeError):
        value.document["revision"] = "another revision"
    public = history.store.read(name, length=1, expected_revision=value.document["revision"])
    assert public["content"] == extracted[:1]
    assert "text" not in public and "raw" not in public


@pytest.mark.parametrize("start,length", [(0, 16000), (25, 20)])
def test_first_read_after_external_edit_does_not_mix_new_body_with_old_sources(history, monkeypatch, start, length):
    history.write("a.md", b"# A\nFirst support.\n", None, "human", "create")
    history.write("b.md", b"# B\nSecond support.\n", None, "human", "create")
    a, b = history.store.read("a.md"), history.store.read("b.md")
    history.write("consumer.md", source(recorded=a["revision"]), None, "human", "create")
    updated = source("b.md", recorded=b["revision"])
    target = history.store.path("consumer.md")
    read_bytes = Path.read_bytes
    edited = []

    def edit_at_body_read(path):
        if path == target and not edited:
            target.write_bytes(updated)
            edited.append(True)
        return read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", edit_at_body_read)
    snapshot = history.store.read_snapshot("consumer.md", start=start, length=length)
    document, raw = snapshot.document, snapshot.raw
    assert edited == [True]
    assert raw == updated and document["content"] == updated.decode()[start:start + length]
    assert document["revision"] == revision(raw)
    assert document["metadata"]["sources"][0]["resource"] == "b.md"
    refs = document["metadata"]["source_refs"]
    assert len(refs) == 1 and refs[0]["doc"] == b["id"]
    assert refs[0]["revision"] == b["revision"] and not document["stale"]


def test_revision_conflict_does_not_poison_the_next_read_or_its_source_evidence(history):
    history.write("a.md", b"# A\nFirst support.\n", None, "human", "create")
    history.write("b.md", b"# B\nSecond support.\n", None, "human", "create")
    a, b = history.store.read("a.md"), history.store.read("b.md")
    old = source(recorded=a["revision"])
    history.write("consumer.md", old, None, "human", "create")
    updated = source("b.md", recorded=b["revision"])
    history.store.path("consumer.md").write_bytes(updated)
    with pytest.raises(WikiError) as conflict:
        history.store.read("consumer.md", expected_revision=revision(old))
    assert conflict.value.status == 409
    snapshot = history.store.read_snapshot("consumer.md")
    document, raw = snapshot.document, snapshot.raw
    assert raw == updated and document["revision"] == revision(updated)
    assert document["metadata"]["source_refs"][0]["doc"] == b["id"]
