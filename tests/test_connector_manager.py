"""ConnectorManager (connectors.md §5.5): install upsert + identity, the
server-side token-refresh / needs_reconnect lifecycle (incl. the per-install
lock that refreshes exactly once under concurrency), and agent enablement."""

from __future__ import annotations

import asyncio
import sqlite3
import time
from datetime import UTC, datetime

import pytest

from server.connector_manager import (
    ConnectorError,
    ConnectorManager,
    _deserialize_token_set,
)
from server.connectors.base import ConnectorBase
from server.connectors.registry import KIND_REGISTRY, register
from server.crypto import decrypt
from server.database import Database
from server.oauth_providers import OAuthTokenSet
from server.users import UserManager

ACCESS_TOKEN = "test-access-4f50fcb687c9436fa736685c97f93634"
REFRESH_TOKEN = "test-refresh-e2f103670a924731a943dd879a72ef18"
ROTATED_TOKEN = "test-rotated-8c71e758508b432eab1f3fece60c9721"


class FakeProvider:
    kind = "fakekind"
    authorize_url = "https://fake/auth"
    token_url = "https://fake/token"
    default_scopes = ["s"]
    pkce = True
    client_id = "cid"  # presence of both makes the kind "available"
    client_secret = "csec"

    def __init__(self) -> None:
        self.refresh_calls = 0
        self.refresh_should_fail = False
        self.refresh_delay = 0.0

    def build_authorize_url(self, **k):
        return "https://fake/auth"

    async def exchange_code(self, **k):
        return OAuthTokenSet(ACCESS_TOKEN, REFRESH_TOKEN, time.time() + 3600)

    async def refresh(self, *, client_id, client_secret, refresh_token):
        self.refresh_calls += 1
        if self.refresh_delay:
            await asyncio.sleep(self.refresh_delay)
        if self.refresh_should_fail:
            from server.connectors.errors import ConnectorRefreshError

            raise ConnectorRefreshError(400, "invalid_grant")
        return OAuthTokenSet(ROTATED_TOKEN, None, time.time() + 3600, scopes=["s"])


class FakeConnector(ConnectorBase):
    kind = "fakekind"
    display_name = "Fake"
    category = "test"
    tools = ("search",)

    def __init__(self, provider: FakeProvider, identity=("ext-1", "me@x.com")):
        self.oauth = provider
        self._identity = identity

    async def fetch_external_identity(self, token_set):
        return self._identity


class UnconfiguredProvider:
    """No client creds → kind shows as unavailable."""

    kind = "noconfig"
    pkce = False


class UnconfiguredConnector(ConnectorBase):
    kind = "noconfig"
    display_name = "NoConfig"
    oauth = UnconfiguredProvider()


@pytest.fixture
def clean_registry():
    saved = dict(KIND_REGISTRY)
    KIND_REGISTRY.clear()
    yield
    KIND_REGISTRY.clear()
    KIND_REGISTRY.update(saved)


@pytest.fixture
async def db():
    d = Database(":memory:")
    await d.initialize()
    yield d
    await d.close()


@pytest.fixture
async def owner(db):
    from tests.accounts import create_account

    return (await create_account(db))[0]


@pytest.fixture
def provider():
    return FakeProvider()


@pytest.fixture
async def mgr(db, provider, clean_registry):
    register(FakeConnector(provider))
    m = ConnectorManager(db)
    # Configure the kind's OAuth client in-app so it's "available" and the
    # token-refresh path can resolve client creds.
    await m.set_client_creds("fakekind", "cid", "csec")
    return m


def _token_set(expires_in: float = 3600.0) -> OAuthTokenSet:
    return OAuthTokenSet(ACCESS_TOKEN, REFRESH_TOKEN, time.time() + expires_in, scopes=["s"])


@pytest.mark.parametrize("phase", ["provider", "save_lock", "commit"])
async def test_refresh_waiter_cancellation_keeps_the_original_rotated_result(mgr, db, owner, provider, monkeypatch, phase):
    installation = await mgr.complete_install(kind="fakekind", token_set=_token_set(1), user_id=owner["id"])
    entered, release, returned = asyncio.Event(), asyncio.Event(), asyncio.Event()
    saving, finish_commit = asyncio.Event(), asyncio.Event()
    refresh, commit = provider.refresh, db.conn.commit
    lock_held = False
    initiated = []

    async def controlled_provider(**kwargs):
        initiated.append(kwargs)
        entered.set()
        await release.wait()
        result = await refresh(**kwargs)
        returned.set()
        return result

    async def controlled_commit():
        if returned.is_set() and phase == "commit":
            saving.set()
            await finish_commit.wait()
        await commit()

    monkeypatch.setattr(provider, "refresh", controlled_provider)
    monkeypatch.setattr(db.conn, "commit", controlled_commit)
    waiter = asyncio.create_task(mgr.get_access_token(installation["id"], owner["id"]))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        if phase == "save_lock":
            await db._write_lock.acquire()
            lock_held = True
        if phase != "provider":
            release.set()
            await asyncio.wait_for(returned.wait(), 2)
        if phase == "commit":
            await asyncio.wait_for(saving.wait(), 2)
        waiter.cancel()
        await asyncio.sleep(0)
        release.set()
        finish_commit.set()
        if lock_held:
            db._write_lock.release()
            lock_held = False
        with pytest.raises(asyncio.CancelledError):
            await waiter
        token = await mgr.get_access_token(installation["id"], owner["id"])
        assert token["access_token"] == ROTATED_TOKEN
        assert provider.refresh_calls == 1, "A cancelled waiter discarded the original rotation"
        assert len(initiated) == 1, "A cancelled waiter restarted the original remote request"
        saved = await db.get_connector_secret(installation["id"])
        key = await UserManager(db).data_key_for(owner["id"])
        assert _deserialize_token_set(decrypt(saved, key)).access_token == ROTATED_TOKEN
    finally:
        release.set()
        finish_commit.set()
        if lock_held:
            db._write_lock.release()
        await asyncio.gather(waiter, return_exceptions=True)


