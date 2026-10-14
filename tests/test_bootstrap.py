"""Turning this install into its first account (multi-tenancy.md §9).

The one irreversible step in the plan, so what is tested is what a failure
would leave behind — not just the happy path. A half-upgraded install, with an
account whose credentials do not decrypt or with rows nobody owns, is worse
than either the before or the after.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from httpx import ASGITransport, AsyncClient

from server import crypto, deps
from server.bootstrap import BootstrapError, bootstrap_first_account
from server.config import settings
from server.crypto import decrypt, encrypt
from server.database import Database
from server.main import app
from server.routers import auth as auth_router
from server.session_manager import session_manager
from server.users import UserManager
from server.workspace import paths_for


async def test_bootstrap_adopts_registered_wiki_state_and_exact_root(legacy, tmp_path, monkeypatch):
    from server.wiki.jobs import WikiJobs
    from server.wiki.registry import registry
    db, agent = legacy
    registry.legacy_enabled = True
    registry.unblock(None)
    vault = tmp_path / "outside-vault"
    (vault / "Library").mkdir(parents=True)
    (vault / "Library/note.md").write_text("# Note\noriginal material\n")
    config = registry.configure(None, vault, agent["id"])
    note = registry.store(None).read("Library/note.md")
    jobs = WikiJobs(None)
    job = jobs.maintain("migration-pending", "", "verify note", ["Library/note.md"], "", [], "human")
    try:
        summary = await bootstrap_first_account(db, username="wikiowner", password="password1", session_manager=session_manager)
        owner = await db.get_user(summary["user_id"])
        assert registry.config(owner["id"])["vault_id"] == config["vault_id"]
        current = registry.store(owner["id"]).read("Library/note.md")
        assert current["id"] == note["id"] and current["revision"] == note["revision"]
        assert WikiJobs(owner["id"]).get(job["id"])["id"] == job["id"]
        assert str(vault.resolve()) in json.loads(owner["extra_roots"])
        assert str(vault.parent.resolve()) not in json.loads(owner["extra_roots"])
        assert not (registry.state(None) / "config.json").exists()
        assert (vault / "Library/note.md").read_text() == "# Note\noriginal material\n"
    finally:
        registry.close()
        registry.legacy_enabled = True
        registry._blocked.clear()


async def _stored_secret(db, credential_id: str) -> str:
    """The ciphertext as it sits in the split-out secrets table."""
    cursor = await db.conn.execute(
        "SELECT secret_encrypted FROM credential_secrets WHERE credential_id = ?",
        (credential_id,),
    )
    row = await cursor.fetchone()
    return row[0]


@pytest.fixture
async def legacy(tmp_path, monkeypatch):
    """An install as it was before accounts: rows with no owner, secrets keyed
    to `OCTOPUS_AUTH_TOKEN`, sessions working outside any workspace."""
    monkeypatch.setattr(settings, "master_key", "test-master-key")
    monkeypatch.setattr(settings, "auth_token", "the-old-token")
    monkeypatch.setattr(settings, "users_root", str(tmp_path / "users"))
    monkeypatch.setattr(settings, "agents_dir", str(tmp_path / "legacy-agents"))
    crypto._MASTER_CACHE.clear()

    db = Database(str(tmp_path / "legacy.db"))
    await db.initialize()
    session_manager.sessions.clear()
    session_manager._admission_closed = False
    await session_manager.initialize(db)

    agent = await db.get_system_agent()
    await db.save_session(
        session_id="s-old",
        name="work in progress",
        working_dir="/home/somebody/a-repo",
        created_at="2026-01-01T00:00:00Z",
        agent_id=agent["id"],
    )
    await db.save_credential(
        credential_id="cred-1",
        backend="claude-code",
        label="Claude",
        auth_type="api_key",
        secret_encrypted=encrypt("sk-ant-the-real-key", "the-old-token"),
        created_at="2026-01-01T00:00:00Z",
    )
    # Agent memory where a pre-accounts install keeps it.
    legacy_memory = tmp_path / "legacy-agents" / agent["id"] / "memory"
    legacy_memory.mkdir(parents=True)
    (legacy_memory / "MEMORY.md").write_text("- [Magic word](magic.md)\n")

    try:
        yield db, agent
    finally:
        await db.close()
        session_manager.sessions.clear()
        session_manager._admission_closed = False


@pytest.mark.asyncio
async def test_the_upgrade_hands_over_everything_that_was_here(legacy):
    db, agent = legacy

    summary = await bootstrap_first_account(
        db, username="archer", password="password1"
    )

    user = await db.get_user(summary["user_id"])
    assert user["is_admin"] is True, "the first account has to be able to invite"

    # The credential still opens — with the account's key, not the old token.
    row = await _stored_secret(db, "cred-1")
    users = UserManager(db)
    assert decrypt(row, users.data_key(user)) == "sk-ant-the-real-key"
    with pytest.raises(ValueError):
        decrypt(row, "the-old-token")

    # Every row belongs to them, and nothing is left unowned.
    assert summary["adopted"].get("sessions") == 1
    assert await db.count_orphan_rows() == {}

    # Their existing working directory keeps working, which is the difference
    # between an upgrade and a breakage.
    assert "/home/somebody/a-repo" in json.loads(user["extra_roots"])

    # Agent memory moved under them rather than being left behind.
    moved = paths_for(user["id"]).agents / agent["id"] / "memory" / "MEMORY.md"
    assert moved.exists() and "Magic word" in moved.read_text()


@pytest.mark.asyncio
async def test_a_secret_that_will_not_decrypt_changes_nothing(legacy):
    """The failure that matters. Re-keying reads everything before it writes
    anything, so a row that cannot be decrypted costs nothing rather than
    leaving half the secrets keyed to something nobody has."""
    db, _agent = legacy
    await db.save_credential(
        credential_id="cred-broken",
        backend="claude-code",
        label="Corrupt",
        auth_type="api_key",
        secret_encrypted="not-a-ciphertext",
        created_at="2026-01-01T00:00:00Z",
    )

    with pytest.raises(BootstrapError):
        await bootstrap_first_account(db, username="archer", password="password1")

    # The good secret is untouched and still opens with the old token.
    assert decrypt(await _stored_secret(db, "cred-1"), "the-old-token")


@pytest.mark.asyncio
async def test_failed_bootstrap_rekey_restores_secrets_and_keeps_concurrent_messages(legacy, monkeypatch):
    db, _agent = legacy
    await db.save_connector_installation(
        installation_id="inst-1", kind="github", label="GitHub", auth_type="oauth",
        secret_encrypted=encrypt("github-secret", "the-old-token"), created_at="2026-10-14",
    )
    await db.append_message("s-old", 0, "assistant", "text", "Before upgrade")
    entered, release = asyncio.Event(), asyncio.Event()
    execute = db.conn.execute

    async def fail_second_secret(sql, parameters=()):
        if sql.startswith("UPDATE connector_installation_secrets"):
            entered.set()
            await release.wait()
            raise OSError("secret update failed")
        return await execute(sql, parameters)

    monkeypatch.setattr(db.conn, "execute", fail_second_secret)
    upgrade = asyncio.create_task(bootstrap_first_account(db, username="archer", password="password1"))
    concurrent = None
    try:
        await asyncio.wait_for(entered.wait(), 1)
        concurrent = asyncio.create_task(
            db.append_message("s-old", 1, "assistant", "text", "During upgrade")
        )
        await asyncio.wait({concurrent}, timeout=0.05)
        release.set()
        with pytest.raises(OSError, match="secret update failed"):
            await upgrade
        await concurrent
    finally:
        release.set()
        await asyncio.gather(upgrade, *([concurrent] if concurrent else []), return_exceptions=True)

    await db.flush()
    assert [row["content"] for row in await db.load_messages("s-old")] == ["Before upgrade", "During upgrade"]
    assert decrypt(await _stored_secret(db, "cred-1"), "the-old-token") == "sk-ant-the-real-key"
    assert decrypt(await db.get_connector_secret("inst-1"), "the-old-token") == "github-secret"


@pytest.mark.asyncio
async def test_it_can_only_happen_once(legacy):
    db, _agent = legacy
    await bootstrap_first_account(db, username="archer", password="password1")
    with pytest.raises(BootstrapError) as e:
        await bootstrap_first_account(db, username="vera", password="password2")
    assert e.value.status_code == 409


@pytest.mark.asyncio
@pytest.mark.parametrize("acknowledgement_error", [OSError, asyncio.CancelledError])
async def test_committed_bootstrap_rekey_with_lost_acknowledgement_finishes_adoption(legacy, monkeypatch, acknowledgement_error):
    db, _agent = legacy
    commit = db.conn.commit
    injected = False

    async def lose_rekey_acknowledgement():
        nonlocal injected
        ciphertext = await _stored_secret(db, "cred-1")
        changed = False
        try:
            decrypt(ciphertext, "the-old-token")
        except ValueError:
            changed = True
        await commit()
        if changed and not injected:
            injected = True
            raise acknowledgement_error("Commit acknowledgement lost")

    monkeypatch.setattr(db.conn, "commit", lose_rekey_acknowledgement)
    summary = await bootstrap_first_account(db, username="archer", password="password1")
    assert injected and summary["adopted"]["sessions"] == 1
    user = await db.get_user(summary["user_id"])
    assert decrypt(await _stored_secret(db, "cred-1"), UserManager(db).data_key(user)) == "sk-ant-the-real-key"
    assert await db.count_orphan_rows() == {}


@pytest.mark.asyncio
async def test_unverifiable_bootstrap_rekey_retains_account_and_reports_uncertainty(legacy, monkeypatch):
    from server import token_rotation

    db, _agent = legacy
    commit = db.conn.commit

    async def lose_rekey_acknowledgement():
        ciphertext = await _stored_secret(db, "cred-1")
        changed = False
        try:
            decrypt(ciphertext, "the-old-token")
        except ValueError:
            changed = True
        await commit()
        if changed:
            raise OSError("Commit acknowledgement lost")

    def unavailable_reader(*args, **kwargs):
        raise OSError("Committed read unavailable")

    monkeypatch.setattr(db.conn, "commit", lose_rekey_acknowledgement)
    monkeypatch.setattr(token_rotation.aiosqlite, "connect", unavailable_reader)
    with pytest.raises(BootstrapError) as exc:
        await bootstrap_first_account(db, username="archer", password="password1")
    assert exc.value.status_code == 503
    assert "unconfirmed" in exc.value.message.lower()
    assert "nothing was changed" not in exc.value.message.lower()
    user = await db.get_user_by_username("archer")
    assert user is not None
    assert decrypt(await _stored_secret(db, "cred-1"), UserManager(db).data_key(user)) == "sk-ant-the-real-key"
    monkeypatch.setattr(db.conn, "commit", commit)


@pytest.mark.asyncio
async def test_the_route_is_the_only_way_in_and_closes_behind_itself(legacy):
    """The install token authenticates the upgrade and stops working the
    instant it succeeds — the same fact stated twice."""
    db, _agent = legacy
    auth_router.set_db(db)
    deps.set_user_manager(UserManager(db))

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        state = await c.get("/api/auth/state")
        assert state.status_code == 200 and state.json()["accounts_exist"] is False

        res = await c.post(
            "/api/auth/bootstrap",
            json={"username": "archer", "password": "password1"},
            headers={"Authorization": "Bearer the-old-token"},
        )
        assert res.status_code == 200
        token = res.json()["token"]

        # The install token is now dead, the session bearer works, and the
        # screen knows which question to ask.
        assert (
            await c.get(
                "/api/auth/identity", headers={"Authorization": "Bearer the-old-token"}
            )
        ).status_code == 401
        assert (
            await c.get(
                "/api/auth/identity", headers={"Authorization": f"Bearer {token}"}
            )
        ).json()["label"] == "archer"
        assert (await c.get("/api/auth/state")).json()["accounts_exist"] is True

        # And it cannot be used a second time, even with the old token.
        again = await c.post(
            "/api/auth/bootstrap",
            json={"username": "vera", "password": "password2"},
            headers={"Authorization": "Bearer the-old-token"},
        )
        assert again.status_code == 401


@pytest.mark.asyncio
async def test_the_live_sessions_are_adopted_too(legacy):
    """The rows are only half of it.

    `adopt_orphan_rows` updates the database; the `Session` objects already in
    memory keep `user_id = None`. A server that is not restarted therefore goes
    on filtering the new owner's own sessions out of their own list, and fans
    their frames out to everyone — `_audience` reads the same field. Found in
    the e2e run: after claiming the install, the session made a moment earlier
    vanished from the sidebar.
    """
    db, _agent = legacy
    # The manager as a running server has it: loaded before the account existed.
    await session_manager.initialize(db)
    assert session_manager.sessions["s-old"].user_id is None

    summary = await bootstrap_first_account(
        db,
        username="archer",
        password="password1",
        session_manager=session_manager,
    )

    assert summary["live_sessions_adopted"] == 1
    assert session_manager.sessions["s-old"].user_id == summary["user_id"]
    # And the owner can now see it without the server being restarted.
    assert [s.id for s in session_manager.list_sessions(summary["user_id"])] == [
        "s-old"
    ]


@pytest.mark.asyncio
async def test_every_secret_still_opens_through_the_paths_that_read_it(legacy):
    """Re-keying is only half of it: the readers have to find the same key.

    Both of them derive it from the *row's* owner — `data_key_for(row["user_id"])`
    — and neither `_CREDENTIAL_COLS` nor `_CONNECTOR_COLS` selected `user_id`,
    so `row.get("user_id")` was None and the key fell back to the install
    token. The connector path answered 500; the credential path is worse,
    because `resolve_credential_by_id` catches the failure and runs the turn
    *without* the user's credential — silently, on every turn, forever.

    Found by deploying a copy of a real install and asking it for a connector
    token. So this asserts through the readers rather than by re-deriving the
    key, which is exactly what the earlier tests did and why they passed.
    """
    from server import deps
    from server.connector_manager import ConnectorManager
    from server.crypto import encrypt
    from server.users import UserManager

    db, _agent = legacy
    users = UserManager(db)
    deps.set_user_manager(users)

    # A connector installation, keyed to the install token, as one written
    # before accounts would be.
    await db.save_connector_installation(
        installation_id="inst-1",
        kind="github",
        label="archeryue",
        auth_type="oauth",
        secret_encrypted=encrypt(
            json.dumps({"access_token": "gho_real", "expires_at_epoch": None}),
            "the-old-token",
        ),
        created_at="2026-01-01T00:00:00Z",
    )

    summary = await bootstrap_first_account(
        db, username="archer", password="password1"
    )
    uid = summary["user_id"]

    # The rows can say who owns them...
    cred = (await db.load_credentials(uid))[0]
    assert cred["user_id"] == uid, "a credential row cannot name its owner"
    inst = await db.get_connector_installation("inst-1", uid)
    assert inst["user_id"] == uid, "an installation row cannot name its owner"

    # ...so the readers find the right key. Through the real call, not a
    # key this test derived for itself.
    mgr = ConnectorManager(db)
    token = await mgr.get_access_token("inst-1")
    assert token["access_token"] == "gho_real"

    session_manager.sessions.clear()
    session_manager._admission_closed = False
    await session_manager.initialize(db)
    resolved = await session_manager.resolve_credential_by_id(
        cred["id"], style="env", context="test"
    )
    assert resolved is not None, (
        "the credential resolved to nothing — the turn would have run without it"
    )
