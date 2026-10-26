"""Unsupported saved tool restrictions are refused rather than silently ignored."""

import pytest

from server.harness import RunConfig, get_harness
from tests.test_agent_credential_bindings import bindings as bindings


@pytest.mark.parametrize("backend,field", [
    ("codex", "tool_allow"), ("codex", "tool_deny"), ("claude-code", "tool_allow"),
])
async def test_unsupported_policy_is_refused_atomically_and_legacy_value_preserved(bindings, backend, field):
    client, headers, manager, _ = bindings
    created = await client.post("/api/agents", headers=headers, json={"name": "Unchanged", "backend": backend})
    assert created.status_code == 201
    agent_id = created.json()["id"]
    url = f"/api/agents/{agent_id}"
    rejected = await client.patch(url, headers=headers, json={"name": "Changed", field: "Read"})
    assert rejected.status_code == 400, rejected.text
    assert field in rejected.json()["detail"]
    assert (await client.get(url, headers=headers)).json()["name"] == "Unchanged"
    # Legacy values remain inspectable, and are never silently cleared by a save.
    await manager.db.update_agent(agent_id, **{field: "Read"})
    again = await client.patch(url, headers=headers, json={"name": "Changed"})
    assert again.status_code == 400
    null_policy = await client.patch(url, headers=headers, json={"name": "Changed", field: None})
    assert null_policy.status_code == 400
    assert (await client.get(url, headers=headers)).json()[field] == "Read"
    fixed = await client.patch(url, headers=headers, json={field: ""})
    assert fixed.status_code == 200
    invalid_create = await client.post("/api/agents", headers=headers, json={"name": "Invalid", "backend": backend, field: "Read"})
    assert invalid_create.status_code == 400


def test_runtime_refuses_ignored_restrictions_and_renders_claude_denies(tmp_path):
    for backend, field in (("codex", "tool_allow"), ("codex", "tool_deny"), ("claude-code", "tool_allow")):
        run = get_harness(backend).create_run(RunConfig(**{field: ["Read"]}))
        with pytest.raises(ValueError, match=field):
            run.build_argv("test", str(tmp_path))
    argv, _ = get_harness("claude-code").create_run(RunConfig(tool_deny=["Write", "mcp__*"])).build_argv("test", str(tmp_path))
    assert argv[argv.index("--disallowedTools") + 1] == "AskUserQuestion,Write,mcp__*"
    assert "--allowedTools" not in argv


async def test_backend_metadata_exposes_policy_support(bindings):
    client, headers, _, _ = bindings
    response = await client.get("/api/backends", headers=headers)
    assert response.status_code == 200
    policies = response.json()["tool_policy"]
    assert policies["claude-code"] == {"allow": False, "deny": True}
    if "codex" in policies:
        assert policies["codex"] == {"allow": False, "deny": False}


async def test_backend_switch_preserves_supported_deny_until_user_clears_it(bindings):
    client, headers, _, _ = bindings
    created = await client.post("/api/agents", headers=headers, json={"name": "Denied", "backend": "claude-code", "tool_deny": "Bash"})
    assert created.status_code == 201
    url = f"/api/agents/{created.json()['id']}"
    switched = await client.patch(url, headers=headers, json={"backend": "codex"})
    assert switched.status_code == 400
    current = (await client.get(url, headers=headers)).json()
    assert current["backend"] == "claude-code" and current["tool_deny"] == "Bash"
    null_deny = await client.patch(url, headers=headers, json={"name": "Changed", "backend": "codex", "tool_deny": None})
    assert null_deny.status_code == 400
    current = (await client.get(url, headers=headers)).json()
    assert current["name"] == "Denied" and current["backend"] == "claude-code" and current["tool_deny"] == "Bash"
    corrected = await client.patch(url, headers=headers, json={"backend": "codex", "tool_deny": ""})
    assert corrected.status_code == 200


@pytest.mark.parametrize("field,value", [
    ("system_prompt", "中" * 50000),
    ("subagents", [{"name": "reviewer", "description": "Review", "prompt": "中" * 50000}]),
], ids=["system_prompt", "subagents"])
async def test_null_cannot_hide_oversized_retained_static_configuration(bindings, field, value):
    client, headers, manager, _ = bindings
    created = await client.post("/api/agents", headers=headers, json={"name": "Original", "backend": "claude-code"})
    agent_id = created.json()["id"]
    await manager.db.update_agent(agent_id, **{field: value})
    url = f"/api/agents/{agent_id}"
    before = (await client.get(url, headers=headers)).json()
    rejected = await client.patch(url, headers=headers, json={"name": "Changed", field: None})
    assert rejected.status_code == 400
    assert "bytes" in rejected.json()["detail"]
    current = (await client.get(url, headers=headers)).json()
    assert current["name"] == "Original" and current[field] == before[field]


async def test_nullable_fields_still_clear_explicitly(bindings):
    from tests.test_agent_credential_bindings import _create_credential

    client, headers, manager, _ = bindings
    credential = await _create_credential(client, headers, "claude-code")
    created = await client.post("/api/agents", headers=headers, json={
        "name": "Nullable", "backend": "claude-code", "credential_id": credential,
        "model": "claude-opus-4-7", "avatar": "old-avatar",
    })
    assert created.status_code == 201, created.text[:300]
    await manager.db.update_agent(created.json()["id"], reasoning_effort="legacy", service_tier="fast")
    cleared = await client.patch(f"/api/agents/{created.json()['id']}", headers=headers, json={
        "model": None, "avatar": None, "credential_id": None, "reasoning_effort": None, "service_tier": None,
    })
    assert cleared.status_code == 200
    assert all(cleared.json()[field] is None for field in ("model", "avatar", "credential_id", "reasoning_effort", "service_tier"))