@pytest.mark.parametrize("closing", ["db_close", "timeout", "save_failure"])
async def test_owned_rotation_shutdown_keeps_received_response_and_observes_failure(
    mgr, provider, tmp_path, monkeypatch, caplog, closing,
):
    from server import connector_manager
    from tests.accounts import create_account

    path = tmp_path / "owned-refresh.db"
    database = Database(str(path))
    await database.initialize()
    manager = ConnectorManager(database)
    account, _ = await create_account(database)
    await manager.set_client_creds("fakekind", "cid", "csec")
    installation = await manager.complete_install(kind="fakekind", token_set=_token_set(1), user_id=account["id"])
    original = await database.get_connector_secret(installation["id"])
    if closing == "save_failure":
        await database.conn.execute(
            "CREATE TRIGGER refuse_rotation BEFORE INSERT ON connector_installation_secrets "
            "BEGIN SELECT RAISE(ABORT, 'rotated token save rejected'); END"
        )
        await database.conn.commit()
    entered, release = asyncio.Event(), asyncio.Event()
    refresh = provider.refresh

    async def controlled(**kwargs):
        entered.set()
        await release.wait()
        return await refresh(**kwargs)

    monkeypatch.setattr(provider, "refresh", controlled)
    waiter = asyncio.create_task(manager.get_access_token(installation["id"], account["id"]))
    stopping = None
    try:
        await asyncio.wait_for(entered.wait(), 2)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert manager.drain_blockers()
        if closing == "timeout":
            monkeypatch.setattr(connector_manager, "_REFRESH_SHUTDOWN_SECONDS", .01)
            with pytest.raises(TimeoutError, match="have not retired"):
                await manager.shutdown()
            assert manager.drain_blockers()
            assert await database.get_connector_installation(installation["id"])
        stopping = asyncio.create_task(database.close() if closing == "db_close" else manager.shutdown())
        await asyncio.sleep(0)  # The actual owner joins the outstanding rotation.
        assert not stopping.done()
        release.set()
        if closing == "save_failure":
            with pytest.raises(RuntimeError, match="during shutdown") as failed:
                await asyncio.wait_for(stopping, 2)
            assert isinstance(failed.value.__cause__, sqlite3.IntegrityError)
            diagnostic = "\n".join(record.getMessage() for record in caplog.records
                                   if record.name == "server.connector_manager")
            assert "sqlite3.IntegrityError" in diagnostic
            assert "rotated token save rejected" in diagnostic
            assert "_save_refresh_result" in diagnostic
            assert not any(secret in caplog.text for secret in (ACCESS_TOKEN, REFRESH_TOKEN, ROTATED_TOKEN, "csec"))
            assert await database.get_connector_secret(installation["id"]) == original
            assert not (await database.get_connector_installation(installation["id"]))["needs_reconnect"]
        else:
            await asyncio.wait_for(stopping, 2)
        assert not manager.drain_blockers()
        assert provider.refresh_calls == 1
    finally:
        release.set()
        await asyncio.gather(waiter, *([stopping] if stopping else []), return_exceptions=True)
        await database.close()
    if closing != "save_failure":
        reopened = Database(str(path))
        await reopened.initialize()
        try:
            assert (await ConnectorManager(reopened).get_access_token(installation["id"], account["id"]))["access_token"] == ROTATED_TOKEN
            assert provider.refresh_calls == 1
        finally:
            await reopened.close()


