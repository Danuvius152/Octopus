"""Persistent first-account migration, failure recovery and tenant isolation."""

import json
import sqlite3

import pytest
from httpx import ASGITransport, AsyncClient

from server.bootstrap import BootstrapError, bootstrap_first_account
from server.crypto import decrypt, encrypt
from server.main import app
from server.session_manager import session_manager
from server.users import UserManager
from server.wiki import account_upgrade as upgrade
from server.wiki.registry import registry
from tests.test_bootstrap import legacy as legacy_install

legacy = legacy_install


@pytest.fixture
async def registered(legacy, tmp_path):
    db, agent = legacy
    vault = tmp_path / "original-vault"
    (vault / "Library").mkdir(parents=True)
    (vault / "Library/note.md").write_text("# Source\nKeep this material.\n")
    config = registry.configure(None, vault, agent["id"])
    note = registry.store(None).read("Library/note.md")
    try:
        yield db, vault, config, note
    finally:
        registry.close()


@pytest.mark.parametrize("phase", ["publish", "reopen"])
async def test_failed_migration_can_resume_without_losing_identity_or_secrets(registered, monkeypatch, phase):
    db, vault, config, note = registered
    original = upgrade.publish if phase == "publish" else registry._store

    def fail(*args):
        if phase == "reopen" and args[0] is None:
            return original(*args)
        raise OSError("injected migration boundary")

    target = upgrade if phase == "publish" else registry
    attribute = "publish" if phase == "publish" else "_store"
    monkeypatch.setattr(target, attribute, fail)
    with pytest.raises(BootstrapError, match="resume") as error:
        await bootstrap_first_account(db, username="owner", password="password1", session_manager=session_manager)
    assert error.value.status_code == 503
    users = UserManager(db)
    user = await users.authenticate("owner", "password1")
    assert upgrade.pending_upgrade()["user_id"] == user["id"]
    assert session_manager._legacy_transition
    assert (vault / "Library/note.md").read_text() == "# Source\nKeep this material.\n"
    monkeypatch.setattr(target, attribute, original)
    result = await upgrade.resume_upgrade(db, user, session_manager)
    assert result["wiki"]["vault_id"] == config["vault_id"]
    assert registry.store(user["id"]).read("Library/note.md")["id"] == note["id"]
    assert upgrade.pending_upgrade() is None
    assert not session_manager._legacy_transition


async def test_resume_keeps_another_accounts_roots_and_secret_private(registered, monkeypatch, tmp_path):
    db, vault, _, _ = registered
    publish = upgrade.publish
    monkeypatch.setattr(upgrade, "publish", lambda *_: (_ for _ in ()).throw(OSError("publish failure")))
    with pytest.raises(BootstrapError):
        await bootstrap_first_account(db, username="owner", password="password1", session_manager=session_manager)
    users = UserManager(db)
    owner = await users.authenticate("owner", "password1")
    other = await users.create_user(username="second", password="password2")
    private = tmp_path / "second-private"
    private.mkdir()
    agent = await db.get_system_agent(user_id=other["id"])
    if agent is None:
        from server.agent_manager import AgentManager
        agent = await AgentManager(db).create_agent(name="Second private", backend="codex", user_id=other["id"])
    await db.save_session(session_id="second-session", name="Private", working_dir=str(private),
                          created_at="2026-10-14", agent_id=agent["id"], user_id=other["id"])
    encrypted = encrypt("other-provider-secret", users.data_key(other))
    await db.save_credential(credential_id="second-secret", backend="codex", auth_type="api_key",
                             label="Other", secret_encrypted=encrypted, user_id=other["id"], created_at="2026-10-14")
    monkeypatch.setattr(upgrade, "publish", publish)
    await upgrade.resume_upgrade(db, owner, session_manager)
    owner = await db.get_user(owner["id"])
    assert str(private) not in json.loads(owner["extra_roots"])
    assert str(vault) in json.loads(owner["extra_roots"])
    row = await db.get_credential("second-secret", other["id"])
    assert decrypt(row["secret_encrypted"], users.data_key(other)) == "other-provider-secret"


