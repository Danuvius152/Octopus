"""Future delegation rounds persist through the public SQLite boundary."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

import pytest

from server.database import Database
from tests.accounts import create_account

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
    await create_account(database, extra_roots=[tmp_path])
    yield database
    await database.close()


@pytest.fixture
async def owner(db):
    return (await db.get_system_agent())["user_id"]


async def test_saved_round_survives_restart_and_rejects_stale_snapshot(db, tmp_path, owner):
    child, parent, agent = await _save_conversation(db, owner=owner)
    running = json.dumps({"work_id": "round-a", "state": "running"})
    completed = json.dumps({"work_id": "round-a", "state": "completed"})
    follow_up = json.dumps({"work_id": "round-b", "state": "running"})
    scope = {"parent_session_id": parent, "user_id": owner}

    assert await db.compare_and_set_delegation_state(child, None, running, target_agent_id=agent, **scope)
    assert await db.compare_and_set_delegation_state(child, running, completed, target_agent_id=agent, **scope)
    assert not await db.compare_and_set_delegation_state(child, running, running, target_agent_id=agent, **scope)
    assert await db.compare_and_set_delegation_state(child, completed, follow_up, target_agent_id=agent, **scope)
    assert not await db.compare_and_set_delegation_state(child, completed, completed, target_agent_id=agent, **scope)

    await db.close()
    reopened = Database(str(tmp_path / "delegations.db"))
    await reopened.initialize()
    try:
        assert await reopened.load_delegation_state(child, **scope) == follow_up
        assert await reopened.load_delegation_states() == [{
            "id": child, "parent_session_id": parent, "user_id": owner,
            "agent_id": agent, "delegation_state": follow_up,
        }]
    finally:
        await reopened.close()


@pytest.mark.parametrize("owner", [None, "alice"])
async def test_same_owner_rebinding_cannot_commit_an_original_targets_round(db, tmp_path, owner):
    child, parent, target = await _save_conversation(db, owner=owner)
    await db.save_agent(
        agent_id="replacement", name="Replacement", created_at=STAMP,
        updated_at=STAMP, user_id=owner,
    )
    scope = {"parent_session_id": parent, "user_id": owner}
    running = '{"work_id":"round-a","state":"running"}'
    completed = '{"work_id":"round-a","state":"completed"}'
    assert await db.compare_and_set_delegation_state(child, None, running, target_agent_id=target, **scope)

    # A different writer changes only the target, preserving owner and snapshot.
    with sqlite3.connect(tmp_path / "delegations.db") as external:
        external.execute("UPDATE sessions SET agent_id = ? WHERE id = ?", ("replacement", child))
    assert (await db.get_session_binding(child))["agent_id"] != target

    assert not await db.compare_and_set_delegation_state(child, running, completed, target_agent_id=target, **scope)
    assert await db.load_delegation_state(child, **scope) == running
    assert await db.compare_and_set_delegation_state(
        child, running, completed, target_agent_id="replacement", **scope,
    )


async def test_reparenting_cannot_commit_an_original_parents_round(db, tmp_path):
    child, parent, target = await _save_conversation(db, owner="alice")
    await db.save_session("another-parent", "Another parent", "/tmp", STAMP, agent_id=target, user_id="alice")
    scope = {"parent_session_id": parent, "user_id": "alice"}
    running = '{"work_id":"round-a","state":"running"}'
    assert await db.compare_and_set_delegation_state(child, None, running, target_agent_id=target, **scope)
    with sqlite3.connect(tmp_path / "delegations.db") as external:
        external.execute("UPDATE sessions SET parent_session_id = ? WHERE id = ?", ("another-parent", child))

    assert not await db.compare_and_set_delegation_state(child, running, "wrong-parent", target_agent_id=target, **scope)
    assert await db.load_delegation_state(child, **scope) is None
    assert await db.load_delegation_state(child, parent_session_id="another-parent", user_id="alice") == running


async def test_transcript_order_cannot_prove_return_consumption(db):
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
    for work in ("round-a", "round-b", "unknown"):
        remaining, ready = await db.delegation_work_consumption(child, work, {"helper-work"}, "relay")
        assert remaining == {"helper-work"} and not ready


async def test_one_execution_consumes_multiple_owned_returns_only_after_confirmed_delivery(db, owner):
    child, _parent, _agent = await _save_conversation(db, owner=owner)
    for index, request in enumerate(("initial", "return-a", "return-b")):
        created, _ = await db.admit_client_submission(child, request, request, request,
            payload={"source": "delegation", "work_id": "round-a", "order": index,
                     "delivery_id": None if index == 0 else f"agent-terminal:leaf-{index}"}, receipt={})
        assert created
        assert await db.claim_client_submission(child, request, "initial", "same-execution")
        if index:
            assert await db.set_steer_delivery(child, request, confirmed=False)
    await db.record_terminal(session_id=child, request_id="initial", seq=0,
        role="system", type="result", created_at=STAMP,
        content={"kind": "outcome", "request_id": "initial", "status": "success"})
    children = {"leaf-1", "leaf-2"}
    assert await db.delegation_work_consumption(child, "round-a", children, "initial") == (children, False)
    await db.set_steer_delivery(child, "return-a", confirmed=True)
    assert await db.delegation_work_consumption(child, "round-a", children, "initial") == ({"leaf-2"}, False)
    await db.set_steer_delivery(child, "return-b", confirmed=True)
    assert await db.delegation_work_consumption(child, "round-a", children, "unrelated-success") == (set(), False)
    assert await db.delegation_work_consumption(child, "round-a", children, "initial") == (set(), True)


async def test_concurrent_follow_up_reservations_have_one_winner(db, tmp_path, owner):
    child, parent, agent = await _save_conversation(db, owner=owner)
    scope = {"parent_session_id": parent, "user_id": owner}
    second = Database(str(tmp_path / "delegations.db"))
    await second.initialize()
    try:
        outcomes = await asyncio.gather(
            db.compare_and_set_delegation_state(child, None, "first", target_agent_id=agent, **scope),
            second.compare_and_set_delegation_state(child, None, "second", target_agent_id=agent, **scope),
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
    assert not await db.compare_and_set_delegation_state("child", None, "state", target_agent_id="target", **scope)
    assert await db.load_delegation_state("child", **scope) is None
    assert await db.load_delegation_states() == []


async def test_archived_round_survives_but_deleted_parent_removes_authority(db):
    child, parent, agent = await _save_conversation(db, owner="alice")
    scope = {"parent_session_id": parent, "user_id": "alice"}
    assert await db.compare_and_set_delegation_state(child, None, "done", target_agent_id=agent, **scope)
    await db.update_session_field(child, archived=True)
    assert (await db.load_delegation_states())[0]["id"] == child
    assert await db.load_delegation_state(child, **scope) == "done"
    assert await db.load_delegation_state(
        child, parent_session_id=parent, user_id=None,
    ) is None
    await db.delete_session(parent)
    assert await db.load_delegation_states() == []
    assert await db.load_delegation_state(child, **scope) is None
    assert not await db.compare_and_set_delegation_state(child, "done", "new", target_agent_id=agent, **scope)
    assert any(row["id"] == child for row in await db.load_sessions(include_archived=True))


async def test_deleted_child_cannot_reserve_another_round(db, owner):
    child, parent, agent = await _save_conversation(db, owner=owner)
    scope = {"parent_session_id": parent, "user_id": owner}
    running = '{"work_id":"round-a","state":"running"}'
    assert await db.compare_and_set_delegation_state(child, None, running, target_agent_id=agent, **scope)
    await db.delete_session(child)

    assert not await db.compare_and_set_delegation_state(child, running, "new-round", target_agent_id=agent, **scope)
    assert await db.load_delegation_state(child, **scope) is None
    assert await db.load_delegation_states() == []
    assert (await db.get_session_binding(parent))["id"] == parent


async def test_ordinary_session_cannot_gain_delegation_authority(db, owner):
    child, parent, agent = await _save_conversation(db, owner=owner)
    await db.update_session_field(child, origin="fork")
    assert not await db.compare_and_set_delegation_state(
        child, None, "state", parent_session_id=parent, user_id=owner, target_agent_id=agent,
    )
    assert await db.load_delegation_states() == []


async def test_current_reopen_keeps_roundless_conversations_and_new_round_authority(db, tmp_path, owner):
    old_child, parent, _agent = await _save_conversation(db, owner=owner)
    await db.close()
    reopened = Database(str(tmp_path / "delegations.db"))
    await reopened.initialize()
    try:
        assert await reopened.load_delegation_states() == []
        assert await reopened.load_delegation_state(
            old_child, parent_session_id=parent, user_id=owner,
        ) is None
        new_child, new_parent, agent = await _save_conversation(reopened, owner=owner, suffix="-new")
        assert await reopened.compare_and_set_delegation_state(
            new_child, None, "future", parent_session_id=new_parent, user_id=owner, target_agent_id=agent,
        )
        assert [row["id"] for row in await reopened.load_delegation_states()] == [new_child]
    finally:
        await reopened.close()