async def test_shared_refresh_still_rejects_another_account(mgr, db, owner, provider, monkeypatch):
    from tests.accounts import create_account

    other, _ = await create_account(db, username="other-owner")
    installation = await mgr.complete_install(kind="fakekind", token_set=_token_set(1), user_id=owner["id"])
    entered, release = asyncio.Event(), asyncio.Event()
    refresh = provider.refresh

    async def controlled(**kwargs):
        entered.set()
        await release.wait()
        return await refresh(**kwargs)

    monkeypatch.setattr(provider, "refresh", controlled)
    waiter = asyncio.create_task(mgr.get_access_token(installation["id"], owner["id"]))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        with pytest.raises(ConnectorError, match="not found"):
            await mgr.get_access_token(installation["id"], other["id"])
        release.set()
        assert (await waiter)["access_token"] == ROTATED_TOKEN
        assert provider.refresh_calls == 1
    finally:
        release.set()
        await asyncio.gather(waiter, return_exceptions=True)


async def test_refresh_failure_logs_mask_credentials_and_retain_the_exception_chain(
    mgr, db, owner, provider, monkeypatch, caplog,
):
    installation = await mgr.complete_install(kind="fakekind", token_set=_token_set(1), user_id=owner["id"])
    original = await db.get_connector_secret(installation["id"])

    async def fail(**kwargs):
        try:
            raise ValueError(f"credential rejected: {kwargs['refresh_token']} {kwargs['client_secret']}")
        except ValueError as error:
            raise RuntimeError("provider transport failed") from error

    monkeypatch.setattr(provider, "refresh", fail)
    with pytest.raises(ConnectorError) as failed:
        await mgr.get_access_token(installation["id"], owner["id"])
    assert isinstance(failed.value.__cause__, RuntimeError)
    assert isinstance(failed.value.__cause__.__cause__, ValueError)
    diagnostic = "\n".join(record.getMessage() for record in caplog.records
                           if record.name == "server.connector_manager")
    for context in ("ValueError", "credential rejected", "RuntimeError", "provider transport failed",
                    "ConnectorTokenError", "_get_access_token", "[redacted]"):
        assert context in diagnostic
    assert not any(secret in caplog.text for secret in (ACCESS_TOKEN, REFRESH_TOKEN, "csec"))
    assert await db.get_connector_secret(installation["id"]) == original
    assert not (await db.get_connector_installation(installation["id"]))["needs_reconnect"]
    assert not mgr.drain_blockers()


@pytest.mark.parametrize("operation", ["install", "update"])
async def test_committed_installation_result_survives_subsequent_kind_deletion(mgr, db, owner, monkeypatch, operation):
    if operation == "update":
        original = await mgr.complete_install(kind="fakekind", token_set=_token_set(), user_id=owner["id"])
    method = "authorize_connector_installation" if operation == "install" else "update_connector_installation"
    persist = getattr(db, method)

    async def commit_then_delete(*args, **kwargs):
        committed = await persist(*args, **kwargs)
        assert await db.delete_connector_installations_by_kind("fakekind") == 1
        return committed

    monkeypatch.setattr(db, method, commit_then_delete)
    if operation == "install":
        result = await mgr.complete_install(kind="fakekind", token_set=_token_set(), user_id=owner["id"])
    else:
        result = await mgr.update_installation(original["id"], owner["id"], label="Committed update")
        assert result["label"] == "Committed update"
    assert result["kind"] == "fakekind" and result["user_id"] == owner["id"]
    assert await db.get_connector_installation(result["id"]) is None
    assert await db.get_connector_secret(result["id"]) is None


async def test_owned_refresh_keeps_short_database_operations_available(mgr, db, provider, monkeypatch):
    from tests.accounts import create_account

    owner, _ = await create_account(db)
    installation = await mgr.complete_install(kind="fakekind", token_set=_token_set(1), user_id=owner["id"])
    entered, release = asyncio.Event(), asyncio.Event()
    refresh = provider.refresh

    async def blocked(**kwargs):
        entered.set()
        await release.wait()
        return await refresh(**kwargs)

    monkeypatch.setattr(provider, "refresh", blocked)
    refreshing = asyncio.create_task(mgr.get_access_token(installation["id"], owner["id"]))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        await asyncio.wait_for(db.update_user_field(owner["id"], extra_roots="[]"), 1)
        release.set()
        assert (await asyncio.wait_for(refreshing, 2))["access_token"] == ROTATED_TOKEN
        assert provider.refresh_calls == 1
    finally:
        release.set()
        await asyncio.gather(refreshing, return_exceptions=True)


async def _agent(db: Database, agent_id: str = "a-1", *, user_id: str) -> None:
    now = datetime.now(UTC).isoformat()
    await db.save_agent(agent_id=agent_id, name=agent_id, created_at=now, updated_at=now, user_id=user_id)


# --- catalog ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_catalog_reflects_availability(db, clean_registry, provider):
    register(FakeConnector(provider))
    register(UnconfiguredConnector())
    m = ConnectorManager(db)
    # Nothing configured yet → both unavailable.
    cat = {c["kind"]: c for c in await m.catalog()}
    assert cat["fakekind"]["available"] is False
    assert cat["noconfig"]["available"] is False
    # Setting the OAuth client in-app flips availability.
    await m.set_client_creds("fakekind", "cid", "csec")
    cat = {c["kind"]: c for c in await m.catalog()}
    assert cat["fakekind"]["available"] is True
    assert cat["fakekind"]["display_name"] == "Fake"
    assert cat["noconfig"]["available"] is False


