"""Signing in, joining by invite, and the admin surface (multi-tenancy.md §3).

The route layer, where `tests/test_users.py` covers the rules underneath. What
is worth pinning here is what only exists once there is an HTTP boundary: that
a bearer is required, that the throttle actually bites, that an admin route is
closed to a normal account, and that the one way to lock everybody out — an
admin disabling themselves — is refused.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from httpx import ASGITransport, AsyncClient

from server import deps
from server.agent_manager import AgentManager
from server.applications import app_scope_token
from server.database import Database
from server.main import app
from server.mcp_identity import mint
from server.routers import agents as agents_router
from server.routers import auth as auth_router
from server.session_manager import session_manager
from server.users import UserManager, token_digest


@pytest.fixture
async def client(tmp_path, monkeypatch):
    from server import crypto
    from server.config import settings

    # A master key of this test's own, so a DEK minted here cannot be wrapped
    # with the developer's real one.
    monkeypatch.setattr(settings, "master_key", "test-master-key")
    crypto._MASTER_CACHE.clear()

    db = Database(":memory:")
    await db.initialize()
    session_manager.sessions.clear()
    session_manager._admission_closed = False
    await session_manager.initialize(db)
    # The routes these tests reach through, bound the way main.py binds them.
    agents_router.set_manager(AgentManager(db))
    users = UserManager(db)
    deps.set_user_manager(users)
    auth_router._FAILURES.clear()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c, users, db
    await db.close()
    session_manager.sessions.clear()
    session_manager._admission_closed = False


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _credential(source: str, token: str) -> dict:
    # Explicit server-side cookie admission, independent of browser cookie Path.
    return {
        "query": {"params": {"token": token}},
        "header": {"headers": _bearer(token)},
        "cookie": {"headers": {"Cookie": f"octopus_app_token={token}"}},
    }[source]


async def test_concurrent_password_changes_accept_only_one_old_password_proof(client):
    c, users, _db = client
    user = await users.create_user(username="archer", password="password1")
    tokens = [await users.issue_token(user["id"]) for _ in range(2)]
    passwords = ["first-new-password", "second-new-password"]
    responses = await asyncio.gather(*(
        c.post("/api/auth/password", headers=_bearer(token), json={
            "current_password": "password1", "new_password": password,
        }) for token, password in zip(tokens, passwords, strict=True)
    ))
    assert sorted(response.status_code for response in responses) == [204, 409]
    winner = next(index for index, response in enumerate(responses) if response.status_code == 204)
    logged_in = await c.post("/api/auth/login", json={"username": "archer", "password": passwords[winner]})
    assert logged_in.status_code == 200
    assert (await c.post("/api/auth/login", json={
        "username": "archer", "password": passwords[1 - winner],
    })).status_code == 401
    assert (await c.get("/api/auth/identity", headers=_bearer(tokens[winner]))).status_code == 200
    assert (await c.get("/api/auth/identity", headers=_bearer(tokens[1 - winner]))).status_code == 401


async def test_admin_disable_restore_does_not_restore_old_bearers(client):
    c, users, _db = client
    admin = await users.create_user(username="operator", password="password1", is_admin=True)
    account = await users.create_user(username="archer", password="password1")
    headers = _bearer(await users.issue_token(admin["id"]))
    tokens = [await users.issue_token(account["id"], kind=kind) for kind in ("session", "pat")]
    for disabled in (True, False):
        response = await c.post(f"/api/auth/users/{account['id']}/disabled", headers=headers, json={"disabled": disabled})
        assert response.status_code == 200
        for token in tokens:
            assert (await c.get("/api/auth/identity", headers=_bearer(token))).status_code == 401
    assert (await c.post("/api/auth/login", json={"username": "archer", "password": "password1"})).status_code == 200


@pytest.mark.parametrize("change", ["revoke", "disable", "disable_restore"])
async def test_password_commit_uses_entry_identity_and_current_account_state(client, monkeypatch, change):
    c, users, db = client
    user = await users.create_user(username="archer", password="password1")
    bearer = await users.issue_token(user["id"])
    entered, release = asyncio.Event(), asyncio.Event()
    execute, commit = db.conn.execute, db.conn.commit
    proof_read = False

    async def observed_execute(sql, parameters=()):
        nonlocal proof_read
        cursor = await execute(sql, parameters)
        if "FROM users WHERE username = ?" in sql:
            proof_read = True
        return cursor

    async def gated_commit():
        nonlocal proof_read
        if proof_read:
            proof_read = False
            entered.set()
            await release.wait()
        await commit()

    with monkeypatch.context() as patch:
        patch.setattr(db.conn, "execute", observed_execute)
        patch.setattr(db.conn, "commit", gated_commit)
        changing = asyncio.create_task(c.post("/api/auth/password", headers=_bearer(bearer), json={
            "current_password": "password1", "new_password": "changed-password",
        }))
        mutations = []
        try:
            await asyncio.wait_for(entered.wait(), 2)
            mutations.append(asyncio.create_task(
                users.revoke_token(bearer) if change == "revoke" else users.set_disabled(user["id"], True)
            ))
            if change == "disable_restore":
                mutations.append(asyncio.create_task(users.set_disabled(user["id"], False)))
            await asyncio.sleep(0)  # Queue the real writers before the proof reader returns.
            release.set()
            await asyncio.gather(*mutations)
            response = await changing
        finally:
            release.set()
            await asyncio.gather(changing, *mutations, return_exceptions=True)
    assert response.status_code == (409 if change == "disable" else 204)
    assert await users.resolve_token(bearer) is None
    if change == "disable":
        await users.set_disabled(user["id"], False)
    logged_in = await c.post("/api/auth/login", json={
        "username": "archer", "password": "password1" if change == "disable" else "changed-password",
    })
    assert logged_in.status_code == 200


# ---------------------------------------------------------------------------
# Signing in
# ---------------------------------------------------------------------------


async def test_workspace_seal_rejects_a_late_put_and_adopts_the_next_list(client, tmp_path):
    c, users, db = client
    admin = await users.create_user(username="operator", password="password1", is_admin=True)
    headers = _bearer(await users.issue_token(admin["id"]))
    roots = [str(tmp_path / name) for name in ("a", "b")]
    for name in ("a", "b"):
        (tmp_path / name).mkdir()
    await db.update_user_field(admin["id"], extra_roots=json.dumps(roots))
    base = f"/api/auth/users/{admin['id']}"
    opened = await c.post(f"{base}/workspace-edits", headers=headers, json={"flow_id": "late-put"})
    assert opened.status_code == 200
    flow = {key: opened.json()[key] for key in ("flow_id", "service_generation")}
    entered, release = asyncio.Event(), asyncio.Event()

    async def held_body():
        entered.set()
        await release.wait()
        yield json.dumps({**flow, "sequence": 1, "extra_roots": roots[1:]}).encode()

    writing = asyncio.create_task(c.put(f"{base}/extra-roots", headers={**headers, "Content-Type": "application/json"}, content=held_body()))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        assert (await c.get("/api/auth/workspace", headers=headers)).json()["extra_roots"] == roots
        seal = await c.post(f"{base}/workspace-edits/confirm", headers=headers, json={**flow, "sequence": 1})
        assert seal.status_code == 200
        assert seal.json()["sealed_through"] == 1
        assert seal.json()["workspace"]["extra_roots"] == roots
        assert (await c.put(f"{base}/extra-roots", headers=headers,
                            json={**flow, "sequence": 2, "extra_roots": roots[:1]})).status_code == 200
        release.set()
        late = await writing
    finally:
        release.set()
        await asyncio.gather(writing, return_exceptions=True)
    assert late.status_code == 409
    assert (await c.get("/api/auth/workspace", headers=headers)).json()["extra_roots"] == roots[:1]
    repeated = await c.post(f"{base}/workspace-edits/confirm", headers=headers, json={**flow, "sequence": 1})
    assert repeated.json()["sealed_through"] >= 2
    assert repeated.json()["workspace"]["extra_roots"] == roots[:1]


async def test_workspace_confirmation_waits_for_the_original_cancelled_commit(client, tmp_path, monkeypatch):
    c, users, db = client
    admin = await users.create_user(username="operator", password="password1", is_admin=True)
    headers = _bearer(await users.issue_token(admin["id"]))
    roots = [str(tmp_path / name) for name in ("a", "b")]
    for name in ("a", "b"):
        (tmp_path / name).mkdir()
    await db.update_user_field(admin["id"], extra_roots=json.dumps(roots))
    base = f"/api/auth/users/{admin['id']}"
    opened = (await c.post(f"{base}/workspace-edits", headers=headers, json={"flow_id": "cancelled-commit"})).json()
    flow = {key: opened[key] for key in ("flow_id", "service_generation")}
    entered, release = asyncio.Event(), asyncio.Event()
    execute, commit = db.conn.execute, db.conn.commit
    updating = False

    async def observed_execute(sql, parameters=()):
        nonlocal updating
        cursor = await execute(sql, parameters)
        if sql.startswith("UPDATE users SET extra_roots="):
            updating = True
        return cursor

    async def gated_commit():
        nonlocal updating
        if updating:
            updating = False
            entered.set()
            await release.wait()
        await commit()

    with monkeypatch.context() as patch:
        patch.setattr(db.conn, "execute", observed_execute)
        patch.setattr(db.conn, "commit", gated_commit)
        writing = asyncio.create_task(c.put(f"{base}/extra-roots", headers=headers,
                                            json={**flow, "sequence": 1, "extra_roots": roots[1:]}))
        confirmation = None
        try:
            await asyncio.wait_for(entered.wait(), 2)
            writing.cancel()
            await asyncio.sleep(0)
            writing.cancel()
            confirmation = asyncio.create_task(c.post(f"{base}/workspace-edits/confirm", headers=headers,
                                                       json={**flow, "sequence": 1}))
            await asyncio.sleep(0)
            assert not writing.done(), "cancellation discarded the original commit acknowledgment"
            assert not confirmation.done(), "confirmation overtook the still-running original commit"
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await writing
            result = await confirmation
        finally:
            release.set()
            await asyncio.gather(writing, *([confirmation] if confirmation else []), return_exceptions=True)
    assert result.status_code == 200
    assert result.json()["workspace"]["extra_roots"] == roots[1:]
    assert result.json()["sealed_through"] == 1
    next_write = await c.put(f"{base}/extra-roots", headers=headers, json={**flow, "sequence": 2, "extra_roots": []})
    assert next_write.json()["extra_roots"] == []
    assert (await c.put(f"{base}/extra-roots", headers=headers,
                        json={**flow, "sequence": 1, "extra_roots": roots[1:]})).status_code == 409


@pytest.mark.parametrize("change", ["revoke", "disable", "demote"])
async def test_workspace_permission_is_rechecked_in_the_authorization_write_transaction(client, monkeypatch, change):
    c, users, db = client
    admin = await users.create_user(username="operator", password="password1", is_admin=True)
    bearer = await users.issue_token(admin["id"])
    headers = _bearer(bearer)
    base = f"/api/auth/users/{admin['id']}"
    opened = (await c.post(f"{base}/workspace-edits", headers=headers, json={"flow_id": "permission-change"})).json()
    flow = {key: opened[key] for key in ("flow_id", "service_generation")}
    entered, release = asyncio.Event(), asyncio.Event()
    execute, commit = db.conn.execute, db.conn.commit
    proof_read = False

    async def observed_execute(sql, parameters=()):
        nonlocal proof_read
        cursor = await execute(sql, parameters)
        if "FROM auth_tokens AS t JOIN users AS u" in sql and not entered.is_set():
            proof_read = True
        return cursor

    async def gated_commit():
        nonlocal proof_read
        if proof_read:
            proof_read = False
            entered.set()
            await release.wait()
        await commit()

    with monkeypatch.context() as patch:
        patch.setattr(db.conn, "execute", observed_execute)
        patch.setattr(db.conn, "commit", gated_commit)
        writing = asyncio.create_task(c.put(f"{base}/extra-roots", headers=headers,
                                            json={**flow, "sequence": 1, "extra_roots": []}))
        mutation = None
        try:
            await asyncio.wait_for(entered.wait(), 2)
            mutation = asyncio.create_task(users.revoke_token(bearer) if change == "revoke" else
                                           users.set_disabled(admin["id"], True) if change == "disable" else
                                           db.update_user_field(admin["id"], is_admin=0))
            await asyncio.sleep(0)
            release.set()
            await mutation
            result = await writing
        finally:
            release.set()
            await asyncio.gather(writing, *([mutation] if mutation else []), return_exceptions=True)
    assert result.status_code == (403 if change == "demote" else 401)


@pytest.mark.parametrize("previous_writers_retired", [False, True])
async def test_workspace_generation_change_needs_actual_writer_retirement_proof(client, previous_writers_retired):
    c, users, db = client
    admin = await users.create_user(username="operator", password="password1", is_admin=True)
    headers = _bearer(await users.issue_token(admin["id"]))
    base = f"/api/auth/users/{admin['id']}"
    opened = (await c.post(f"{base}/workspace-edits", headers=headers, json={"flow_id": "old-generation"})).json()
    flow = {key: opened[key] for key in ("flow_id", "service_generation")}
    # This flag is supplied only by InstallationOwner's managed retirement
    # proof at real startup. A new random generation alone is insufficient.
    deps.set_user_manager(UserManager(db, previous_writers_retired=previous_writers_retired))
    assert (await c.put(f"{base}/extra-roots", headers=headers,
                        json={**flow, "sequence": 1, "extra_roots": []})).status_code == 409
    confirmation = await c.post(f"{base}/workspace-edits/confirm", headers=headers, json={**flow, "sequence": 1})
    if not previous_writers_retired:
        assert confirmation.status_code == 409
        return
    receipt = confirmation.json()
    assert receipt["previous_generation_retired"] is True
    assert receipt["service_generation"] != flow["service_generation"]
    assert receipt["sealed_through"] == 1
    assert receipt["workspace"]["extra_roots"] == []
    new_flow = {key: receipt[key] for key in ("flow_id", "service_generation")}
    assert (await c.put(f"{base}/extra-roots", headers=headers,
                        json={**new_flow, "sequence": 2, "extra_roots": []})).status_code == 200


async def test_workspace_flow_is_bound_to_operator_and_target_and_legacy_put_cannot_bypass_it(client):
    c, users, _db = client
    first = await users.create_user(username="first", password="password1", is_admin=True)
    second = await users.create_user(username="second", password="password1", is_admin=True)
    headers = _bearer(await users.issue_token(first["id"]))
    other_headers = _bearer(await users.issue_token(second["id"]))
    base = f"/api/auth/users/{first['id']}"
    opened = (await c.post(f"{base}/workspace-edits", headers=headers, json={"flow_id": "bound-flow"})).json()
    flow = {key: opened[key] for key in ("flow_id", "service_generation")}
    for actor, target in ((other_headers, first["id"]), (headers, second["id"])):
        for suffix, body, method in (("workspace-edits/confirm", {**flow, "sequence": 1}, c.post),
                                     ("extra-roots", {**flow, "sequence": 1, "extra_roots": []}, c.put)):
            response = await method(f"/api/auth/users/{target}/{suffix}", headers=actor, json=body)
            assert response.status_code == 403
    assert (await c.put(f"{base}/extra-roots", headers=headers, json={"extra_roots": []})).status_code == 422
    assert (await c.post("/api/auth/users/missing/workspace-edits", headers=headers, json={"flow_id": "missing-target"})).status_code == 404


@pytest.mark.asyncio
async def test_login_returns_a_bearer_that_works(client):
    c, users, _db = client
    await users.create_user(username="archer", password="password1")

    res = await c.post(
        "/api/auth/login", json={"username": "archer", "password": "password1"}
    )
    assert res.status_code == 200
    body = res.json()
    assert body["username"] == "archer" and body["token"]

    who = await c.get("/api/auth/identity", headers=_bearer(body["token"]))
    assert who.status_code == 200
    assert who.json()["label"] == "archer"


@pytest.mark.asyncio
async def test_identity_needs_a_bearer_and_the_password_is_not_one(client):
    c, users, _db = client
    await users.create_user(username="archer", password="password1")

    assert (await c.get("/api/auth/identity")).status_code == 401
    assert (
        await c.get("/api/auth/identity", headers=_bearer("password1"))
    ).status_code == 401


@pytest.mark.asyncio
async def test_a_wrong_password_is_401_and_eventually_429(client):
    """The throttle is the reply to swapping a 256-bit token for something a
    person chose. Without it the login route is an offline attack with a
    network hop in front of it."""
    c, users, _db = client
    await users.create_user(username="archer", password="password1")

    for _ in range(8):
        res = await c.post(
            "/api/auth/login", json={"username": "archer", "password": "wrong"}
        )
        assert res.status_code == 401

    blocked = await c.post(
        "/api/auth/login", json={"username": "archer", "password": "wrong"}
    )
    assert blocked.status_code == 429
    # Even the right password waits: otherwise the throttle is a hint that the
    # guess was close.
    right = await c.post(
        "/api/auth/login", json={"username": "archer", "password": "password1"}
    )
    assert right.status_code == 429


@pytest.mark.asyncio
async def test_a_successful_login_clears_the_count(client):
    c, users, _db = client
    await users.create_user(username="archer", password="password1")
    for _ in range(3):
        await c.post(
            "/api/auth/login", json={"username": "archer", "password": "wrong"}
        )
    ok = await c.post(
        "/api/auth/login", json={"username": "archer", "password": "password1"}
    )
    assert ok.status_code == 200
    for _ in range(5):
        assert (
            await c.post(
                "/api/auth/login", json={"username": "archer", "password": "wrong"}
            )
        ).status_code == 401


@pytest.mark.parametrize("change", ["password", "disable", "delete"])
async def test_login_rechecks_password_proof_when_committing_token(client, monkeypatch, change):
    c, users, db = client
    user = await users.create_user(username="archer", password="password1")
    await c.post("/api/auth/login", json={"username": "archer", "password": "wrong"})
    verified, release = asyncio.Event(), asyncio.Event()
    authenticate = users.authenticate

    async def paused(username, password):
        observed = await authenticate(username, password)
        verified.set()
        await release.wait()
        return observed

    monkeypatch.setattr(users, "authenticate", paused)
    pending = asyncio.create_task(c.post(
        "/api/auth/login", json={"username": "archer", "password": "password1"}
    ))
    try:
        await asyncio.wait_for(verified.wait(), 2)
        if change == "password":
            await users.set_password(user["id"], "changed-password")
        elif change == "disable":
            await users.set_disabled(user["id"], True)
        else:
            async with db.transaction(write=True) as tx:
                await tx.execute("DELETE FROM users WHERE id = ?", (user["id"],))
        release.set()
        response = await asyncio.wait_for(pending, 2)
        assert response.status_code == 401
        assert "token" not in response.json()
        assert await db.list_tokens(user["id"]) == []
        assert [len(attempts) for attempts in auth_router._FAILURES.values()] == [2]
        if change == "disable":
            await users.set_disabled(user["id"], False)
            assert await db.list_tokens(user["id"]) == []
    finally:
        release.set()
        await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["query", "header", "cookie"])
async def test_logout_revokes_only_this_device(client, source):
    c, users, _db = client
    user = await users.create_user(username="archer", password="password1")
    here = await users.issue_token(user["id"])
    elsewhere = await users.issue_token(user["id"])

    assert (await c.post("/api/auth/logout", **_credential(source, here))).status_code == 204
    assert (await c.get("/api/auth/identity", headers=_bearer(here))).status_code == 401
    assert (
        await c.get("/api/auth/identity", headers=_bearer(elsewhere))
    ).status_code == 200


@pytest.mark.parametrize("source", ["header", "query", "cookie"])
async def test_logout_waits_for_revocation_commit_before_acknowledging(client, monkeypatch, source):
    c, users, db = client
    user = await users.create_user(username="archer", password="password1")
    token = await users.issue_token(user["id"])
    entered, release = asyncio.Event(), asyncio.Event()
    execute, commit = db.conn.execute, db.conn.commit
    revoking = False

    async def observed_execute(sql, parameters=()):
        nonlocal revoking
        cursor = await execute(sql, parameters)
        if "UPDATE auth_tokens SET revoked_at" in sql:
            revoking = True
        return cursor

    async def gated_commit():
        if revoking:
            entered.set()
            await release.wait()
        await commit()

    with monkeypatch.context() as patch:
        patch.setattr(db.conn, "execute", observed_execute)
        patch.setattr(db.conn, "commit", gated_commit)
        pending = asyncio.create_task(c.post("/api/auth/logout", **_credential(source, token)))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            assert not pending.done()
            release.set()
            response = await pending
        finally:
            release.set()
            await asyncio.gather(pending, return_exceptions=True)
    assert response.status_code == 204
    assert (await c.get("/api/auth/identity", headers=_bearer(token))).status_code == 401


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["query", "header", "cookie"])
async def test_changing_a_password_needs_the_current_one(client, source):
    """An authenticated request is not enough: a bearer left on a shared
    machine must not be able to take the account over."""
    c, users, _db = client
    user = await users.create_user(username="archer", password="password1")
    token = await users.issue_token(user["id"])

    wrong = await c.post(
        "/api/auth/password",
        json={"current_password": "nope", "new_password": "a-new-password"},
        **_credential(source, token),
    )
    assert wrong.status_code == 403
    assert (await c.get("/api/auth/identity", headers=_bearer(token))).status_code == 200

    elsewhere = await users.issue_token(user["id"])
    ok = await c.post(
        "/api/auth/password",
        json={"current_password": "password1", "new_password": "a-new-password"},
        **_credential(source, token),
    )
    assert ok.status_code == 204
    # The tab that changed it stays in; the password really changed.
    assert (await c.get("/api/auth/identity", headers=_bearer(token))).status_code == 200
    assert (await c.get("/api/auth/identity", headers=_bearer(elsewhere))).status_code == 401
    assert (await c.post(
        "/api/auth/login", json={"username": "archer", "password": "password1"}
    )).status_code == 401
    assert (
        await c.post(
            "/api/auth/login", json={"username": "archer", "password": "a-new-password"}
        )
    ).status_code == 200


@pytest.mark.parametrize("operation", ["logout", "password"])
@pytest.mark.parametrize("with_header", [True, False])
async def test_credential_changes_use_the_highest_priority_presented_source(client, operation, with_header):
    c, users, _db = client
    user = await users.create_user(username="archer", password="password1")
    header, query, cookie, elsewhere = [await users.issue_token(user["id"]) for _ in range(4)]
    headers = {"Cookie": f"octopus_app_token={cookie}"}
    if with_header:
        headers.update(_bearer(header))
    selected = header if with_header else query
    payload = {"current_password": "password1", "new_password": "changed-password"}
    response = await c.post(
        f"/api/auth/{operation}", headers=headers, params={"token": query},
        json=payload if operation == "password" else None,
    )
    assert response.status_code == 204 and response.content == b""
    for token in (header, query, cookie, elsewhere):
        remains_valid = token != selected if operation == "logout" else token == selected
        identity = await c.get("/api/auth/identity", headers=_bearer(token))
        assert identity.status_code == (200 if remains_valid else 401)


@pytest.mark.parametrize("operation", ["logout", "password"])
@pytest.mark.parametrize("source", ["header", "query", "cookie"])
@pytest.mark.parametrize("refusal", ["invalid", "expired", "revoked", "disabled", "mcp", "app"])
async def test_credential_changes_refuse_bad_selected_credentials_without_fallback(client, operation, source, refusal):
    c, users, db = client
    owner = await users.create_user(username="owner", password="password1")
    peer = await users.create_user(username="peer", password="password1")
    selected = await users.issue_token(owner["id"])
    valid = await users.issue_token(peer["id"])
    if refusal == "invalid":
        selected = "invalid-account-token"
    elif refusal == "expired":
        async with db.transaction(write=True) as tx:
            await tx.execute(
                "UPDATE auth_tokens SET expires_at = ? WHERE token_hash = ?",
                ("2000-01-01T00:00:00+00:00", token_digest(selected)),
            )
    elif refusal == "revoked":
        await users.revoke_token(selected)
    elif refusal == "disabled":
        await users.set_disabled(owner["id"], True)
    elif refusal == "mcp":
        selected = mint("account-only-refusal", user_id=owner["id"])
        # This scope is accepted by the runtime-aware identity route.
        assert (await c.get("/api/auth/identity", headers=_bearer(selected))).status_code == 200
    else:
        selected = app_scope_token("owned-app")

    headers = {"Cookie": f"octopus_app_token={selected if source == 'cookie' else valid}"}
    params = {}
    if source == "header":
        headers.update(_bearer(selected))
        params["token"] = valid
    elif source == "query":
        params["token"] = selected
    response = await c.post(
        f"/api/auth/{operation}", headers=headers, params=params,
        json={"current_password": "password1", "new_password": "changed-password"},
    )
    assert response.status_code == 401
    assert response.json() == {"detail": "Invalid token"}
    assert (await c.get("/api/auth/identity", headers=_bearer(valid))).status_code == 200
    assert (await c.post("/api/auth/login", json={
        "username": "peer", "password": "password1",
    })).status_code == 200


# ---------------------------------------------------------------------------
# Joining
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_registration_needs_an_invite_and_signs_you_in(client):
    from server.config import settings

    c, users, _db = client
    setup = await c.post(
        "/api/auth/setup", headers=_bearer(settings.auth_token),
        json={"username": "archer", "password": "password1"},
    )
    assert setup.status_code == 200 and setup.json()["is_admin"] is True
    admin_token = setup.json()["token"]

    made = await c.post(
        "/api/auth/invites", json={"max_uses": 1}, headers=_bearer(admin_token)
    )
    assert made.status_code == 200
    code = made.json()["code"]

    no_code = await c.post(
        "/api/auth/register",
        json={"invite_code": "nope", "username": "vera", "password": "password2"},
    )
    assert no_code.status_code == 403

    joined = await c.post(
        "/api/auth/register",
        json={"invite_code": code, "username": "vera", "password": "password2"},
    )
    assert joined.status_code == 200
    assert joined.json()["is_admin"] is False
    assert (
        await c.get("/api/auth/identity", headers=_bearer(joined.json()["token"]))
    ).json()["label"] == "vera"

    # Single use.
    assert (
        await c.post(
            "/api/auth/register",
            json={"invite_code": code, "username": "pete", "password": "password3"},
        )
    ).status_code == 403


@pytest.mark.parametrize("entry", ["setup", "register"])
async def test_account_disabled_between_creation_and_issuance_returns_explicit_refusal(client, monkeypatch, entry):
    from server.config import settings

    c, users, db = client
    payload = {"username": "new-owner", "password": "new-account-password"}
    if entry == "register":
        admin = await users.create_user(username="operator", password="password1", is_admin=True)
        invite = await users.create_invite(created_by=admin["id"])
        payload["invite_code"] = invite["code"]
    entered, release = asyncio.Event(), asyncio.Event()
    execute, commit = db.conn.execute, db.conn.commit
    created = []

    async def observed_execute(sql, parameters=()):
        cursor = await execute(sql, parameters)
        if sql.startswith("INSERT INTO users"):
            created.append(parameters[0])
        return cursor

    async def gated_commit():
        if created and not entered.is_set():
            entered.set()
            await release.wait()
        await commit()

    with monkeypatch.context() as patch:
        patch.setattr(db.conn, "execute", observed_execute)
        patch.setattr(db.conn, "commit", gated_commit)
        registering = asyncio.create_task(c.post(f"/api/auth/{entry}", json=payload, headers=_bearer(settings.auth_token)))
        disabling = None
        try:
            await asyncio.wait_for(entered.wait(), 2)
            disabling = asyncio.create_task(users.set_disabled(created[0], True))
            await asyncio.sleep(0)
            release.set()
            await disabling
            response = await registering
        finally:
            release.set()
            await asyncio.gather(registering, *([disabling] if disabling else []), return_exceptions=True)
    assert response.status_code == 403
    assert (await db.get_user(created[0]))["disabled_at"]
    assert await db.list_tokens(created[0]) == []
    if entry == "register":
        assert (await db.get_invite(invite["code"]))["used_count"] == 1


async def test_retained_invite_cannot_create_the_first_account(client, monkeypatch):
    from pathlib import Path

    from server.config import settings

    c, users, db = client
    monkeypatch.setattr(settings, "master_key", "")
    invite = await users.create_invite(created_by=None)
    before = await db.list_invites()
    assert not Path(settings.master_key_file).exists()
    assert not Path(settings.users_root).exists()
    response = await c.post("/api/auth/register", json={
        "invite_code": invite["code"], "username": "first-invite", "password": "registered-password",
    })
    assert response.status_code == 409
    assert await db.list_invites() == before
    assert await db.list_users() == []
    assert await db.load_agents(include_archived=True) == []
    async with db.transaction(write=False) as tx:
        assert await tx.fetch_one("SELECT COUNT(*) FROM auth_tokens") == (0,)
    assert not Path(settings.master_key_file).exists()
    assert not Path(settings.users_root).exists()


# ---------------------------------------------------------------------------
# Admin
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_admin_routes_are_closed_to_a_normal_account(client):
    c, users, _db = client
    normal = await users.create_user(username="vera", password="password2")
    token = await users.issue_token(normal["id"])

    for method, path in (
        ("post", "/api/auth/invites"),
        ("get", "/api/auth/invites"),
        ("get", "/api/auth/users"),
    ):
        res = await getattr(c, method)(
            path, headers=_bearer(token), **({"json": {}} if method == "post" else {})
        )
        assert res.status_code == 403, f"{method} {path} let a normal account in"


@pytest.mark.asyncio
async def test_an_admin_cannot_disable_themselves(client):
    """The one way to end up with a site nobody can administer."""
    c, users, _db = client
    admin = await users.create_user(
        username="archer", password="password1", is_admin=True
    )
    token = await users.issue_token(admin["id"])

    res = await c.post(
        f"/api/auth/users/{admin['id']}/disabled",
        json={"disabled": True},
        headers=_bearer(token),
    )
    assert res.status_code == 400


@pytest.mark.asyncio
async def test_disabling_an_account_ends_its_sessions_now(client):
    c, users, _db = client
    admin = await users.create_user(
        username="archer", password="password1", is_admin=True
    )
    admin_token = await users.issue_token(admin["id"])
    victim = await users.create_user(username="vera", password="password2")
    victim_token = await users.issue_token(victim["id"])
    assert (
        await c.get("/api/auth/identity", headers=_bearer(victim_token))
    ).status_code == 200

    res = await c.post(
        f"/api/auth/users/{victim['id']}/disabled",
        json={"disabled": True},
        headers=_bearer(admin_token),
    )
    assert res.status_code == 200 and res.json()["disabled_at"]
    assert (
        await c.get("/api/auth/identity", headers=_bearer(victim_token))
    ).status_code == 401, "a disabled account kept its bearer"


# ---------------------------------------------------------------------------
# Setup proof is never an ordinary account credential.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_setup_proof_cannot_open_business_before_accounts(client):
    from server.config import settings

    c, _users, _db = client
    res = await c.get("/api/sessions", headers=_bearer(settings.auth_token))
    assert res.status_code == 401


@pytest.mark.asyncio
async def test_setup_proof_does_not_invent_an_operator_identity(client):
    from server.config import settings

    c, _users, _db = client
    res = await c.get("/api/auth/identity", headers=_bearer(settings.auth_token))
    assert res.status_code == 401
    assert settings.auth_token not in res.text


async def test_setup_proof_has_no_installation_administrator_authority(client):
    from server.config import settings

    http, users, _ = client
    response = await http.put("/api/auth/users/missing/extra-roots", headers=_bearer(settings.auth_token),
                              json={"extra_roots": []})
    assert response.status_code == 401
    assert await users.db.count_users() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("token", [None, "wrong-install-token"])
async def test_identity_rejects_missing_or_invalid_bearer_before_accounts(client, token):
    c, _users, _db = client
    res = await c.get("/api/auth/identity", headers=_bearer(token) if token else {})
    assert res.status_code == 401


@pytest.mark.asyncio
async def test_setup_proof_cannot_open_business_after_account_creation_either(client):
    from server.config import settings

    c, users, _db = client
    assert (
        await c.get("/api/sessions", headers=_bearer(settings.auth_token))
    ).status_code == 401
    assert (
        await c.get("/api/auth/identity", headers=_bearer(settings.auth_token))
    ).status_code == 401

    await users.create_user(username="archer", password="password1")

    for path in ("/api/sessions", "/api/auth/identity"):
        assert (
            await c.get(path, headers=_bearer(settings.auth_token))
        ).status_code == 401, "the global token outlived the first account"


@pytest.mark.asyncio
async def test_a_session_bearer_opens_the_ordinary_routes(client):
    """The replacement works on the surface the global token used to open,
    which is what makes retiring it possible rather than theoretical."""
    c, users, _db = client
    user = await users.create_user(username="archer", password="password1")
    token = await users.issue_token(user["id"])

    assert (await c.get("/api/sessions", headers=_bearer(token))).status_code == 200
    assert (await c.get("/api/agents", headers=_bearer(token))).status_code == 200


@pytest.mark.asyncio
async def test_a_personal_access_token_works_where_a_session_does(client):
    """What scripts and the CLI use once the global token is gone (§3.1)."""
    c, users, _db = client
    user = await users.create_user(username="archer", password="password1")
    pat = await users.issue_token(user["id"], kind="pat", label="a script")

    assert (await c.get("/api/sessions", headers=_bearer(pat))).status_code == 200


@pytest.mark.parametrize("credential", ["missing", "wrong", "setup", "ownerless"])
async def test_workspace_requires_current_business_identity_before_setup(client, credential):
    from server.config import settings
    from server.mcp_identity import mint

    c, _users, _db = client
    tokens = {"wrong": "wrong-workspace-token", "setup": settings.auth_token,
              "ownerless": mint("retained-workspace-session")}
    headers = {} if credential == "missing" else _bearer(tokens[credential])
    response = await c.get("/api/auth/workspace", headers=headers)
    assert response.status_code == 401, response.text
    assert "workspace" not in response.json() and "confined" not in response.json()


async def test_workspace_reports_only_the_signed_in_accounts_roots(client):
    from server.workspace import paths_for

    c, users, _db = client
    owner = await users.create_user(username="workspace-owner", password="password1")
    token = await users.issue_token(owner["id"])
    response = await c.get("/api/auth/workspace", headers=_bearer(token))
    assert response.status_code == 200, response.text
    assert response.json() == {"workspace": str(paths_for(owner["id"]).workspace),
                               "extra_roots": [], "confined": True}


@pytest.mark.asyncio
async def test_an_extra_root_can_be_opened_and_closed_after_the_upgrade(
    client, tmp_path, monkeypatch
):
    """The gap this closes: `extra_roots` was written once, by the upgrade, and
    nothing could change it afterwards. Clone a repository the day after and
    every session in it is refused, with no way to allow it short of editing
    the database by hand."""
    from server.config import settings

    c, users, _db = client
    monkeypatch.setattr(settings, "users_root", str(tmp_path / "users"))
    admin = await users.create_user(username="archer", password="password1", is_admin=True)
    token = await users.issue_token(admin["id"])
    repo = tmp_path / "a-new-repo"
    repo.mkdir()
    flow = (await c.post(f"/api/auth/users/{admin['id']}/workspace-edits", json={"flow_id": "open-close"}, headers=_bearer(token))).json()
    identity = {key: flow[key] for key in ("flow_id", "service_generation")}

    opened = await c.put(
        f"/api/auth/users/{admin['id']}/extra-roots",
        json={**identity, "sequence": 1, "extra_roots": [str(repo)]},
        headers=_bearer(token),
    )
    assert opened.status_code == 200, opened.text
    assert opened.json()["extra_roots"] == [str(repo.resolve())]

    # And it is what `confine` now consults.
    from server.workspace import confine

    assert confine(str(repo / "src"), user_id=admin["id"], extra_roots=[str(repo)])

    seen = await c.get("/api/auth/workspace", headers=_bearer(token))
    assert seen.json() == {
        "workspace": str(tmp_path / "users" / admin["id"] / "workspace"),
        "extra_roots": [str(repo.resolve())],
        "confined": True,
    }

    closed = await c.put(
        f"/api/auth/users/{admin['id']}/extra-roots",
        json={**identity, "sequence": 2, "extra_roots": []},
        headers=_bearer(token),
    )
    assert closed.json()["extra_roots"] == []


@pytest.mark.asyncio
async def test_an_extra_root_that_would_switch_confinement_off_is_refused(
    client, tmp_path
):
    """`/` and `/home` do not open a repository — they turn the boundary off
    while leaving it looking on."""
    c, users, _db = client
    admin = await users.create_user(username="archer", password="password1", is_admin=True)
    token = await users.issue_token(admin["id"])

    flow = (await c.post(f"/api/auth/users/{admin['id']}/workspace-edits", json={"flow_id": "invalid-path"}, headers=_bearer(token))).json()
    identity = {key: flow[key] for key in ("flow_id", "service_generation")}
    for sequence, bad in enumerate(("/", "/home", "relative/path", str(tmp_path / "does-not-exist")), start=1):
        res = await c.put(
            f"/api/auth/users/{admin['id']}/extra-roots",
            json={**identity, "sequence": sequence, "extra_roots": [bad]},
            headers=_bearer(token),
        )
        assert res.status_code == 400, f"{bad!r} answered {res.status_code}"


@pytest.mark.asyncio
async def test_an_account_cannot_open_its_own_hole_in_the_confinement(client):
    """An extra root is a hole in the boundary, so it is the operator's to
    punch. An account that could punch its own would make the boundary
    advisory."""
    c, users, _db = client
    await users.create_user(username="archer", password="password1", is_admin=True)
    vera = await users.create_user(username="vera", password="password2")
    token = await users.issue_token(vera["id"])

    res = await c.put(
        f"/api/auth/users/{vera['id']}/extra-roots",
        json={"extra_roots": ["/tmp"]},
        headers=_bearer(token),
    )
    assert res.status_code == 403, res.text
