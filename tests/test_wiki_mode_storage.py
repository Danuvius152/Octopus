"""Finite mode preparation preserves current inputs and their execution facts."""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path

import aiosqlite
import pytest

from server.database import Database
from server.db.schema import UnsupportedMainStorage, prepare_main_storage
from server.db.transaction import Transaction
from server.knowledge import decode_context, encode
from server.mcp_identity import McpScope
from server.scheduler import ScheduleRunner
from server.service_health import ServiceHealth
from server.session_manager import SessionManager
from server.sessions.client_submissions import input_digest
from server.wiki.store import WikiError
from server.wiki.tasks import tasks
from tests.accounts import create_account
from tests.main_storage import write_unversioned_baseline, write_version_one_baseline
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


async def _historical_database(tmp_path: Path, *, version: int = 2, source: str = "session") -> tuple[Path, dict]:
    path = tmp_path / "main.db"
    database = Database(str(path))
    await database.initialize()
    try:
        owner, _ = await create_account(database, extra_roots=[tmp_path])
        agent = await database.get_system_agent(owner["id"])
        for sid in ("source-original", "execution-target"):
            await database.save_session(sid, sid, str(tmp_path), STAMP,
                                        agent_id=agent["id"], user_id=owner["id"])
        source_context = {"value": {
            "origin": {"user_id": owner["id"], "request_id": "trusted-source-request", "session_id": "source-original"},
            "trusted_request": "/wiki Compare sources; preserve 中文 and use local notes only.",
            "mode": "read", "sources": ["wiki", "web"],
            "maintenance": {"may_submit": False, "required_selections": []},
            "registration": ["original-vault", "original-registration"],
            "task_requirements": ["Use Library/Original.md and report uncertainty."],
        }, "version": 1}
        context = json.loads(json.dumps(source_context))
        if source == "definition":
            context["value"]["origin"].update(session_id="definition:original-schedule", request_id="definition-request")
        context = json.dumps(context, ensure_ascii=False, indent=2) + "\n"
        await database.save_schedule(
            "old-off", agent["id"], "Original reminder", "/wiki maintain Library/Original.md", STAMP,
            interval_seconds=3600, origin_session_id="execution-target",
            wiki_mode="off" if version < 3 else "read", knowledge_context=context,
        )
        source_contract = decode_context(json.dumps(source_context))
        assert source_contract is not None
        source_payload = {
            "content": source_context["value"]["trusted_request"], "attachment_ids": [], "attachments": [],
            "wiki_mode": "read", "knowledge": encode(source_contract),
            "trusted_request": source_context["value"]["trusted_request"], "work_id": "original-work", "source": "human",
        }
        recovered_context = json.loads(json.dumps(source_context))
        recovered_context["value"]["origin"]["request_id"] = "recovered-request"
        recovered_contract = decode_context(json.dumps(recovered_context))
        assert recovered_contract is not None
        recovered_payload = {
            **source_payload, "knowledge": encode(recovered_contract),
            "work_id": "recovered-work", "recovery_of_client_id": "original-input",
        }
        unrelated_context = json.loads(json.dumps(source_context))
        unrelated_context["value"]["origin"].update(session_id="execution-target", request_id="unrelated-request")
        unrelated_context["value"]["mode"] = "auto"
        unrelated_context["value"]["maintenance"]["may_submit"] = True
        unrelated_contract = decode_context(json.dumps(unrelated_context))
        assert unrelated_contract is not None
        unrelated_payload = {
            **source_payload, "wiki_mode": "auto", "knowledge": encode(unrelated_contract), "work_id": "unrelated-work",
        }
        receipt = '{ "accepted": true, "request_id": "trusted-source-request", "disposition": "accepted" }'
        async with database.transaction() as tx:
            await tx.execute(
                "INSERT INTO client_submissions(session_id,client_id,payload_sha256,request_id,payload,receipt,"
                "execution_state,execution_owner,execution_generation,delivery_state,recovery_of) "
                "VALUES(?,?,?,?,?,?,'withdrawn',?,?,'unknown',?)",
                ("source-original", "original-input", input_digest(source_payload), "trusted-source-request",
                 json.dumps(source_payload, ensure_ascii=False, indent=1),
                 receipt, "original-owner", "original-generation", None),
            )
            await tx.execute(
                "INSERT INTO client_submissions(session_id,client_id,payload_sha256,request_id,payload,receipt,"
                "execution_state,recovery_of) VALUES(?,?,?,?,?,?,'withdrawn',?)",
                ("source-original", "recovered-input", input_digest(recovered_payload), "recovered-request",
                 json.dumps(recovered_payload, ensure_ascii=False, indent=1),
                 '{"accepted":true,"request_id":"recovered-request"}', "original-input"),
            )
            await tx.execute(
                "INSERT INTO client_submissions(session_id,client_id,payload_sha256,request_id,payload,receipt,"
                "execution_state) VALUES(?,?,?,?,?,?,'withdrawn')",
                ("execution-target", "unrelated-input", input_digest(unrelated_payload), "unrelated-request",
                 json.dumps(unrelated_payload, ensure_ascii=False, indent=3),
                 '{ "accepted": true, "request_id": "unrelated-request" }'),
            )
        await database.append_message("source-original", 4, "user", "text", "Original historical input 中文")
    finally:
        await database.close()
    original = _facts(path)
    if version == 0:
        write_unversioned_baseline(path)
    elif version == 1:
        write_version_one_baseline(path)
    else:
        with closing(sqlite3.connect(path)) as writer:
            writer.execute(f"PRAGMA user_version={version}")
            writer.commit()
    return path, original


