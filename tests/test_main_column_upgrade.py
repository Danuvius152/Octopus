"""Supported column retirement preserves current facts and refuses unknown copies."""

import json
import os
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path

import aiosqlite
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from server import deps
from server.config import settings
from server.crypto import decrypt, encrypt
from server.database import Database
from server.routers import auth, credentials, sessions
from server.sessions import SessionManager
from server.users import UserManager
from tests.accounts import create_account
from tests.main_storage import write_unversioned_baseline, write_version_one_baseline
from tests.test_service_runtime import installation_settings

ROOT = Path(__file__).resolve().parents[1]


async def seed(path, version):
    database = Database(str(path))
    try:
        await database.initialize()
        owner, bearer = await create_account(database, extra_roots=[path.parent])
        key = await UserManager(database).data_key_for(owner["id"])
        ciphertext = encrypt("original-api-key", key)
        await database.save_credential("saved", "codex", "Custom credential", "api_key", ciphertext, "original-time",
                                       user_id=owner["id"])
        agent = await database.get_system_agent(owner["id"])
        for identity in ("original", "other"):
            await database.save_session(identity, "Original session " + identity, str(path.parent), "original-time",
                                        agent_id=agent["id"], user_id=owner["id"], backend="codex")
        await database.append_message("original", 7, "user", "text", "Original historical body 原文")
        for identity, predecessor in (("source", None), ("child", "source"), ("grandchild", "child"), ("unknown", None)):
            await database.admit_client_submission(
                "original", identity, "original-digest-" + identity, "original-request-" + identity,
                payload={"content": "Original input 原文 " + identity, "attachment_ids": [], "attachments": [], "wiki_mode": None},
                receipt={"accepted": True, "disposition": "accepted"}, recovery_of=predecessor,
            )
            async with database.transaction() as tx:
                await tx.execute("UPDATE client_submissions SET execution_state='withdrawn' WHERE client_id=?", (identity,))
        async with database.transaction() as tx:
            await tx.execute("UPDATE client_submissions SET execution_state='unknown',execution_generation='original-generation',"
                             "reason_code='service_restart_execution_unknown' WHERE client_id='unknown'")
    finally:
        await database.close()
    if version == 0:
        write_unversioned_baseline(path)
    elif version == 1:
        write_version_one_baseline(path)
    elif version == 2:
        with closing(sqlite3.connect(path)) as writer:
            writer.execute("PRAGMA user_version=2")
    return owner, bearer, key, ciphertext


def facts(path):
    """Include original rowids and all facts except the two proven duplicates."""
    with closing(sqlite3.connect(path)) as reader:
        result = {}
        tables = reader.execute("SELECT name FROM sqlite_schema WHERE type='table' AND name NOT GLOB 'sqlite_*' "
                                "AND name!='schema_migrations'").fetchall()
        for (table,) in tables:
            columns = [row[1] for row in reader.execute(f'PRAGMA table_info("{table}")')]
            retired = {"backend_credentials": "secret_encrypted", "client_submissions": "recovered_by"}.get(table)
            names = ','.join('"' + column + '"' for column in columns if column != retired)
            result[table] = reader.execute(f'SELECT rowid,{names} FROM "{table}" ORDER BY rowid').fetchall()  # noqa: S608
        return result


