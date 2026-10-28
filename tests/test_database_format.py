"""Main storage refuses unsupported formats before opening its writer."""

import json
import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
from contextlib import closing
from pathlib import Path

import aiosqlite
import pytest

from server.config import settings
from server.database import Database
from tests.accounts import create_account
from tests.test_service_runtime import installation_settings

ROOT = Path(__file__).resolve().parents[1]


def refuse_start(configuration, entry, tmp_path, cause="RetainedLegacyInstallation"):
    """Observe public startup refusal and its fail-stop diagnostic."""
    admitted = tmp_path / "admitted"
    with tempfile.TemporaryDirectory(prefix="octopus-startup-", dir="/var/tmp") as directory:
        journal = str(Path(directory) / "journal")
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as receiver:
            receiver.bind(journal)
            receiver.settimeout(1)
            result = subprocess.run([sys.executable, "-c", """
import asyncio,json,sys
from pathlib import Path
from server.config import settings
for name,value in json.loads(Path(sys.argv[1]).read_text()).items():setattr(settings,name,value)
settings.master_key=''
from server import service_health
service_health._JOURNAL_SOCKET=sys.argv[4]
async def start():
    if sys.argv[2]=='database':
        from server.database import Database
        database=Database(settings.db_path)
        await database.initialize()
        await database.close()
        Path(sys.argv[3]).touch()
    else:
        from server.main import app
        async with app.router.lifespan_context(app):Path(sys.argv[3]).touch()
asyncio.run(start())
""", str(configuration), entry, str(admitted), journal], cwd=ROOT,
                env=dict(os.environ, OCTOPUS_ENV_FILE=str(tmp_path / "retained.env"),
                         CODEX_HOME=str(tmp_path / "provider-codex"), CLAUDE_CONFIG_DIR=str(tmp_path / "provider-claude")),
                capture_output=True, text=True, timeout=15)
            assert result.returncode == 70, result.stdout + result.stderr
            diagnostic = receiver.recv(8192).decode()
            assert f"OCTOPUS_CAUSE={cause}" in diagnostic
            assert "original ciphertext" not in diagnostic and "retained-setup-proof" not in diagnostic
    assert not admitted.exists()


async def test_current_format_is_prepared_once_and_reopen_preserves_owned_facts(tmp_path, monkeypatch):
    path = tmp_path / "main.db"
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
        assert reader.execute("PRAGMA user_version").fetchone() == (5,)
        assert reader.execute("SELECT name FROM sqlite_schema WHERE name='schema_migrations'").fetchall() == []
        original_rows = reader.execute("SELECT * FROM sessions").fetchall()
        original_messages = reader.execute("SELECT * FROM messages").fetchall()
        original_agents = reader.execute("SELECT * FROM agents").fetchall()
    statements = []
    connect = aiosqlite.Connection._connect

    async def traced_connect(connection):
        await connect(connection)
        await connection.set_trace_callback(statements.append)
        return connection

    monkeypatch.setattr(aiosqlite.Connection, "_connect", traced_connect)
    for _ in range(2):
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
        assert not [sql for sql in statements if sql.lstrip().upper().startswith(
            ("CREATE", "ALTER", "DROP", "INSERT", "UPDATE", "DELETE", "PRAGMA USER_VERSION="),
        )]
        with closing(sqlite3.connect(path)) as reader:
            assert reader.execute("PRAGMA user_version").fetchone() == (5,)
            assert reader.execute("SELECT * FROM sessions").fetchall() == original_rows
            assert reader.execute("SELECT * FROM messages").fetchall() == original_messages
            assert reader.execute("SELECT * FROM agents").fetchall() == original_agents