# --- install ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_complete_install_creates_and_encrypts(mgr, db, owner):
    inst = await mgr.complete_install(kind="fakekind", token_set=_token_set(), user_id=owner["id"])
    assert inst["kind"] == "fakekind"
    assert inst["label"] == "me@x.com"
    assert inst["external_account_id"] == "ext-1"
    assert inst["scopes"] == ["s"]
    # Secret is stored encrypted and round-trips to the original token set.
    blob = await db.get_connector_secret(inst["id"])
    ts = _deserialize_token_set(decrypt(blob, await UserManager(db).data_key_for(owner["id"])))
    assert ts.access_token == ACCESS_TOKEN and ts.refresh_token == REFRESH_TOKEN


@pytest.mark.asyncio
async def test_complete_install_upserts_same_account(mgr, db, owner):
    a = await mgr.complete_install(kind="fakekind", token_set=_token_set(), user_id=owner["id"])
    b = await mgr.complete_install(kind="fakekind", token_set=_token_set(), user_id=owner["id"])
    assert a["id"] == b["id"]  # same account → same row, not a duplicate
    assert len(await db.load_connector_installations()) == 1


async def test_concurrent_first_authorizations_return_the_same_committed_installation(
    mgr, db, owner, monkeypatch,
):
    connector = await mgr.get("fakekind")
    both_resolved = asyncio.Event()
    resolutions = 0

    async def identity(token_set):
        nonlocal resolutions
        resolutions += 1
        if resolutions == 2:
            both_resolved.set()
        await both_resolved.wait()
        return "ext-1", "shared identity"

    monkeypatch.setattr(connector, "fetch_external_identity", identity)
    results = await asyncio.gather(*(
        mgr.complete_install(
            kind="fakekind", user_id=owner["id"], requested_label=label,
            token_set=OAuthTokenSet(token, REFRESH_TOKEN, time.time() + 3600, scopes=[label]),
        ) for label, token in (("first", "first-token"), ("second", "second-token"))
    ), return_exceptions=True)
    assert all(isinstance(result, dict) for result in results), results
    assert results[0]["id"] == results[1]["id"]
    installations = await mgr.list_installations(owner["id"])
    assert len(installations) == 1
    current = installations[0]
    token = await mgr.get_access_token(current["id"], owner["id"])
    assert token["access_token"] == {"first": "first-token", "second": "second-token"}[current["label"]]
    assert current["scopes"] == [current["label"]]
    assert "secret_encrypted" not in current and "access_token" not in current


async def test_reauthorization_keeps_identity_configuration_and_agent_links(mgr, db, owner):
    original = await mgr.complete_install(kind="fakekind", token_set=_token_set(), user_id=owner["id"])
    await _agent(db, user_id=owner["id"])
    await mgr.set_agent_connector("a-1", original["id"], True, owner["id"])
    await mgr.update_installation(original["id"], owner["id"], enable_by_default=True,
                                  needs_reconnect=True, last_refresh_error_code="old-error")
    prepared = await mgr.prepare_install(
        connector=await mgr.get("fakekind"), user_id=owner["id"], requested_label="First opened",
        token_set=OAuthTokenSet("first-token", "first-rt", time.time() + 3600, scopes=["first-scope"]),
    )
    await mgr.complete_install(kind="fakekind", user_id=owner["id"], requested_label="Later opened",
                               token_set=OAuthTokenSet("later-token", "later-rt", time.time() + 3600))
    # Saving the earlier-prepared authorization later makes it authoritative.
    updated = await mgr.persist_install(prepared)
    assert updated["id"] == original["id"] and updated["created_at"] == original["created_at"]
    assert updated["user_id"] == owner["id"] and updated["enable_by_default"] is True
    assert updated["label"] == "First opened" and updated["scopes"] == ["first-scope"]
    assert updated["needs_reconnect"] is False and updated["last_refresh_error_code"] is None
    assert await mgr.get_agent_connector_ids("a-1") == [original["id"]]
    assert (await mgr.get_access_token(original["id"], owner["id"]))["access_token"] == "first-token"


async def test_same_provider_identity_stays_isolated_by_current_account(mgr, db, owner):
    from tests.accounts import create_account

    other, _ = await create_account(db, username="another-connector-owner")
    first, second = await asyncio.gather(*(
        mgr.complete_install(kind="fakekind", token_set=OAuthTokenSet(token, None, time.time() + 3600), user_id=user)
        for user, token in ((owner["id"], "mine"), (other["id"], "theirs"))
    ))
    assert first["id"] != second["id"]
    assert len(await mgr.list_installations(owner["id"])) == len(await mgr.list_installations(other["id"])) == 1
    assert (await mgr.get_access_token(first["id"], owner["id"]))["access_token"] == "mine"
    assert (await mgr.get_access_token(second["id"], other["id"]))["access_token"] == "theirs"
    with pytest.raises(ConnectorError, match="not found"):
        await mgr.get_access_token(second["id"], owner["id"])
    assert await db.get_connector_installation_by_account("fakekind", "ext-1", None) is None