@pytest.mark.parametrize("version", [0, 1, 2, 3])
@pytest.mark.parametrize("source", ["session", "definition"])
async def test_supported_formats_prepare_read_projection_without_rewriting_original_facts(tmp_path, monkeypatch, version, source):
    path, original = await _historical_database(tmp_path, version=version, source=source)
    database = Database(str(path))
    await database.initialize()
    try:
        schedule = await database.get_schedule_definition("old-off")
        assert schedule["wiki_mode"] == "read"
        assert schedule["enabled"] is True
        assert schedule["knowledge_context"] == original["schedules"][0]["knowledge_context"]
        if source == "definition":
            context = decode_context(schedule["knowledge_context"])
            assert context.origin.session_id == "definition:original-schedule"
            assert context.origin.request_id == "definition-request"
            assert await database.get_submission_for_request(context.origin.session_id, context.origin.request_id) is None
    finally:
        await database.close()
    with closing(sqlite3.connect(path)) as reader:
        assert reader.execute("PRAGMA user_version").fetchone() == (4,)
    original["schedules"][0]["wiki_mode"] = "read"
    assert _facts(path) == original
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
    assert _facts(path) == original


async def test_empty_storage_and_current_reopen_share_format_four_without_repreparing(tmp_path, monkeypatch):
    path = tmp_path / "empty.db"
    database = Database(str(path))
    await database.initialize()
    await database.close()
    with closing(sqlite3.connect(path)) as reader:
        assert reader.execute("PRAGMA user_version").fetchone() == (4,)
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


@pytest.mark.parametrize("version", [0, 1, 2])
@pytest.mark.parametrize(("raw", "enabled", "reason"), [
    (None, True, None),
    ("not a JSON contract 中文", True, None),
    ('{ "version": 999, "value": {} }\n', True, None),
    ('{ "version": 1, "value": {} }\n', True, None),
    (None, False, "origin_agent_mismatch"),
    ("broken contract", False, None),
    ("broken contract", True, "stale_reason"),
])
async def test_missing_contracts_are_preserved_and_disabled_without_manufacturing_authority(
    tmp_path, version, raw, enabled, reason,
):
    path, original = await _historical_database(tmp_path, version=version)
    with closing(sqlite3.connect(path)) as writer:
        writer.execute("UPDATE schedules SET knowledge_context=?,enabled=?,invalid_reason=?", (raw, enabled, reason))
        writer.commit()
    expected = original["schedules"][0]
    expected.update(wiki_mode="read", knowledge_context=raw, enabled=0,
                    invalid_reason=reason if not enabled and reason is not None else "knowledge_contract_missing")
    database = Database(str(path))
    await database.initialize()
    try:
        schedule = await database.get_schedule_definition("old-off")
        assert schedule["enabled"] is False
        assert schedule["wiki_mode"] == "read"
        assert schedule["knowledge_context"] == raw
        assert schedule["invalid_reason"] == expected["invalid_reason"]
    finally:
        await database.close()
    assert _facts(path) == original


@pytest.mark.parametrize("version", [0, 1, 2, 3, 4])
@pytest.mark.parametrize("contradiction", ["auto", "maintain", "submit_limit", "foreign_owner", "missing_agent"])
async def test_contradictory_off_contract_is_refused_before_reads_can_open_a_writer_or_preparation_can_write(
    tmp_path, version, contradiction,
):
    path, original = await _historical_database(tmp_path, version=version)
    context = json.loads(original["schedules"][0]["knowledge_context"])
    if contradiction in ("auto", "maintain"):
        context["value"]["mode"] = contradiction
    elif contradiction == "submit_limit":
        context["value"]["maintenance"]["may_submit"] = True
    elif contradiction == "foreign_owner":
        context["value"]["origin"]["user_id"] = "another-account"
    with closing(sqlite3.connect(path)) as writer:
        writer.execute("UPDATE schedules SET wiki_mode='off',knowledge_context=?", (json.dumps(context),))
        if contradiction == "missing_agent":
            writer.execute("UPDATE schedules SET agent_id=NULL")
        writer.commit()
    original_bytes = path.read_bytes()
    with pytest.raises(UnsupportedMainStorage):
        await Database(str(path)).inspect()
    assert path.read_bytes() == original_bytes

    statements = []
    async with aiosqlite.connect(path) as connection:
        await connection.set_trace_callback(statements.append)
        tx = Transaction(connection, ServiceHealth(), asyncio.get_running_loop().time() + 5)
        await tx.execute("BEGIN IMMEDIATE")
        try:
            with pytest.raises(UnsupportedMainStorage):
                await prepare_main_storage(tx)
        finally:
            await tx.finish()
            await connection.rollback()
    assert not [sql for sql in statements if sql.lstrip().upper().startswith(
        ("CREATE", "ALTER", "DROP", "INSERT", "UPDATE", "DELETE", "PRAGMA USER_VERSION="),
    )]
    assert path.read_bytes() == original_bytes


