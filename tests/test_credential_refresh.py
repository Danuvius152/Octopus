"""Refresh ownership through execution admission and an actual temporary database."""

import asyncio
import json
import time

import pytest

from server.crypto import decrypt, encrypt
from server.database import Database
from server.oauth_providers import OAuthTokenSet, get_provider
from server.session_manager import SessionManager
from server.users import UserManager
from tests.accounts import create_account


@pytest.fixture
async def refreshing(tmp_path, monkeypatch):
    db = Database(str(tmp_path / "refresh.db"))
    await db.initialize()
    owner, _ = await create_account(db)
    manager = SessionManager()
    await manager.initialize(db)
    key = await UserManager(db).data_key_for(owner["id"])
    for credential in ("first", "second"):
        await db.save_credential(credential, "claude-code", credential, "oauth", encrypt(json.dumps({
            "access_token": "expired", "refresh_token": credential, "expires_at_epoch": 0,
        }), key), "now", user_id=owner["id"])
    entered = asyncio.Queue()
    release = asyncio.Event()
    calls = []

    async def refresh(token):
        calls.append(token)
        await entered.put(token)
        if token == "first":
            await release.wait()
        return OAuthTokenSet("fresh-" + token, "rotated-" + token, time.time() + 3600)

    monkeypatch.setattr(get_provider("claude-code"), "refresh_access_token", refresh)

    async def resolve(credential="first"):
        return await manager.resolve_credential_by_id(credential, backend="claude-code", user_id=owner["id"])

    yield db, manager, owner, key, resolve, entered, release, calls
    release.set()
    await db.close()


async def test_same_credential_shares_refresh_and_different_credential_progresses(refreshing):
    db, manager, owner, key, resolve, entered, release, calls = refreshing
    first = asyncio.create_task(resolve())
    assert await entered.get() == "first"
    second = asyncio.create_task(resolve())
    other = await asyncio.wait_for(resolve("second"), 2)
    assert other.secret == "fresh-second"
    release.set()
    results = await asyncio.gather(first, second)
    assert [result.secret for result in results] == ["fresh-first", "fresh-first"]
    assert calls == ["first", "second"]
    assert (await resolve()).secret == "fresh-first"
    assert calls == ["first", "second"]


@pytest.mark.parametrize("lifetime", [120, 3600])
async def test_successful_refresh_returns_a_still_valid_short_lived_token(refreshing, monkeypatch, lifetime):
    db, manager, owner, key, resolve, entered, release, calls = refreshing

    async def refresh(token):
        return OAuthTokenSet("short-lived", "rotated", time.time() + lifetime)

    monkeypatch.setattr(get_provider("claude-code"), "refresh_access_token", refresh)
    assert (await resolve()).secret == "short-lived"
    row = await db.get_credential("first")
    assert json.loads(decrypt(row["secret_encrypted"], key))["access_token"] == "short-lived"
    assert row["status"] == "active"


@pytest.mark.parametrize("failure", [None, "unknown", "invalid_grant"])
@pytest.mark.parametrize("change", ["reauthorize", "key", "delete", "revoke", "owner", "disable"])
async def test_late_refresh_cannot_overwrite_current_authority(refreshing, monkeypatch, failure, change):
    db, manager, owner, key, resolve, entered, release, calls = refreshing
    if failure:
        async def failed(token):
            await entered.put(token)
            await release.wait()
            if failure == "invalid_grant":
                from server.oauth_errors import OAuthRefreshError, RefreshErrorCode

                raise OAuthRefreshError("Rejected", RefreshErrorCode.refresh_token_invalidated, reconnect=True)
            raise RuntimeError("old unknown provider failure")
        monkeypatch.setattr(get_provider("claude-code"), "refresh_access_token", failed)
    operation = asyncio.create_task(resolve())
    await entered.get()
    if change in {"reauthorize", "key"}:
        secret = encrypt("new-login-secret", key)
        if change == "reauthorize":
            await db.reauthorize_credential("first", user_id=owner["id"], backend="claude-code", secret_encrypted=secret)
        else:
            await db.update_credential("first", secret_encrypted=secret)
    elif change == "delete":
        await db.delete_credential("first")
    elif change == "revoke":
        await db.update_credential("first", status="revoked")
    elif change == "disable":
        await db.update_user_field(owner["id"], disabled_at="now")
    else:
        other, _ = await create_account(db, username="other-owner")
        async with db.transaction() as tx:
            await tx.execute("UPDATE backend_credentials SET user_id=? WHERE id='first'", (other["id"],))
    current = await db.get_credential("first")
    release.set()
    if change in {"reauthorize", "key"}:
        assert (await operation).secret == "new-login-secret"
        assert (await resolve()).auth_type == "api_key"
        assert decrypt((await db.get_credential("first"))["secret_encrypted"], key) == "new-login-secret"
    else:
        with pytest.raises(ValueError):
            await operation
    assert await db.get_credential("first") == current


