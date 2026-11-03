"""Untrusted frontmatter cannot take down unrelated Wiki reads or writes."""

import json
import uuid

import pytest

from server.wiki import bounded_yaml, format
from server.wiki import store as document_storage
from server.wiki.store import WikiError, WikiStore


def alias_expansion():
    lines = ["a0: &a0 [word, word]"]
    lines += [f"a{n}: &a{n} [*a{n - 1}, *a{n - 1}]" for n in range(1, 24)]
    return "\n".join(lines) + "\nx_wiki: {expansion: *a23}\n"


BAD = [
    "x_wiki: " + "[" * 1200 + "end" + "]" * 1200,
    "x_wiki: &cycle {self: *cycle}",
    alias_expansion(),
    "description: " + "x" * bounded_yaml.MAX_BYTES,
    "description: " + "9" * 5000,
]


@pytest.mark.parametrize("source", BAD, ids=["depth", "cycle", "expansion", "bytes", "scalar"])
def test_format_and_patch_reject_unbounded_frontmatter_without_recursing(source):
    text = "---\n" + source + "\n---\n# Original\nKeep these bytes.\n"
    parsed = format.parse(text)
    assert parsed.error and parsed.fields == {}
    with pytest.raises(WikiError) as error:
        format.patch(text, fields={"title": "Changed"})
    assert error.value.status == 422


@pytest.mark.parametrize("source", BAD, ids=["depth", "cycle", "expansion", "bytes", "scalar"])
def test_invalid_note_is_local_to_its_index_row(tmp_path, source):
    root = tmp_path / "vault"
    root.mkdir()
    bad = ("---\n" + source + "\n---\n# Original\nBad metadata, intact body.\n").encode()
    (root / "bad.md").write_bytes(bad)
    (root / "good.md").write_text("# Healthy\nUnrelated evidence remains readable.\n")
    store = WikiStore(root, tmp_path / "state", str(uuid.uuid4()), create=True)
    try:
        store.refresh()
        with store.connect() as db:
            row = db.execute("SELECT id,metadata FROM documents WHERE path='bad.md'").fetchone()
            metadata = json.loads(row["metadata"])
        if len(bad) > document_storage.MAX_DOCUMENT_BYTES:
            assert metadata["resource_error"] == "document_too_large"
            with pytest.raises(WikiError) as error:
                store.read("bad.md")
            assert error.value.status == 413
        else:
            assert metadata["frontmatter_error"]
            assert metadata["uid"] == "urn:uuid:" + str(uuid.UUID(row["id"]))
            assert "portable_uid" not in metadata
        assert store.read("good.md")["path"] == "good.md"
        assert (root / "bad.md").read_bytes() == bad
        errors = store.index_state()["index_errors"]
        assert any(error["path"] == "bad.md" for error in errors)
        # Fixing only this original is sufficient; no database reset is needed.
        (root / "bad.md").write_text("---\ntags: [recovered]\n---\n# Recovered\n")
        store.refresh()
        with store.connect() as db:
            metadata = json.loads(db.execute("SELECT metadata FROM documents WHERE path='bad.md'").fetchone()[0])
        assert "frontmatter_error" not in metadata
        assert metadata["tags"] == ["recovered"]
    finally:
        store.close()


def test_retained_metadata_requires_the_same_document_identity(tmp_path):
    root = tmp_path / "vault"
    root.mkdir()
    uid = "urn:uuid:" + str(uuid.uuid4())
    (root / "note.md").write_text(f"---\nuid: {uid}\ntitle: Stable title\ntags: [stable]\n---\n# Original\n")
    store = WikiStore(root, tmp_path / "state", str(uuid.uuid4()), create=True)
    try:
        original = store.read("note.md")
        (root / "note.md").write_text("---\nx_wiki: &x {self: *x}\n---\n# Edited\n")
        store.refresh()
        retained = store.read("note.md")
        assert retained["metadata"]["uid"] == uid
        assert retained["metadata"]["tags"] == ["stable"]
        assert retained["metadata"]["frontmatter_retained_from"] == {
            "doc": original["id"], "revision": original["revision"],
        }
        (root / "note.md").unlink()
        store.refresh()
        (root / "note.md").write_text("---\nx_wiki: &x {self: *x}\n---\n# Replacement\n")
        store.refresh()
        replacement = store.read("note.md")
        assert replacement["id"] != original["id"]
        assert replacement["metadata"]["uid"] != uid
        assert "portable_uid" not in replacement["metadata"]
        assert "frontmatter_retained_from" not in replacement["metadata"]
    finally:
        store.close()


def test_normal_anchors_and_dates_still_preserve_source_spelling():
    text = "---\ndate: 2026-10-17\ntags: &tags [one, two]\naliases: *tags\n# keep comment\n---\n# Original\n"
    parsed = format.parse(text)
    assert parsed.error is None
    assert parsed.fields["date"] == "2026-10-17"
    assert parsed.fields["tags"] == parsed.fields["aliases"] == ["one", "two"]
    changed = format.patch(text, fields={"title": "New title"})
    assert "date: 2026-10-17" in changed and "# keep comment" in changed
    assert changed.endswith("# Original\n")
