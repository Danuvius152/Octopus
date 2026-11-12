"""Opening supported storage never guesses or changes a fork's account."""


import pytest

from server.database import Database
from server.session_manager import SessionManager
from tests.accounts import create_account


@pytest.mark.asyncio
async def test_current_fork_owners_and_ambiguous_relationships_survive_repeated_open(tmp_path):
    path = str(tmp_path / "sessions.db")
    db = Database(path)
    await db.initialize()
    account_a, _ = await create_account(db, username="owner-a", extra_roots=[tmp_path])
    account_b, _ = await create_account(db, username="owner-b", extra_roots=[tmp_path])
    owner_a, owner_b = account_a["id"], account_b["id"]
    # Keep an unresolved historical agent alongside the real current accounts.
    await db.save_agent(
        agent_id="legacy-default", name="Octo", is_system=True,
        created_at="2026-10-11", updated_at="2026-10-11",
    )
    system_agent = await db.get_agent("legacy-default")
    for agent_id, owner in (("agent-a", owner_a), ("agent-b", owner_b)):
        await db.save_agent(agent_id=agent_id, name=agent_id, created_at="2026-10-11",
                             updated_at="2026-10-11", user_id=owner)
    rows = [
        # Current writers commit the owner along with the original fork.
        ("root-a", "user", None, "agent-a", owner_a, 1),
        ("child", "fork", "middle", "agent-a", owner_a, 0),
        ("middle", "fork", "root-a", "agent-a", owner_a, 1),
        ("deleted-parent", "fork", "gone", "agent-a", owner_a, 0),
        # Never move an owned row or guess across conflicting account links.
        ("owned", "fork", "root-a", "agent-a", owner_b, 0),
        ("conflict", "fork", "root-a", "agent-b", None, 0),
        ("unknown", "fork", "gone", None, None, 0),
        ("cycle-1", "fork", "cycle-2", None, None, 0),
        ("cycle-2", "fork", "cycle-1", None, None, 0),
        ("install-root", "user", None, "agent-a", None, 0),
        ("unresolved", "fork", "install-root", "agent-a", None, 0),
        ("unrelated", "user", None, "agent-a", None, 0),
    ]
    for sid, origin, parent, agent_id, owner, archived in rows:
        await db.conn.execute(
            "INSERT INTO sessions "
            "(id, name, working_dir, created_at, origin, forked_from_session_id, "
            "agent_id, user_id, archived, backend, fork_status) "
            "VALUES (?, ?, ?, '2026-10-11', ?, ?, ?, ?, ?, 'codex', 'ready')",
            (sid, sid, str(tmp_path), origin, parent, agent_id or system_agent["id"], owner, archived),
        )
    await db.conn.commit()
    await db.append_message(
        session_id="child", seq=0, role="user", type="text", content="keep this history"
    )
    before = {r["id"]: r for r in await db.load_sessions(include_archived=True)}
    messages = await db.load_messages("child")
    await db.close()

    db = Database(path)
    try:
        await db.initialize()
        after = {r["id"]: r for r in await db.load_sessions(include_archived=True)}
        assert after == before
        assert await db.load_messages("child") == messages
        await db.close()
        db = Database(path)
        await db.initialize()
        assert {r["id"]: r for r in await db.load_sessions(include_archived=True)} == after
        async with db.transaction(write=False) as view:
            assert await view.fetch_one("PRAGMA user_version") == (10,)
        manager = SessionManager()
        await manager.initialize(db)
        assert manager.get_session("child", owner_a) is not None
        assert manager.get_session("child", owner_b) is None
        assert {row["id"] for row in await db.list_users()} == {owner_a, owner_b}
    finally:
        await db.close()