async def test_resume_recovers_committed_account_before_owner_journal_ack(registered, monkeypatch):
    db, _, config, _ = registered
    original = upgrade.save
    lost = False

    def lose_owner_ack(journal):
        nonlocal lost
        if journal["phase"] == "account_created":
            lost = True
            raise OSError("owner journal write unavailable")
        return original(journal)

    monkeypatch.setattr(upgrade, "save", lose_owner_ack)
    with pytest.raises(OSError):
        await bootstrap_first_account(db, username="owner", password="password1", session_manager=session_manager)
    assert lost
    assert upgrade.pending_upgrade()["user_id"] is None
    monkeypatch.setattr(upgrade, "save", original)
    user = await UserManager(db).authenticate("owner", "password1")
    result = await upgrade.resume_upgrade(db, user, session_manager)
    assert result["wiki"]["vault_id"] == config["vault_id"]


async def test_opt_out_retains_legacy_state_without_granting_its_root(registered):
    db, vault, _, _ = registered
    summary = await bootstrap_first_account(db, username="owner", password="password1", adopt=False, session_manager=session_manager)
    assert summary["wiki"] == {"adopted": False}
    assert (registry.state(None) / "config.json").is_file()
    assert not (registry.state(summary["user_id"]) / "config.json").exists()
    assert not registry.legacy_enabled
    assert not any(owner is None for owner, _ in registry.states())
    assert str(vault) not in json.loads((await db.get_user(summary["user_id"]))["extra_roots"] or "[]")


async def test_wal_backup_preserves_committed_history_and_snapshot_files(registered, tmp_path):
    db, _, _, _ = registered
    source = registry.state(None)
    snapshot = source / "snapshots"
    snapshot.mkdir(exist_ok=True)
    (snapshot / "proof.json").write_text('{"proof":"retained"}')
    with sqlite3.connect(source / "index.sqlite3") as writer:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("CREATE TABLE durable_probe(id TEXT PRIMARY KEY)")
        writer.execute("INSERT INTO durable_probe VALUES('committed-wal-row')")
        writer.commit()
        summary = await bootstrap_first_account(db, username="owner", password="password1", session_manager=session_manager)
    target = registry.state(summary["user_id"])
    assert (target / "snapshots/proof.json").read_text() == '{"proof":"retained"}'
    with sqlite3.connect(target / "index.sqlite3") as copied:
        assert copied.execute("SELECT id FROM durable_probe").fetchone()[0] == "committed-wal-row"


async def test_only_authenticated_migration_owner_can_inspect_and_resume(registered, monkeypatch):
    db, _, config, _ = registered
    publish = upgrade.publish
    monkeypatch.setattr(upgrade, "publish", lambda *_: (_ for _ in ()).throw(OSError("publish failure")))
    with pytest.raises(BootstrapError):
        await bootstrap_first_account(db, username="owner", password="password1", session_manager=session_manager)
    users = UserManager(db)
    other = await users.create_user(username="second", password="password2")
    from server import deps
    from server.routers import auth as auth_router
    monkeypatch.setattr(deps, "_user_manager", users)
    monkeypatch.setattr(auth_router, "_db", db)
    other_headers = {"Authorization": f"Bearer {await users.issue_token(other['id'])}"}
    monkeypatch.setattr(upgrade, "publish", publish)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.post("/api/auth/bootstrap/resume")).status_code == 401
        assert (await client.get("/api/auth/bootstrap/status", headers=other_headers)).json() == {"pending": False}
        assert (await client.post("/api/auth/bootstrap/resume", headers=other_headers)).status_code == 404
        login = await client.post("/api/auth/login", json={"username": "owner", "password": "password1"})
        assert login.status_code == 200
        headers = {"Authorization": f"Bearer {login.json()['token']}"}
        assert (await client.get("/api/auth/bootstrap/status", headers=headers)).json()["pending"]
        resumed = await client.post("/api/auth/bootstrap/resume", headers=headers)
        assert resumed.status_code == 200, resumed.text
        assert resumed.json()["wiki"]["vault_id"] == config["vault_id"]
        assert (await client.get("/api/wiki/config", headers=other_headers)).status_code == 404
        assert (await client.get("/api/wiki/config", headers=headers)).json()["vault_id"] == config["vault_id"]
