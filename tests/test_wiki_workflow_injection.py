"""Stable Wiki discovery and authorized just-in-time guide loading."""

from types import SimpleNamespace

import pytest

from server.wiki.tasks import tasks
from server.wiki.workflow import BOOTSTRAP, guide, instructions, load_guide, view
from tests import wiki_api as wiki_api

client = wiki_api.client


def card(token):
    return view(tasks.active.get(token)).card


@pytest.mark.parametrize("mode", ["auto", "read", "maintain"])
@pytest.mark.parametrize("curator", [False, True])
async def test_regular_turn_only_advertises_stable_discovery_and_loads_current_guides_on_use(client, tmp_path, mode, curator):
    vault = tmp_path / "vault"
    vault.mkdir()
    config = await wiki_api.configure(client, vault)
    owner = (await wiki_api.session_manager.db.get_system_agent())["user_id"]
    session = SimpleNamespace(id="reader", user_id=owner, agent_id=config["curator_agent_id"] if curator else "ordinary-reader")
    token = tasks.open(session, mode)
    try:
        prompt = instructions(mode, token)
        operation = card(token)
        assert operation["injected_guides"] == []
        assert operation["mode"] == mode
        assert operation["vault_id"] == config["vault_id"]
        assert operation["curator_agent_id"] == config["curator_agent_id"]
        assert operation["writable_files"] == []
        assert "capabilities" not in operation and "guides" not in operation
        assert prompt.startswith(BOOTSTRAP)
        routing = prompt.splitlines()[-1]
        assert ("当前角色 query" if mode == "read" else "当前角色 submitter") in routing
        assert ("加载 query" if mode == "read" else "加载 entry") in routing
        for name in ("access", "query-v2", "entry", "worker-v2", "retrieval-v2"):
            assert guide(name) not in prompt
        assert config["vault_id"] not in prompt and config["registration_generation"] not in prompt
        # Marker reuse is advisory, never a cached denial of a fresh guide request.
        assert load_guide("query", token) == load_guide("query", token) == guide("query-v2")
        info = (await client.get("/api/wiki/info", headers=client.headers)).json()
        assert info["capabilities"]["incremental_index"] is True
    finally:
        tasks.close(token)


async def test_unbound_turn_does_not_advertise_a_vault_or_write_grants(client):
    owner = (await wiki_api.session_manager.db.get_system_agent())["user_id"]
    token = tasks.open(SimpleNamespace(id="unbound", user_id=owner, agent_id="reader"), "auto")
    try:
        prompt = instructions("auto", token)
        operation = card(token)
        assert operation["state"] == "主库尚未绑定；用 info 确认状态"
        assert operation["injected_guides"] == []
        assert prompt.startswith(BOOTSTRAP)
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
    owner = (await wiki_api.session_manager.db.get_system_agent())["user_id"]
    session = SimpleNamespace(id="worker", user_id=owner, agent_id=config["curator_agent_id"])
    jobs = WikiJobs(owner)
    job = jobs.maintain("injection-worker", "", "organize", ["Library/note.md"], None, [], "human")
    jobs.update(job["id"], status="running", session_id=session.id)
    tasks.assign(session.id, job["id"], job["items"])
    token = tasks.open(session, "maintain")
    try:
        prompt = instructions("maintain", token)
        operation = card(token)
        assert operation["injected_guides"] == []
        assert operation["job_id"] == job["id"]
        assert operation["writable_files"] == ["Library/note.md"]
        assert prompt.startswith(BOOTSTRAP) and guide("worker-v2") not in prompt
        assert "当前角色 worker" in prompt.splitlines()[-1] and "加载 worker" in prompt.splitlines()[-1]
        assert guide("worker-v2") in load_guide("worker", token)
        assert load_guide("writing", token) == load_guide("writing", token) == guide("writing")
        jobs.update(job["id"], status="cancelled")
        ended = instructions("maintain", token)
        assert card(token)["injected_guides"] == [] and card(token)["actor_role"] == "ended_worker"
        assert ended.startswith(BOOTSTRAP)
        assert "当前角色 ended_worker" in ended.splitlines()[-1] and "仅可查询" in ended.splitlines()[-1]
        with pytest.raises(WikiError):
            load_guide("worker", token)
        with pytest.raises(WikiError) as refused:
            load_guide("writing", token)
        assert refused.value.status == 403
        assert load_guide("query", token) == guide("query-v2")
    finally:
        tasks.close(token)
        tasks.unassign(session.id)


def test_worker_retains_cold_read_safeguards_and_writing_semantic_checks():
    worker = guide("worker-v2")
    for text in ("全部新增、修改和结构移动影响的最终区段", "必要邻接上下文", "不只检查最后一个补丁", "正文逐字节相同", "当前元数据已观察", "no_change 不新增写后阅读义务"):
        assert text in worker
    writing = guide("writing")
    for text in ("论点、归属、条件和来源位置", "突兀过渡", "遗漏理由", "被强化的结论", "完整终稿冷读合同"):
        assert text in writing
    assert "完整终稿冷读合同" in guide("maintenance-v2")


