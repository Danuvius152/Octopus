"""The sidebar-pins migration (sidebar-pins.md §4) on a database that predates it.

Every agent and application that existed before pins must come out pinned, in
the order the sidebar already showed them — the Default Agent first, then by
creation — so the first boot on the new schema changes nothing the user sees.
Run twice to prove the backfill is idempotent.
"""

import pytest

from server.database import Database
from tests.accounts import create_account


async def _seed(path: str) -> tuple[str, str]:
    db = Database(path)
    await db.initialize()
    owner, _ = await create_account(db)
    default = await db.get_system_agent(owner["id"])
    conn = db.conn
    # Inserted out of creation order, with the Default Agent created last, so
    # the backfill has to sort rather than trust rowid.
    await conn.execute("UPDATE agents SET created_at='2026-04-01', updated_at='2026-04-01' WHERE id=?", (default["id"],))
    for agent_id, name, created, is_system, archived in (
        ("b", "Beta", "2026-02-01", 0, 0),
        ("a", "Alpha", "2026-01-01", 0, 0),
        ("z", "Zed", "2026-03-01", 0, 1),
    ):
        await conn.execute(
            "INSERT INTO agents (id, name, is_system, archived, created_at, "
            "updated_at, user_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (agent_id, name, is_system, archived, created, created, owner["id"]),
        )
    for app_id, created in (("app2", "2026-02-01"), ("app1", "2026-01-01")):
        await conn.execute(
            "INSERT INTO applications (id, name, app_dir, created_at, "
            "updated_at, user_id) VALUES (?, ?, '/tmp/x', ?, ?, ?)",
            (app_id, app_id, created, created, owner["id"]),
        )
    # Keep account ownership while recreating precisely the missing columns.
    for table in ("agents", "applications"):
        for column in ("pinned", "pin_order"):
            await conn.execute(f"ALTER TABLE {table} DROP COLUMN {column}")
    await conn.commit()
    await db.close()
    return owner["id"], default["id"]


def _order(rows: list[dict]) -> list[tuple[str, bool, int]]:
    return [
        (r["id"], r["pinned"], r["pin_order"])
        for r in sorted(rows, key=lambda r: r["pin_order"])
    ]


@pytest.mark.asyncio
async def test_existing_rows_come_out_pinned_in_sidebar_order(tmp_path):
    path = str(tmp_path / "pre-pins.db")
    owner, default = await _seed(path)

    for _ in range(2):  # the second boot must change nothing
        db = Database(path)
        await db.initialize()
        agents = await db.load_agents(include_archived=True, user_id=owner)
        apps = await db.load_applications(include_archived=True, user_id=owner)
        await db.close()

        assert _order(agents) == [
            (default, True, 1),
            ("a", True, 2),
            ("b", True, 3),
            ("z", True, 4),
        ]
        assert _order(apps) == [("app1", True, 1), ("app2", True, 2)]
        assert all(row["user_id"] == owner for row in [*agents, *apps])


@pytest.mark.asyncio
async def test_rows_written_after_the_migration_append(tmp_path):
    path = str(tmp_path / "pre-pins.db")
    owner, _ = await _seed(path)
    db = Database(path)
    await db.initialize()
    await db.save_agent(
        agent_id="new", name="New", created_at="2026-05-01", updated_at="2026-05-01", user_id=owner,
    )
    agent = await db.get_agent("new")
    await db.close()
    assert agent is not None
    assert agent["pinned"] is True
    assert agent["pin_order"] == 5
