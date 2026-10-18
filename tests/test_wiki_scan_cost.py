"""Material lookup preserves current bytes without inventorying known matches."""

import os
import uuid

import pytest

from server.wiki import materials
from server.wiki.history import WikiHistory
from server.wiki.store import WikiStore


@pytest.fixture
def store(tmp_path):
    root = tmp_path / "vault"
    (root / "Library").mkdir(parents=True)
    (root / "tmp/resources").mkdir(parents=True)
    (root / "Library/note.md").write_bytes(b"# Existing note\nKeep original bytes.\n")
    for index in range(20):
        (root / f"tmp/resources/resource-{index}.html").write_text(
            f"<p>Supporting fixture {index}</p>", encoding="utf-8"
        )
    current = WikiStore(root, tmp_path / "state", str(uuid.uuid4()))
    try:
        current.synchronize()
        yield current
    finally:
        current.close()


def count_inventories(monkeypatch):
    walked = []
    original = os.walk

    def inventory(*args, **kwargs):
        walked.append(args[0])
        yield from original(*args, **kwargs)

    monkeypatch.setattr(os, "walk", inventory)
    return walked


def test_known_material_match_does_not_inventory_unrelated_directories(store, monkeypatch):
    original = store.read("Library/note.md")
    raw = store.path(original["path"]).read_bytes()
    walked = count_inventories(monkeypatch)
    with WikiHistory(store).locked():
        matched = materials.find(store, raw, markdown_only=True)
    assert matched["id"] == original["id"]
    assert matched["revision"] == original["revision"]
    assert walked == []


def test_unobserved_hardlink_edit_is_discovered_before_material_miss(store, tmp_path):
    original = store.read("Library/note.md")
    outside = tmp_path / "outside.md"
    os.link(store.path(original["path"]), outside)
    raw = b"# Changed through an outside hardlink\nCurrent original.\n"
    outside.write_bytes(raw)
    with WikiHistory(store).locked():
        matched = materials.find(store, raw, markdown_only=True)
    assert matched["id"] == original["id"]
    assert store.path(matched["path"]).read_bytes() == raw


def test_stale_hash_candidate_cannot_reuse_a_later_human_revision(store, tmp_path):
    original = store.read("Library/note.md")
    before = store.path(original["path"]).read_bytes()
    outside = tmp_path / "outside.md"
    os.link(store.path(original["path"]), outside)
    changed = before + b"Human correction must remain authoritative.\n"
    outside.write_bytes(changed)
    with WikiHistory(store).locked():
        assert materials.find(store, before, markdown_only=True) is None
    assert store.path(original["path"]).read_bytes() == changed


def test_new_file_is_discovered_before_creating_a_duplicate_material(store):
    raw = b"# Newly captured material\nAlready exists on disk.\n"
    store.path("Library/new.md").write_bytes(raw)
    with WikiHistory(store).locked():
        matched = materials.find(store, raw, markdown_only=True)
    assert matched["path"] == "Library/new.md"
    assert store.path(matched["path"]).read_bytes() == raw


def test_missing_candidate_does_not_hide_a_current_matching_document(store):
    raw = store.path("Library/note.md").read_bytes()
    original = store.read("Library/note.md")
    store.path("Library/note.md").unlink()
    store.path("Library/replacement.md").write_bytes(raw)
    with WikiHistory(store).locked():
        matched = materials.find(store, raw, markdown_only=True)
    assert matched["path"] == "Library/replacement.md"
    assert matched["id"] != original["id"]