@pytest.mark.parametrize("all_waiters", [False, True])
async def test_cancelled_waiters_leave_refresh_owned_until_database_close(refreshing, all_waiters):
    db, manager, owner, key, resolve, entered, release, calls = refreshing
    first = asyncio.create_task(resolve())
    await entered.get()
    second = None if all_waiters else asyncio.create_task(resolve())
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    if all_waiters:
        closing = asyncio.create_task(db.close())
        await asyncio.sleep(0)
        assert not closing.done()
        release.set()
        await closing
    else:
        release.set()
        assert (await second).secret == "fresh-first"
        assert (await resolve()).secret == "fresh-first"
    assert calls == ["first"]


@pytest.mark.parametrize("kind", ["503", "network", "timeout", "unknown", "invalid_grant"])
async def test_provider_failure_only_blocks_explicit_token_rejection(refreshing, monkeypatch, kind):
    import httpx

    from server.oauth_errors import OAuthRefreshError
    from server.oauth_providers import ClaudeCodeProvider

    db, manager, owner, key, resolve, entered, release, calls = refreshing
    provider = ClaudeCodeProvider()
    attempts = []
    real_client = httpx.AsyncClient

    def respond(request):
        attempts.append(request)
        if len(attempts) > 1:
            return httpx.Response(200, json={"access_token": "recovered", "expires_in": 3600})
        if kind == "network":
            raise httpx.ConnectError("connection failed", request=request)
        if kind == "timeout":
            raise httpx.ReadTimeout("timeout", request=request)
        if kind == "unknown":
            raise RuntimeError("unclassified failure")
        return httpx.Response(400 if kind == "invalid_grant" else 503, json={"error": "invalid_grant"})

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real_client(transport=httpx.MockTransport(respond), **kw))
    monkeypatch.setattr(get_provider("claude-code"), "refresh_access_token", provider.refresh_access_token)
    with pytest.raises(ValueError) as caught:
        await resolve()
    row = await db.get_credential("first")
    assert row["needs_reconnect"] is (kind == "invalid_grant")
    assert row["status"] == ("needs_reconnect" if kind == "invalid_grant" else "active")
    if kind == "invalid_grant":
        assert row["last_refresh_error_code"] == "refresh_token_invalidated"
        with pytest.raises(ValueError):
            await resolve()
        assert len(attempts) == 1
    else:
        if kind != "unknown":
            assert isinstance(caught.value.__cause__, OAuthRefreshError)
            assert not caught.value.__cause__.reconnect
        assert (await resolve()).secret == "recovered"
        assert len(attempts) == 2


@pytest.mark.parametrize("cancel_waiter", [False, True])
async def test_slow_response_has_one_total_deadline_and_closes_after_waiter_cancellation(
    refreshing, monkeypatch, tmp_path, cancel_waiter,
):
    import httpx

    from server.oauth_errors import OAuthRefreshError, RefreshErrorCode
    from server.oauth_providers import ClaudeCodeProvider

    db, manager, owner, key, resolve, entered, release, calls = refreshing
    streaming = asyncio.Event()
    finished = False

    class SlowJSON(httpx.AsyncByteStream):
        async def __aiter__(self):
            nonlocal finished
            streaming.set()
            yield b'{"access_token":'
            for _ in range(10):
                await asyncio.sleep(0.02)
                yield b' '
            finished = True
            yield b'"must-not-commit","expires_in":3600}'

    real_client = httpx.AsyncClient
    transport = httpx.MockTransport(lambda request: httpx.Response(200, stream=SlowJSON()))
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real_client(transport=transport, **kw))
    provider = ClaudeCodeProvider()
    monkeypatch.setattr(provider, "REFRESH_TIMEOUT", 0.05)
    monkeypatch.setattr(get_provider("claude-code"), "refresh_access_token", provider.refresh_access_token)
    operation = asyncio.create_task(resolve())
    await streaming.wait()
    if cancel_waiter:
        operation.cancel()
        with pytest.raises(asyncio.CancelledError):
            await operation
        await db.close()
        reopened = Database(str(tmp_path / "refresh.db"))
        await reopened.initialize()
        try:
            row = await reopened.get_credential("first")
        finally:
            await reopened.close()
    else:
        with pytest.raises(ValueError) as caught:
            await operation
        assert isinstance(caught.value.__cause__, OAuthRefreshError)
        assert caught.value.__cause__.code == RefreshErrorCode.network_error
        row = await db.get_credential("first")
    assert not finished
    assert row["status"] == "active" and not row["needs_reconnect"]
    assert json.loads(decrypt(row["secret_encrypted"], key))["access_token"] == "expired"
