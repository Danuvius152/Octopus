"""Public database operations share complete, isolated short transactions."""

import asyncio
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from server.database import Database


async def test_every_acknowledged_message_and_token_touch_releases_the_writer(tmp_path):
    path = tmp_path / "acknowledged.db"
    database = Database(str(path))
    await database.initialize()
    try:
        await database.save_session("s", "transcript", str(tmp_path), "2026-10-20")
        for seq in range(3):
            await database.append_message("s", seq, "user", "text", f"message {seq}")
            await database.touch_token("missing-token", at="2026-10-20")
            with sqlite3.connect(path, timeout=.02, isolation_level=None) as reader:
                assert reader.execute("SELECT count(*) FROM messages").fetchone()[0] == seq + 1
                reader.execute("BEGIN IMMEDIATE")
                reader.rollback()
    finally:
        await database.close()


async def test_close_lock_timeout_keeps_the_transaction_owner_and_connection(tmp_path):
    database = Database(str(tmp_path / "close-wait.db"))
    await database.initialize()
    try:
        async with database.transaction() as tx:
            await tx.execute("INSERT INTO invites(code,created_at,max_uses) VALUES('owner','2026-10-20',1)")
            closer = asyncio.create_task(database.close(timeout=.01))
            with pytest.raises(TimeoutError):
                await closer
        assert (await database.get_invite("owner"))["code"] == "owner"
    finally:
        await database.close()


@pytest.mark.parametrize("stage", ["before", "after", "hung"])
def test_unconfirmed_database_close_exits_without_retrying_the_connection(tmp_path, stage):
    program = """
import asyncio
import sys
from pathlib import Path
from server.database import Database

async def main():
    database = Database(sys.argv[1])
    await database.initialize()
    await database.create_invite(code="durable", created_by=None, created_at="2026-10-20")
    close = database.conn.close
    async def unconfirmed_close():
        if sys.argv[2] == "after":
            await close()
        if sys.argv[2] == "hung":
            await asyncio.Event().wait()
        raise OSError("close acknowledgment unavailable")
    database.conn.close = unconfirmed_close
    await database.close(**({"timeout": .05} if sys.argv[2] == "hung" else {}))
    Path(sys.argv[3]).touch()

asyncio.run(main())
"""
    path, success = tmp_path / "close.db", tmp_path / "closed-successfully"
    result = subprocess.run(
        [sys.executable, "-c", program, str(path), stage, str(success)],
        cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=5,
    )
    assert result.returncode == 70, result.stderr
    assert not success.exists()
    with sqlite3.connect(path) as reader:
        assert reader.execute("SELECT code FROM invites").fetchall() == [("durable",)]


async def test_concurrent_reads_and_writes_cannot_observe_or_commit_an_aborted_operation(tmp_path):
    database = Database(str(tmp_path / "transactions.db"))
    await database.initialize()
    reader = writer = None
    try:
        with pytest.raises(ValueError, match="abort this operation"):
            async with database.transaction() as tx:
                await tx.execute(
                    "INSERT INTO invites(code,created_at,max_uses) VALUES(?,?,?)",
                    ("aborted", "2026-10-20", 1),
                )
                reader = asyncio.create_task(database.get_invite("aborted"))
                writer = asyncio.create_task(database.create_invite(
                    code="survives", created_by=None, created_at="2026-10-20",
                ))
                await asyncio.sleep(0)
                assert not reader.done(), "a public read observed another operation's intermediate state"
                assert not writer.done(), "another writer could commit the incomplete operation"
                raise ValueError("abort this operation")
        assert await reader is None
        assert (await writer)["code"] == "survives"
        assert await database.get_invite("aborted") is None
    finally:
        if reader is not None and writer is not None:
            await asyncio.gather(reader, writer, return_exceptions=True)
        await database.close()


async def test_cancelling_a_lock_waiter_does_not_cancel_the_transaction_owner(tmp_path):
    database = Database(str(tmp_path / "lock-wait.db"))
    await database.initialize()
    try:
        async with database.transaction() as tx:
            await tx.execute("INSERT INTO invites(code,created_at,max_uses) VALUES('owner','2026-10-20',1)")
            reader = asyncio.create_task(database.get_invite("owner"))
            await asyncio.sleep(0)
            reader.cancel()
            with pytest.raises(asyncio.CancelledError):
                await reader
        assert (await database.get_invite("owner"))["code"] == "owner"
    finally:
        await database.close()