async def test_authorizations_without_a_provider_identity_are_not_merged(mgr, owner):
    (await mgr.get("fakekind"))._identity = ("", "unknown")
    first, second = await asyncio.gather(*(
        mgr.complete_install(kind="fakekind", token_set=_token_set(), user_id=owner["id"])
        for _ in range(2)
    ))
    assert first["id"] != second["id"]
    assert len(await mgr.list_installations(owner["id"])) == 2


@pytest.mark.parametrize("reauthorize", [False, True])
async def test_authorization_secret_failure_rolls_back_metadata_and_identity(mgr, db, owner, reauthorize):
    import sqlite3

    original = await mgr.complete_install(kind="fakekind", token_set=_token_set(), user_id=owner["id"]) if reauthorize else None
    async with db.transaction(write=True) as tx:
        await tx.execute("CREATE TRIGGER fail_authorization_secret BEFORE INSERT ON connector_installation_secrets "
                         "BEGIN SELECT RAISE(ABORT, 'secret write failed'); END")
    with pytest.raises(sqlite3.IntegrityError, match="secret write failed"):
        await mgr.complete_install(kind="fakekind", requested_label="Uncommitted", user_id=owner["id"],
                                   token_set=OAuthTokenSet("uncommitted", None, time.time() + 3600))
    current = await mgr.list_installations(owner["id"])
    if original is None:
        assert current == []
    else:
        assert current == [original]
        assert (await mgr.get_access_token(original["id"], owner["id"]))["access_token"] == ACCESS_TOKEN


@pytest.mark.asyncio
async def test_requested_label_overrides_identity(mgr, owner):
    inst = await mgr.complete_install(
        kind="fakekind", token_set=_token_set(), requested_label="Work"
    , user_id=owner["id"])
    assert inst["label"] == "Work"


@pytest.mark.asyncio
async def test_unknown_kind_rejected(mgr, owner):
    with pytest.raises(ConnectorError):
        await mgr.complete_install(kind="ghost", token_set=_token_set(), user_id=owner["id"])


# --- token access / refresh -----------------------------------------------


@pytest.mark.asyncio
async def test_token_no_refresh_when_fresh(mgr, provider, owner):
    inst = await mgr.complete_install(kind="fakekind", token_set=_token_set(3600), user_id=owner["id"])
    out = await mgr.get_access_token(inst["id"], user_id=owner["id"])
    assert out["access_token"] == ACCESS_TOKEN
    assert provider.refresh_calls == 0


@pytest.mark.asyncio
async def test_token_refreshes_near_expiry(mgr, db, provider, owner):
    inst = await mgr.complete_install(kind="fakekind", token_set=_token_set(10), user_id=owner["id"])
    out = await mgr.get_access_token(inst["id"], user_id=owner["id"])
    assert out["access_token"] == ROTATED_TOKEN
    assert provider.refresh_calls == 1
    # New token persisted; reconnect state stays clean.
    refreshed = await mgr.get_installation(inst["id"])
    assert refreshed["needs_reconnect"] is False
    blob = await db.get_connector_secret(inst["id"])
    ts = _deserialize_token_set(decrypt(blob, await UserManager(db).data_key_for(owner["id"])))
    assert ts.access_token == ROTATED_TOKEN
    assert ts.refresh_token == REFRESH_TOKEN  # carried over when refresh omits it


@pytest.mark.asyncio
async def test_refresh_failure_marks_needs_reconnect(mgr, provider, owner):
    provider.refresh_should_fail = True
    inst = await mgr.complete_install(kind="fakekind", token_set=_token_set(10), user_id=owner["id"])
    with pytest.raises(ConnectorError):
        await mgr.get_access_token(inst["id"], user_id=owner["id"])
    after = await mgr.get_installation(inst["id"])
    assert after["needs_reconnect"] is True
    assert after["last_refresh_error_code"] == "invalid_grant"


@pytest.mark.asyncio
async def test_concurrent_token_refreshes_once(mgr, provider, owner):
    provider.refresh_delay = 0.05
    inst = await mgr.complete_install(kind="fakekind", token_set=_token_set(10), user_id=owner["id"])
    results = await asyncio.gather(
        mgr.get_access_token(inst["id"], user_id=owner["id"]), mgr.get_access_token(inst["id"], user_id=owner["id"])
    )
    assert provider.refresh_calls == 1  # lock serializes; 2nd sees fresh token
    assert all(r["access_token"] == ROTATED_TOKEN for r in results)


