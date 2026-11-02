"""Signing in, joining by invite, and the admin surface (multi-tenancy.md §3).

The route layer, where `tests/test_users.py` covers the rules underneath. What
is worth pinning here is what only exists once there is an HTTP boundary: that
a bearer is required, that the throttle actually bites, that an admin route is
closed to a normal account, and that the one way to lock everybody out — an
admin disabling themselves — is refused.
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from server import deps
from server.agent_manager import AgentManager
from server.database import Database
from server.main import app
from server.routers import agents as agents_router
from server.routers import auth as auth_router
from server.session_manager import session_manager
from server.users import UserManager


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


# ---------------------------------------------------------------------------
# Signing in
# ---------------------------------------------------------------------------


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


@pytest.mark.asyncio
async def test_logout_revokes_only_this_device(client):
    c, users, _db = client
    user = await users.create_user(username="archer", password="password1")
    here = await users.issue_token(user["id"])
    elsewhere = await users.issue_token(user["id"])

    assert (
        await c.post("/api/auth/logout", headers=_bearer(here))
    ).status_code == 204
    assert (await c.get("/api/auth/identity", headers=_bearer(here))).status_code == 401
    assert (
        await c.get("/api/auth/identity", headers=_bearer(elsewhere))
    ).status_code == 200


@pytest.mark.asyncio
async def test_changing_a_password_needs_the_current_one(client):
    """An authenticated request is not enough: a bearer left on a shared
    machine must not be able to take the account over."""
    c, users, _db = client
    user = await users.create_user(username="archer", password="password1")
    token = await users.issue_token(user["id"])

    wrong = await c.post(
        "/api/auth/password",
        json={"current_password": "nope", "new_password": "a-new-password"},
        headers=_bearer(token),
    )
    assert wrong.status_code == 403

    elsewhere = await users.issue_token(user["id"])
    ok = await c.post(
        "/api/auth/password",
        json={"current_password": "password1", "new_password": "a-new-password"},
        headers=_bearer(token),
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

    opened = await c.put(
        f"/api/auth/users/{admin['id']}/extra-roots",
        json={"extra_roots": [str(repo)]},
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
        json={"extra_roots": []},
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

    for bad in ("/", "/home", "relative/path", str(tmp_path / "does-not-exist")):
        res = await c.put(
            f"/api/auth/users/{admin['id']}/extra-roots",
            json={"extra_roots": [bad]},
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
