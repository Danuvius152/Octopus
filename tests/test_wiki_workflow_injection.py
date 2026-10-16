"""Injected-guide metadata describes the actual fresh instruction branch."""

import json
from types import SimpleNamespace

import pytest

from server.wiki.tasks import tasks
from server.wiki.workflow import guide, instructions, load_guide
from tests import test_wiki_api as wiki_api

client = wiki_api.client


def card(prompt):
    return json.loads(prompt.splitlines()[0].split("：", 1)[1])


@pytest.mark.parametrize("mode,injected,entry", [
    ("auto", ["query", "entry"], "auto-entry"),
    ("read", ["query"], None),
    ("maintain", ["query", "entry"], "entry"),
])
async def test_regular_turn_marks_only_injected_content_and_keeps_fresh_scope(client, tmp_path, mode, injected, entry):
    vault = tmp_path / "vault"
    vault.mkdir()
    config = await wiki_api.configure(client, vault)
    session = SimpleNamespace(id="reader", user_id=None, agent_id="ordinary-reader")
    token = tasks.open(session, mode)
    try:
        prompt = instructions(mode, token)
        operation = card(prompt)
        assert operation["injected_guides"] == injected
        assert operation["mode"] == mode
        assert operation["vault_id"] == config["vault_id"]
        assert operation["curator_agent_id"] == config["curator_agent_id"]
        assert operation["writable_files"] == []
        assert "capabilities" not in operation and "guides" not in operation
        assert guide("access") in prompt and guide("query-v2") in prompt
        assert guide("worker-v2") not in prompt and guide("retrieval-v2") not in prompt
        if entry:
            assert guide(entry) in prompt
        else:
            assert guide("entry") not in prompt and guide("auto-entry") not in prompt
        # Marker reuse is advisory, never a cached denial of a fresh guide request.
        assert load_guide("query", token) == load_guide("query", token) == guide("query-v2")
        info = (await client.get("/api/wiki/info", headers=wiki_api.HEADERS)).json()
        assert info["capabilities"]["incremental_index"] is True
    finally:
        tasks.close(token)


async def test_unbound_turn_does_not_advertise_a_vault_or_write_grants(client):
    token = tasks.open(SimpleNamespace(id="unbound", user_id=None, agent_id="reader"), "auto")
    try:
        prompt = instructions("auto", token)
        operation = card(prompt)
        assert operation["state"] == "主库尚未绑定；用 info 确认状态"
        assert operation["injected_guides"] == ["query", "entry"]
        assert "vault_id" not in operation and "writable_files" not in operation
        assert "capabilities" not in operation and "guides" not in operation
    finally:
        tasks.close(token)


async def test_worker_and_cancelled_assignment_mark_the_actual_branch(client, tmp_path):
    from server.wiki.jobs import WikiJobs
    from server.wiki.store import WikiError

    vault = tmp_path / "vault"
    (vault / "Library").mkdir(parents=True)
    (vault / "Library/note.md").write_text("# Source\nKeep these words.\n")
    config = await wiki_api.configure(client, vault)
    session = SimpleNamespace(id="worker", user_id=None, agent_id=config["curator_agent_id"])
    jobs = WikiJobs(None)
    job = jobs.maintain("injection-worker", "", "organize", ["Library/note.md"], None, [], "human")
    jobs.update(job["id"], status="running", session_id=session.id)
    tasks.assign(session.id, job["id"], ["Library/note.md"])
    token = tasks.open(session, "maintain")
    try:
        prompt = instructions("maintain", token)
        operation = card(prompt)
        assert operation["injected_guides"] == ["worker"]
        assert operation["job_id"] == job["id"]
        assert operation["writable_files"] == ["Library/note.md"]
        assert guide("worker-v2") in prompt and guide("query-v2") not in prompt
        assert load_guide("writing", token) == load_guide("writing", token) == guide("writing")
        jobs.update(job["id"], status="cancelled")
        ended = instructions("maintain", token)
        assert card(ended)["injected_guides"] == ["query"]
        assert guide("query-v2") in ended and guide("worker-v2") not in ended
        assert guide("entry") not in ended and "只可查询，不发起或继续维护" in ended
        with pytest.raises(WikiError) as refused:
            load_guide("writing", token)
        assert refused.value.status == 403
        assert load_guide("query", token) == guide("query-v2")
    finally:
        tasks.close(token)
        tasks.unassign(session.id)


def test_worker_retains_cold_read_safeguards_and_writing_semantic_checks():
    worker = guide("worker-v2")
    for text in ("全部新增、修改区段", "必要邻接上下文", "不只检查最后一个补丁", "正文逐字节相同", "当前元数据已观察", "no_change 不新增写后阅读义务"):
        assert text in worker
    writing = guide("writing")
    for text in ("论点、归属、条件和来源位置", "突兀过渡", "遗漏理由", "被强化的结论", "完整终稿冷读合同"):
        assert text in writing
    assert "完整终稿冷读合同" in guide("maintenance-v2")