@pytest.mark.asyncio
async def test_mark_needs_reconnect_and_missing(mgr, owner):
    inst = await mgr.complete_install(kind="fakekind", token_set=_token_set(), user_id=owner["id"])
    stamp = (await mgr.get_access_token(inst["id"], owner["id"]))["grant_stamp"]
    assert await mgr.mark_needs_reconnect(inst["id"], stamp, user_id=owner["id"]) == "applied"
    assert (await mgr.get_installation(inst["id"]))["needs_reconnect"] is True
    assert await mgr.mark_needs_reconnect("ghost", stamp, user_id=owner["id"]) == "gone"


@pytest.mark.asyncio
async def test_delete_missing_raises(mgr):
    with pytest.raises(ConnectorError):
        await mgr.delete_installation("ghost")


# --- agent enablement ------------------------------------------------------


@pytest.mark.asyncio
async def test_agent_enable_validates_installation(mgr, db, owner):
    await _agent(db, user_id=owner["id"])
    with pytest.raises(ConnectorError):
        await mgr.set_agent_connector("a-1", "ghost", True)
    inst = await mgr.complete_install(kind="fakekind", token_set=_token_set(), user_id=owner["id"])
    await mgr.set_agent_connector("a-1", inst["id"], True)
    assert await mgr.get_agent_connector_ids("a-1") == [inst["id"]]


@pytest.mark.asyncio
async def test_replace_agent_connectors_diffs(mgr, db, owner):
    await _agent(db, user_id=owner["id"])
    i1 = await mgr.complete_install(kind="fakekind", token_set=_token_set(), user_id=owner["id"])
    # Make a 2nd installation by faking a different account on the connector.
    from server.connectors.registry import get_connector

    get_connector("fakekind")._identity = ("ext-2", "two@x.com")  # type: ignore
    i2 = await mgr.complete_install(kind="fakekind", token_set=_token_set(), user_id=owner["id"])

    await mgr.set_agent_connector("a-1", i1["id"], True)
    result = await mgr.replace_agent_connectors("a-1", [i2["id"]], owner["id"])
    assert result == [i2["id"]]
    assert await mgr.get_agent_connector_ids("a-1") == [i2["id"]]

    with pytest.raises(ConnectorError):
        await mgr.replace_agent_connectors("a-1", ["ghost"], owner["id"])


async def _installations(db, owner, ids):
    for iid in ids:
        await db.save_connector_installation(
            installation_id=iid, kind="fakekind", label=iid, auth_type="oauth",
            secret_encrypted="test-only", created_at="now", user_id=owner["id"],
        )


async def test_replace_rolls_back_every_member_when_storage_rejects_a_later_insert(mgr, db, owner):
    import sqlite3

    await _agent(db, user_id=owner["id"])
    await _installations(db, owner, ["first", "second"])
    async with db.transaction(write=True) as tx:
        await tx.execute("CREATE TRIGGER fail_second BEFORE INSERT ON agent_connectors "
                         "WHEN (SELECT COUNT(*) FROM agent_connectors) > 0 "
                         "BEGIN SELECT RAISE(ABORT, 'injected insert failure'); END")
    with pytest.raises(sqlite3.IntegrityError, match="injected insert failure"):
        await mgr.replace_agent_connectors("a-1", ["first", "second"], owner["id"])
    assert await mgr.get_agent_connector_ids("a-1") == []


async def test_concurrent_replacements_commit_one_complete_target(mgr, db, owner):
    await _agent(db, user_id=owner["id"])
    await _installations(db, owner, ["a", "b", "c", "d"])
    entered = [asyncio.Event(), asyncio.Event()]

    async def replace_target(index, target):
        entered[index].set()
        return await mgr.replace_agent_connectors("a-1", target, owner["id"])

    # Both requests queue on the real transaction boundary before either writes.
    async with db.transaction(write=True):
        first = asyncio.create_task(replace_target(0, ["a", "b"]))
        second = asyncio.create_task(replace_target(1, ["c", "d"]))
        await asyncio.gather(*(event.wait() for event in entered))
    assert await asyncio.gather(first, second) == [["a", "b"], ["c", "d"]]
    assert set(await mgr.get_agent_connector_ids("a-1")) in ({"a", "b"}, {"c", "d"})
    assert await mgr.replace_agent_connectors("a-1", [], owner["id"]) == []
    assert await mgr.get_agent_connector_ids("a-1") == []


async def test_replace_rejects_foreign_or_missing_members_without_changing_selection(mgr, db, owner):
    from tests.accounts import create_account

    await _agent(db, user_id=owner["id"])
    await _installations(db, owner, ["original", "new"])
    other, _ = await create_account(db, username="other")
    await _installations(db, other, ["foreign"])
    assert await mgr.replace_agent_connectors("a-1", ["original", "original"], owner["id"]) == ["original"]
    for invalid in ("foreign", "missing"):
        with pytest.raises(ConnectorError, match="installation not found"):
            await mgr.replace_agent_connectors("a-1", ["new", invalid], owner["id"])
        assert await mgr.get_agent_connector_ids("a-1") == ["original"]
    with pytest.raises(ConnectorError, match="agent not found"):
        await mgr.replace_agent_connectors("a-1", [], other["id"])
    assert await mgr.get_agent_connector_ids("a-1") == ["original"]


