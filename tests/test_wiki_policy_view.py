"""A fresh policy description agrees with the live HTTP/MCP authorization."""

import json
from types import SimpleNamespace

import pytest

from server.mcp_identity import mint, reset_current_scope, set_current_scope, verify
from server.mcp_result import TEXT_RESULT_MAX_BYTES
from server.mcp_servers.wiki import mcp
from server.wiki import workflow
from server.wiki.jobs import WikiJobs
from server.wiki.tasks import tasks
from tests import test_wiki_api as wiki_api

client = wiki_api.client


def operation(token, mode):
    return json.loads(workflow.instructions(mode, token).splitlines()[0].split("：", 1)[1])


@pytest.mark.parametrize("mode", ["auto", "read", "maintain"])
async def test_operation_card_and_actual_tools_guides_share_one_current_policy(client, tmp_path, mode):
    vault = tmp_path / "vault"
    vault.mkdir()
    config = await wiki_api.configure(client, vault)
    session = SimpleNamespace(id="policy-reader", user_id=None, agent_id="ordinary")
    token = tasks.open(session, mode, request_id="original-user-input")
    bearer = mint(session.id, wiki_task_id=token)
    scope = set_current_scope(verify(bearer))
    try:
        card = operation(token, mode)
        assert card["policy_version"] >= 1 and len(card["guide_hash"]) == 64
        assert card["registration_generation"] == config["registration_generation"]
        assert card["actor_role"] == ("query" if mode == "read" else "submitter")
        assert set(card["available_tools"]) == {tool.name for tool in await mcp.list_tools()}
        assert card["response_limits"]["text_bytes"] == TEXT_RESULT_MAX_BYTES
        assert set(card["available_guides"]) == {"query", "retrieval", "entry"}
        loaded = await client.get("/api/wiki/guide", params={"module": "retrieval"},
                                  headers={"Authorization": "Bearer " + bearer})
        assert loaded.status_code == 200, loaded.text
        assert loaded.json()["policy_version"] == card["policy_version"]
        assert loaded.json()["guide_hash"] == card["guide_hash"]
        assert loaded.json()["content"] == workflow.guide("retrieval-v2")
        assert operation(token, mode)["guide_hash"] == card["guide_hash"]
    finally:
        reset_current_scope(scope)
        tasks.close(token)


async def test_cancelled_worker_card_and_tools_revoke_together_without_trusting_the_old_hash(client, tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "note.md").write_text("# Note\nPreserve the original argument.\n")
    config = await wiki_api.configure(client, vault)
    session = SimpleNamespace(id="policy-worker", user_id=None, agent_id=config["curator_agent_id"])
    jobs = WikiJobs(None)
    job = jobs.maintain("policy-cancel", "", "Review this note", ["note.md"], None, [], "human")
    jobs.update(job["id"], status="running", session_id=session.id)
    tasks.assign(session.id, job["id"], job["items"], registration=config)
    token = tasks.open(session, "maintain")
    bearer = mint(session.id, wiki_task_id=token)
    scope = set_current_scope(verify(bearer))
    try:
        old = operation(token, "maintain")
        assert old["actor_role"] == "worker" and old["grant_id"] == job["id"]
        assert {"organize", "cancel_job", "retry_job"} <= set(old["available_tools"])
        jobs.mark_cancelled(job["id"])
        ended = operation(token, "maintain")
        assert ended["actor_role"] == "ended_worker" and ended["injected_guides"] == ["query"]
        assert ended["writable_files"] == []
        assert set(ended["available_tools"]) == {tool.name for tool in await mcp.list_tools()}
        assert not set(ended["available_tools"]) & {"maintain", "organize", "cancel_job", "retry_job", "publish"}
        denied = await client.get("/api/wiki/guide", params={"module": "writing", "guide_hash": old["guide_hash"]},
                                  headers={"Authorization": "Bearer " + bearer})
        assert denied.status_code == 403, denied.text
    finally:
        reset_current_scope(scope)
        tasks.close(token)
        tasks.unassign(session.id)


async def test_large_exact_batch_is_bounded_without_shortening_any_identity_or_path(client, tmp_path):
    vault = tmp_path / "vault"
    paths = ["Library/" + (f"segment-{index}-" + "x" * 180 + "/") * 10 + "note.md" for index in range(3)]
    for path in paths:
        target = vault / path
        target.parent.mkdir(parents=True)
        target.write_text("# Note\nKeep the complete current text.\n")
    config = await wiki_api.configure(client, vault)
    jobs = WikiJobs(None)
    job = jobs.maintain("policy-large", "", "Review these exact paths", paths, None, [], "human")
    session = SimpleNamespace(id="large-worker", user_id=None, agent_id=config["curator_agent_id"])
    jobs.update(job["id"], status="running", session_id=session.id)
    tasks.assign(session.id, job["id"], job["items"], registration=config)
    token = tasks.open(session, "maintain")
    try:
        card = operation(token, "maintain")
        assert len(json.dumps(card, ensure_ascii=False).encode()) <= card["response_limits"]["card_bytes"]
        assert card["writable_files_count"] == 3 and card["writable_files_truncated"]
        assert set(card["writable_files"]) < set(paths)
        assert card["scope_details"] == {"tool": "jobs", "job_id": job["id"], "view": "full", "export": True}
        assert {item["path"] for item in jobs.get(job["id"])["items"]} == set(paths)
    finally:
        tasks.close(token)
        tasks.unassign(session.id)


async def test_guide_hash_changes_with_the_actual_guide_bundle_but_never_enables_writes(client, tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    vault.mkdir()
    await wiki_api.configure(client, vault)
    token = tasks.open(SimpleNamespace(id="hash-reader", user_id=None, agent_id="ordinary"), "read")
    try:
        before = operation(token, "read")
        original = workflow.guide
        monkeypatch.setattr(workflow, "guide", lambda name: original(name) + ("\nRevised retrieval policy.\n" if name == "retrieval-v2" else ""))
        after = operation(token, "read")
        assert before["guide_hash"] != after["guide_hash"]
        assert after["available_tools"] == before["available_tools"]
        assert after["writable_files"] == []
        tasks.close(token)
        closed = operation(token, "maintain")
        assert "maintain" not in closed["available_tools"] and closed["injected_guides"] == ["query"]
    finally:
        tasks.close(token)
