"""Fixed process inputs determine reuse; encoded launch budgets fail clearly."""

import asyncio
import json
import os
import sys
from dataclasses import replace

import pytest

from server.connectors.base import ConnectorInstallation
from server.connectors.registry import get_connector
from server.harness import Harness, RunConfig, get_harness
from server.harness.launch_limits import LaunchConfigurationError, validate_launch
from server.harness.profile import McpServerEntry
from server.harness.registry import _REGISTRY
from server.session_manager import session_manager
from tests.harness_profiles import native_submission
from tests.test_agent_credential_bindings import bindings as bindings


def test_process_identity_includes_actual_fixed_inputs(tmp_path):
    harness = get_harness("claude-code")
    base = RunConfig(mcp_servers=[])
    reference = harness.create_run(base).spawn_signature(str(tmp_path), None)
    for changed in (
        replace(base, mcp_servers=None),
        replace(base, turn_instructions="This run requires a different instruction"),
        replace(base, subagents=[{"name": "reviewer", "description": "Review", "prompt": "New rules"}]),
    ):
        assert harness.create_run(changed).spawn_signature(str(tmp_path), None) != reference
    connector = get_connector("github")
    first = ConnectorInstallation(id="abcdef123456", kind="github", label="Old account")
    second = replace(first, label="New account")
    a = harness.create_run(replace(base, connectors=[(connector, first)]))
    b = harness.create_run(replace(base, connectors=[(connector, second)]))
    assert a.spawn_signature(str(tmp_path), None) != b.spawn_signature(str(tmp_path), None)


def test_equivalent_configuration_and_ephemeral_http_identity_can_reuse(tmp_path):
    harness = get_harness("claude-code")
    first = RunConfig(session_id="synthetic-session", mcp_servers=["bg", "ask"], subagents=[
        {"name": "reviewer", "description": "Review", "prompt": "Rules"},
    ])
    second = replace(first, mcp_servers=["ask", "bg"], subagents=[
        {"prompt": "Rules", "description": "Review", "name": "reviewer"},
    ])
    a, b = harness.create_run(first), harness.create_run(second)
    assert a.run_lease_id != b.run_lease_id
    assert a.spawn_signature(str(tmp_path), None) == b.spawn_signature(str(tmp_path), None)
    codex = get_harness("codex")
    assert codex.create_run(RunConfig(turn_instructions="First")).spawn_signature(str(tmp_path), None) == codex.create_run(
        RunConfig(turn_instructions="Second")).spawn_signature(str(tmp_path), None)


@pytest.mark.parametrize("backend", ["claude-code", "codex"])
def test_http_connector_binding_uses_full_installation_identity(tmp_path, backend):
    connector = get_connector("github")
    first = ConnectorInstallation(id="abcdef111111", kind="github", label="Same label")
    second = replace(first, id="abcdef222222")
    assert connector.mcp_key(first) == connector.mcp_key(second)
    harness = get_harness(backend)
    config = RunConfig(session_id="connector-binding", mcp_servers=[], connectors=[(connector, first)])
    a = harness.create_run(config)
    b = harness.create_run(replace(config, connectors=[(connector, second)]))
    assert a.spawn_signature(str(tmp_path), None) != b.spawn_signature(str(tmp_path), None)
    # A new lease for the same full account binding still reuses its process.
    assert a.spawn_signature(str(tmp_path), None) == harness.create_run(config).spawn_signature(str(tmp_path), None)


async def test_changed_subagents_replace_held_process_and_deliver_new_configuration(bindings, tmp_path, monkeypatch):
    client, headers, _, _ = bindings
    model = tmp_path / "held_configuration.py"
    model.write_text(
        "import json,os,sys\n"
        "config=json.loads(sys.argv[1])\n"
        "for line in sys.stdin:\n"
        " json.loads(line)\n"
        " text=json.dumps(dict(pid=os.getpid(),**config))\n"
        " print(json.dumps({'type':'assistant','message':{'content':[{'type':'text','text':text}]}}),flush=True)\n"
        " print(json.dumps({'type':'result','subtype':'success','session_id':'synthetic-thread'}),flush=True)\n"
    )
    profile = get_harness("claude-code").profile

    def render(ctx):
        return [sys.executable, str(model), json.dumps({"subagents": ctx.subagents})], {"cwd": ctx.working_dir}

    monkeypatch.setitem(_REGISTRY, "claude-code", Harness(replace(profile, build_turn_argv=render, activity_observer=None)))
    agent = await client.post("/api/agents", headers=headers, json={"name": "Configuration", "backend": "claude-code"})
    agent_id = agent.json()["id"]
    created = await client.post("/api/sessions", headers=headers, json={"agent_id": agent_id})
    session_id = created.json()["id"]
    try:
        first = await session_manager.start_message(session_id, "First")
        assert (await asyncio.wait_for(first.wait(), 5)).outcome.status == "success"
        definition = {"name": "reviewer", "description": "Review", "prompt": "New rules", "tools": []}
        patched = await client.patch(f"/api/agents/{agent_id}", headers=headers, json={"subagents": [definition]})
        assert patched.status_code == 200, patched.text
        second = await session_manager.start_message(session_id, "Second")
        assert (await asyncio.wait_for(second.wait(), 5)).outcome.status == "success"
        third = await session_manager.start_message(session_id, "Equivalent configuration")
        assert (await asyncio.wait_for(third.wait(), 5)).outcome.status == "success"
        detail = (await client.get(f"/api/sessions/{session_id}", headers=headers)).json()
        observed = [json.loads(message["content"]) for message in detail["messages"] if message["role"] == "assistant" and message["type"] == "text"]
        assert len(observed) == 3
        assert observed[0]["pid"] != observed[1]["pid"]
        assert observed[0]["subagents"] == []
        assert observed[1]["subagents"][0]["prompt"] == "New rules"
        assert observed[1] == observed[2]
    finally:
        await session_manager.delete_session(session_id)