@pytest.mark.parametrize("operation", ["reauth", "delete", "mark", "metadata"])
@pytest.mark.parametrize("refresh_fails", [False, True])
async def test_same_installation_mutations_wait_for_inflight_refresh(mgr, db, owner, provider, monkeypatch, operation, refresh_fails):
    installation = await mgr.complete_install(kind="fakekind", token_set=_token_set(1), user_id=owner["id"])
    iid = installation["id"]
    entered, release = asyncio.Event(), asyncio.Event()
    refresh = provider.refresh

    async def blocked_refresh(**kwargs):
        entered.set()
        await release.wait()
        if refresh_fails:
            raise RuntimeError("old refresh rejected")
        return await refresh(**kwargs)

    monkeypatch.setattr(provider, "refresh", blocked_refresh)
    refreshing = asyncio.create_task(mgr.get_access_token(iid, owner["id"]))
    mutation = None
    try:
        await asyncio.wait_for(entered.wait(), 2)
        if operation == "reauth":
            call = mgr.complete_install(kind="fakekind", token_set=OAuthTokenSet("reauthorized", "new-rt", time.time() + 3600), user_id=owner["id"])
        elif operation == "delete":
            call = mgr.delete_installation(iid, owner["id"])
        elif operation == "mark":
            from server.connectors.grants import grant_stamp

            stamp = grant_stamp(installation, await db.get_connector_secret(iid), None)
            call = mgr.mark_needs_reconnect(iid, stamp, user_id=owner["id"])
        else:
            call = mgr.update_installation(iid, owner["id"], label="Updated")
        mutation = asyncio.create_task(call)
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(asyncio.shield(mutation), 0.05)
    finally:
        release.set()
        results = await asyncio.gather(refreshing, *([mutation] if mutation else []), return_exceptions=True)
    assert isinstance(results[0], ConnectorError) if refresh_fails else isinstance(results[0], dict)
    assert not isinstance(results[1], BaseException)
    current = await mgr.get_installation(iid, owner["id"])
    if operation == "reauth":
        assert not current["needs_reconnect"]
        assert (await mgr.get_access_token(iid, owner["id"]))["access_token"] == "reauthorized"
    elif operation == "delete":
        assert current is None
    elif operation == "mark":
        assert current["last_refresh_error_code"] == ("auth_rejected" if refresh_fails else None)
    else:
        assert current["label"] == "Updated"


@pytest.mark.parametrize("refresh_fails", [False, True])
async def test_batch_delete_during_refresh_never_recreates_secret_or_returns_sqlite_error(mgr, db, owner, provider, monkeypatch, refresh_fails):
    installation = await mgr.complete_install(kind="fakekind", token_set=_token_set(1), user_id=owner["id"])
    iid = installation["id"]
    entered, release = asyncio.Event(), asyncio.Event()
    refresh = provider.refresh

    async def blocked_refresh(**kwargs):
        entered.set()
        await release.wait()
        if refresh_fails:
            raise RuntimeError("old refresh rejected")
        return await refresh(**kwargs)

    monkeypatch.setattr(provider, "refresh", blocked_refresh)
    refreshing = asyncio.create_task(mgr.get_access_token(iid, owner["id"]))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        assert await db.delete_connector_installations_by_kind("fakekind") == 1
    finally:
        release.set()
    result = (await asyncio.gather(refreshing, return_exceptions=True))[0]
    assert isinstance(result, ConnectorError)
    assert await db.get_connector_secret(iid) is None
    assert await mgr.get_installation(iid, owner["id"]) is None


async def test_captured_reauthorization_target_deleted_while_waiting_is_not_recreated(mgr, db, owner, provider, monkeypatch):
    installation = await mgr.complete_install(kind="fakekind", token_set=_token_set(1), user_id=owner["id"])
    entered, release, captured = asyncio.Event(), asyncio.Event(), asyncio.Event()
    refresh = provider.refresh
    lookup = db.get_connector_installation_by_account

    async def blocked_refresh(**kwargs):
        entered.set()
        await release.wait()
        return await refresh(**kwargs)

    async def observed_lookup(*args, **kwargs):
        row = await lookup(*args, **kwargs)
        assert row["id"] == installation["id"]
        captured.set()
        return row

    monkeypatch.setattr(provider, "refresh", blocked_refresh)
    monkeypatch.setattr(db, "get_connector_installation_by_account", observed_lookup)
    refreshing = asyncio.create_task(mgr.get_access_token(installation["id"], owner["id"]))
    reauthorizing = None
    try:
        await asyncio.wait_for(entered.wait(), 2)
        reauthorizing = asyncio.create_task(mgr.complete_install(kind="fakekind", token_set=_token_set(), user_id=owner["id"]))
        await asyncio.wait_for(captured.wait(), 2)
        await db.delete_connector_installations_by_kind("fakekind")
    finally:
        release.set()
        results = await asyncio.gather(refreshing, *([reauthorizing] if reauthorizing else []), return_exceptions=True)
    assert len(results) == 2 and all(isinstance(result, ConnectorError) for result in results)
    assert await db.load_connector_installations(owner["id"]) == []
    assert await db.get_connector_secret(installation["id"]) is None