@pytest.mark.parametrize("version,duplicate", [(0, False), (1, False), (1, True), (2, False), (3, False)])
async def test_upgrade_and_current_startup_preserve_dek_rows_and_derived_http_relationships(tmp_path, monkeypatch, version, duplicate):
    path = tmp_path / "owned.db"
    owner, bearer, key, ciphertext = await seed(path, version)
    if duplicate:
        with closing(sqlite3.connect(path)) as writer:
            writer.execute("UPDATE backend_credentials SET secret_encrypted=? WHERE id='saved'", (ciphertext,))
            writer.execute("UPDATE client_submissions SET recovered_by='child' WHERE client_id='source'")
            writer.execute("UPDATE client_submissions SET recovered_by='grandchild' WHERE client_id='child'")
            writer.commit()
    original = facts(path)
    database = Database(str(path))
    await database.initialize()
    try:
        assert facts(path) == original
        users = UserManager(database)
        assert await users.data_key_for(owner["id"]) == key
        stored = await database.get_credential("saved", owner["id"])
        assert stored["secret_encrypted"] == ciphertext and stored["user_id"] == owner["id"]
        assert decrypt(stored["secret_encrypted"], key) == "original-api-key"
        async with database.transaction(write=False) as tx:
            assert await tx.fetch_one("PRAGMA user_version") == (3,)
            assert "secret_encrypted" not in {row[1] for row in await tx.fetch_all("PRAGMA table_info(backend_credentials)")}
            assert "recovered_by" not in {row[1] for row in await tx.fetch_all("PRAGMA table_info(client_submissions)")}
        manager = SessionManager()
        deps.set_user_manager(users)
        await manager.initialize(database)
        app = FastAPI()
        app.include_router(credentials.router)
        app.include_router(sessions.router)
        app.include_router(auth.router)
        app.dependency_overrides[deps.get_session_manager] = lambda: manager
        monkeypatch.setattr(credentials, "_db", database)
        async with AsyncClient(transport=ASGITransport(app), base_url="http://test",
                               headers={"Authorization": f"Bearer {bearer}"}) as client:
            source = await client.get("/api/sessions/original/submissions/source")
            child = await client.get("/api/sessions/original/submissions/child")
            assert source.status_code == child.status_code == 200
            assert source.json()["recovered_by_client_id"] == "child"
            assert child.json()["recovery_of_client_id"] == "source"
            assert child.json()["recovered_by_client_id"] == "grandchild"
            assert source.json()["input"]["content"] == "Original input 原文 source"
            assert not source.json()["execution"]["recoverable_input"]
            uncertain = await client.get("/api/sessions/original/submissions/unknown")
            assert uncertain.json()["execution"]["state"] == "unknown"
            assert (await database.get_client_submission("original", "unknown"))["execution_generation"] == "original-generation"
            created = await client.post("/api/credentials", json={"backend": "codex", "label": "New credential", "secret": "new-api-key"})
            assert created.status_code == 201, created.text
            assert "secret_encrypted" not in created.json()
            credential_id = created.json()["id"]
            updated = await client.patch(f"/api/credentials/{credential_id}", json={"secret": "updated-api-key"})
            assert updated.status_code == 200
            fresh = await database.get_credential(credential_id, owner["id"])
            assert decrypt(fresh["secret_encrypted"], key) == "updated-api-key"
            changed = await client.post("/api/auth/password", json={"current_password": "owned-test-password", "new_password": "new-owned-password"})
            assert changed.status_code == 204, changed.text
            assert await users.data_key_for(owner["id"]) == key
            assert (await database.get_credential("saved"))["secret_encrypted"] == ciphertext
            _, foreign_bearer = await create_account(database, username="foreign-account")
            assert (await client.get("/api/sessions/original/submissions/source",
                                     headers={"Authorization": f"Bearer {foreign_bearer}"})).status_code == 404
        with pytest.raises(sqlite3.IntegrityError):
            async with database.transaction() as tx:
                await tx.execute("INSERT INTO client_submissions(session_id,client_id,payload_sha256,request_id,recovery_of) "
                                 "VALUES('original','second-child','digest','second-request','source')")
    finally:
        await database.close()
    original = facts(path)
    statements = []
    connect = aiosqlite.Connection._connect

    async def traced(connection):
        await connect(connection)
        await connection.set_trace_callback(statements.append)
        return connection

    monkeypatch.setattr(aiosqlite.Connection, "_connect", traced)
    for _ in range(2):
        reopened = Database(str(path))
        await reopened.initialize()
        await reopened.close()
        assert facts(path) == original
    assert not [sql for sql in statements if sql.lstrip().upper().startswith(
        ("CREATE", "ALTER", "DROP", "INSERT", "UPDATE", "DELETE", "PRAGMA USER_VERSION="),
    )]


@pytest.mark.parametrize("entry", ["database", "service"])
@pytest.mark.parametrize("damage", ["missing_split", "different_copy", "same_plaintext", "forward_only",
                                  "wrong_successor", "cross_session", "version_zero_identical_copy"])
