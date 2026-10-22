"""A direct read returns body and dependency evidence from one version."""

from pathlib import Path

import pytest

from server.wiki.store import WikiError, revision
from tests.test_wiki_source_bindings import history as history
from tests.test_wiki_source_bindings import source


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
    document, raw = history.store.read_snapshot("consumer.md", start=start, length=length)
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
    document, raw = history.store.read_snapshot("consumer.md")
    assert raw == updated and document["revision"] == revision(updated)
    assert document["metadata"]["source_refs"][0]["doc"] == b["id"]
