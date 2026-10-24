"""Retained pre-account input is refused; fresh storage remains unowned.

The old fixture stays explicit so startup cannot silently adopt it or use an
orphan schedule to recreate the retired installation's Default Agent.
"""

import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from server.database import Database

# The schema as it existed before the Agents refactor, for the three tables
# the migration transforms. Everything else is created fresh by Database.
_OLD_SCHEMA = """
CREATE TABLE sessions (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    working_dir TEXT NOT NULL,
    created_at TEXT NOT NULL,
    claude_session_id TEXT,
    archived INTEGER NOT NULL DEFAULT 0,
    credential_id TEXT
);
CREATE TABLE schedules (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    name TEXT NOT NULL,
    prompt TEXT NOT NULL,
    interval_seconds INTEGER NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    last_run_at TEXT,
    FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
);
CREATE TABLE bridge_mappings (
    platform TEXT NOT NULL,
    chat_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    PRIMARY KEY (platform, chat_id),
    FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
);
"""


def _seed_old_db(path: str) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(_OLD_SCHEMA)
    conn.execute(
        "INSERT INTO sessions (id, name, working_dir, created_at) "
        "VALUES ('s1', 'Old Session', '/tmp', '2025-01-01T00:00:00+00:00')"
    )
    conn.execute(
        "INSERT INTO schedules (id, session_id, name, prompt, interval_seconds, created_at) "
        "VALUES ('sch1', 's1', 'daily', 'do it', 3600, '2025-01-01T00:00:00+00:00')"
    )
    conn.execute(
        "INSERT INTO bridge_mappings (platform, chat_id, session_id) "
        "VALUES ('telegram', 'c1', 's1')"
    )
    conn.commit()
    conn.close()


async def _table_exists(db: Database, table: str) -> bool:
    cursor = await db.conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
    )
    return await cursor.fetchone() is not None


async def _column_names(db: Database, table: str) -> set[str]:
    cursor = await db.conn.execute(f"PRAGMA table_info({table})")
    return {row[1] for row in await cursor.fetchall()}


@pytest.mark.parametrize("orphan_schedule", [False, True])
def test_preaccount_database_is_refused_without_adoption(tmp_path, orphan_schedule):
    """The retired pre-account conversion cannot acquire current ownership."""
    db_path = str(tmp_path / "old.db")
    _seed_old_db(db_path)
    if orphan_schedule:
        with sqlite3.connect(db_path) as writer:
            writer.execute("DELETE FROM sessions WHERE id='s1'")
    original = Path(db_path).read_bytes()
    result = subprocess.run(
        [sys.executable, "-c", """
import asyncio,sys
from server.database import Database
asyncio.run(Database(sys.argv[1]).initialize())
""", db_path],
        cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 70, result.stderr
    assert Path(db_path).read_bytes() == original
    with sqlite3.connect(Path(db_path).as_uri() + "?mode=ro", uri=True) as reader:
        assert reader.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == (0 if orphan_schedule else 1)
        assert reader.execute("SELECT session_id FROM schedules").fetchone()[0] == "s1"
        assert reader.execute("SELECT COUNT(*) FROM bridge_mappings").fetchone()[0] == 1
        assert reader.execute("SELECT COUNT(*) FROM sqlite_schema WHERE name IN ('users','agents')").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_fresh_db_keeps_agents_empty_until_account_creation(tmp_path):
    """A brand-new DB has the current schema without ownerless business rows."""
    db = Database(str(tmp_path / "fresh.db"))
    await db.initialize()
    try:
        assert await db.get_system_agent() is None
        assert await db.load_agents(include_archived=True) == []
        # No session_id leftover on the freshly-created tables.
        assert "session_id" not in await _column_names(db, "schedules")
        assert not await _table_exists(db, "bridge_mappings")

        # Second startup still leaves account creation to the account aggregate.
        await db.close()
        db = Database(str(tmp_path / "fresh.db"))
        await db.initialize()
        agents = await db.load_agents(include_archived=True)
        assert agents == []
    finally:
        await db.close()
