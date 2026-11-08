"""Request identity uses the app's dependency wiring and real account tokens."""

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from server import deps
from server.database import Database
from server.users import UserManager


@pytest.fixture
async def identities():
    database = Database(":memory:")
    await database.initialize()
    users = UserManager(database)
    app = FastAPI()

    @app.get("/current")
    async def current(user: deps.CurrentUser):
        return {"user_id": user["id"]}

    @app.get("/scope")
    async def scope(user_id: deps.ScopeUser):
        return {"user_id": user_id}

    # This is the sole override; account resolution and MCP authorization stay real.
    app.dependency_overrides[deps.get_user_manager] = lambda: users
    try:
        owner = await users.create_user(username="request-owner", password="test-password")
        token = await users.issue_token(owner["id"])
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://identity") as client:
            yield client, users, owner, token
    finally:
        await database.close()


@pytest.mark.parametrize("presentation", ["header", "query", "cookie"])
async def test_both_identity_dependencies_use_the_same_manager_override(identities, presentation):
    client, _users, owner, token = identities
    request = {
        "header": {"headers": {"Authorization": "Bearer " + token}},
        "query": {"params": {"token": token}},
        "cookie": {"headers": {"Cookie": "octopus_app_token=" + token}},
    }[presentation]
    responses = [await client.get(path, **request) for path in ("/current", "/scope")]
    assert [response.status_code for response in responses] == [200, 200]
    assert [response.json() for response in responses] == [{"user_id": owner["id"]}] * 2


@pytest.mark.parametrize("refusal", ["missing", "invalid", "revoked", "disabled"])
async def test_the_override_does_not_bypass_account_authority(identities, refusal):
    client, users, owner, token = identities
    if refusal == "revoked":
        await users.revoke_token(token)
    elif refusal == "disabled":
        await users.set_disabled(owner["id"], True)
    elif refusal == "invalid":
        token = "invalid-account-token"
    headers = {} if refusal == "missing" else {"Authorization": "Bearer " + token}
    for path in ("/current", "/scope"):
        response = await client.get(path, headers=headers)
        assert response.status_code == 401
        assert response.json() == {"detail": "Invalid token"}