@pytest.mark.parametrize("stage", ["sql", "cursor", "commit"])
@pytest.mark.parametrize("cancellations", [1, 2])
def test_cancellation_during_a_short_operation_waits_for_its_known_outcome(tmp_path, stage, cancellations):
    program = """
import asyncio
import sys
from server.database import Database

async def main():
    database = Database(sys.argv[1])
    await database.initialize()
    entered = asyncio.Event()
    if sys.argv[2] == "commit":
        commit = database.conn.commit
        async def completing_commit():
            entered.set()
            await asyncio.sleep(.02)
            await commit()
        database.conn.commit = completing_commit
    else:
        execute = database.conn.execute
        async def completing_execute(sql, parameters=()):
            cursor = await execute(sql, parameters)
            if sql.startswith("INSERT INTO invites"):
                if sys.argv[2] == "sql":
                    entered.set()
                    await asyncio.sleep(.02)
                else:
                    fetchall = cursor.fetchall
                    async def completing_cursor():
                        entered.set()
                        await asyncio.sleep(.02)
                        return await fetchall()
                    cursor.fetchall = completing_cursor
            return cursor
        database.conn.execute = completing_execute
    owner = asyncio.create_task(database.create_invite(code="completed",created_by=None,created_at="2026-10-20"))
    await entered.wait()
    for _ in range(int(sys.argv[3])):
        owner.cancel("normal service shutdown")
        await asyncio.sleep(.005)
    try:
        await owner
    except asyncio.CancelledError:
        pass
    else:
        raise AssertionError("the waiter must still observe cancellation")
    assert (await database.get_invite("completed"))["code"] == "completed"
    await database.close()

asyncio.run(main())
"""
    result = subprocess.run(
        [sys.executable, "-c", program, str(tmp_path / "cancelled-operation.db"), stage, str(cancellations)],
        cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=5,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("committed", [False, True])
async def test_failed_commit_exits_without_publishing_success_and_restart_reads_the_actual_fact(tmp_path, committed):
    path = tmp_path / "commit.db"
    program = """
import asyncio
import sys
from pathlib import Path
from server.database import Database

async def main():
    database = Database(sys.argv[1])
    await database.initialize()
    commit = database.conn.commit
    async def failed_commit():
        if sys.argv[2] == "True":
            await commit()
        raise OSError("commit outcome cannot be acknowledged")
    database.conn.commit = failed_commit
    async with database.transaction() as tx:
        row = await tx.fetch_one("INSERT INTO invites(code,created_at,max_uses) VALUES('once','2026-10-20',1) RETURNING code")
        assert row[0] == "once"
    Path(sys.argv[3]).touch()

asyncio.run(main())
"""
    marker = tmp_path / "success-was-published"
    result = subprocess.run(
        [sys.executable, "-c", program, str(path), str(committed), str(marker)],
        cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=5,
    )
    assert result.returncode == 70, result.stderr
    assert not marker.exists()
    database = Database(str(path))
    await database.initialize()
    try:
        assert (await database.get_invite("once") is not None) is committed
    finally:
        await database.close()


@pytest.mark.parametrize("operation", ["commit", "rollback"])
@pytest.mark.parametrize("applied", [False, True])
@pytest.mark.parametrize("cancellations", [0, 2])
async def test_hung_commit_or_rollback_still_exits_within_the_original_deadline(tmp_path, operation, applied, cancellations):
    path = tmp_path / "hung-transaction.db"
    marker = tmp_path / "success"
    program = """
import asyncio,sys,threading,time
from pathlib import Path
from server.database import Database
async def main():
    database=Database(sys.argv[1])
    await database.initialize()
    operation=sys.argv[2]
    driver=getattr(database.conn._conn,operation)
    entered=threading.Event()
    def hung_driver():
        if sys.argv[3]=='True': driver()
        entered.set()
        time.sleep(60)
        if sys.argv[3]!='True': driver()
    async def unconfirmed():
        await database.conn._execute(hung_driver)
    setattr(database.conn,operation,unconfirmed)
    async def work():
        async with database.transaction(timeout=.15) as tx:
            await tx.execute("INSERT INTO invites(code,created_at,max_uses) VALUES('uncertain','2026-10-20',1)")
            if operation=='rollback': raise ValueError('ordinary conflict')
        Path(sys.argv[5]).touch()
    owner=asyncio.create_task(work())
    while not entered.is_set(): await asyncio.sleep(.005)
    for _ in range(int(sys.argv[4])):
        owner.cancel()
        await asyncio.sleep(.02)
    await owner
asyncio.run(main())
"""
    result = subprocess.run(
        [sys.executable, "-c", program, str(path), operation, str(applied), str(cancellations), str(marker)],
        capture_output=True, text=True, timeout=5,
    )
    assert result.returncode == 70, result.stderr
    assert not marker.exists()
    database = Database(str(path))
    await database.initialize()
    try:
        assert (await database.get_invite("uncertain") is not None) is (operation == "commit" and applied)
    finally:
        await database.close()


@pytest.mark.parametrize("stop", ["cancel", "deadline"])
def test_sql_already_running_cannot_leave_a_live_connection_after_waiter_stops(tmp_path, stop):
    program = """
import asyncio
import sys
import time
from server.database import Database

async def main():
    database = Database(sys.argv[1])
    await database.initialize()
    await database.conn.create_function("blocked_storage", 0, lambda: time.sleep(60))
    if sys.argv[2] == "cancel":
        asyncio.get_running_loop().call_later(.05, asyncio.current_task().cancel)
    try:
        async with database.transaction(timeout=.1) as tx:
            await tx.fetch_one("SELECT blocked_storage()")
    finally:
        # Ordinary close would hang behind the worker's outstanding SQL.
        await database.close()

asyncio.run(main())
"""
    result = subprocess.run(
        [sys.executable, "-c", program, str(tmp_path / "blocked.db"), stop],
        cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=5,
    )
    assert result.returncode == 70, result.stderr
