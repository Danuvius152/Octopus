"""New public maintenance controls reject retired modes before admission."""

import pytest

from server.applications import app_scope_token
from tests import test_app_agent as app_agent_tests
from tests import test_schedule_tool as schedule_tests

# Keep the App fixture's original named dependencies alongside its client alias.
apps_root = app_agent_tests.apps_root
db = app_agent_tests.db
account_token = app_agent_tests.account_token
app_client = app_agent_tests.client
schedule_client = schedule_tests.client


@pytest.mark.parametrize("endpoint", ["ask", "chat"])
async def test_app_rejects_new_off_without_a_conversation_or_execution(
    app_client, account_token, endpoint,
):
    application = await app_agent_tests._api_app(app_client, account_token)
    base = f"/apps/{application['id']}/agent"
    headers = {"X-Octopus-App-Token": app_scope_token(application["id"])}
    before = await app_client.get(f"{base}/conversations", headers=headers)
    assert before.status_code == 200
    admitted = list(app_client.turns.prompts)

    response = await app_client.post(
        f"{base}/{endpoint}", headers=headers,
        json={"message": "read the article", "wiki_mode": "off"},
    )

    assert response.status_code == 422, response.text
    after = await app_client.get(f"{base}/conversations", headers=headers)
    assert after.status_code == 200
    assert after.json() == before.json()
    assert app_client.turns.prompts == admitted


@pytest.mark.parametrize("endpoint", ["ask", "chat"])
@pytest.mark.parametrize("mode", ["auto", "read", "maintain"])
async def test_app_accepts_the_three_current_controls(
    app_client, account_token, endpoint, mode,
):
    application = await app_agent_tests._api_app(app_client, account_token)
    response = await app_client.post(
        f"/apps/{application['id']}/agent/{endpoint}",
        headers={"X-Octopus-App-Token": app_scope_token(application["id"])},
        json={"message": "read the article", "wiki_mode": mode},
    )
    assert response.status_code == 200, response.text


async def schedule_creation(schedule_client, entry):
    session_id = await schedule_tests._session(schedule_client)
    agents = await schedule_client.get("/api/agents")
    assert agents.status_code == 200
    agent_id = agents.json()[0]["id"]
    body = {"name": "Article check", "prompt": "read the article", "interval_seconds": 600}
    if entry == "account":
        path = "/api/schedules"
        body["agent_id"] = agent_id
    elif entry == "agent":
        path = f"/api/agents/{agent_id}/schedules"
    elif entry == "text":
        path = f"/api/agents/{agent_id}/schedules/from_text"
        body = {"text": "10m read the article"}
    else:
        path = f"/api/sessions/{session_id}/schedules"
    return path, body


@pytest.mark.parametrize("entry", ["account", "agent", "text", "session"])
async def test_schedule_creation_rejects_new_off_without_a_task(
    schedule_client, entry,
):
    path, body = await schedule_creation(schedule_client, entry)
    before = await schedule_client.get("/api/sessions")
    assert before.status_code == 200

    response = await schedule_client.post(path, json={**body, "wiki_mode": "off"})

    assert response.status_code == 422, response.text
    schedules = await schedule_client.get("/api/schedules")
    assert schedules.status_code == 200
    assert schedules.json() == []
    after = await schedule_client.get("/api/sessions")
    assert after.status_code == 200
    assert after.json() == before.json()


@pytest.mark.parametrize("entry", ["account", "agent", "text", "session"])
@pytest.mark.parametrize("mode", ["auto", "read", "maintain"])
async def test_schedule_creation_accepts_the_three_current_controls(
    schedule_client, entry, mode,
):
    path, body = await schedule_creation(schedule_client, entry)
    response = await schedule_client.post(path, json={**body, "wiki_mode": mode})
    assert response.status_code == 201, response.text
    assert response.json()["wiki_mode"] == mode


@pytest.mark.parametrize("entry", ["account", "session"])
async def test_schedule_updates_reject_new_off_and_keep_the_saved_task(
    schedule_client, entry,
):
    session_id = await schedule_tests._session(schedule_client)
    path = f"/api/sessions/{session_id}/schedules"
    created = await schedule_client.post(
        path, json={"prompt": "read the article", "interval_seconds": 600, "wiki_mode": "read"},
    )
    assert created.status_code == 201, created.text
    schedule_id = created.json()["id"]
    if entry == "account":
        path = "/api/schedules"
    before = await schedule_client.get(path)
    assert before.status_code == 200

    response = await schedule_client.patch(
        f"{path}/{schedule_id}",
        json={"prompt": "replace the task", "wiki_mode": "off"},
    )

    assert response.status_code == 422, response.text
    after = await schedule_client.get(path)
    assert after.status_code == 200
    assert after.json() == before.json()


@pytest.mark.parametrize("entry", ["account", "session"])
@pytest.mark.parametrize("mode", ["auto", "read", "maintain"])
async def test_schedule_updates_accept_the_three_current_controls(
    schedule_client, entry, mode,
):
    session_id = await schedule_tests._session(schedule_client)
    path = f"/api/sessions/{session_id}/schedules"
    created = await schedule_client.post(
        path, json={"prompt": "read the article", "interval_seconds": 600},
    )
    assert created.status_code == 201, created.text
    if entry == "account":
        path = "/api/schedules"
    response = await schedule_client.patch(
        f"{path}/{created.json()['id']}", json={"wiki_mode": mode},
    )
    assert response.status_code == 200, response.text
    assert response.json()["wiki_mode"] == mode


@pytest.mark.parametrize("name", ["create", "update"])
async def test_schedule_mcp_advertises_only_the_three_current_controls(name):
    from server.mcp_servers.schedule import mcp

    tool = next(tool for tool in await mcp.list_tools() if tool.name == name)
    field = tool.inputSchema["properties"]["wiki_mode"]
    choices = next(part for part in field["anyOf"] if part.get("type") == "string")
    assert choices["enum"] == ["auto", "read", "maintain"]
    assert "auto/read/maintain" in tool.description


@pytest.mark.parametrize("name", ["create", "update"])
async def test_schedule_mcp_rejects_off_before_reaching_http(name, monkeypatch):
    import httpx
    from mcp.server.fastmcp.exceptions import ToolError

    from server.mcp_servers.schedule import mcp

    monkeypatch.setenv("OCTOPUS_API_BASE", "http://test")
    monkeypatch.setenv("OCTOPUS_SESSION_ID", "session")
    monkeypatch.setenv("OCTOPUS_AUTH_TOKEN", "scoped-token")
    sent = []

    def request(*args, **kwargs):
        sent.append((args, kwargs))
        return httpx.Response(201, json={"id": "unexpected", "prompt": "read"})

    monkeypatch.setattr(httpx, "post", request)
    monkeypatch.setattr(httpx, "patch", request)
    arguments = {"wiki_mode": "off"}
    if name == "create":
        arguments.update(prompt="read", interval_seconds=600)
    else:
        arguments.update(schedule_id="saved-task")

    with pytest.raises(ToolError, match="wiki_mode"):
        await mcp.call_tool(name, arguments)
    assert sent == []
