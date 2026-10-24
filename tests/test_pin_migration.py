"""Saved sidebar choices survive the supported upgrade and repeated startup."""

from pathlib import Path

import pytest

from server.database import Database
from tests.accounts import create_account
from tests.main_storage import write_unversioned_baseline


async def _seed(path: Path, version: int) -> tuple[str, str]:
    db = Database(str(path))
    await db.initialize()
    owner, _ = await create_account(db)
    default = await db.get_system_agent(owner["id"])
    # Creation order, insertion order and the user's chosen order differ.
    for agent_id, name, created in (
        ("b", "Beta", "2026-02-01"), ("a", "Alpha", "2026-01-01"), ("z", "Zed", "2026-03-01"),
    ):
        await db.save_agent(agent_id=agent_id, name=name, created_at=created,
                             updated_at=created, user_id=owner["id"])
    await db.reorder_agent_pins([default["id"], "a", "b", "z"])
    await db.archive_agent("z")
    for app_id, created in (("app2", "2026-02-01"), ("app1", "2026-01-01")):
        await db.save_application(app_id=app_id, name=app_id, app_dir="/tmp/x",
                                   created_at=created, updated_at=created, user_id=owner["id"])
    await db.reorder_application_pins(["app1", "app2"])
    await db.close()
    if version == 0:
        write_unversioned_baseline(path)
    return owner["id"], default["id"]


def _order(rows: list[dict]) -> list[tuple[str, bool, int]]:
    return [
        (r["id"], r["pinned"], r["pin_order"])
        for r in sorted(rows, key=lambda r: r["pin_order"])
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("version", [0, 1])
async def test_existing_sidebar_order_and_ownership_survive_repeated_open(tmp_path, version):
    path = tmp_path / "pins.db"
    owner, default = await _seed(path, version)

    for _ in range(2):  # the second boot must change nothing
        db = Database(str(path))
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
        assert next(row for row in agents if row["id"] == "z")["archived"]


@pytest.mark.asyncio
@pytest.mark.parametrize("version", [0, 1])
async def test_new_pinned_rows_append_after_the_saved_positions(tmp_path, version):
    path = tmp_path / "pins.db"
    owner, _ = await _seed(path, version)
    db = Database(str(path))
    await db.initialize()
    await db.save_agent(
        agent_id="new", name="New", created_at="2026-05-01", updated_at="2026-05-01", user_id=owner,
    )
    agent = await db.get_agent("new")
    await db.close()
    assert agent is not None
    assert agent["pinned"] is True
    assert agent["pin_order"] == 5
