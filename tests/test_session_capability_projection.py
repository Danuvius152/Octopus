"""All session API views describe the same backend capabilities."""

import pytest

from tests.test_agent_credential_bindings import bindings as bindings


@pytest.mark.parametrize("backend,can_steer", [("claude-code", True), ("codex", False)])
async def test_session_capabilities_are_consistent_across_live_and_archived_views(bindings, backend, can_steer):
    client, headers, _, _ = bindings
    agent = await client.post("/api/agents", headers=headers, json={"name": "Projection", "backend": backend})
    agent_id = agent.json()["id"]
    created = await client.post("/api/sessions", headers=headers, json={"agent_id": agent_id, "name": "Projection"})
    assert created.status_code == 201
    session_id = created.json()["id"]
    url = f"/api/sessions/{session_id}"
    detail = await client.get(url, headers=headers)
    listing = (await client.get("/api/sessions", headers=headers)).json()
    updated = await client.patch(url, headers=headers, json={"name": "Updated"})
    for response in (created.json(), detail.json(), updated.json(), next(s for s in listing if s["id"] == session_id)):
        assert response["backend"] == backend
        assert response["can_steer"] is can_steer
        assert response["message_count"] == 0
        assert response["transcript_integrity"] == "ok"
    archived = await client.post(url + "/archive?replace=false", headers=headers)
    assert archived.is_success, archived.text
    history = (await client.get(url, headers=headers)).json()
    all_sessions = (await client.get("/api/sessions?include_archived=true", headers=headers)).json()
    summary = next(s for s in all_sessions if s["id"] == session_id)
    assert history["can_steer"] is can_steer and summary["can_steer"] is can_steer
    assert history["archived"] and summary["archived"]
    assert history["messages"] == [] and history["next_message_seq"] == 0
    restored = await client.post(url + "/unarchive", headers=headers)
    assert restored.status_code == 200, restored.text
    assert restored.json()["can_steer"] is can_steer


@pytest.mark.parametrize("backend,can_steer", [("claude-code", True), ("codex", False)])
async def test_import_uses_common_fields_without_changing_transcript_watermark(bindings, backend, can_steer):
    client, headers, _, _ = bindings
    imported = await client.post("/api/sessions/import", headers=headers, json={
        "backend": backend, "name": "Imported", "messages": [
            {"role": "user", "type": "text", "content": "question"},
            {"role": "assistant", "type": "text", "content": "answer"},
        ],
    })
    assert imported.is_success, imported.text
    body = imported.json()
    assert body["can_steer"] is can_steer
    assert body["message_count"] == 2
    assert body["next_message_seq"] == 2
    assert [message["content"] for message in body["messages"]] == ["question", "answer"]
