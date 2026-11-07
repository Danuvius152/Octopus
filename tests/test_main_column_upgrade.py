"""Current startup preserves account keys, credentials and recovery relationships."""

import sqlite3
from contextlib import closing

import aiosqlite
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from server import deps
from server.crypto import decrypt, encrypt
from server.database import Database
from server.routers import auth, credentials, sessions
from server.sessions import SessionManager
from server.users import UserManager
from tests.accounts import create_account


async def seed(path):
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
    return owner, bearer, key, ciphertext


def facts(path):
    """Include original rowids and every stored column from every user table."""
    with closing(sqlite3.connect(path)) as reader:
        result = {}
        tables = reader.execute("SELECT name FROM sqlite_schema WHERE type='table' AND name NOT GLOB 'sqlite_*'").fetchall()
        for (table,) in tables:
            result[table] = reader.execute(f'SELECT rowid,* FROM "{table}" ORDER BY rowid').fetchall()  # noqa: S608
        return result


async def test_current_startup_preserve_dek_rows_and_derived_http_relationships(tmp_path, monkeypatch):
    path = tmp_path / "owned.db"
    owner, bearer, key, ciphertext = await seed(path)
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
            assert await tx.fetch_one("PRAGMA user_version") == (8,)
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
