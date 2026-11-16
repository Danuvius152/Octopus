"""Account transfer keeps Wiki originals and historical proofs independent of local stats."""

import hashlib
import os
import sqlite3
import zipfile
from contextlib import closing
from pathlib import Path

import pytest

from server.config import settings
from server.database import Database
from server.portability import export_account, import_account
from server.wiki.history import WikiHistory
from server.wiki.portability import inspect_wiki
from server.wiki.registry import WikiRegistry
from server.wiki.store import MAX_DOCUMENT_BYTES, DocumentTooLarge
from tests.accounts import create_account
from tests.installation_settings import installation_settings


@pytest.mark.parametrize("suffix", [".pdf", ".PDF"])
async def test_account_round_trip_preserves_pdf_and_markdown_but_rebuilds_local_revisions(tmp_path, monkeypatch, suffix):
    home = tmp_path / "source"
    config, _ = installation_settings(home)
    working = home / "workspace"
    working.mkdir()
    for name, value in config.model_dump().items():
        monkeypatch.setattr(settings, name, value)
    database = Database(config.db_path)
    await database.initialize()
    try:
        owner, _ = await create_account(database, extra_roots=[working])
        agent = await database.get_system_agent(owner["id"])
    finally:
        await database.close()
    vault = home / "vault"
    vault.mkdir()
    pdf = vault / ("attachment" + suffix)
    # Larger than the text limit; this asset must never be loaded as a text body.
    with pdf.open("wb") as stream:
        for _ in range(128):
            stream.write(b"%PDF-opaque-original\x00\xff\n" * 2048)
    os.utime(pdf, ns=(1_600_000_000_000_000_000, 1_600_000_000_000_000_000))
    with pdf.open("rb") as stream:
        original_hash = hashlib.file_digest(stream, "sha256").hexdigest()
    registry = WikiRegistry()
    try:
        registration = registry.configure(owner["id"], vault, agent["id"])
        store = registry.store(owner["id"])
        history = WikiHistory(store)
        first = history.write("original.md", b"# Knowledge\n\nOriginal knowledge.\n", None, "human", "create")
        second = history.write("original.md", b"# Knowledge\n\nPortable knowledge.\n", first["revision"], "human", "edit")
        assert first["receipt_id"] and second["receipt_id"]
        original_history = history.history("original.md")
        documents = {row["path"]: row for row in store.documents(include_assets=True)["items"]}
        receipt_ids = (first["receipt_id"], second["receipt_id"])
        receipt_query = """SELECT id,doc_id,path,input_revision,output_revision,change_id
            FROM operation_receipts WHERE id IN (?,?) ORDER BY rowid"""
        expected_receipts = [
            (first["receipt_id"], documents["original.md"]["id"], "original.md", None,
             first["revision"], first["change_id"]),
            (second["receipt_id"], documents["original.md"]["id"], "original.md", first["revision"],
             second["revision"], second["change_id"]),
        ]
        with store.connect() as db:
            assert [tuple(row) for row in db.execute(receipt_query, receipt_ids)] == expected_receipts
        old_pdf = store.read(pdf.name)
        assert old_pdf["metadata"]["revision_kind"] == "stat" and old_pdf["quality"] == "excluded"
    finally:
        registry.close()

    read_bytes = Path.read_bytes

    def no_pdf_body(path):
        if path.suffix.lower() == ".pdf":
            raise AssertionError("PDF originals require streaming integrity checks")
        return read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", no_pdf_body)
    file_reads = []
    opened = Path.open

    class StreamingOriginal:
        def __init__(self, handle):
            self.handle = handle

        def __enter__(self):
            self.handle.__enter__()
            return self

        def __exit__(self, *args):
            return self.handle.__exit__(*args)

        def __getattr__(self, name):
            return getattr(self.handle, name)

        def read(self, size=-1):
            assert 0 < size <= 256 * 1024, "Original integrity must stream large files"
            file_reads.append(("read", size))
            return self.handle.read(size)

        def readinto(self, buffer):
            assert 0 < len(buffer) <= 256 * 1024, "Original integrity must use bounded buffers"
            file_reads.append(("readinto", len(buffer)))
            return self.handle.readinto(buffer)

    def bounded_original_open(path, mode="r", *args, **kwargs):
        handle = opened(path, mode, *args, **kwargs)
        return StreamingOriginal(handle) if path.suffix.lower() == ".pdf" and mode == "rb" else handle

    monkeypatch.setattr(Path, "open", bounded_original_open)
    archive_reads = []
    read = zipfile.ZipExtFile.read
    readinto = zipfile.ZipExtFile.readinto

    def bounded_archive_read(stream, size=-1):
        if Path(stream.name).suffix.lower() == ".pdf":
            assert 0 < size <= 256 * 1024, "Archive integrity must stream large originals"
            archive_reads.append(("read", size))
        return read(stream, size)

    def bounded_archive_readinto(stream, buffer):
        if Path(stream.name).suffix.lower() == ".pdf":
            assert 0 < len(buffer) <= 256 * 1024, "Archive integrity must use bounded buffers"
            archive_reads.append(("readinto", len(buffer)))
        return readinto(stream, buffer)

    monkeypatch.setattr(zipfile.ZipExtFile, "read", bounded_archive_read)
    monkeypatch.setattr(zipfile.ZipExtFile, "readinto", bounded_archive_readinto)
    archive = tmp_path / "account.zip"
    export_account(config, owner["id"], archive, home=home)
    assert any(operation == "readinto" for operation, _ in file_reads)
    restored_home = tmp_path / "restored"
    restored = import_account(archive, home=restored_home)
    assert {operation for operation, _ in archive_reads} == {"read", "readinto"}
    copied_pdf = restored_home / pdf.relative_to(home)
    assert copied_pdf.stat().st_mtime_ns != pdf.stat().st_mtime_ns
    with copied_pdf.open("rb") as stream:
        assert hashlib.file_digest(stream, "sha256").hexdigest() == original_hash
    for name, value in restored.model_dump().items():
        monkeypatch.setattr(settings, name, value)
    registry = WikiRegistry()
    try:
        copied = registry.store(owner["id"])
        # Admission after construction proves refresh rebuilt the local stat
        # projection, before read() can independently index this PDF again.
        assert inspect_wiki(copied.state)["vault_id"] == registration["vault_id"]
        assert registry.config(owner["id"])["vault_id"] == registration["vault_id"]
        assert copied.path("original.md").read_bytes() == b"# Knowledge\n\nPortable knowledge.\n"
        assert WikiHistory(copied).history("original.md") == original_history
        with copied.connect() as db:
            assert [tuple(row) for row in db.execute(receipt_query, receipt_ids)] == expected_receipts
        assert copied.search("Portable")["items"][0]["id"] == documents["original.md"]["id"]
        current_pdf = copied.read(pdf.name)
        assert current_pdf["id"] == old_pdf["id"]
        assert current_pdf["revision"] != old_pdf["revision"]
        assert current_pdf["quality"] == "excluded" and current_pdf["characters"] == 0
        assert current_pdf["metadata"]["revision_kind"] == "stat"
    finally:
        registry.close()


