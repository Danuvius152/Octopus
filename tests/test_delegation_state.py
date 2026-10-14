"""Future delegation rounds persist through the public SQLite boundary."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

import pytest

from server.database import Database

STAMP = "2026-10-14T00:00:00+00:00"


async def _save_conversation(
    db: Database, *, owner: str | None = None, suffix: str = ""
) -> tuple[str, str, str]:
    agent = f"agent{suffix}"
    parent = f"parent{suffix}"
    child = f"child{suffix}"
    await db.save_agent(
        agent_id=agent, name=f"Vera{suffix}", created_at=STAMP,
        updated_at=STAMP, user_id=owner,
    )
    await db.save_session(
        parent, "Parent", "/tmp", STAMP, agent_id=agent, user_id=owner,
    )
    await db.save_session(
        child, "Child", "/tmp", STAMP, agent_id=agent, user_id=owner,
        origin="delegation", parent_session_id=parent,
        delegation_request="Review this change",
    )
    return child, parent, agent


@pytest.fixture
async def db(tmp_path: Path):
    database = Database(str(tmp_path / "delegations.db"))
    await database.initialize()
    yield database
    await database.close()


async def test_saved_round_survives_restart_and_rejects_stale_snapshot(db, tmp_path):
    child, parent, agent = await _save_conversation(db)
    running = json.dumps({"work_id": "round-a", "state": "running"})
    completed = json.dumps({"work_id": "round-a", "state": "completed"})
    follow_up = json.dumps({"work_id": "round-b", "state": "running"})
    scope = {"parent_session_id": parent, "user_id": None}

    assert await db.compare_and_set_delegation_state(child, None, running, **scope)
    assert await db.compare_and_set_delegation_state(child, running, completed, **scope)
    assert not await db.compare_and_set_delegation_state(child, running, running, **scope)
    assert await db.compare_and_set_delegation_state(child, completed, follow_up, **scope)
    assert not await db.compare_and_set_delegation_state(child, completed, completed, **scope)

    await db.close()
    reopened = Database(str(tmp_path / "delegations.db"))
    await reopened.initialize()
    try:
        assert await reopened.load_delegation_state(child, **scope) == follow_up
        assert await reopened.load_delegation_states() == [{
            "id": child, "parent_session_id": parent, "user_id": None,
            "agent_id": agent, "delegation_state": follow_up,
        }]
    finally:
        await reopened.close()


async def test_latest_request_is_bound_to_child_work_and_its_owner(db):
    child, parent, _agent = await _save_conversation(db, owner="owner")
    await db.append_message(
        child, 0, "user", "text", content="Initial review",
        tool_input={"request_context": {"work_id": "round-a", "request_id": "initial"}},
    )
    await db.append_message(
        child, 1, "user", "text", content="Grandchild reply",
        tool_input={"request_context": {"work_id": "round-a", "request_id": "relay"}},
    )
    await db.append_message(
        child, 2, "assistant", "text", content="Model text with tool-shaped input",
        tool_input={"request_context": {"work_id": "round-a", "request_id": "untrusted"}},
    )
    await db.append_message(
        child, 3, "user", "text", content="Another round",
        tool_input={"request_context": {"work_id": "round-b", "request_id": "next-round"}},
    )
    scope = {"parent_session_id": parent, "user_id": "owner"}
    assert await db.get_latest_delegation_request(child, "round-a", **scope) == "relay"
    assert await db.get_latest_delegation_request(child, "round-b", **scope) == "next-round"
    assert await db.get_latest_delegation_request(child, "unknown", **scope) is None
    assert await db.get_latest_delegation_request(
        child, "round-a", parent_session_id=parent, user_id="another-owner",
    ) is None


@pytest.mark.parametrize("failure_type", [OSError, asyncio.CancelledError])
async def test_failed_snapshot_write_cannot_be_committed_by_later_update(db, monkeypatch, failure_type):
    child, parent, _agent = await _save_conversation(db)
    scope = {"parent_session_id": parent, "user_id": None}
    running = '{"state":"running"}'
    interrupted = '{"state":"interrupted"}'
    commit = db.conn.commit
    fail_next = True

    async def fail_once():
        nonlocal fail_next
        if fail_next:
            fail_next = False
            raise failure_type("SQLite commit unavailable")
        await commit()

    monkeypatch.setattr(db.conn, "commit", fail_once)
    with pytest.raises(failure_type, match="commit unavailable"):
        await db.compare_and_set_delegation_state(child, None, running, **scope)
    assert await db.load_delegation_state(child, **scope) is None

    assert await db.compare_and_set_delegation_state(child, None, interrupted, **scope)
    assert await db.load_delegation_state(child, **scope) == interrupted


async def test_snapshot_rollback_preserves_preceding_transcript_batch(db, monkeypatch, tmp_path):
    child, parent, _agent = await _save_conversation(db)
    await db.append_message(child, 0, "user", "text", content="Keep the original request")
    commit = db.conn.commit
    commits = 0

    async def fail_round_commit():
        nonlocal commits
        commits += 1
        if commits == 2:
            raise OSError("Round snapshot unavailable")
        await commit()

    monkeypatch.setattr(db.conn, "commit", fail_round_commit)
    with pytest.raises(OSError, match="snapshot unavailable"):
        await db.compare_and_set_delegation_state(
            child, None, "running", parent_session_id=parent, user_id=None,
        )
    reader = Database(str(tmp_path / "delegations.db"))
    await reader.initialize()
    try:
        assert [row["content"] for row in await reader.load_messages(child)] == ["Keep the original request"]
        assert await reader.load_delegation_states() == []
    finally:
        await reader.close()


async def test_lost_commit_acknowledgement_can_be_verified_from_committed_storage(db, monkeypatch):
    child, parent, _agent = await _save_conversation(db)
    scope = {"parent_session_id": parent, "user_id": None}
    completed = '{"state":"completed"}'
    commit = db.conn.commit

    async def acknowledge_after_error():
        await commit()
        raise OSError("Commit acknowledgement lost")

    monkeypatch.setattr(db.conn, "commit", acknowledge_after_error)
    with pytest.raises(OSError, match="acknowledgement lost"):
        await db.compare_and_set_delegation_state(child, None, completed, **scope)
    assert await db.load_delegation_state(child, **scope) == completed
    monkeypatch.setattr(db.conn, "commit", commit)


async def test_concurrent_follow_up_reservations_have_one_winner(db, tmp_path):
    child, parent, _agent = await _save_conversation(db)
    scope = {"parent_session_id": parent, "user_id": None}
    second = Database(str(tmp_path / "delegations.db"))
    await second.initialize()
    try:
        outcomes = await asyncio.gather(
            db.compare_and_set_delegation_state(child, None, "first", **scope),
            second.compare_and_set_delegation_state(child, None, "second", **scope),
        )
        assert sorted(outcomes) == [False, True]
        assert await db.load_delegation_state(child, **scope) in {"first", "second"}
    finally:
        await second.close()


@pytest.mark.parametrize(
    ("child_owner", "parent_owner", "target_owner"),
    [(None, "alice", None), ("alice", None, "alice"),
     ("alice", "alice", None), (None, None, "alice"),
     ("alice", "bob", "alice"), ("alice", "alice", "bob")],
)
async def test_foreign_or_ownerless_relationships_have_no_round_authority(
    db, child_owner, parent_owner, target_owner,
):
    await db.save_agent(
        agent_id="target", name="Target", created_at=STAMP, updated_at=STAMP,
        user_id=target_owner,
    )
    await db.save_session(
        "parent", "Parent", "/tmp", STAMP, agent_id="target", user_id=parent_owner,
    )
    await db.save_session(
        "child", "Child", "/tmp", STAMP, agent_id="target", user_id=child_owner,
        origin="delegation", parent_session_id="parent",
    )
    scope = {"parent_session_id": "parent", "user_id": child_owner}
    assert not await db.compare_and_set_delegation_state("child", None, "state", **scope)
    assert await db.load_delegation_state("child", **scope) is None
    assert await db.load_delegation_states() == []


async def test_archived_round_survives_but_deleted_parent_removes_authority(db):
    child, parent, _agent = await _save_conversation(db, owner="alice")
    scope = {"parent_session_id": parent, "user_id": "alice"}
    assert await db.compare_and_set_delegation_state(child, None, "done", **scope)
    await db.update_session_field(child, archived=True)
    assert (await db.load_delegation_states())[0]["id"] == child
    assert await db.load_delegation_state(child, **scope) == "done"
    assert await db.load_delegation_state(
        child, parent_session_id=parent, user_id=None,
    ) is None
    await db.delete_session(parent)
    assert await db.load_delegation_states() == []
    assert await db.load_delegation_state(child, **scope) is None
    assert not await db.compare_and_set_delegation_state(child, "done", "new", **scope)
    assert any(row["id"] == child for row in await db.load_sessions(include_archived=True))


async def test_ordinary_session_cannot_gain_delegation_authority(db):
    child, parent, _agent = await _save_conversation(db)
    await db.update_session_field(child, origin="fork")
    assert not await db.compare_and_set_delegation_state(
        child, None, "state", parent_session_id=parent, user_id=None,
    )
    assert await db.load_delegation_states() == []


async def test_additive_migration_leaves_previous_delegations_without_round_state(db, tmp_path):
    old_child, parent, _agent = await _save_conversation(db)
    await db.close()
    # Build an older installation at the SQLite boundary, then upgrade it.
    with sqlite3.connect(tmp_path / "delegations.db") as older:
        older.execute("ALTER TABLE sessions DROP COLUMN delegation_state")
    reopened = Database(str(tmp_path / "delegations.db"))
    await reopened.initialize()
    try:
        assert await reopened.load_delegation_states() == []
        assert await reopened.load_delegation_state(
            old_child, parent_session_id=parent, user_id=None,
        ) is None
        new_child, new_parent, _agent = await _save_conversation(reopened, suffix="-new")
        assert await reopened.compare_and_set_delegation_state(
            new_child, None, "future", parent_session_id=new_parent, user_id=None,
        )
        assert [row["id"] for row in await reopened.load_delegation_states()] == [new_child]
    finally:
        await reopened.close()