async def test_different_installations_refresh_concurrently(mgr, owner, provider, monkeypatch):
    first = await mgr.complete_install(kind="fakekind", token_set=_token_set(1), user_id=owner["id"])
    (await mgr.get("fakekind"))._identity = ("ext-2", "second")
    second = await mgr.complete_install(kind="fakekind", token_set=_token_set(1), user_id=owner["id"])
    both_entered, release = asyncio.Event(), asyncio.Event()
    refresh = provider.refresh
    calls = 0

    async def blocked_refresh(**kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            both_entered.set()
        await release.wait()
        return await refresh(**kwargs)

    monkeypatch.setattr(provider, "refresh", blocked_refresh)
    running = [asyncio.create_task(mgr.get_access_token(inst["id"], owner["id"])) for inst in (first, second)]
    try:
        await asyncio.wait_for(both_entered.wait(), 2)
    finally:
        release.set()
        results = await asyncio.gather(*running, return_exceptions=True)
    assert all(isinstance(result, dict) for result in results)


@pytest.mark.parametrize("refresh_fails", [False, True])
async def test_refresh_cannot_commit_after_its_account_becomes_unavailable(
    mgr, db, owner, provider, monkeypatch, refresh_fails,
):
    installation = await mgr.complete_install(kind="fakekind", token_set=_token_set(1), user_id=owner["id"])
    iid = installation["id"]
    original_blob = await db.get_connector_secret(iid)
    entered, release = asyncio.Event(), asyncio.Event()

    async def refresh(**kwargs):
        entered.set()
        await release.wait()
        if refresh_fails:
            raise RuntimeError("old token rejected")
        return OAuthTokenSet("new-token", "new-refresh", time.time() + 3600)

    monkeypatch.setattr(provider, "refresh", refresh)
    refreshing = asyncio.create_task(mgr.get_access_token(iid, owner["id"]))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        await db.update_user_field(owner["id"], disabled_at="2026-10-28T00:00:00Z")
    finally:
        release.set()
    result = (await asyncio.gather(refreshing, return_exceptions=True))[0]
    assert isinstance(result, ConnectorError) and "unavailable" in str(result), result
    assert await db.get_connector_secret(iid) == original_blob
    current = await db.get_connector_installation(iid)
    assert current["needs_reconnect"] is False and current["last_refresh_error_code"] is None


@pytest.mark.asyncio
async def test_resolve_client_creds_db_and_env(db, clean_registry, provider, monkeypatch):
    from server.config import settings

    register(FakeConnector(provider))
    m = ConnectorManager(db)

    # Nothing set anywhere.
    assert await m.resolve_client_creds("fakekind") is None

    # Env fallback (github is a real settings field; resolution doesn't require
    # the kind to be registered).
    assert await m.resolve_client_creds("github") is None
    monkeypatch.setattr(settings, "github_oauth_client_id", "env-id")
    monkeypatch.setattr(settings, "github_oauth_client_secret", "env-sec")
    assert await m.resolve_client_creds("github") == ("env-id", "env-sec")

    # DB config for a registered kind; secret round-trips, config hides it.
    await m.set_client_creds("fakekind", "db-id", "db-sec")
    assert await m.resolve_client_creds("fakekind") == ("db-id", "db-sec")
    fcfg = await m.client_config("fakekind", "https://octo.example")
    assert fcfg["configured"] is True
    assert fcfg["client_id"] == "db-id" and fcfg["source"] == "db"
    assert "client_secret" not in fcfg
    assert fcfg["redirect_uri"] == "https://octo.example/api/connectors/oauth/callback"

    # Clearing drops back to "nothing" (fakekind has no env field).
    assert await m.clear_client_creds("fakekind") is True
    assert await m.resolve_client_creds("fakekind") is None

    # Unknown kind can't be configured.
    with pytest.raises(ConnectorError):
        await m.set_client_creds("ghost", "x", "y")


@pytest.mark.asyncio
async def test_enabled_installations_for_agent_views(mgr, db, owner):
    await _agent(db, user_id=owner["id"])
    inst = await mgr.complete_install(kind="fakekind", token_set=_token_set(), user_id=owner["id"])
    await mgr.set_agent_connector("a-1", inst["id"], True)
    views = await mgr.enabled_installations_for_agent("a-1")
    assert len(views) == 1
    assert views[0].id == inst["id"]
    assert views[0].kind == "fakekind"
    assert views[0].label == "me@x.com"
