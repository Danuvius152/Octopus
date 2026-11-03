"""A document resource refusal preserves files, evidence and usable old index."""

import os
import uuid
from pathlib import Path

import pytest

from server.wiki import store as storage
from server.wiki.history import WikiHistory
from server.wiki.store import WikiError, WikiStore


@pytest.fixture
def store(tmp_path, monkeypatch):
    root = tmp_path / "vault"
    root.mkdir()
    instance = WikiStore(root, tmp_path / "state", uuid.uuid4().hex, create=True)
    monkeypatch.setattr(storage, "MAX_DOCUMENT_BYTES", 128)
    try:
        yield instance
    finally:
        instance.close()


@pytest.mark.parametrize("existing", [False, True])
def test_oversized_write_is_refused_before_file_or_receipt_effects(store, existing):
    history = WikiHistory(store)
    before = history.write("note.md", b"# Retained\nOriginal argument.\n", None, "human", "create") if existing else None
    original = store.path("note.md").read_bytes() if existing else None
    with store.connect() as db:
        counts = [db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                  for table in ("changes", "operation_receipts", "job_publications")]
    with pytest.raises(WikiError) as failure:
        history.write("note.md", b"x" * 129, before["revision"] if before else None, "human", "oversized edit")
    assert failure.value.status == 413
    assert (store.path("note.md").read_bytes() if existing else None) == original
    assert store.path("note.md").exists() == existing
    with store.connect() as db:
        assert [db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                for table in ("changes", "operation_receipts", "job_publications")] == counts
    if before:
        assert store.read("note.md")["revision"] == before["revision"]


def test_external_growth_keeps_old_index_and_other_documents_then_recovers_same_bytes(store):
    original = b"# Retained\nOriginal argument.\n"
    store.path("note.md").write_bytes(original)
    before = store.read("note.md")
    with store.connect() as db:
        indexed = [tuple(row) for row in db.execute("SELECT * FROM search_fts ORDER BY rowid")]
    store.path("note.md").write_bytes(b"x" * 129)
    store.path("healthy.md").write_bytes(b"# Healthy\nCurrent material.\n")
    store.refresh()
    result = store.documents(view="full")
    rejected = next(row for row in result["items"] if row["path"] == "note.md")
    assert rejected["id"] == before["id"] and rejected["revision"] == before["revision"]
    assert rejected["quality"] == "error" and rejected["stale"]
    assert result["index_status"] == "partial"
    assert "note.md" in str(result["index_errors"])
    compact = store.documents(view="compact")
    assert next(row for row in compact["items"] if row["path"] == "note.md")["resource_error"] == "document_too_large"
    assert store.read("healthy.md")["content"].startswith("# Healthy")
    with pytest.raises(WikiError) as failure:
        store.read(before["id"])
    assert failure.value.status == 413
    with pytest.raises(WikiError, match="note.md"):
        store.rebuild_search()
    with store.connect() as db:
        assert all(row in [tuple(value) for value in db.execute("SELECT * FROM search_fts")] for row in indexed)
    assert store.path("note.md").read_bytes() == b"x" * 129
    store.path("note.md").write_bytes(original)
    store.refresh()
    # Reopen with the same identity and policy: no error fast path may survive
    # just because the repaired bytes hash to the last usable revision.
    store.close()
    reopened = WikiStore(store.root, store.state, store.vault_id, create=False)
    try:
        current = reopened.read("note.md")
        assert current["id"] == before["id"] and current["revision"] == before["revision"]
        assert current["quality"] == "text" and "resource_error" not in current["metadata"]
        assert reopened.index_state()["index_error_count"] == 0
    finally:
        reopened.close()


@pytest.mark.parametrize("size,accepted", [(127, True), (128, True), (129, False)])
def test_document_byte_boundary_applies_to_reads_and_supplied_bytes(store, size, accepted):
    raw = b"x" * size
    path = store.path("boundary.txt")
    path.write_bytes(raw)
    if accepted:
        assert store.read(path.name, length=24000)["content"] == raw.decode()
        assert storage.require_document_bytes(raw, path.name) == raw
    else:
        for operation in (lambda: store.read(path.name, length=1), lambda: storage.require_document_bytes(raw, path.name)):
            with pytest.raises(WikiError) as failure:
                operation()
            assert failure.value.status == 413


