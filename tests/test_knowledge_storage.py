"""Request contracts survive the existing persistence seams unchanged."""

from __future__ import annotations

import asyncio
import copy
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from server.database import Database

CREATED_AT = "2026-10-19T12:00:00+00:00"
CONTEXT = '{"version":1,"return_binding":{"request_id":"parent-read"}}'
REQUEST_CONTEXT = {
    "request_id": "request-one", "source": "human", "work_id": None,
    "knowledge": {"version": 1, "source_intent": ["local", "web"]},
}


@pytest.fixture
async def database(tmp_path):
    db = Database(str(tmp_path / "knowledge.db"))
    await db.initialize()
    await db.save_session("parent", "Parent", "/tmp", CREATED_AT)
    try:
        yield db
    finally:
        await db.close()


async def test_background_job_retains_opaque_context_through_completion(database):
    await database.create_bg_task(
        "bg-one", "parent", "true", None, "/tmp", CREATED_AT,
        work_id="parent-work", knowledge_context=CONTEXT,
    )
    await database.update_bg_task("bg-one", status="completed", exit_code=0)
    job = await database.get_bg_task("bg-one")
    assert job["knowledge_context"] == CONTEXT
    assert job["work_id"] == "parent-work"


async def test_research_job_retains_context_through_restart_settlement(database):
    await database.create_research_job(
        "research-one", "parent", "Why?", CREATED_AT, knowledge_context=CONTEXT,
    )
    assert await database.mark_in_flight_research_jobs_interrupted(CREATED_AT) == 1
    job = await database.get_research_job("research-one")
    assert job["knowledge_context"] == CONTEXT
    assert job["status"] == "interrupted"
    assert job["injection_status"] == "pending"


async def test_schedule_keeps_definition_context_when_prompt_is_edited(database):
    agent = await database.get_system_agent()
    await database.save_schedule(
        "schedule-one", agent["id"], "Daily", "Read X", CREATED_AT,
        interval_seconds=600, wiki_mode="read", knowledge_context=CONTEXT,
    )
    await database.update_schedule("schedule-one", prompt="Read Y")
    schedule = (await database.load_schedules())[0]
    assert schedule["knowledge_context"] == CONTEXT
    assert schedule["wiki_mode"] == "read"


async def test_request_input_and_context_are_committed_before_return(database, tmp_path):
    reader = Database(str(tmp_path / "knowledge.db"))
    await reader.initialize()
    try:
        await database.record_request(
            request_id="request-one", session_id="parent", seq=0,
            role="user", type="text", content="/wiki Why?",
            tool_input={"request_context": REQUEST_CONTEXT},
        )
        messages = await reader.load_messages("parent")
        assert len(messages) == 1
        assert messages[0]["content"] == "/wiki Why?"
        assert messages[0]["tool_input"] == {"request_context": REQUEST_CONTEXT}
    finally:
        await reader.close()


@pytest.mark.parametrize("change", ["sequence", "identity", "knowledge", "content", "role"])
async def test_request_retry_never_reuses_a_different_input(database, change):
    message = {
        "session_id": "parent", "seq": 0, "role": "user", "type": "text",
        "content": "/wiki Why?", "tool_input": {"request_context": REQUEST_CONTEXT},
    }
    await database.record_request(request_id="request-one", **message)
    await database.record_request(request_id="request-one", **message)
    original = (await database.load_messages("parent"))[0]
    changed = copy.deepcopy(message)
    request_id = "request-one"
    if change == "sequence":
        changed["seq"] = 1
    elif change == "identity":
        request_id = "request-two"
        changed["tool_input"]["request_context"]["request_id"] = request_id
    elif change == "knowledge":
        changed["tool_input"]["request_context"]["knowledge"]["source_intent"] = ["web"]
    else:
        changed[change] = "changed"
    with pytest.raises(ValueError, match="conflict"):
        await database.record_request(request_id=request_id, **changed)
    assert await database.load_messages("parent") == [original]


