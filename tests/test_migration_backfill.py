"""Fresh storage leaves ownership to account creation, including after reopening."""

import pytest

from server.database import Database


async def _table_exists(db: Database, table: str) -> bool:
    cursor = await db.conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
    )
    return await cursor.fetchone() is not None


async def _column_names(db: Database, table: str) -> set[str]:
    cursor = await db.conn.execute(f"PRAGMA table_info({table})")
    return {row[1] for row in await cursor.fetchall()}


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
