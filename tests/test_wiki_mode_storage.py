"""Current read-only schedules preserve their source contracts after reopening."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from contextlib import closing
from pathlib import Path

import aiosqlite
import pytest

from server.database import Database
from server.knowledge import decode_context
from server.mcp_identity import McpScope
from server.scheduler import ScheduleRunner
from server.session_manager import SessionManager
from server.wiki.store import WikiError
from server.wiki.tasks import tasks
from tests.accounts import create_account
from tests.test_scheduler import capture_model_runs

STAMP = "2026-10-19T12:00:00+00:00"


def _facts(path: Path) -> dict[str, list[dict]]:
    with closing(sqlite3.connect(path)) as reader:
        reader.row_factory = sqlite3.Row
        tables = [row[0] for row in reader.execute(
            "SELECT name FROM sqlite_schema WHERE type='table' AND name NOT GLOB 'sqlite_*' ORDER BY name",
        )]
        return {table: [dict(row) for row in reader.execute(f'SELECT * FROM "{table}" ORDER BY rowid')]
                for table in tables}


async def _current_database(tmp_path: Path) -> tuple[Path, str]:
    path = tmp_path / "main.db"
    database = Database(str(path))
    await database.initialize()
    try:
        owner, _ = await create_account(database, extra_roots=[tmp_path])
        agent = await database.get_system_agent(owner["id"])
        for sid in ("source-original", "execution-target"):
            await database.save_session(sid, sid, str(tmp_path), STAMP,
                                        agent_id=agent["id"], user_id=owner["id"])
        context = json.dumps({"value": {
            "origin": {"user_id": owner["id"], "request_id": "trusted-source-request", "session_id": "source-original"},
            "trusted_request": "/wiki Compare sources; preserve 中文 and use local notes only.",
            "mode": "read", "sources": ["wiki", "web"],
            "maintenance": {"may_submit": False, "required_selections": []},
            "registration": ["original-vault", "original-registration"],
            "task_requirements": ["Use Library/Original.md and report uncertainty."],
        }, "version": 1}, ensure_ascii=False, indent=2) + "\n"
        await database.save_schedule(
            "old-off", agent["id"], "Original reminder", "/wiki maintain Library/Original.md", STAMP,
            interval_seconds=3600, origin_session_id="execution-target",
            wiki_mode="read", knowledge_context=context,
        )
    finally:
        await database.close()
    return path, context


async def test_empty_storage_and_current_reopen_share_current_format_without_repreparing(tmp_path, monkeypatch):
    path = tmp_path / "empty.db"
    database = Database(str(path))
    await database.initialize()
    await database.close()
    with closing(sqlite3.connect(path)) as reader:
        assert reader.execute("PRAGMA user_version").fetchone() == (8,)
    before = _facts(path)
    statements = []
    connect = aiosqlite.Connection._connect

    async def traced_connect(connection):
        await connect(connection)
        await connection.set_trace_callback(statements.append)
        return connection

    monkeypatch.setattr(aiosqlite.Connection, "_connect", traced_connect)
    reopened = Database(str(path))
    await reopened.initialize()
    await reopened.close()
    assert not [sql for sql in statements if sql.lstrip().upper().startswith(
        ("CREATE", "ALTER", "DROP", "INSERT", "UPDATE", "DELETE", "PRAGMA USER_VERSION="),
    )]
    assert _facts(path) == before


async def test_prepared_schedule_executes_its_source_contract_as_read_only(tmp_path, monkeypatch):
    path, original_context = await _current_database(tmp_path)
    database = Database(str(path))
    await database.initialize()
    manager = SessionManager()
    await manager.initialize(database)
    runner = ScheduleRunner(manager, database)
    runs = capture_model_runs(manager, monkeypatch, hold=True)
    try:
        await runner.initialize(paused=True)
        assert runner.next_run_at("old-off") is not None
        await runner._fire("old-off")
        async with asyncio.timeout(5):
            while not runs:
                await asyncio.sleep(0.01)
        session = manager.get_session("execution-target")
        scope = McpScope(session.id, user_id=session.user_id, wiki_task_id=session._run.wiki_task_id)
        task = tasks.require(scope)
        assert task.mode == "read" and not tasks.may_submit(task)
        with pytest.raises(WikiError, match="read-only"):
            tasks.require(scope, write=True)
        messages = await database.load_messages(session.id)
        fired = next(message for message in messages if message["role"] == "user")
        contract = decode_context(json.dumps(fired["tool_input"]["request_context"]["knowledge"]))
        from dataclasses import replace
        assert contract == replace(decode_context(original_context), interaction_policy="unattended")
        assert contract.origin.session_id == "source-original"
        assert contract.origin.request_id == "trusted-source-request"
        assert fired["tool_input"]["request_context"]["source"] == "schedule"
        assert runs[0].prompt.endswith("/wiki maintain Library/Original.md")
        runs[0].release.set()
        await manager.await_session_execution(session.id, timeout=5)
        retained = await database.get_schedule_definition("old-off")
        assert retained["enabled"] is True and retained["wiki_mode"] == "read"
        assert retained["knowledge_context"] == original_context
    finally:
        for run in runs:
            run.release.set()
        await runner.shutdown()
        await database.close()