@pytest.fixture
async def transferable_wiki(tmp_path, monkeypatch):
    home = tmp_path / "source"
    config, _ = installation_settings(home)
    working = home / "workspace"
    working.mkdir()
    for name, value in config.model_dump().items():
        monkeypatch.setattr(settings, name, value)
    database = Database(config.db_path)
    await database.initialize()
    try:
        owner, _ = await create_account(database, extra_roots=[working])
        agent = await database.get_system_agent(owner["id"])
    finally:
        await database.close()
    vault = home / "vault"
    vault.mkdir()
    (vault / "attachment.PDF").write_bytes(b"%PDF-retained-original\x00\xff\n")
    registry = WikiRegistry()
    try:
        registry.configure(owner["id"], vault, agent["id"])
        store = registry.store(owner["id"])
        history = WikiHistory(store)
        receipt = history.write("original.md", b"# Knowledge\n\nPortable knowledge.\n", None, "human", "create")
        state = store.state
    finally:
        registry.close()
    return home, config, owner, vault, state, receipt


@pytest.mark.parametrize("change", ["mtime", "size", "missing"])
async def test_export_refuses_pdf_source_drift_without_rewriting_originals(transferable_wiki, tmp_path, change):
    home, config, owner, vault, state, _ = transferable_wiki
    pdf = vault / "attachment.PDF"
    if change == "mtime":
        stat = pdf.stat()
        os.utime(pdf, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))
    elif change == "size":
        stat = pdf.stat()
        with pdf.open("ab") as stream:
            stream.write(b"changed original")
        # Identity's existing file_key includes mtime, so hold that fact fixed:
        # the PDF stat revision must independently detect the changed size.
        os.utime(pdf, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    else:
        pdf.unlink()
    original = pdf.read_bytes() if pdf.exists() else None
    ledger = (state / "index.sqlite3").read_bytes()
    archive = tmp_path / "refused.zip"
    with pytest.raises(ValueError, match="requires source reconciliation"):
        export_account(config, owner["id"], archive, home=home)
    assert not archive.exists()
    assert (pdf.read_bytes() if pdf.exists() else None) == original
    assert (state / "index.sqlite3").read_bytes() == ledger


@pytest.mark.parametrize("damage", ["same-size", "truncated"])
async def test_import_rejects_damaged_pdf_before_creating_target(transferable_wiki, tmp_path, damage):
    home, config, owner, vault, _, _ = transferable_wiki
    archive = tmp_path / "account.zip"
    export_account(config, owner["id"], archive, home=home)
    pdf_name = "files/" + (vault / "attachment.PDF").relative_to(home).as_posix()
    damaged = tmp_path / "damaged.zip"
    with zipfile.ZipFile(archive) as source, zipfile.ZipFile(damaged, "w") as target:
        for entry in source.infolist():
            raw = source.read(entry)
            if entry.filename == pdf_name:
                raw = b"X" + raw[1:] if damage == "same-size" else raw[:-1]
            target.writestr(entry, raw)
    restored_home = tmp_path / "refused"
    with pytest.raises(ValueError, match="Archive file (integrity|size) mismatch"):
        import_account(damaged, home=restored_home)
    assert not restored_home.exists()
    assert (vault / "attachment.PDF").read_bytes() == b"%PDF-retained-original\x00\xff\n"


@pytest.mark.parametrize("existing", [False, True])
async def test_account_round_trip_retains_resource_rejected_text_without_accepting_new_revision(
    transferable_wiki, tmp_path, monkeypatch, existing,
):
    home, config, owner, vault, _, receipt = transferable_wiki
    name = "original.md" if existing else "oversized.md"
    raw = b"x" * (MAX_DOCUMENT_BYTES + 1)
    (vault / name).write_bytes(raw)
    registry = WikiRegistry()
    try:
        store = registry.store(owner["id"])
        store.refresh()
        rejected = next(row for row in store.documents()["items"] if row["path"] == name)
        assert rejected["metadata"]["resource_error"] == "document_too_large"
        assert rejected["revision"] == (receipt["revision"] if existing else "")
        history = WikiHistory(store).history(name)
    finally:
        registry.close()
    archive = tmp_path / "account.zip"
    export_account(config, owner["id"], archive, home=home)
    restored_home = tmp_path / "restored"
    restored = import_account(archive, home=restored_home)
    for key, value in restored.model_dump().items():
        monkeypatch.setattr(settings, key, value)
    registry = WikiRegistry()
    try:
        copied = registry.store(owner["id"])
        current = next(row for row in copied.documents()["items"] if row["path"] == name)
        assert (current["id"], current["revision"]) == (rejected["id"], rejected["revision"])
        assert current["metadata"]["resource_error"] == "document_too_large"
        with pytest.raises(DocumentTooLarge):
            copied.read(name)
        assert copied.path(name).read_bytes() == raw
        assert WikiHistory(copied).history(name) == history
    finally:
        registry.close()


async def test_export_rejects_corrupt_retained_wiki_history(transferable_wiki, tmp_path):
    home, config, owner, vault, state, receipt = transferable_wiki
    blob = state / "blobs" / receipt["revision"]
    blob.write_bytes(b"corrupt retained historical proof")
    original = (vault / "original.md").read_bytes()
    archive = tmp_path / "refused.zip"
    with pytest.raises(ValueError, match="missing or corrupted retained blob"):
        export_account(config, owner["id"], archive, home=home)
    assert not archive.exists()
    assert (vault / "original.md").read_bytes() == original
    assert blob.read_bytes() == b"corrupt retained historical proof"


async def test_stat_metadata_cannot_bypass_markdown_byte_stability(transferable_wiki, tmp_path):
    home, config, owner, vault, state, _ = transferable_wiki
    original = vault / "original.md"
    stat = original.stat()
    original.write_bytes(b"# Knowledge\n\nChanged! knowledge.\n")
    os.utime(original, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    # An arbitrary metadata claim is not authority to turn a text revision into
    # a stat revision. File identity, size and mtime deliberately remain equal.
    assert original.stat().st_size == stat.st_size
    with closing(sqlite3.connect(state / "index.sqlite3")) as db, db:
        db.execute("PRAGMA recursive_triggers=ON")
        db.execute("UPDATE documents SET metadata=json_set(metadata,'$.revision_kind','stat') WHERE path='original.md'")
    archive = tmp_path / "refused.zip"
    with pytest.raises(ValueError, match="requires source reconciliation"):
        export_account(config, owner["id"], archive, home=home)
    assert not archive.exists()
    assert original.read_bytes() == b"# Knowledge\n\nChanged! knowledge.\n"
