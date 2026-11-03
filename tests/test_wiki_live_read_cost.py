"""Unchanged reads still verify bytes without rewriting derived facts."""

from contextlib import contextmanager

from server.wiki.reading import read
from tests.byte_reads import observe_byte_reads
from tests.test_wiki_markdown_navigation import store as store


def test_unchanged_live_read_checks_bytes_without_identity_material_signature_writes(store, monkeypatch):
    path = store.root / "current.md"
    path.write_text("# Current\nKeep the actual bytes.\n")
    document = store.read("current.md")
    byte_reads, statements = [], []
    connect = store.connect

    def read_current(file, raw):
        if file == path:
            byte_reads.append(len(raw))

    @contextmanager
    def traced():
        with connect() as connection:
            connection.set_trace_callback(statements.append)
            yield connection

    observe_byte_reads(monkeypatch, after=read_current)
    monkeypatch.setattr(store, "connect", traced)
    for _ in range(2):
        current = store.read(document["id"])
        assert current["revision"] == document["revision"]
        assert current["content"] == "# Current\nKeep the actual bytes.\n"
    assert len(byte_reads) >= 2 and sum(byte_reads) >= 2 * len(path.read_text().encode())
    writes = [sql for sql in statements if sql.startswith(("UPDATE document_identities", "UPDATE documents SET signature", "INSERT OR REPLACE INTO raw_materials"))]
    assert writes == []


def test_location_and_content_share_one_live_read_snapshot(store, monkeypatch):
    path = store.root / "location.md"
    original = "# Source\n\nIntroduction.\n\n## Claim\n\nOriginal condition.\n"
    changed = "# Source\n\nChanged introduction.\n\n## Claim\n\nNew condition.\n"
    path.write_text(original)
    document = store.read("location.md")
    byte_reads = []

    def change_after_first_read(file, raw):
        if file == path:
            byte_reads.append(len(raw))
            if len(byte_reads) == 1:
                path.write_text(changed)

    observe_byte_reads(monkeypatch, after=change_after_first_read)
    result = read(store, document["id"], None, location="#claim")
    assert result["content"] == "## Claim\n\nOriginal condition.\n"
    assert result["revision"] == document["revision"]
    assert len(byte_reads) == 1
    current = read(store, document["id"], None, location="#claim")
    assert current["revision"] != result["revision"]
    assert current["content"] == "## Claim\n\nNew condition.\n"
