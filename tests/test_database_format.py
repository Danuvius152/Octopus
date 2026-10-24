"""Main storage refuses unsupported formats before opening its writer."""

import json
import os
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path

import aiosqlite
import pytest

from server.config import settings
from server.database import Database
from tests.accounts import create_account
from tests.main_storage import write_unversioned_baseline, write_version_one_baseline
from tests.test_service_runtime import installation_settings

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("baseline", ["empty", "complete_v0", "complete_v1", "owned_v0", "owned_v1"])
async def test_current_format_is_prepared_once_and_reopen_preserves_owned_facts(tmp_path, monkeypatch, baseline):
    path = tmp_path / "main.db"
    if baseline in ("complete_v0", "complete_v1"):
        with closing(sqlite3.connect(path)) as original:
            original.executescript((ROOT / "tests/fixtures/main-baseline-v0.sql").read_text())
            if baseline == "complete_v1":
                original.execute("DROP TABLE schema_migrations")
                original.execute("PRAGMA user_version=1")
                original.commit()
    database = Database(str(path))
    await database.initialize()
    owner, _ = await create_account(database, extra_roots=[tmp_path])
    agent = await database.get_system_agent(owner["id"])
    await database.save_session("original", "Custom session", str(tmp_path), "time",
                                agent_id=agent["id"], user_id=owner["id"])
    await database.append_message("original", 7, "user", "text", "Original input\n原文")
    await database.save_credential("saved", "codex", "Custom credential", "api_key",
                                   "account-wrapped-original", "time", user_id=owner["id"])
    await database.admit_client_submission("original", "steer", "digest", "request",
                                           payload={"text": "Unknown delivery"}, receipt={"accepted": True})
    async with database.transaction() as tx:
        await tx.execute("UPDATE client_submissions SET execution_state='unknown', "
                         "reason_code='steer_delivery_unknown',delivery_state='unknown'")
    await database.close()
    with closing(sqlite3.connect(path)) as reader:
        assert reader.execute("PRAGMA user_version").fetchone() == (3,)
        assert reader.execute("SELECT name FROM sqlite_schema WHERE name='schema_migrations'").fetchall() == []
        original_rows = reader.execute("SELECT * FROM sessions").fetchall()
        original_messages = reader.execute("SELECT * FROM messages").fetchall()
        original_agents = reader.execute("SELECT * FROM agents").fetchall()
    if baseline == "owned_v0":
        write_unversioned_baseline(path)
    elif baseline == "owned_v1":
        write_version_one_baseline(path)
    statements = []
    connect = aiosqlite.Connection._connect

    async def traced_connect(connection):
        await connect(connection)
        await connection.set_trace_callback(statements.append)
        return connection

    monkeypatch.setattr(aiosqlite.Connection, "_connect", traced_connect)
    for opening in range(2):
        statements.clear()
        reopened = Database(str(path))
        await reopened.initialize()
        assert (await reopened.get_agent(agent["id"]))["user_id"] == owner["id"]
        assert (await reopened.load_messages("original"))[0]["content"] == "Original input\n原文"
        assert (await reopened.get_credential("saved", owner["id"]))["secret_encrypted"] == "account-wrapped-original"
        submission = await reopened.get_client_submission("original", "steer")
        assert (submission["execution_state"], submission["reason_code"], submission["delivery_state"]) == (
            "unknown", "steer_delivery_unknown", "unknown",
        )
        await reopened.close()
        if opening or baseline not in ("owned_v0", "owned_v1"):
            assert not [sql for sql in statements if sql.lstrip().upper().startswith(
                ("CREATE", "ALTER", "DROP", "INSERT", "UPDATE", "DELETE", "PRAGMA USER_VERSION="),
            )]
        with closing(sqlite3.connect(path)) as reader:
            assert reader.execute("PRAGMA user_version").fetchone() == (3,)
            assert reader.execute("SELECT * FROM sessions").fetchall() == original_rows
            assert reader.execute("SELECT * FROM messages").fetchall() == original_messages
            assert reader.execute("SELECT * FROM agents").fetchall() == original_agents