@pytest.mark.parametrize("entry", ["database", "service"])
@pytest.mark.parametrize("damage", ["future_version", "future_version_wal", "previous_version", "unmarked"])
async def test_unsupported_format_is_refused_without_writes(tmp_path, monkeypatch, entry, damage):
    config, configuration = installation_settings(tmp_path)
    for name, value in json.loads(configuration.read_text()).items():
        monkeypatch.setattr(settings, name, value)
    monkeypatch.setattr(settings, "master_key", "format-fixture-master")
    path = Path(config.db_path)
    environment = tmp_path / "retained.env"
    environment.write_text("OCTOPUS_AUTH_TOKEN=retained-setup-proof\n")
    original_environment = environment.read_bytes()
    if damage == "unmarked":
        with closing(sqlite3.connect(path)) as writer:
            writer.execute("CREATE TABLE retained(original BLOB)")
            writer.execute("INSERT INTO retained VALUES(?)", (b"original ciphertext\x00\xff",))
            writer.commit()
    else:
        database = Database(str(path))
        await database.initialize()
        owner, _ = await create_account(database, extra_roots=[tmp_path])
        await database.save_session("original", "Original session", str(tmp_path), "time", user_id=owner["id"])
        await database.append_message(session_id="original", seq=0, role="user", type="text", content="Original input must survive")
        await database.close()
    if damage == "future_version_wal":
        main_bytes = path.read_bytes()
        subprocess.run([sys.executable, "-c", """
import os,sqlite3,sys
writer=sqlite3.connect(sys.argv[1])
writer.execute('PRAGMA wal_autocheckpoint=0')
writer.execute('PRAGMA user_version=999')
writer.execute("UPDATE messages SET content='original ciphertext committed in WAL' WHERE session_id='original'")
writer.commit()
os._exit(0)
""", str(path)], cwd=ROOT, check=True, timeout=10)
        assert path.read_bytes() == main_bytes
    elif damage != "unmarked":
        with closing(sqlite3.connect(path)) as writer:
            writer.execute(f"PRAGMA user_version={999 if damage == 'future_version' else 3}")
            writer.commit()
    original = path.read_bytes()
    wal = Path(str(path) + "-wal")
    original_wal = wal.read_bytes() if wal.exists() else None
    if damage == "future_version_wal":
        assert int.from_bytes(original[60:64], "big") == 5
        assert original_wal is not None and len(original_wal) > 32
    directories = {child.relative_to(tmp_path) for child in tmp_path.rglob("*") if child.is_dir()}
    refuse_start(configuration, entry, tmp_path, cause="UnsupportedMainStorage")
    assert path.read_bytes() == original
    # SQLite may create empty WAL/read-index coordination files in mode=ro.
    # It must neither append committed frames nor change retained WAL bytes.
    if original_wal is not None:
        assert wal.read_bytes() == original_wal
    elif wal.exists():
        assert wal.stat().st_size == 0
    assert {child.relative_to(tmp_path) for child in tmp_path.rglob("*") if child.is_dir()} == directories
    assert not Path(config.master_key_file).exists()
    assert environment.read_bytes() == original_environment
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as reader:
        if damage == "unmarked":
            assert not Path(config.users_root).exists()
            assert reader.execute("SELECT original FROM retained").fetchone()[0] == b"original ciphertext\x00\xff"
            assert reader.execute("SELECT 1 FROM sqlite_schema WHERE name='agents'").fetchone() is None
        elif damage == "future_version_wal":
            assert reader.execute("PRAGMA user_version").fetchone()[0] == 999
            assert reader.execute("SELECT content FROM messages WHERE session_id='original'").fetchone()[0] == "original ciphertext committed in WAL"


@pytest.mark.parametrize("failure", ["during_ddl", "before_version", "commit_ack"])
async def test_preparation_interruption_reopens_only_the_actual_committed_format(tmp_path, failure):
    path = tmp_path / "interrupted.db"
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
        assert reader.execute("PRAGMA user_version").fetchone() == (5 if committed else 0,)
        tables = {row[0] for row in reader.execute("SELECT name FROM sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
        assert bool(tables) == committed
    reopened = Database(str(path))
    await reopened.initialize()
    await reopened.close()
    with closing(sqlite3.connect(path)) as reader:
        assert reader.execute("PRAGMA user_version").fetchone() == (5,)
        assert reader.execute("SELECT name FROM sqlite_schema WHERE name='schema_migrations'").fetchall() == []
