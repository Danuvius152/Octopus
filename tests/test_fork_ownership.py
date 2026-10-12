"""Recover account ownership lost by older rewind/duplicate writers."""

import pytest

from server.database import Database
from server.session_manager import SessionManager


@pytest.mark.asyncio
async def test_startup_repairs_only_unambiguous_fork_owners(tmp_path):
    path = str(tmp_path / "sessions.db")
    db = Database(path)
    await db.initialize()
    system_agent = await db.get_system_agent()
    for agent_id, owner in (("agent-a", "owner-a"), ("agent-b", "owner-b")):
        await db.conn.execute(
            "INSERT INTO agents (id, name, created_at, updated_at, user_id) "
            "VALUES (?, ?, '2026-10-11', '2026-10-11', ?)",
            (agent_id, agent_id, owner),
        )
    rows = [
        # Archived originals are still evidence; a fork chain needs iteration.
        ("root-a", "user", None, "agent-a", "owner-a", 1),
        ("child", "fork", "middle", "agent-a", None, 0),
        ("middle", "fork", "root-a", "agent-a", None, 1),
        ("deleted-parent", "fork", "gone", "agent-a", None, 0),
        # Never move an owned row or guess across conflicting account links.
        ("owned", "fork", "root-a", "agent-a", "owner-b", 0),
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
    await db.append_message(
        session_id="child", seq=0, role="user", type="text", content="keep this history"
    )
    await db.flush()
    before = {r["id"]: r for r in await db.load_sessions(include_archived=True)}
    messages = await db.load_messages("child")
    await db.close()

    db = Database(path)
    try:
        await db.initialize()
        after = {r["id"]: r for r in await db.load_sessions(include_archived=True)}
        repaired = {"child", "middle", "deleted-parent"}
        for sid, row in before.items():
            expected = {**row, "user_id": "owner-a"} if sid in repaired else row
            assert after[sid] == expected
        assert await db.load_messages("child") == messages
        await db._apply_migrations()
        assert {r["id"]: r for r in await db.load_sessions(include_archived=True)} == after
        ledger = await db.conn.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE version='backfill:fork_session_owners'"
        )
        assert (await ledger.fetchone())[0] == 1
        manager = SessionManager()
        await manager.initialize(db)
        assert manager.get_session("child", "owner-a") is not None
        assert manager.get_session("child", "owner-b") is None
    finally:
        await db.close()