@pytest.mark.parametrize("entry", ["database", "service"])
@pytest.mark.parametrize("damage", ["future_version", "future_version_wal", "unknown_structure", "no_upgrade_path"])
async def test_unsupported_owned_format_is_refused_without_writes(tmp_path, monkeypatch, request, entry, damage):
    config, configuration = installation_settings(tmp_path)
    for name, value in json.loads(configuration.read_text()).items():
        monkeypatch.setattr(settings, name, value)
    monkeypatch.setattr(settings, "master_key", "format-fixture-master")
    path = Path(config.db_path)
    database = Database(str(path))
    await database.initialize()
    owner, _ = await create_account(database, extra_roots=[tmp_path])
    await database.save_session("original", "Original session", str(tmp_path), "time", user_id=owner["id"])
    await database.append_message(session_id="original", seq=0, role="user", type="text", content="Original input must survive")
    await database.close()
    if damage == "no_upgrade_path":
        write_unversioned_baseline(path)
    if damage == "future_version_wal":
        keeper = sqlite3.connect(path)
        request.addfinalizer(keeper.close)
        keeper.execute("PRAGMA wal_autocheckpoint=0")
        keeper.execute("SELECT 1 FROM sessions").fetchall()
    with closing(sqlite3.connect(path)) as writer:
        if damage in ("future_version", "future_version_wal"):
            writer.execute("PRAGMA user_version=1000000")
        elif damage == "unknown_structure":
            writer.execute("PRAGMA user_version=3")
            writer.execute("CREATE TABLE future_business(id TEXT PRIMARY KEY, original BLOB)")
            writer.execute("INSERT INTO future_business VALUES('original', ?)", (b"unknown durable fact\x00\xff",))
        else:
            writer.execute("PRAGMA user_version=0")
            writer.execute("ALTER TABLE agents DROP COLUMN service_tier")
        writer.commit()
    original = path.read_bytes()
    wal = Path(str(path) + "-wal")
    original_wal = wal.read_bytes() if wal.exists() else None
    if damage == "future_version_wal":
        assert int.from_bytes(original[60:64], "big") == 3
        assert original_wal is not None and len(original_wal) > 32
    directories = {child.relative_to(tmp_path) for child in tmp_path.rglob("*") if child.is_dir()}
    admitted = tmp_path / "admitted"
    result = subprocess.run([sys.executable, "-c", """
import asyncio,json,sys
from pathlib import Path
from server.config import settings
for name,value in json.loads(Path(sys.argv[1]).read_text()).items():setattr(settings,name,value)
settings.master_key=''
from server.database import Database
async def main():
    if sys.argv[2]=='database':
        database=Database(settings.db_path)
        await database.initialize()
        Path(sys.argv[3]).touch()
        await database.close()
    else:
        from server.main import app
        async with app.router.lifespan_context(app):Path(sys.argv[3]).touch()
asyncio.run(main())
""", str(configuration), entry, str(admitted)], cwd=ROOT, capture_output=True, text=True, timeout=15,
        env=dict(os.environ, OCTOPUS_ENV_FILE=str(tmp_path / "no-project.env"),
                 CODEX_HOME=str(tmp_path / "provider-codex"), CLAUDE_CONFIG_DIR=str(tmp_path / "provider-claude")))
    assert result.returncode == 70, result.stdout + result.stderr
    assert not admitted.exists()
    assert path.read_bytes() == original
    # SQLite may create empty WAL/read-index coordination files in mode=ro.
    # It must neither append committed frames nor change retained WAL bytes.
    if original_wal is not None:
        assert wal.read_bytes() == original_wal
    elif wal.exists():
        assert wal.stat().st_size == 0
    assert {child.relative_to(tmp_path) for child in tmp_path.rglob("*") if child.is_dir()} == directories
    assert not Path(config.master_key_file).exists()


