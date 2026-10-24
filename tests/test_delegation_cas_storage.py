"""Delegation CAS storage failures stop the process before any online recovery."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from server.database import Database

RUNNING = '{"work_id":"round-original","state":"running"}'
COMPLETED = '{"work_id":"round-original","state":"completed"}'

_UNCONFIRMED_FINISH = """
import asyncio
import sqlite3
import sys
from pathlib import Path
from server.database import Database
from server.users import UserManager

async def main():
    database = Database(sys.argv[1])
    await database.initialize()
    owner = await UserManager(database).create_user(username="storage-owner", password="storage-password")
    stamp = "2026-10-21T00:00:00+00:00"
    await database.save_agent(
        agent_id="target", name="Target", created_at=stamp, updated_at=stamp,
        user_id=owner["id"],
    )
    await database.save_session("parent", "Parent", "/tmp", stamp, agent_id="target", user_id=owner["id"])
    await database.save_session(
        "child", "Child", "/tmp", stamp, agent_id="target",
        origin="delegation", parent_session_id="parent", user_id=owner["id"],
    )
    await database.append_message(
        "child", 0, "user", "text", "Keep the original request",
        tool_input={"request_context": {
            "work_id": "round-original", "request_id": "request-original",
        }},
    )
    await database.append_message(
        "child", 1, "assistant", "text", "Keep the acknowledged child output",
    )
    running = '{"work_id":"round-original","state":"running"}'
    completed = '{"work_id":"round-original","state":"completed"}'
    previous = running if sys.argv[5] == "True" else None
    scope = {"parent_session_id": "parent", "user_id": owner["id"], "target_agent_id": "target"}
    if previous is not None:
        assert await database.compare_and_set_delegation_state("child", None, running, **scope)

    operation, applied, failure = sys.argv[2:5]
    finish = getattr(database.conn, operation)
    async def unconfirmed_finish():
        if applied == "True":
            await finish()
        if failure == "cancel":
            raise asyncio.CancelledError("transaction-end driver acknowledgement cancelled")
        if failure == "corrupt":
            raise sqlite3.DatabaseError("database disk image is malformed")
        raise OSError("transaction-end driver acknowledgement lost")
    setattr(database.conn, operation, unconfirmed_finish)

    if operation == "rollback":
        execute = database.conn.execute
        async def reject_after_round_write(sql, parameters=()):
            cursor = await execute(sql, parameters)
            if sql.startswith("UPDATE sessions SET delegation_state = "):
                await cursor.close()
                # A real SQLite domain error after the CAS SQL needs rollback.
                await execute(
                    "INSERT INTO sessions(id,name,working_dir,created_at) VALUES(?,?,?,?)",
                    ("child", "duplicate", "/tmp", stamp),
                )
            return cursor
        database.conn.execute = reject_after_round_write

    trace_path = Path(sys.argv[7])
    def record(sql):
        with trace_path.open("a") as stream:
            stream.write(sql + "\\n")
    await database.conn.set_trace_callback(record)

    try:
        await database.compare_and_set_delegation_state("child", previous, completed, **scope)
    except BaseException:
        Path(sys.argv[6]).write_text("storage error returned to its caller")
    else:
        Path(sys.argv[6]).write_text("unconfirmed storage success was published")
    # Neither reads nor a second write may run in the untrusted generation.
    await database.load_delegation_state("child", parent_session_id="parent", user_id=owner["id"])
    await database.compare_and_set_delegation_state("child", previous, completed, **scope)
    await database.close()

