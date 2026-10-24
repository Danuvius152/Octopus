"""Wiki format admission includes current owner layouts and durable references."""

import sqlite3
from contextlib import closing
from pathlib import Path
from urllib.parse import unquote, urlsplit

import pytest

from server.config import settings
from server.storage_verification import verify_storage
from server.wiki.store import WikiError
from tests.test_wiki_format_admission import registered_account as registered_account


async def test_reordered_receipt_columns_are_refused_before_positional_writers_can_enter(registered_account):
    _, state, _, _ = registered_account
    path = state / "index.sqlite3"
    with closing(sqlite3.connect(path)) as database:
        assert database.execute("SELECT count(*) FROM operation_receipts").fetchone() == (0,)
        original = database.execute("SELECT sql FROM sqlite_schema WHERE name='operation_receipts'").fetchone()[0]
        changed = original.replace("reason TEXT NOT NULL", "retained_swap TEXT NOT NULL").replace(
            "created_at TEXT NOT NULL", "reason TEXT NOT NULL").replace("retained_swap TEXT NOT NULL", "created_at TEXT NOT NULL")
        assert changed != original
        dependents = [row[0] for row in database.execute(
            "SELECT sql FROM sqlite_schema WHERE tbl_name='operation_receipts' AND type IN ('index','trigger') AND sql IS NOT NULL")]
        database.execute("DROP TABLE operation_receipts")
        database.execute(changed)
        for sql in dependents:
            database.execute(sql)
        database.commit()
    original_bytes = path.read_bytes()
    with pytest.raises((WikiError, RuntimeError)):
        verify_storage(settings)
    assert path.read_bytes() == original_bytes


@pytest.mark.parametrize("damage", ["missing_protocol", "unknown_trigger", "misplaced_index"])
@pytest.mark.parametrize("projection", ["maintenance", "search"])
async def test_damaged_projection_does_not_authorize_detaching_unrecognized_or_unprotected_state(registered_account, monkeypatch, damage, projection):
    root, state, registration, _ = registered_account
    path = state / "index.sqlite3"
    with closing(sqlite3.connect(path)) as database:
        if projection == "maintenance":
            database.execute("DROP TABLE maintenance_job_items")
        else:
            database.execute("ALTER TABLE search_sections DROP COLUMN readable_body")
        if damage == "missing_protocol":
            database.execute("DELETE FROM wiki_protocols WHERE name='wiki_grants'")
        elif damage == "unknown_trigger":
            database.execute("CREATE TRIGGER maintenance_input_future_evidence AFTER UPDATE ON documents BEGIN SELECT 1; END")
        else:
            database.execute("DROP INDEX maintenance_receipt_document")
            database.execute("CREATE INDEX maintenance_receipt_document ON documents(id)")
        database.commit()
    original = path.read_bytes()
    dropped = []
    connect = sqlite3.connect

    def observe(action, first, second, database_name, source):
        if action in (sqlite3.SQLITE_DROP_TABLE, sqlite3.SQLITE_DROP_TRIGGER, sqlite3.SQLITE_DROP_INDEX):
            dropped.append((action, first))
        return sqlite3.SQLITE_OK

    def observed(*args, **kwargs):
        database = connect(*args, **kwargs)
        raw = str(args[0])
        location = unquote(urlsplit(raw).path) if raw.startswith("file:") else raw
        if location != ":memory:" and Path(location).resolve() == path:
            database.set_authorizer(observe)
        return database

    monkeypatch.setattr(sqlite3, "connect", observed)
    with pytest.raises((WikiError, RuntimeError)):
        verify_storage(settings)
    assert dropped == []
    assert path.read_bytes() == original


@pytest.mark.parametrize("damage", ["middle_column", "extra_column"])
async def test_search_owner_does_not_admit_unrecognized_section_declarations(registered_account, damage):
    root, state, _, _ = registered_account
    path = state / "index.sqlite3"
    with closing(sqlite3.connect(path)) as database:
        if damage == "middle_column":
            database.execute("ALTER TABLE search_sections DROP COLUMN section_role")
        else:
            database.execute("ALTER TABLE search_sections ADD COLUMN future_fact TEXT")
        database.commit()
    original, config, body = path.read_bytes(), (state / "config.json").read_bytes(), (root / "source.md").read_bytes()
    with pytest.raises((WikiError, RuntimeError)):
        verify_storage(settings)
    assert path.read_bytes() == original
    assert (state / "config.json").read_bytes() == config
    assert (root / "source.md").read_bytes() == body