def test_a_readable_note_does_not_claim_its_oversized_source_is_missing_or_current(store, monkeypatch):
    monkeypatch.setattr(storage, "MAX_DOCUMENT_BYTES", 512)
    history = WikiHistory(store)
    original = history.write("source.md", b"# Source\nCurrent evidence.\n", None, "human", "create")
    derived = ("---\nsources:\n  - resource: source.md\n    id: evidence\n    location: L1\n"
               f"    revision: {original['revision']}\n---\n# Derived\nA conditional claim.\n").encode()
    history.write("derived.md", derived, None, "human", "derive")
    store.path("source.md").write_bytes(b"x" * 513)
    store.refresh()
    derived = store.read("derived.md")
    reference = derived["metadata"]["source_refs"][0]
    assert reference["status"] == "version_unknown"
    assert reference["current_revision"] is None
    assert reference["resource_error"] == "document_too_large"
    assert derived["stale"]


@pytest.mark.parametrize("change", ["growth", "replacement", "unknown-size"])
def test_original_reads_are_bounded_despite_external_file_changes(store, monkeypatch, change):
    path = store.path("changing.txt")
    path.write_bytes(b"x" * 128)
    before = store.read(path.name)
    opened, reads = Path.open, []
    changed = False

    class ChangingFile:
        def __init__(self, handle):
            self.handle = handle

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.handle.close()

        def fileno(self):
            return self.handle.fileno()

        def read(self, size=-1):
            nonlocal changed
            reads.append(size)
            assert 0 <= size <= 129, "A document read must never allocate the whole unbounded file"
            if not changed:
                changed = True
                if change == "replacement":
                    path.rename(path.with_suffix(".old"))
                    with opened(path, "wb") as writer:
                        writer.write(b"y" * 129)
                else:
                    with opened(path, "ab") as writer:
                        writer.write(b"y")
            return self.handle.read(size)

    def controlled_open(target, mode="r", *args, **kwargs):
        handle = opened(target, mode, *args, **kwargs)
        return ChangingFile(handle) if target == path and mode == "rb" else handle

    monkeypatch.setattr(Path, "open", controlled_open)
    if change == "unknown-size":
        fstat = os.fstat

        def unknown_size(fd):
            observed = fstat(fd)
            if observed.st_ino == path.stat().st_ino:
                fields = list(observed)
                fields[6] = 0
                return os.stat_result(fields)
            return observed

        monkeypatch.setattr(os, "fstat", unknown_size)
    with pytest.raises(WikiError) as failure:
        store.read(path.name)
    assert failure.value.status == 413
    assert reads and all(size == 129 for size in reads)
    with store.connect() as db:
        assert db.execute("SELECT revision FROM documents WHERE id=?", (before["id"],)).fetchone()[0] == before["revision"]


def test_new_policy_refuses_an_old_large_restore_without_truncating_its_blob(store, monkeypatch):
    monkeypatch.setattr(storage, "MAX_DOCUMENT_BYTES", 512)
    history = WikiHistory(store)
    old = history.write("history.txt", b"x" * 200, None, "human", "old supported document")
    current = history.write("history.txt", b"current", old["revision"], "human", "smaller document")
    blob = history.blobs / old["revision"]
    monkeypatch.setattr(storage, "MAX_DOCUMENT_BYTES", 128)
    with pytest.raises(WikiError) as failure:
        history.restore(old["change_id"], current["revision"], "after", "human")
    assert failure.value.status == 413
    assert store.path("history.txt").read_bytes() == b"current"
    assert blob.read_bytes() == b"x" * 200


def test_deduplication_does_not_reuse_or_block_on_an_oversized_candidate(store):
    from server.wiki.materials import find

    raw = b"# Candidate\nOriginal bytes.\n"
    store.path("candidate.md").write_bytes(raw)
    store.read("candidate.md")
    store.path("candidate.md").write_bytes(b"x" * 129)
    assert find(store, raw) is None
    assert store.path("candidate.md").stat().st_size == 129


def test_resource_rejected_originals_remain_eligible_for_offline_account_copy(tmp_path, monkeypatch):
    from server.config import settings
    from server.wiki.portability import inspect_wiki
    from server.wiki.registry import WikiRegistry

    monkeypatch.setattr(settings, "wiki_dir", str(tmp_path / "registered-state"))
    monkeypatch.setattr(storage, "MAX_DOCUMENT_BYTES", 128)
    root = tmp_path / "registered-vault"
    root.mkdir()
    registry = WikiRegistry()
    configured = registry.configure(None, root, "curator")
    registered = registry.store(None)
    try:
        path = root / "oversized.md"
        path.write_bytes(b"x" * 129)
        registered.refresh()
        registered.close_admission()
        registered.synchronize_shutdown()
        registry.close()
        assert inspect_wiki(registered.state)["vault_id"] == configured["vault_id"]
        assert path.read_bytes() == b"x" * 129
    finally:
        registry.close()