@pytest.mark.parametrize("baseline", ["empty", "owned_v0"])
@pytest.mark.parametrize("failure", ["during_ddl", "before_version", "commit_ack"])
async def test_preparation_interruption_reopens_only_the_actual_committed_format(tmp_path, baseline, failure):
    path = tmp_path / "interrupted.db"
    original = None
    if baseline == "owned_v0":
        database = Database(str(path))
        await database.initialize()
        owner, _ = await create_account(database)
        await database.save_session("saved", "Original identity", str(tmp_path), "time", user_id=owner["id"])
        await database.append_message("saved", 9, "user", "text", "Original input before interruption")
        await database.close()
        write_unversioned_baseline(path)
        with closing(sqlite3.connect(path)) as reader:
            original = reader.execute("SELECT * FROM messages").fetchall()
    proof = tmp_path / "fault-reached"
    result = subprocess.run([sys.executable, "-c", """
import asyncio,os,sys
from pathlib import Path
import aiosqlite
from aiosqlite.context import contextmanager
from server.database import Database
phase=sys.argv[2]
connect=aiosqlite.Connection._connect
async def fault_connection(connection):
    await connect(connection)
    execute=connection.execute
    commit=connection.commit
    created=0
    def stop():
        Path(sys.argv[3]).write_text(phase)
        os._exit(71)
    @contextmanager
    async def fault_execute(sql,parameters=()):
        nonlocal created
        normalized=sql.strip().upper()
        if phase=='before_version' and normalized.startswith('PRAGMA USER_VERSION='):
            stop()
        cursor=await execute(sql,parameters)
        if normalized.startswith('CREATE TABLE'):
            created+=1
        if phase=='during_ddl' and (created==3 or normalized=='DROP TABLE SCHEMA_MIGRATIONS'):
            stop()
        return cursor
    async def fault_commit():
        await commit()
        if phase=='commit_ack':
            Path(sys.argv[3]).write_text(phase)
            raise OSError('Commit acknowledgment lost after actual commit')
    connection.execute=fault_execute
    connection.commit=fault_commit
    return connection
aiosqlite.Connection._connect=fault_connection
async def main():
    database=Database(sys.argv[1])
    await database.initialize()
    await database.close()
asyncio.run(main())
""", str(path), failure, str(proof)], cwd=ROOT, capture_output=True, text=True, timeout=15)
    assert result.returncode == (70 if failure == "commit_ack" else 71), result.stdout + result.stderr
    assert proof.read_text() == failure
    with closing(sqlite3.connect(path)) as reader:
        committed = failure == "commit_ack"
        assert reader.execute("PRAGMA user_version").fetchone() == (3 if committed else 0,)
        tables = {row[0] for row in reader.execute("SELECT name FROM sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
        if baseline == "empty" and not committed:
            assert tables == set()
        else:
            assert ("schema_migrations" in tables) == (not committed)
        if original is not None:
            assert reader.execute("SELECT * FROM messages").fetchall() == original
    reopened = Database(str(path))
    await reopened.initialize()
    if original is not None:
        assert (await reopened.load_messages("saved"))[0]["content"] == "Original input before interruption"
    await reopened.close()
    with closing(sqlite3.connect(path)) as reader:
        assert reader.execute("PRAGMA user_version").fetchone() == (3,)
        assert reader.execute("SELECT name FROM sqlite_schema WHERE name='schema_migrations'").fetchall() == []


@pytest.mark.parametrize("retained", ["legacy_secret", "legacy_stage", "forward_only", "legacy_steer",
                                     "missing_pin", "repairable_owner", "refused_recovery"])
async def test_version_zero_needing_retired_conversion_is_preserved_and_refused(tmp_path, retained):
    path = tmp_path / "retained.db"
    database = Database(str(path))
    await database.initialize()
    owner, _ = await create_account(database)
    agent = await database.get_system_agent(owner["id"])
    await database.save_session("saved", "Retained session", str(tmp_path), "time",
                                agent_id=agent["id"], user_id=owner["id"])
    await database.save_credential("credential", "codex", "Retained credential", "api_key",
                                   "current-authority", "time", user_id=owner["id"])
    await database.admit_client_submission("saved", "original", "digest", "request",
                                           payload={"text": "Preserve original input"}, receipt={"accepted": True})
    await database.close()
    write_unversioned_baseline(path)
    changes = {
        "legacy_secret": "UPDATE backend_credentials SET secret_encrypted='retained-legacy-only-value'",
        "legacy_stage": "UPDATE client_submissions SET execution_state='claimed'",
        "forward_only": "UPDATE client_submissions SET recovered_by='missing-successor'",
        "legacy_steer": "UPDATE client_submissions SET reason_code='steer_delivery_unknown',delivery_state='none'",
        "missing_pin": "UPDATE agents SET pin_order=NULL",
        "repairable_owner": "UPDATE sessions SET origin='fork',user_id=NULL",
        "refused_recovery": "UPDATE client_submissions SET reason_code='legacy_rejected',recovery_of='original-refusal'",
    }
    with closing(sqlite3.connect(path)) as writer:
        writer.execute(changes[retained])
        writer.commit()
    original = path.read_bytes()
    result = subprocess.run([sys.executable, "-c", """
import asyncio,sys
from server.database import Database
async def main():
    database=Database(sys.argv[1])
    await database.initialize()
    await database.close()
asyncio.run(main())
""", str(path)], cwd=ROOT, capture_output=True, text=True, timeout=15)
    assert result.returncode == 70, result.stdout + result.stderr
    assert path.read_bytes() == original
    with closing(sqlite3.connect(path)) as reader:
        assert reader.execute("PRAGMA user_version").fetchone() == (0,)
        assert reader.execute("SELECT name FROM sqlite_schema WHERE name='schema_migrations'").fetchone()


@pytest.mark.parametrize("damage", ["missing_index", "extra_column", "weakened_fk", "weakened_check",
                                  "unknown_trigger", "prefix_table", "incomplete_guards"])
def test_complete_column_names_cannot_hide_an_unsupported_constraint(tmp_path, damage):
    path = tmp_path / "constraints.db"
    script = (ROOT / "tests/fixtures/main-baseline-v0.sql").read_text()
    if damage == "weakened_fk":
        script = script.replace("ON DELETE CASCADE", "ON DELETE SET NULL", 1)
    elif damage == "weakened_check":
        script = script.replace("CHECK(state_version>0)", "CHECK(state_version>=0)")
    with closing(sqlite3.connect(path)) as writer:
        writer.executescript(script)
        if damage == "missing_index":
            writer.execute("DROP INDEX messages_terminal_request")
        elif damage == "extra_column":
            writer.execute("ALTER TABLE agents ADD COLUMN unknown_configuration TEXT")
        elif damage == "unknown_trigger":
            writer.execute("CREATE TRIGGER unknown_effect AFTER INSERT ON agents BEGIN DELETE FROM messages; END")
        elif damage == "prefix_table":
            writer.execute("CREATE TABLE sqlitex_external (unknown_fact TEXT)")
        elif damage == "incomplete_guards":
            writer.execute("DROP INDEX messages_unique_sequence")
            guards = (ROOT / "tests/fixtures/message-sequence-guards.sql").read_text()
            writer.executescript(guards.split("CREATE TRIGGER messages_sequence_update")[0])
        writer.commit()
    original = path.read_bytes()
    result = subprocess.run([sys.executable, "-c", """
import asyncio,sys
from server.database import Database
asyncio.run(Database(sys.argv[1]).initialize())
""", str(path)], cwd=ROOT, capture_output=True, text=True, timeout=15)
    assert result.returncode == 70, result.stdout + result.stderr
    assert path.read_bytes() == original


@pytest.mark.parametrize("version", [0, 1, 2])
async def test_complete_historical_sequence_guards_survive_format_preparation(tmp_path, version):
    path = tmp_path / "historical.db"
    database = Database(str(path))
    await database.initialize()
    owner, _ = await create_account(database)
    await database.save_session("history", "Original history", str(tmp_path), "time", user_id=owner["id"])
    await database.append_message("history", 5, "user", "text", "First historical row")
    await database.close()
    if version == 0:
        write_unversioned_baseline(path)
    elif version == 1:
        write_version_one_baseline(path)
    with closing(sqlite3.connect(path)) as writer:
        writer.execute("DROP INDEX messages_unique_sequence")
        writer.execute("INSERT INTO messages(session_id,seq,role,type,content) VALUES('history',5,'assistant','text',?)",
                       (json.dumps("Second historical row"),))
        writer.executescript((ROOT / "tests/fixtures/message-sequence-guards.sql").read_text())
        writer.commit()
        original = writer.execute("SELECT id,session_id,seq,content FROM messages ORDER BY id").fetchall()
    database = Database(str(path))
    await database.initialize()
    try:
        assert [message["content"] for message in await database.load_messages("history")] == [
            "First historical row", "Second historical row",
        ]
        with pytest.raises(sqlite3.IntegrityError):
            await database.append_message("history", 5, "user", "text", "Forbidden collision")
        await database.append_message("history", 6, "user", "text", "Legal new sequence")
        with pytest.raises(sqlite3.IntegrityError):
            async with database.transaction() as tx:
                await tx.execute("UPDATE messages SET seq=5 WHERE session_id='history' AND seq=6")
    finally:
        await database.close()
    with closing(sqlite3.connect(path)) as reader:
        assert reader.execute("PRAGMA user_version").fetchone() == (3,)
        assert reader.execute("SELECT id,session_id,seq,content FROM messages WHERE seq=5 ORDER BY id").fetchall() == original
        assert {row[0] for row in reader.execute("SELECT name FROM sqlite_schema WHERE type='trigger'")} == {
            "messages_sequence_insert", "messages_sequence_update",
        }