async def test_unproven_retirement_is_refused_before_keys_or_writes_including_real_wal(tmp_path, monkeypatch, request, entry, damage):
    config, configuration = installation_settings(tmp_path)
    for name, value in json.loads(configuration.read_text()).items():
        monkeypatch.setattr(settings, name, value)
    monkeypatch.setattr(settings, "master_key", "column-retirement-fixture-master")
    path = Path(config.db_path)
    _, _, key, ciphertext = await seed(path, 0 if damage == "version_zero_identical_copy" else 1)
    keeper = sqlite3.connect(path)
    request.addfinalizer(keeper.close)
    keeper.execute("PRAGMA journal_mode=WAL")
    keeper.execute("BEGIN")
    keeper.execute("SELECT id FROM backend_credentials").fetchall()
    with closing(sqlite3.connect(path)) as writer:
        writer.execute("PRAGMA wal_autocheckpoint=0")
        if damage in ("missing_split", "different_copy", "same_plaintext", "version_zero_identical_copy"):
            copy = encrypt("original-api-key" if damage == "same_plaintext" else "other-api-key", key)
            if damage in ("missing_split", "version_zero_identical_copy"):
                copy = ciphertext
            if damage == "same_plaintext":
                assert copy != ciphertext and decrypt(copy, key) == decrypt(ciphertext, key)
            writer.execute("UPDATE backend_credentials SET secret_encrypted=? WHERE id='saved'", (copy,))
            if damage == "missing_split":
                writer.execute("DELETE FROM credential_secrets WHERE credential_id='saved'")
        else:
            successor = {"forward_only": "child", "wrong_successor": "grandchild", "cross_session": "foreign-child"}[damage]
            writer.execute("UPDATE client_submissions SET recovered_by=? WHERE client_id='source'", (successor,))
            if damage == "forward_only":
                writer.execute("UPDATE client_submissions SET recovery_of=NULL WHERE client_id='child'")
            elif damage == "cross_session":
                writer.execute("INSERT INTO client_submissions(session_id,client_id,payload_sha256,request_id,recovery_of) "
                               "VALUES('other','foreign-child','foreign-digest','foreign-request','source')")
        writer.commit()
    wal = Path(str(path) + "-wal")
    before, original_wal = path.read_bytes(), wal.read_bytes()
    assert len(original_wal) > 32
    original = facts(path)
    directories = {p.relative_to(tmp_path) for p in tmp_path.rglob("*") if p.is_dir()}
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
    assert not admitted.exists() and not Path(config.master_key_file).exists()
    assert path.read_bytes() == before and wal.read_bytes() == original_wal
    assert facts(path) == original
    assert {p.relative_to(tmp_path) for p in tmp_path.rglob("*") if p.is_dir()} == directories


@pytest.mark.parametrize("version", [0, 1])
@pytest.mark.parametrize("phase", ["between_drops", "before_version", "commit_ack"])
async def test_column_retirement_crash_keeps_the_complete_old_or_new_format(tmp_path, version, phase):
    path = tmp_path / "interrupted.db"
    owner, _, key, ciphertext = await seed(path, version)
    original = facts(path)
    proof = tmp_path / "fault-reached"
    result = subprocess.run([sys.executable, "-c", """
import asyncio,os,sys
from pathlib import Path
import aiosqlite
from aiosqlite.context import contextmanager
from server.database import Database
connect=aiosqlite.Connection._connect
phase=sys.argv[2]
async def fault_connection(connection):
    await connect(connection)
    execute,commit=connection.execute,connection.commit
    def stop():
        Path(sys.argv[3]).write_text(phase)
        os._exit(71)
    @contextmanager
    async def fault_execute(sql,parameters=()):
        normalized=sql.strip().upper()
        if phase=='before_version' and normalized.startswith('PRAGMA USER_VERSION='):stop()
        cursor=await execute(sql,parameters)
        if phase=='between_drops' and normalized=='ALTER TABLE BACKEND_CREDENTIALS DROP COLUMN SECRET_ENCRYPTED':stop()
        return cursor
    async def fault_commit():
        await commit()
        if phase=='commit_ack':
            Path(sys.argv[3]).write_text(phase)
            raise OSError('Actual commit succeeded; acknowledgment unavailable')
    connection.execute,connection.commit=fault_execute,fault_commit
    return connection
aiosqlite.Connection._connect=fault_connection
asyncio.run(Database(sys.argv[1]).initialize())
""", str(path), phase, str(proof)], cwd=ROOT, capture_output=True, text=True, timeout=15)
    assert result.returncode == (70 if phase == "commit_ack" else 71), result.stdout + result.stderr
    assert proof.read_text() == phase
    committed = phase == "commit_ack"
    with closing(sqlite3.connect(path)) as reader:
        assert reader.execute("PRAGMA user_version").fetchone() == (3 if committed else version,)
        assert ("secret_encrypted" in {row[1] for row in reader.execute("PRAGMA table_info(backend_credentials)")}) == (not committed)
        assert ("recovered_by" in {row[1] for row in reader.execute("PRAGMA table_info(client_submissions)")}) == (not committed)
        assert bool(reader.execute("SELECT 1 FROM sqlite_schema WHERE name='schema_migrations'").fetchone()) == (version == 0 and not committed)
    assert facts(path) == original
    reopened = Database(str(path))
    await reopened.initialize()
    try:
        assert facts(path) == original
        assert (await reopened.get_credential("saved"))["secret_encrypted"] == ciphertext
        assert await UserManager(reopened).data_key_for(owner["id"]) == key
        assert decrypt(ciphertext, key) == "original-api-key"
        source = await reopened.get_client_submission("original", "source")
        assert source["recovered_by"] == "child" and source["payload"]["content"] == "Original input 原文 source"
        assert (await reopened.get_client_submission("original", "unknown"))["execution_generation"] == "original-generation"
    finally:
        await reopened.close()
