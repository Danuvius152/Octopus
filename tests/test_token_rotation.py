"""Rotating the access token (docs/plans/token-rotation.md).

The token is also the key every stored secret is encrypted with, so a
rotation either re-keys all of them and changes what the server accepts, or
it changes nothing. These tests pin both halves — and the refusals, because
the failure this feature exists to prevent is a database nobody can decrypt.
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import subprocess
import sys
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient

from server.config import settings
from server.crypto import decrypt, encrypt, master_key
from server.database import Database
from server.main import app
from server.routers import auth as auth_router
from server.session_manager import session_manager
from server.token_rotation import (
    TokenRotationError,
    env_files_defining_token,
    rotate_auth_token,
    validate_new_token,
)

OLD = "old-token-1234"
NEW = "new-token-abcdef-9876"
HEADERS = {"Authorization": f"Bearer {OLD}"}


async def test_rotation_does_not_retain_an_active_old_key_secret(db, env_file):
    old_ciphertext = encrypt("retained credential", OLD)
    await db.save_credential("retained", "codex", "Personal", "api_key", old_ciphertext, "t")

    await rotate_auth_token(db, NEW)
    row = await db.get_credential("retained")
    assert decrypt(row["secret_encrypted"], NEW) == "retained credential"

    # Inspect the persisted logical database, including obsolete columns that
    # normal credential reads no longer use. Rotation must retire those copies.
    with sqlite3.connect(db._db_path) as stored:
        assert old_ciphertext not in "\n".join(stored.iterdump())


async def test_rotation_keeps_running_app_and_inflight_request(db, env_file, tmp_path, monkeypatch):
    import httpx

    from server import app_backends
    from server.applications import app_scope_token

    directory = tmp_path / "app"
    directory.mkdir()
    (directory / "backend.py").write_text(
        "import os, time\nfrom pathlib import Path\nfrom http.server import BaseHTTPRequestHandler, ThreadingHTTPServer\n"
        "class Handler(BaseHTTPRequestHandler):\n"
        " def do_GET(self):\n"
        "  Path('entered').touch()\n"
        "  while not Path('release').exists(): time.sleep(.01)\n"
        "  self.send_response(200); self.end_headers(); self.wfile.write(b'complete')\n"
        "ThreadingHTTPServer(('127.0.0.1', int(os.environ['PORT'])), Handler).serve_forever()\n"
    )
    script = directory / "start.sh"
    script.write_text(f'#!/bin/sh\nexec "{sys.executable}" -u backend.py\n')
    script.chmod(0o700)
    supervisor = app_backends.BackendSupervisor()
    monkeypatch.setattr(app_backends, "backend_supervisor", supervisor)
    held_cleanup = AsyncMock()
    token = app_scope_token("test-app")
    request = None
    try:
        running = await supervisor.ensure_running("test-app", str(directory))
        assert running.state == app_backends.RUNNING
        pid = running.pid
        async with httpx.AsyncClient(trust_env=False) as client:
            request = asyncio.create_task(client.get(f"http://127.0.0.1:{running.port}/slow"))

            async def entered():
                while not (directory / "entered").exists():
                    await asyncio.sleep(.01)

            await asyncio.wait_for(entered(), 3)
            await rotate_auth_token(db, NEW, session_mgr=SimpleNamespace(stop_all_held_processes=held_cleanup))
            held_cleanup.assert_awaited_once()
            assert running.process.returncode is None and running.pid == pid
            assert app_scope_token("test-app") == token
            (directory / "release").touch()
            response = await request
            assert response.status_code == 200 and response.text == "complete"
    finally:
        (directory / "release").touch()
        if request is not None:
            await asyncio.gather(request, return_exceptions=True)
        await supervisor.shutdown()


@pytest.fixture
def env_file(tmp_path, monkeypatch):
    """An env file that defines the token, found the way the real one is."""
    path = tmp_path / ".env"
    path.write_text(
        "OCTOPUS_HOST=0.0.0.0\nOCTOPUS_AUTH_TOKEN=old-token-1234\nOCTOPUS_PORT=8080\n"
    )
    monkeypatch.setenv("OCTOPUS_ENV_FILE", str(path))
    monkeypatch.delenv("OCTOPUS_AUTH_TOKEN", raising=False)
    monkeypatch.setattr(settings, "auth_token", OLD)
    return path


@pytest.fixture
async def db(tmp_path):
    d = Database(str(tmp_path / "rot.db"))
    await d.initialize()
    yield d
    await d.close()


async def _seed_secrets(db: Database) -> None:
    await db._ensure_connected()
    # Parents first: the secret tables are FK-bound to the rows they belong to.
    await db._conn.execute(
        "INSERT INTO backend_credentials "
        "(id, backend, label, auth_type, secret_encrypted, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("cred-1", "claude-code", "key", "api_key", "", "t"),
    )
    await db._conn.execute(
        "INSERT INTO connector_installations "
        "(id, kind, label, auth_type, created_at) VALUES (?, ?, ?, ?, ?)",
        ("inst-1", "github", "acct", "oauth", "t"),
    )
    await db._conn.execute(
        "INSERT INTO credential_secrets (credential_id, secret_encrypted) VALUES (?, ?)",
        ("cred-1", encrypt("sk-ant-secret", OLD)),
    )
    await db._conn.execute(
        "INSERT INTO connector_installation_secrets (installation_id, secret_encrypted) "
        "VALUES (?, ?)",
        ("inst-1", encrypt('{"access_token": "gho_x"}', OLD)),
    )
    await db._conn.execute(
        "INSERT INTO connector_oauth_clients "
        "(kind, client_id, client_secret_encrypted, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?)",
        ("github", "cid", encrypt("client-secret", OLD), "t", "t"),
    )
    await db._conn.commit()


async def _stored(db: Database, table: str, column: str, key_col: str, key: str) -> str:
    cursor = await db._conn.execute(
        f"SELECT {column} FROM {table} WHERE {key_col} = ?", (key,)
    )
    return (await cursor.fetchone())[0]


# ------------------------------------------------------------------ refusals


@pytest.mark.parametrize(
    "candidate, because",
    [
        ("", "empty"),
        ("   ", "blank"),
        ("short", "too short"),
        ("changeme", "the default"),
        ("has spaces in it", "whitespace breaks a URL query"),
        ("semi;colon-token", "a cookie value ends at the semicolon"),
        (OLD, "already the current token"),
    ],
)
def test_a_token_that_would_break_something_is_refused(candidate, because, env_file):
    with pytest.raises(TokenRotationError):
        validate_new_token(candidate)


def test_the_env_file_is_found_by_what_it_defines(env_file):
    assert env_files_defining_token() == [env_file.resolve()]


async def test_rotation_cannot_claim_a_token_that_changes_when_settings_reload(db, env_file):
    token = "rotation-${OCTOPUS_PORT}"
    original = env_file.read_text()
    await db.save_credential("reload", "codex", "Personal", "api_key", encrypt("secret", OLD), "t")
    try:
        await rotate_auth_token(db, token)
    except TokenRotationError:
        assert env_file.read_text() == original
        assert settings.auth_token == OLD
        assert decrypt((await db.get_credential("reload"))["secret_encrypted"], OLD) == "secret"
    else:
        env = dict(os.environ, OCTOPUS_ENV_FILE=str(env_file))
        env.pop("OCTOPUS_AUTH_TOKEN", None)
        loaded = subprocess.run(
            [sys.executable, "-c", "import json; from server.config import settings; print(json.dumps(settings.auth_token))"],
            env=env, capture_output=True, text=True, timeout=5,
        )
        assert loaded.returncode == 0, loaded.stderr
        assert json.loads(loaded.stdout) == token


# ----------------------------------------------------------------- rotating


@pytest.mark.asyncio
async def test_rotation_rekeys_every_secret_and_the_live_token(db, env_file):
    await _seed_secrets(db)

    result = await rotate_auth_token(db, NEW)

    # The live token changed…
    assert settings.auth_token == NEW
    # …the file it has to survive a restart in changed…
    text = env_file.read_text()
    assert f"OCTOPUS_AUTH_TOKEN={NEW}" in text
    # …and nothing else in that file was touched.
    assert "OCTOPUS_HOST=0.0.0.0" in text and "OCTOPUS_PORT=8080" in text

    # …and every secret now opens with the new token, not the old one.
    cred = await _stored(db, "credential_secrets", "secret_encrypted", "credential_id", "cred-1")
    assert decrypt(cred, NEW) == "sk-ant-secret"
    with pytest.raises(ValueError):
        decrypt(cred, OLD)

    inst = await _stored(
        db, "connector_installation_secrets", "secret_encrypted", "installation_id", "inst-1"
    )
    assert decrypt(inst, NEW) == '{"access_token": "gho_x"}'
    client = await _stored(
        db, "connector_oauth_clients", "client_secret_encrypted", "kind", "github"
    )
    assert decrypt(client, master_key()) == "client-secret"

    assert result.reencrypted == {
        "credential_secrets": 1,
        "connector_installation_secrets": 1,
        "connector_oauth_clients": 1,
    }


@pytest.mark.asyncio
async def test_a_secret_that_cannot_be_decrypted_changes_nothing(db, env_file):
    """Half a rotation is a database where some secrets are keyed to a token
    nobody has. Better to refuse and say which row is the problem."""
    await _seed_secrets(db)
    await db._conn.execute(
        "INSERT INTO backend_credentials "
        "(id, backend, label, auth_type, secret_encrypted, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("cred-2", "codex", "other", "api_key", "", "t"),
    )
    await db._conn.execute(
        "INSERT INTO credential_secrets (credential_id, secret_encrypted) VALUES (?, ?)",
        ("cred-2", encrypt("keyed-to-something-else", "a-different-token")),
    )
    await db._conn.commit()

    with pytest.raises(TokenRotationError) as e:
        await rotate_auth_token(db, NEW)
    assert "cred-2" in e.value.message

    assert settings.auth_token == OLD
    assert f"OCTOPUS_AUTH_TOKEN={OLD}" in env_file.read_text()
    cred = await _stored(db, "credential_secrets", "secret_encrypted", "credential_id", "cred-1")
    assert decrypt(cred, OLD) == "sk-ant-secret"


@pytest.mark.asyncio
@pytest.mark.parametrize("cancellations", [1, 2])
async def test_cancelled_rotation_waiter_keeps_the_command_and_message_writer_owned(db, env_file, monkeypatch, cancellations):
    await _seed_secrets(db)
    await db.save_session("active", "active conversation", "/tmp", "2026-10-14")
    await db.append_message("active", 0, "assistant", "text", "Before cancellation")
    entered, release = asyncio.Event(), asyncio.Event()
    execute = db.conn.execute

    async def hold_second_secret(sql, parameters=()):
        if sql.startswith("UPDATE connector_installation_secrets"):
            entered.set()
            await release.wait()
        return await execute(sql, parameters)

    monkeypatch.setattr(db.conn, "execute", hold_second_secret)
    rotation = asyncio.create_task(rotate_auth_token(db, NEW))
    concurrent = None
    try:
        await asyncio.wait_for(entered.wait(), 1)
        concurrent = asyncio.create_task(
            db.append_message("active", 1, "assistant", "text", "After cancellation")
        )
        for _ in range(cancellations):
            rotation.cancel("HTTP waiter disconnected")
            await asyncio.sleep(0)
        assert not rotation.done()
        assert not concurrent.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(rotation, 3)
        await asyncio.wait_for(concurrent, 1)
    finally:
        release.set()
        await asyncio.gather(rotation, *([concurrent] if concurrent else []), return_exceptions=True)

    assert [row["content"] for row in await db.load_messages("active")] == ["Before cancellation", "After cancellation"]
    assert decrypt(await _stored(db, "credential_secrets", "secret_encrypted", "credential_id", "cred-1"), NEW) == "sk-ant-secret"
    assert settings.auth_token == NEW
    assert f"OCTOPUS_AUTH_TOKEN={NEW}" in env_file.read_text()


@pytest.mark.asyncio
@pytest.mark.parametrize("cancellations", [1, 2])
async def test_cancelled_rotation_waiter_finishes_captured_commit_before_key_handoff(db, env_file, monkeypatch, cancellations):
    await _seed_secrets(db)
    commit = db.conn.commit
    entered, release = asyncio.Event(), asyncio.Event()

    async def delayed_acknowledgement():
        await commit()
        entered.set()
        await release.wait()

    monkeypatch.setattr(db.conn, "commit", delayed_acknowledgement)
    rotation = asyncio.create_task(rotate_auth_token(db, NEW))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        for _ in range(cancellations):
            rotation.cancel()
            await asyncio.sleep(0)
        assert not rotation.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(rotation, 3)
    finally:
        release.set()
        await asyncio.gather(rotation, return_exceptions=True)
    assert settings.auth_token == NEW
    assert f"OCTOPUS_AUTH_TOKEN={NEW}" in env_file.read_text()
    assert decrypt(await _stored(db, "credential_secrets", "secret_encrypted", "credential_id", "cred-1"), NEW) == "sk-ant-secret"


@pytest.mark.asyncio
async def test_a_rotation_with_nowhere_to_persist_is_refused(db, tmp_path, monkeypatch):
    """A token that can't survive a restart would leave the next boot unable
    to decrypt the database it just re-keyed."""
    monkeypatch.setenv("OCTOPUS_ENV_FILE", str(tmp_path / "nonexistent.env"))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(settings, "auth_token", OLD)

    with pytest.raises(TokenRotationError) as e:
        await rotate_auth_token(db, NEW)
    assert e.value.status_code == 409
    assert settings.auth_token == OLD


async def test_external_startup_token_override_is_refused_before_effects(db, env_file, monkeypatch):
    await _seed_secrets(db)
    original = env_file.read_bytes()
    monkeypatch.setenv("OCTOPUS_AUTH_TOKEN", OLD)
    with pytest.raises(TokenRotationError) as refusal:
        await rotate_auth_token(db, NEW)
    assert refusal.value.status_code == 409
    assert env_file.read_bytes() == original
    assert settings.auth_token == OLD
    assert decrypt((await db.get_credential("cred-1"))["secret_encrypted"], OLD) == "sk-ant-secret"


@pytest.mark.parametrize("source", ["lower_parent", "mixed_parent", "duplicate_file", "lower_file", "mixed_file", "export_file", "commented_file"])
async def test_rotation_matches_the_case_insensitive_settings_sources(db, env_file, monkeypatch, source):
    await _seed_secrets(db)
    alias = "octopus_auth_token" if source.startswith("lower") else "Octopus_Auth_Token"
    if source.endswith("parent"):
        monkeypatch.setenv(alias, OLD)
    elif source == "duplicate_file":
        env_file.write_text(env_file.read_text() + f"octopus_auth_token={OLD}\n")
    elif source == "export_file":
        env_file.write_text(env_file.read_text().replace(f"OCTOPUS_AUTH_TOKEN={OLD}", f" \texport\t{alias} \t= \t'{OLD}' # token"))
    elif source == "commented_file":
        env_file.write_text(f"# 'octopus_auth_token'={OLD}\n" + env_file.read_text())
    else:
        env_file.write_text(env_file.read_text().replace("OCTOPUS_AUTH_TOKEN", alias))
    original = env_file.read_bytes()

    def cold_token():
        loaded = subprocess.run(
            [sys.executable, "-c", "import json; from server.config import settings; print(json.dumps(settings.auth_token))"],
            env=dict(os.environ), capture_output=True, text=True, timeout=5,
        )
        assert loaded.returncode == 0, loaded.stderr
        return json.loads(loaded.stdout)

    assert cold_token() == OLD, "the source must first be recognized by actual Settings"
    if source.endswith("parent") or source == "duplicate_file":
        with pytest.raises(TokenRotationError) as refused:
            await rotate_auth_token(db, NEW)
        assert refused.value.status_code == 409
        assert env_file.read_bytes() == original
        assert cold_token() == OLD and settings.auth_token == OLD
        assert decrypt((await db.get_credential("cred-1"))["secret_encrypted"], OLD) == "sk-ant-secret"
        async with db.transaction(write=False) as tx:
            assert await tx.fetch_one("SELECT id FROM token_rotation_intent") is None
    else:
        await rotate_auth_token(db, NEW)
        assert cold_token() == NEW and settings.auth_token == NEW
        assert decrypt((await db.get_credential("cred-1"))["secret_encrypted"], NEW) == "sk-ant-secret"


@pytest.mark.parametrize("concurrent_change", ["master_client", "invalid_secret"])
async def test_rotation_rereads_the_actual_secrets_after_file_publication(db, env_file, monkeypatch, concurrent_change):
    from pathlib import Path

    from server.connector_manager import ConnectorManager

    await _seed_secrets(db)
    await db.save_session("active", "Unrelated transcript", "/tmp", "2026-10-20")
    entered, release = threading.Event(), threading.Event()
    fsync = os.fsync

    def hold_file_write(descriptor):
        path = Path(os.readlink(f"/proc/self/fd/{descriptor}"))
        if not entered.is_set() and path.name.startswith(".env-rotate-"):
            entered.set()
            assert release.wait(3), "token file publication was not released"
        return fsync(descriptor)

    monkeypatch.setattr(os, "fsync", hold_file_write)
    rotating = asyncio.create_task(rotate_auth_token(db, NEW))
    clients = ConnectorManager(db)
    try:
        assert await asyncio.to_thread(entered.wait, 3)
        # No SQL transaction spans filesystem work or this independent write.
        await db.append_message("active", 0, "assistant", "text", "Committed during file publication")
        if concurrent_change == "master_client":
            await clients.set_client_creds("github", "latest-client", "latest-secret")
        else:
            await db.save_credential("later-invalid", "claude-code", "Retain evidence", "api_key", encrypt("other-key-secret", "another-key"), "2026-10-20")
        release.set()
        if concurrent_change == "master_client":
            result = await asyncio.wait_for(rotating, 3)
            assert result.reencrypted["connector_oauth_clients"] == 0
            assert await clients.resolve_client_creds("github") == ("latest-client", "latest-secret")
            expected = NEW
        else:
            with pytest.raises(TokenRotationError) as refused:
                await asyncio.wait_for(rotating, 3)
            assert refused.value.status_code == 409
            assert await db.get_credential("later-invalid") is not None
            expected = OLD
        assert settings.auth_token == expected
        assert f"OCTOPUS_AUTH_TOKEN={expected}" in env_file.read_text()
        assert decrypt((await db.get_credential("cred-1"))["secret_encrypted"], expected) == "sk-ant-secret"
        assert (await db.load_messages("active"))[0]["content"] == "Committed during file publication"
    finally:
        release.set()
        await asyncio.gather(rotating, return_exceptions=True)


@pytest.mark.asyncio
async def test_rotation_works_with_no_secrets_stored(db, env_file):
    result = await rotate_auth_token(db, NEW)
    assert settings.auth_token == NEW
    assert sum(result.reencrypted.values()) == 0


# --------------------------------------------------------------------- route


@pytest.fixture
async def client(db, env_file):
    session_manager.sessions.clear()
    session_manager._admission_closed = False
    await session_manager.initialize(db)
    auth_router.set_db(db)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.mark.parametrize("quoted_name", ["octopus_auth_token", "OCTOPUS_AUTH_TOKEN"])
@pytest.mark.parametrize("quoted_first", [True, False])
async def test_the_route_refuses_quoted_duplicate_token_definitions_before_effects(
    client, db, env_file, quoted_name, quoted_first,
):
    definitions = [f"'{quoted_name}'={OLD}\n", f"OCTOPUS_AUTH_TOKEN={OLD}\n"]
    if not quoted_first:
        definitions.reverse()
    env_file.write_text("".join(definitions))
    original = env_file.read_bytes()
    ciphertext = encrypt("retained secret", OLD)
    await db.save_credential("duplicate-source", "codex", "Personal", "api_key", ciphertext, "t")

    response = await client.post("/api/auth/rotate", json={"new_token": NEW}, headers=HEADERS)

    assert response.status_code == 409, response.text
    assert env_file.read_bytes() == original
    assert settings.auth_token == OLD
    credential = await db.get_credential("duplicate-source")
    assert credential["secret_encrypted"] == ciphertext
    assert decrypt(credential["secret_encrypted"], OLD) == "retained secret"
    async with db.transaction(write=False) as tx:
        assert await tx.fetch_one("SELECT id FROM token_rotation_intent") is None


@pytest.mark.asyncio
async def test_the_route_needs_the_old_token_and_reports_what_it_did(client, db, env_file):
    await _seed_secrets(db)

    assert (
        await client.post("/api/auth/rotate", json={"new_token": NEW})
    ).status_code in (401, 403)

    resp = await client.post(
        "/api/auth/rotate", json={"new_token": NEW}, headers=HEADERS
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["env_files"] == [str(env_file.resolve())]
    assert body["reencrypted"]["credential_secrets"] == 1

    # The old token stops working immediately; the new one works.
    assert (await client.get("/api/sessions", headers=HEADERS)).status_code in (401, 403)
    assert (
        await client.get("/api/sessions", headers={"Authorization": f"Bearer {NEW}"})
    ).status_code == 200


@pytest.mark.asyncio
async def test_open_clients_are_handed_the_new_token_unless_revoking(client, db, env_file):
    seen: list[dict] = []

    async def collect(msg: dict) -> None:
        seen.append(msg)

    session_manager.on_broadcast("rotation-test", collect)
    try:
        await client.post(
            "/api/auth/rotate", json={"new_token": NEW}, headers=HEADERS
        )
        rotated = [m for m in seen if m["type"] == "auth_token_rotated"]
        assert rotated and rotated[-1]["token"] == NEW

        seen.clear()
        third = "third-token-xyz-123"
        await client.post(
            "/api/auth/rotate",
            json={"new_token": third, "revoke_other_clients": True},
            headers={"Authorization": f"Bearer {NEW}"},
        )
        rotated = [m for m in seen if m["type"] == "auth_token_rotated"]
        # Nothing to hand out: every other client must prove it has the new one.
        assert rotated and rotated[-1]["token"] is None
    finally:
        session_manager.remove_broadcast("rotation-test")


@pytest.mark.asyncio
async def test_the_route_refuses_a_weak_token_without_touching_anything(
    client, db, env_file
):
    resp = await client.post(
        "/api/auth/rotate", json={"new_token": "short"}, headers=HEADERS
    )
    assert resp.status_code == 400
    assert settings.auth_token == OLD


# `/api/auth/identity` answers `OCTOPUS_USER_LABEL` before accounts exist and
# the signed-in account's username afterward. Its tests live with the other
# account routes, in tests/test_auth_routes.py.