async def workflow_article(client, tmp_path, summary_heading="Key note"):
    vault = tmp_path / "vault"
    (vault / "Library").mkdir(parents=True)
    note = vault / "Library/note.md"
    original = (
        f"# Note\n\n## {summary_heading}\n\nThe conclusion applies only under condition A.\n\n"
        "## 正文\n\nOriginal claim and its supporting condition remain.\n"
    )
    note.write_text(original)
    config = await wiki_api.configure(client, vault)
    return note, config, original


async def test_actual_read_survives_guide_loading_but_not_a_fresh_task(client, tmp_path):
    from server.mcp_identity import mint
    from server.wiki.history import WikiHistory
    from server.wiki.jobs import WikiJobs
    from server.wiki.registry import registry

    note, config, original = await workflow_article(client, tmp_path)
    path = "Library/note.md"
    owner = (await wiki_api.session_manager.db.get_system_agent())["user_id"]
    jobs = WikiJobs(owner)
    job = jobs.maintain("guide-read-reuse", "", "organize", [path], None, [], "human")
    session = SimpleNamespace(id="guide-read-worker", user_id=owner, agent_id=config["curator_agent_id"])
    jobs.update(job["id"], status="running", session_id=session.id)
    tasks.assign(session.id, job["id"], job["items"])
    token = tasks.open(session, "maintain")

    def headers():
        return {"Authorization": f"Bearer {mint(session.id, user_id=session.user_id, wiki_task_id=token)}"}

    try:
        response = await client.get("/api/wiki/read", params={"doc": path, "view": "compact"}, headers=headers())
        assert response.status_code == 200, response.text
        current = response.json()
        assert current["content"] == original and current["next_start"] is None
        for module in ("article", "writing"):
            loaded = await client.get("/api/wiki/guide", params={"module": module}, headers=headers())
            assert loaded.status_code == 200, loaded.text
            assert loaded.json()["content"] == load_guide(module, token)
        normalized = await client.post("/api/wiki/organize", headers=headers(), json={
            "path": path, "expected_revision": current["revision"], "reuse_existing_summary": True,
            "frontmatter_patch": {"title": "Note", "x_wiki": {"profile": "article-v1", "body_dialect": "markdown"}},
            "reason": "Reuse the already read article after loading its guides",
        })
        assert normalized.status_code == 200, normalized.text
        assert note.read_text().endswith(original)
        # Record the new version in the old task, so the next denial tests the
        # task boundary rather than merely using an unread changed revision.
        observed = await client.get("/api/wiki/read", params={"doc": path}, headers=headers())
        assert observed.status_code == 200, observed.text
        final = observed.json()
        assert final["revision"] == normalized.json()["revision"] and final["next_start"] is None
        history = WikiHistory(registry.store(owner))
        before_history, before_bytes = history.history(path), note.read_bytes()
        tasks.close(token)
        token = tasks.open(session, "maintain")
        body = {"doc": path, "expected_revision": final["revision"], "operation": "no_change", "reason": "Check the current article"}
        denied = await client.post("/api/wiki/revise", json=body, headers=headers())
        assert denied.status_code == 422 and "full current note" in denied.text
        read = await client.get("/api/wiki/read", params={"doc": path}, headers=headers())
        assert read.status_code == 200 and read.json()["next_start"] is None
        accepted = await client.post("/api/wiki/revise", json=body, headers=headers())
        assert accepted.status_code == 200, accepted.text
        assert accepted.json()["change_id"] is None and accepted.json()["receipt_id"]
        assert note.read_bytes() == before_bytes and history.history(path) == before_history
    finally:
        tasks.close(token)
        tasks.unassign(session.id)


@pytest.mark.parametrize("case", ["unsupported_frontmatter", "external_resource_without_doc", "unrecognized_summary"])
async def test_article_request_errors_do_not_write_or_convert_originals(client, tmp_path, case):
    from server.wiki.history import WikiHistory
    from server.wiki.registry import registry

    heading = "摘要" if case == "unrecognized_summary" else "Key note"
    note, _, original = await workflow_article(client, tmp_path, heading)
    path = "Library/note.md"
    current = (await client.get("/api/wiki/read", params={"doc": path}, headers=client.headers)).json()
    owner = (await wiki_api.session_manager.db.get_system_agent())["user_id"]
    history = WikiHistory(registry.store(owner))
    before_history = history.history(path)
    body = {
        "path": path, "expected_revision": current["revision"], "reuse_existing_summary": True,
        "frontmatter_patch": {"title": "Note", "x_wiki": {"profile": "article-v1", "body_dialect": "markdown"}},
        "reason": "Validate the article request without changing the original",
    }
    if case == "unsupported_frontmatter":
        body["frontmatter_patch"].update({"source": "/materials/source.md", "source_sha256": "a" * 64, "source_bytes": 123})
    elif case == "external_resource_without_doc":
        body["sources"] = [{"resource": "file:///materials/source.md", "revision": current["revision"], "location": "L1"}]
    result = await client.post("/api/wiki/organize", headers=client.headers, json=body)
    assert result.status_code == 422, result.text
    if case == "unsupported_frontmatter":
        assert "Unsupported frontmatter_patch field" in result.text
    elif case == "external_resource_without_doc":
        assert any(error["loc"] == ["body", "sources", 0, "doc"] for error in result.json()["detail"])
    else:
        assert "missing Key note" in result.text
    assert note.read_bytes() == original.encode()
    assert history.history(path) == before_history