asyncio.run(main())
"""


@pytest.mark.parametrize("operation", ["commit", "rollback"])
@pytest.mark.parametrize("applied", [False, True])
@pytest.mark.parametrize("failure", ["io", "cancel", "corrupt"])
@pytest.mark.parametrize("has_snapshot", [False, True])
async def test_unconfirmed_round_finish_exits_and_cold_reopen_preserves_actual_facts(
    tmp_path, operation, applied, failure, has_snapshot,
):
    path = tmp_path / "round.db"
    continued = tmp_path / "continued-in-failed-generation"
    trace = tmp_path / "driver-sql.log"
    result = subprocess.run(
        [sys.executable, "-c", _UNCONFIRMED_FINISH, str(path), operation,
         str(applied), failure, str(has_snapshot), str(continued), str(trace)],
        cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=5,
    )
    assert result.returncode == 70, result.stderr
    assert not continued.exists()
    statements = trace.read_text().splitlines()
    assert sum(sql.startswith("UPDATE sessions SET delegation_state = ") for sql in statements) == 1
    assert not any(sql.startswith("SELECT") for sql in statements), "the failed generation read storage back"
    assert statements.count("COMMIT") == int(operation == "commit" and applied)
    assert statements.count("ROLLBACK") == int(operation == "rollback" and applied)

    previous = RUNNING if has_snapshot else None
    expected = COMPLETED if operation == "commit" and applied else previous
    # Two cold lifecycles must retain the same snapshot, work and transcript.
    for _ in range(2):
        reopened = Database(str(path))
        await reopened.initialize()
        try:
            owner = await reopened.get_user_by_username("storage-owner")
            assert owner is not None
            scope = {"parent_session_id": "parent", "user_id": owner["id"]}
            stored = await reopened.load_delegation_state("child", **scope)
            assert stored == expected
            assert await reopened.load_delegation_states() == ([] if expected is None else [{
                "id": "child", "parent_session_id": "parent", "user_id": owner["id"],
                "agent_id": "target", "delegation_state": expected,
            }])
            if stored is not None:
                assert json.loads(stored)["work_id"] == "round-original"
            messages = await reopened.load_messages("child")
            assert [(row["seq"], row["role"], row["content"]) for row in messages] == [
                (0, "user", "Keep the original request"),
                (1, "assistant", "Keep the acknowledged child output"),
            ]
            assert messages[0]["tool_input"] == {"request_context": {
                "work_id": "round-original", "request_id": "request-original",
            }}
            assert await reopened.get_latest_delegation_request("child", "round-original", **scope) == "request-original"
        finally:
            await reopened.close()


@pytest.mark.parametrize("stage", ["before", "after"])
async def test_cancelling_cas_waiter_keeps_a_confirmed_commit_and_its_original_work(tmp_path, stage):
    path = tmp_path / "cancelled-waiter.db"
    program = """
import asyncio
import sys
from server.database import Database
from server.users import UserManager

async def main():
    database = Database(sys.argv[1])
    await database.initialize()
    owner = await UserManager(database).create_user(username="storage-owner", password="storage-password")
    stamp = "2026-10-21T00:00:00+00:00"
    await database.save_agent(agent_id="target", name="Target", created_at=stamp, updated_at=stamp, user_id=owner["id"])
    await database.save_session("parent", "Parent", "/tmp", stamp, agent_id="target", user_id=owner["id"])
    await database.save_session(
        "child", "Child", "/tmp", stamp, agent_id="target",
        origin="delegation", parent_session_id="parent", user_id=owner["id"],
    )
    await database.append_message("child", 0, "user", "text", "Original request")
    commit = database.conn.commit
    entered, release = asyncio.Event(), asyncio.Event()
    async def confirmed_commit():
        if sys.argv[2] == "after":
            await commit()
        entered.set()
        await release.wait()
        if sys.argv[2] == "before":
            await commit()
    database.conn.commit = confirmed_commit
    command = asyncio.create_task(database.compare_and_set_delegation_state(
        "child", None, '{"work_id":"round-original","state":"running"}',
        parent_session_id="parent", user_id=owner["id"], target_agent_id="target",
    ))
    await entered.wait()
    command.cancel("first caller cancellation")
    await asyncio.sleep(0)
    command.cancel("repeated caller cancellation")
    release.set()
    try:
        await command
    except asyncio.CancelledError:
        pass
    else:
        raise AssertionError("the caller must observe its cancellation")
    database.conn.commit = commit
    await database.close()

asyncio.run(main())
"""
    result = subprocess.run(
        [sys.executable, "-c", program, str(path), stage],
        cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=5,
    )
    assert result.returncode == 0, result.stderr
    reopened = Database(str(path))
    await reopened.initialize()
    try:
        owner = await reopened.get_user_by_username("storage-owner")
        assert owner is not None
        assert await reopened.load_delegation_state("child", parent_session_id="parent", user_id=owner["id"]) == RUNNING
        assert [row["content"] for row in await reopened.load_messages("child")] == ["Original request"]
    finally:
        await reopened.close()