@pytest.mark.parametrize("failure", ["before_commit", "no_commit", "lost_acknowledgement"])
async def test_request_commit_failure_is_not_success_and_retry_keeps_one_input(
    tmp_path, failure,
):
    program = """
import asyncio
import sys
import time
from pathlib import Path
from server.database import Database
from tests.test_knowledge_storage import CREATED_AT, REQUEST_CONTEXT

async def main():
    database = Database(sys.argv[1])
    await database.initialize()
    await database.save_session("parent", "Parent", "/tmp", CREATED_AT)
    driver_commit = database.conn._conn.commit
    def unconfirmed_driver_commit():
        if sys.argv[2] == "lost_acknowledgement":
            driver_commit()
        if sys.argv[2] == "no_commit":
            time.sleep(60)
            driver_commit()
        raise OSError("original commit acknowledgement unavailable")
    async def failed_commit():
        await database.conn._execute(unconfirmed_driver_commit)
    database.conn.commit = failed_commit
    await database.record_request(
        request_id="request-one", session_id="parent", seq=0,
        role="user", type="text", content="/wiki Why?",
        tool_input={"request_context": REQUEST_CONTEXT},
    )
    Path(sys.argv[3]).touch()

asyncio.run(main())
"""
    path, success = tmp_path / "knowledge.db", tmp_path / "returned-success"
    result = subprocess.run(
        [sys.executable, "-c", program, str(path), failure, str(success)],
        cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=8,
    )
    assert result.returncode == 70, result.stderr
    assert not success.exists(), "An unconfirmed commit returned successful acknowledgement"
    message = {
        "request_id": "request-one", "session_id": "parent", "seq": 0,
        "role": "user", "type": "text", "content": "/wiki Why?",
        "tool_input": {"request_context": REQUEST_CONTEXT},
    }
    for reopening in range(2):
        reader = Database(str(path))
        await reader.initialize()
        try:
            committed = await reader.load_messages("parent")
            expected = 1 if reopening or failure == "lost_acknowledgement" else 0
            assert len(committed) == expected
            # This is an explicit same-identity retry after cold reopening,
            # never a continuation on the connection that lost trust.
            await reader.record_request(**message)
            messages = await reader.load_messages("parent")
            assert len(messages) == 1
            assert messages[0]["content"] == "/wiki Why?"
            assert messages[0]["tool_input"]["request_context"] == REQUEST_CONTEXT
            assert await reader.get_committed_terminal("parent", "request-one") is None
        finally:
            await reader.close()


async def test_request_waits_for_its_original_commit_acknowledgement(database, monkeypatch, tmp_path):
    reader = Database(str(tmp_path / "knowledge.db"))
    await reader.initialize()
    committing, release = asyncio.Event(), asyncio.Event()
    commit = database.conn.commit

    async def paused_commit():
        committing.set()
        await release.wait()
        await commit()

    monkeypatch.setattr(database.conn, "commit", paused_commit)
    message = {
        "request_id": "request-one", "session_id": "parent", "seq": 0,
        "role": "user", "type": "text", "content": "/wiki Why?",
        "tool_input": {"request_context": REQUEST_CONTEXT},
    }
    pending = asyncio.create_task(database.record_request(**message))
    try:
        await asyncio.wait_for(committing.wait(), 2)
        assert not pending.done()
        assert await reader.load_messages("parent") == []
        release.set()
        await pending
        messages = await reader.load_messages("parent")
        assert len(messages) == 1
        assert messages[0]["tool_input"]["request_context"] == REQUEST_CONTEXT
    finally:
        release.set()
        await asyncio.gather(pending, return_exceptions=True)
        await reader.close()


