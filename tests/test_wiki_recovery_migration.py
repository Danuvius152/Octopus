"""Supported recovery metadata stays readable before ordinary Store admission."""

import sqlite3
from contextlib import closing

import pytest

from server.config import settings
from server.storage_verification import verify_storage
from server.wiki import recovery_state, schema
from server.wiki.recovery import Metadata
from server.wiki.registry import WikiRegistry
from server.wiki.store import WikiError
from tests.test_wiki_format_admission import registered_account as registered_account
from tests.test_wiki_job_event_migration import baseline as baseline
from tests.wiki_current_storage import facts


async def test_current_uncertain_metadata_is_available_without_opening_an_ordinary_store(registered_account, monkeypatch):
    root, state, registration, owner = registered_account
    path = state / "index.sqlite3"
    with closing(sqlite3.connect(path)) as db:
        db.execute("UPDATE filesystem_state SET uncertain='Original unresolved filesystem reception'")
        db.commit()
        original = facts(db)
    original_bytes, config_bytes = path.read_bytes(), (state / "config.json").read_bytes()
    registry = WikiRegistry()

    def no_store(*args, **kwargs):
        raise AssertionError("Recovery metadata must not depend on ordinary Store admission")

    monkeypatch.setattr(registry, "store", no_store)
    try:
        snapshot = Metadata(registry, owner).snapshot()
        assert snapshot["filesystem"]["uncertain"] == "Original unresolved filesystem reception"
        assert snapshot["vault_id"] == registration["vault_id"]
        assert path.read_bytes() == original_bytes and (state / "config.json").read_bytes() == config_bytes
        with closing(sqlite3.connect(path)) as db:
            assert db.execute("PRAGMA user_version").fetchone()[0] == schema.VERSION
            assert facts(db) == original
    finally:
        registry.close()


@pytest.mark.parametrize("loss", [*recovery_state.TABLES, "guard", "protocol"])
async def test_index_repair_does_not_recreate_lost_recovery_facts(baseline, loss):
    root, state, registration = baseline
    path = state / "index.sqlite3"
    with closing(sqlite3.connect(path)) as db:
        original_jobs = list(db.execute("SELECT * FROM jobs ORDER BY id"))
        db.execute("DROP TABLE search_fts")
        if loss == "guard":
            db.execute("DROP TRIGGER filesystem_recovery_update_guard")
        elif loss == "protocol":
            db.execute("DELETE FROM wiki_protocols WHERE name='filesystem_recovery'")
        else:
            db.execute("DROP TABLE " + loss)
        db.commit()
    original_bytes = path.read_bytes()
    with pytest.raises((WikiError, RuntimeError)):
        verify_storage(settings)
    assert path.read_bytes() == original_bytes
    with closing(sqlite3.connect(path)) as db:
        assert list(db.execute("SELECT * FROM jobs ORDER BY id")) == original_jobs
        assert db.execute("SELECT 1 FROM sqlite_schema WHERE name='search_fts'").fetchone() is None
