"""All session API views describe the same backend capabilities."""

import asyncio
import json
import sys
from dataclasses import replace

import pytest

from server.harness import get_harness
from server.harness.profile import StdinMode
from server.mcp_identity import reset_current_scope, set_current_scope, verify
from server.mcp_servers.wiki import mcp as wiki_mcp
from server.session_manager import session_manager
from server.wiki.registry import registry
from server.wiki.tasks import tasks
from server.workspace import paths_for
from tests.test_agent_credential_bindings import bindings as bindings


@pytest.mark.parametrize("can_steer,reusable", [(False, True), (True, False), (False, False), (True, True)])
async def test_owner_uses_steer_and_reuse_capabilities_independently(bindings, tmp_path, monkeypatch, can_steer, reusable):
    client, headers, _agent_manager, user_id = bindings
    manager = session_manager
    harness = get_harness("codex")
    effects, release = tmp_path / "inputs.jsonl", tmp_path / "release"
    script = tmp_path / "capabilities.py"
    script.write_text(
        "import json,os,sys,time\nfrom pathlib import Path\n"
        "def record(line):\n"
        " text=json.loads(line)['message']['content']\n"
        " with Path(sys.argv[1]).open('a') as out: out.write(json.dumps({'pid':os.getpid(),'text':text})+'\\n')\n"
        " print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':text}}),flush=True)\n"
        " return text\n"
        "for line in sys.stdin:\n"
        " text=record(line)\n"
        " if text == 'first':\n"
        f"  if {can_steer!r}: record(sys.stdin.readline())\n"
        "  while not Path(sys.argv[2]).exists(): time.sleep(.01)\n"
        " print('{\"type\":\"turn.completed\",\"usage\":{}}',flush=True)\n"
    )
    scopes = []

    def argv(context):
        scopes.extend(verify(entry.credential) for entry in context.mcp_servers if entry.key == "wiki")
        return [sys.executable, "-u", str(script), str(effects), str(release)], {"cwd": context.working_dir}

    monkeypatch.setattr(harness, "profile", replace(
        harness.profile, binary=sys.executable, activity_observer=None, prepare_resume=None,
        stdin_mode=StdinMode.STREAM_JSON, can_steer=can_steer, reusable=reusable,
        build_turn_argv=argv,
    ))
    agent = await client.post("/api/agents", headers=headers, json={"name": "Independent capabilities", "backend": "codex"})
    vault = paths_for(user_id).workspace / "capability-vault"
    vault.mkdir()
    registry.configure(user_id, vault, agent.json()["id"])
    created = await client.post("/api/sessions", headers=headers, json={"agent_id": agent.json()["id"]})
    assert created.status_code == 201, created.text
    session_id = created.json()["id"]
    assert created.json()["can_steer"] is can_steer
    ready = asyncio.Event()

    async def observe(event):
        if event.get("session_id") == session_id and event.get("type") == "assistant_text" and event.get("content") == "first":
            ready.set()

    manager.on_broadcast("capability-ready", observe)
    try:
        first = await manager.start_message(session_id, "first")
        await asyncio.wait_for(ready.wait(), 3)
        scope = scopes[-1]
        token = set_current_scope(scope)
        try:
            assert {"read", "search"} <= {tool.name for tool in await wiki_mcp.list_tools()}
            assert tasks.require(scope).request_id == first.request_id
        finally:
            reset_current_scope(token)
        second = await manager.start_message(session_id, "second", steerable=True)
        assert second.disposition == ("steered" if can_steer else "accepted")
        if not can_steer:
            assert [item["request_id"] for item in manager.dispatch_snapshot(session_id)["items"]] == [second.request_id]
        release.touch()
        first_done, second_done = await asyncio.wait_for(asyncio.gather(first.wait(), second.wait()), 5)
        assert first_done.outcome.status == second_done.outcome.status == "success"
        assert second_done.request_id == (first.request_id if can_steer else second.request_id)
        observed = [json.loads(line) for line in effects.read_text().splitlines()]
        assert [row["text"] for row in observed] == ["first", "second"]
        assert (observed[0]["pid"] == observed[1]["pid"]) is (can_steer or reusable)
    finally:
        release.touch()
        manager.remove_broadcast("capability-ready")
        await manager.drain_turns()
        await manager.stop_all_held_processes()


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