@pytest.mark.parametrize("backend", ["claude-code", "codex"])
async def test_oversized_saved_system_prompt_is_refused_without_changing_original(bindings, backend, monkeypatch):
    if backend == "codex":
        monkeypatch.setattr("server.harness.codex_app_server._FRAME_LIMIT_BYTES", 128 * 1024)
    client, headers, _, _ = bindings
    created = await client.post("/api/agents", headers=headers, json={"name": "Unchanged", "backend": backend, "system_prompt": "Keep original"})
    assert created.status_code == 201
    url = f"/api/agents/{created.json()['id']}"
    oversized = "配置" * 30000  # 180000 UTF-8 bytes before renderer overhead.
    rejected = await client.patch(url, headers=headers, json={"system_prompt": oversized, "name": "Changed"})
    assert rejected.status_code == 400, rejected.text[:300]
    assert "bytes" in rejected.json()["detail"]
    original = (await client.get(url, headers=headers)).json()
    assert original["system_prompt"] == "Keep original" and original["name"] == "Unchanged"
    assert len(oversized.encode()) == 180000


@pytest.mark.parametrize("kind", ["argument", "environment"])
@pytest.mark.parametrize("delta", [-1, 0, 1])
def test_encoded_item_limit_includes_unicode_and_nul(kind, delta):
    # Native /bin/true probe fixes this contract at 32 Linux pages, NUL included.
    limit = 32 * os.sysconf("SC_PAGE_SIZE")
    payload_size = limit + delta - (1 if kind == "argument" else 3)
    text = "中" * (payload_size // 3) + "x" * (payload_size % 3)
    argv, env = (["/bin/true", text], {}) if kind == "argument" else (["/bin/true"], {"X": text})
    if delta > 0:
        with pytest.raises(LaunchConfigurationError, match=f"{limit + 1} bytes"):
            validate_launch(argv, env)
    else:
        validate_launch(argv, env)


@pytest.mark.parametrize("delta", [-1, 0, 1])
def test_combined_budget_counts_argument_and_environment_pointer_slots(delta):
    import struct

    # Independent worked exec stack: 33 strings, 35 pointer slots, no env.
    budget = min(os.sysconf("SC_ARG_MAX"), 2097152) - 16384
    prefix = ["/bin/true", *(["x" * 65500] * 31)]
    prefix_size = 10 + 31 * 65501 + 35 * struct.calcsize("P")
    final = "x" * (budget + delta - prefix_size - 1)
    if delta > 0:
        with pytest.raises(LaunchConfigurationError, match="Combined arguments/environment"):
            validate_launch([*prefix, final], {})
    else:
        validate_launch([*prefix, final], {})


@pytest.mark.parametrize("backend", ["claude-code", "codex"])
@pytest.mark.parametrize("surface", ["controller", "MCP", "environment"])
async def test_dynamic_configuration_budget_refuses_before_any_spawn(tmp_path, monkeypatch, backend, surface):
    if backend == "codex":
        monkeypatch.setattr("server.harness.codex_app_server._FRAME_LIMIT_BYTES", 128 * 1024)
    profile = get_harness(backend).profile
    original = profile.build_turn_argv
    spawned = []

    def render(ctx):
        dynamic = ctx
        if ctx.prompt and surface == "controller":
            dynamic = replace(ctx, system_prompt=ctx.system_prompt + "中" * 50000)
        elif ctx.prompt and surface == "MCP":
            dynamic = replace(ctx, mcp_servers=[McpServerEntry(key="budget_test", command=sys.executable, env={"TEST_ONLY": '"\\中' * 50000})])
        argv, kwargs = original(dynamic)
        if ctx.prompt and surface == "environment":
            kwargs["env"]["TEST_ONLY"] = "never-quote-this-value" + "x" * 150000
        return [sys.executable, *argv[1:]], kwargs

    async def forbidden_spawn(*args, **kwargs):
        spawned.append(args)
        pytest.fail("Oversized dynamic configuration must never submit an OS spawn")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden_spawn)
    harness = Harness(replace(profile, build_turn_argv=render, activity_observer=None))
    harness.validate_configuration(RunConfig(mcp_servers=[]))
    run = harness.create_run(RunConfig(mcp_servers=[],
        system_prompt="中" * 50000 if backend == "codex" and surface == "controller" else None))
    if backend == "codex":
        native_submission(run, "configuration-request", "configuration-attempt")
    try:
        with pytest.raises((LaunchConfigurationError, ValueError), match="bytes") as refused:
            await run.start("Dynamic additions appear at execution", str(tmp_path))
        assert "never-quote-this-value" not in str(refused.value)
    finally:
        await run.stop()
    assert not spawned


async def test_escaped_subagent_configuration_is_checked_after_serialization(bindings):
    client, headers, _, _ = bindings
    created = await client.post("/api/agents", headers=headers, json={"name": "Subagents", "backend": "claude-code"})
    url = f"/api/agents/{created.json()['id']}"
    rejected = await client.patch(url, headers=headers, json={"subagents": [
        {"name": "reviewer", "description": "Review", "prompt": '"\\中' * 20000},
    ]})
    assert rejected.status_code == 400, rejected.text[:300]
    assert "subagents" in rejected.json()["detail"] and "bytes" in rejected.json()["detail"]
    assert (await client.get(url, headers=headers)).json()["subagents"] == []