async def test_additive_migration_preserves_legacy_rows_and_does_not_replay(tmp_path):
    path = str(tmp_path / "legacy.db")
    legacy = Database(path)
    await legacy.initialize()
    try:
        await legacy.save_session("parent", "Parent", "/tmp", CREATED_AT)
        agent = await legacy.get_system_agent()
        await legacy.create_bg_task("bg-old", "parent", "true", None, "/tmp", CREATED_AT)
        await legacy.create_research_job("research-old", "parent", "Why?", CREATED_AT)
        await legacy.save_schedule(
            "schedule-old", agent["id"], "Old read", "/wiki Why?", CREATED_AT,
            interval_seconds=600, wiki_mode="read",
        )
    finally:
        await legacy.close()
    # Emulate the former on-disk schema at the storage boundary. Verification
    # below uses only the public Database readers and startup settlement API.
    with sqlite3.connect(path) as disk:
        for table in ("bg_tasks", "research_jobs", "schedules"):
            disk.execute(f"ALTER TABLE {table} DROP COLUMN knowledge_context")

    upgraded = Database(path)
    await upgraded.initialize()
    try:
        bg = await upgraded.get_bg_task("bg-old")
        research = await upgraded.get_research_job("research-old")
        schedule = (await upgraded.load_schedules())[0]
        assert bg["knowledge_context"] is None
        assert research["knowledge_context"] is None
        assert schedule["knowledge_context"] is None
        assert schedule["wiki_mode"] == "read"
        assert await upgraded.mark_in_flight_bg_tasks_interrupted(CREATED_AT) == 1
        assert await upgraded.mark_in_flight_research_jobs_interrupted(CREATED_AT) == 1
        await upgraded.update_schedule("schedule-old", knowledge_context=CONTEXT)
        await upgraded.update_bg_task("bg-old", knowledge_context=CONTEXT)
        await upgraded.update_research_job("research-old", knowledge_context=CONTEXT)
    finally:
        await upgraded.close()

    reopened = Database(path)
    await reopened.initialize()
    try:
        assert (await reopened.get_bg_task("bg-old"))["knowledge_context"] == CONTEXT
        assert (await reopened.get_research_job("research-old"))["knowledge_context"] == CONTEXT
        assert (await reopened.load_schedules())[0]["knowledge_context"] == CONTEXT
        assert await reopened.mark_in_flight_bg_tasks_interrupted(CREATED_AT) == 0
        assert await reopened.mark_in_flight_research_jobs_interrupted(CREATED_AT) == 0
    finally:
        await reopened.close()


@pytest.mark.parametrize("tool_input", [None, {}, {"request_context": {"request_id": "other"}}])
async def test_request_rejects_missing_or_inconsistent_context(database, tool_input):
    with pytest.raises(ValueError, match="context identity"):
        await database.record_request(
            request_id="request-one", session_id="parent", seq=0,
            role="user", type="text", tool_input=tool_input,
        )
    assert await database.count_messages("parent") == 0


async def test_in_memory_request_confirms_commit_without_claiming_disk_durability():
    db = Database(":memory:")
    await db.initialize()
    try:
        await db.save_session("parent", "Parent", "/tmp", CREATED_AT)
        await db.record_request(
            request_id="request-one", session_id="parent", seq=0,
            role="user", type="text", tool_input={"request_context": REQUEST_CONTEXT},
        )
        assert await db.count_messages("parent") == 1
    finally:
        await db.close()


async def test_request_context_boolean_and_number_are_not_interchangeable(database):
    message = {
        "request_id": "request-one", "session_id": "parent", "seq": 0,
        "role": "user", "type": "text",
        "tool_input": {"request_context": copy.deepcopy(REQUEST_CONTEXT)},
    }
    message["tool_input"]["request_context"]["knowledge"]["may_submit"] = False
    await database.record_request(**message)
    message["tool_input"]["request_context"]["knowledge"]["may_submit"] = 0
    with pytest.raises(ValueError, match="conflict"):
        await database.record_request(**message)