@pytest.mark.parametrize("version", [3, 4])
async def test_prepared_formats_reject_a_retained_off_instead_of_repeating_preparation(tmp_path, version):
    path, _ = await _historical_database(tmp_path, version=version)
    with closing(sqlite3.connect(path)) as writer:
        writer.execute("UPDATE schedules SET wiki_mode='off'")
        writer.commit()
    original_bytes = path.read_bytes()
    with pytest.raises(UnsupportedMainStorage):
        await Database(str(path)).inspect()
    assert path.read_bytes() == original_bytes


async def test_prepared_schedule_executes_its_source_contract_as_read_only(tmp_path, monkeypatch):
    path, original = await _historical_database(tmp_path)
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
        assert contract == decode_context(original["schedules"][0]["knowledge_context"])
        assert contract.origin.session_id == "source-original"
        assert contract.origin.request_id == "trusted-source-request"
        assert fired["tool_input"]["request_context"]["source"] == "schedule"
        assert runs[0].prompt.endswith("/wiki maintain Library/Original.md")
        runs[0].release.set()
        await manager.await_session_execution(session.id, timeout=5)
        retained = await database.get_schedule_definition("old-off")
        assert retained["enabled"] is True and retained["wiki_mode"] == "read"
        assert retained["knowledge_context"] == original["schedules"][0]["knowledge_context"]
    finally:
        for run in runs:
            run.release.set()
        await runner.shutdown()
        await database.close()


async def test_read_only_admission_observes_contradiction_in_the_real_wal_without_changing_bytes(tmp_path):
    path, original = await _historical_database(tmp_path)
    context = json.loads(original["schedules"][0]["knowledge_context"])
    context["value"]["mode"] = "auto"
    with closing(sqlite3.connect(path)) as keeper:
        keeper.execute("PRAGMA wal_autocheckpoint=0")
        keeper.execute("BEGIN")
        keeper.execute("SELECT knowledge_context FROM schedules").fetchall()
        before = path.read_bytes()
        with closing(sqlite3.connect(path)) as writer:
            writer.execute("UPDATE schedules SET knowledge_context=?", (json.dumps(context),))
            writer.commit()
        wal = Path(str(path) + "-wal")
        before_wal = wal.read_bytes()
        assert path.read_bytes() == before
        assert len(before_wal) > 32
        with pytest.raises(UnsupportedMainStorage):
            await Database(str(path)).inspect()
        assert path.read_bytes() == before
        assert wal.read_bytes() == before_wal


@pytest.mark.parametrize("version", [0, 2])
async def test_process_loss_after_schedule_update_rolls_back_projection_and_version_together(tmp_path, version):
    path, original = await _historical_database(tmp_path, version=version)
    proof = tmp_path / "update-reached"
    result = subprocess.run([sys.executable, "-c", """
import asyncio,os,sys
from pathlib import Path
import aiosqlite
from aiosqlite.context import contextmanager
from server.database import Database
connect=aiosqlite.Connection._connect
async def fault_connection(connection):
    await connect(connection)
    execute=connection.execute
    @contextmanager
    async def fault_execute(sql,parameters=()):
        cursor=await execute(sql,parameters)
        if sql.strip().upper().startswith("UPDATE SCHEDULES SET WIKI_MODE="):
            Path(sys.argv[2]).write_text('updated inside original transaction')
            os._exit(71)
        return cursor
    connection.execute=fault_execute
    return connection
aiosqlite.Connection._connect=fault_connection
asyncio.run(Database(sys.argv[1]).initialize())
""", str(path), str(proof)], cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=15,
        env=dict(os.environ, OCTOPUS_ENV_FILE=str(tmp_path / "no-project.env")))
    assert result.returncode == 71, result.stdout + result.stderr
    assert proof.read_text() == "updated inside original transaction"
    with closing(sqlite3.connect(path)) as reader:
        assert reader.execute("PRAGMA user_version").fetchone() == (version,)
        assert reader.execute("SELECT wiki_mode FROM schedules").fetchone() == ("off",)
        if version == 0:
            assert reader.execute("SELECT name FROM sqlite_schema WHERE name='schema_migrations'").fetchone()
    reopened = Database(str(path))
    await reopened.initialize()
    await reopened.close()
    original["schedules"][0]["wiki_mode"] = "read"
    assert _facts(path) == original
