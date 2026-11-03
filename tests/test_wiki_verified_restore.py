"""History restores validate retained bytes without trusting later blob reads."""

import uuid

import pytest

from server.wiki.history import WikiHistory
from server.wiki.store import WikiError, WikiStore


@pytest.fixture
def history(tmp_path):
    root = tmp_path / "vault"
    root.mkdir()
    store = WikiStore(root, tmp_path / "state", str(uuid.uuid4()), create=True)
    try:
        yield WikiHistory(store)
    finally:
        store.close()


@pytest.mark.parametrize("side", ["before", "after"])
def test_corrupted_history_bytes_cannot_replace_the_current_document(history, side):
    original = history.write("note.md", b"# Original\nTrusted words.\n", None, "human", "create")
    current = history.write("note.md", b"# Current\nKeep these words.\n", original["revision"], "human", "edit")
    document = history.store.read("note.md")
    selected = current if side == "before" else original
    retained = history.blobs / original["revision"]
    retained.write_bytes(b"# Forged\nUntrusted replacement.\n")

    with pytest.raises(WikiError) as rejected:
        history.restore(selected["change_id"], current["revision"], side, "human")

    assert rejected.value.status == 409
    assert history.store.path("note.md").read_bytes() == b"# Current\nKeep these words.\n"
    assert retained.read_bytes() == b"# Forged\nUntrusted replacement.\n"
    unchanged = history.store.read(document["id"])
    assert unchanged["id"] == document["id"]
    assert unchanged["revision"] == current["revision"]
    assert {row["id"] for row in history.history("note.md")["items"]} == {
        original["change_id"], current["change_id"],
    }


def test_restore_uses_verified_bytes_when_the_blob_is_replaced_after_read(history, monkeypatch):
    original = history.write("note.md", b"# Original\nTrusted words.\n", None, "human", "create")
    current = history.write("note.md", b"# Current\nNewer words.\n", original["revision"], "human", "edit")
    document = history.store.read("note.md")
    retained = history.blobs / original["revision"]
    replacement = history.state / "replacement-blob"

    def replace_after_read(path, raw):
        if path == retained:
            replacement.write_bytes(b"# Forged\nReplaced after the read.\n")
            replacement.replace(retained)

    with monkeypatch.context() as boundary:
        from tests.byte_reads import observe_byte_reads

        observe_byte_reads(boundary, after=replace_after_read)
        restored = history.restore(original["change_id"], current["revision"], "after", "human")

    assert history.store.path("note.md").read_bytes() == b"# Original\nTrusted words.\n"
    assert retained.read_bytes() == b"# Forged\nReplaced after the read.\n"
    assert restored["revision"] == original["revision"]
    assert restored["change_id"] not in {original["change_id"], current["change_id"]}
    assert history.store.read(document["id"])["id"] == document["id"]


def test_restore_retains_a_proposal_when_a_real_writer_changes_the_read_revision(history):
    original = history.write("note.md", b"# Original\nTrusted words.\n", None, "human", "create")
    history.write("note.md", b"# Current\nPreviously read words.\n", original["revision"], "human", "edit")
    read = history.store.read("note.md")
    latest = history.write("note.md", b"# Latest\nKeep the concurrent edit.\n", read["revision"], "other-human", "concurrent edit")

    with pytest.raises(WikiError) as rejected:
        history.restore(original["change_id"], read["revision"], "after", "human")

    assert rejected.value.status == 409
    assert history.store.path("note.md").read_bytes() == b"# Latest\nKeep the concurrent edit.\n"
    unchanged = history.store.read(read["id"])
    assert unchanged["id"] == read["id"]
    assert unchanged["revision"] == latest["revision"]
    candidate = history.history("note.md")["items"][0]
    assert candidate["status"] == "conflict"
    assert candidate["before_hash"] == latest["revision"]
    assert candidate["after_hash"] == original["revision"]
    assert candidate["proposed_content"] == "# Original\nTrusted words.\n"
    assert candidate["resolved_at"] is None
