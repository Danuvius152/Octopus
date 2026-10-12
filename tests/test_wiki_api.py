"""Local knowledge behavior at its authenticated HTTP boundary."""

import pytest
from httpx import ASGITransport, AsyncClient

from server.config import settings
from server.database import Database
from server.main import app
from server.session_manager import session_manager

HEADERS = {"Authorization": "Bearer changeme"}


async def test_malformed_portable_metadata_is_evidence_not_executable(client, tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "bad.md").write_text(
        '<!-- octopus-wiki {"kind": [], "sources": ["bad"]} -->\n# 双链\n资料'
    )
    await configure(client, vault)
    found = await client.get(
        "/api/wiki/search", params={"query": "双链"}, headers=HEADERS
    )
    assert found.status_code == 200, found.text
    document = found.json()["items"][0]
    assert document["kind"] == "original"
    assert document["metadata"]["metadata_error"]


async def test_direct_read_detects_changed_source_without_a_search(client, tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "source.md").write_text("# 原文\n原始说法")
    await configure(client, vault)
    source = (
        await client.get("/api/wiki/read", params={"doc": "source.md"}, headers=HEADERS)
    ).json()
    published = await client.post(
        "/api/wiki/publish",
        headers=HEADERS,
        json={
            "path": "Wiki/concepts/claim.md",
            "content": "# 说法\n根据原文",
            "kind": "concept",
            "sources": [
                {
                    "doc": source["id"],
                    "revision": source["revision"],
                    "location": "L1-L2",
                }
            ],
            "expected_revision": None,
            "reason": "test",
        },
    )
    assert published.status_code == 200, published.text
    (vault / "source.md").write_text("# 原文\n后来变化")
    read = await client.get(
        "/api/wiki/read", params={"doc": "Wiki/concepts/claim.md"}, headers=HEADERS
    )
    assert read.status_code == 200, read.text
    assert read.json()["stale"] is True


@pytest.fixture
async def client(tmp_path, monkeypatch):
    import sys

    from server.deps import get_session_manager
    from server.session_manager import SessionManager

    manager = SessionManager()
    monkeypatch.setattr(sys.modules[__name__], "session_manager", manager)
    monkeypatch.setattr(settings, "wiki_dir", str(tmp_path / "state"), raising=False)
    monkeypatch.setattr(settings, "agents_dir", str(tmp_path / "agents"))
    monkeypatch.setattr(settings, "users_root", str(tmp_path / "users"))
    db = Database(":memory:")
    await db.initialize()
    session_manager.sessions.clear()
    await session_manager.initialize(db)
    app.dependency_overrides[get_session_manager] = lambda: manager
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c
    await db.close()
    app.dependency_overrides.pop(get_session_manager, None)


async def configure(client, vault):
    agent_id = (await session_manager.db.load_agents())[0]["id"]
    response = await client.put(
        "/api/wiki/config",
        headers=HEADERS,
        json={"root": str(vault), "curator_agent_id": agent_id},
    )
    assert response.status_code == 200, response.text
    return response.json()


async def test_user_searches_chinese_short_words_and_reads_revisioned_original(
    client, tmp_path
):
    vault = tmp_path / "vault"
    vault.mkdir()
    original = "---\naliases: [协作]\n---\n# Agent 协作\n\n双链支持来源核验。\n"
    (vault / "协作.md").write_text(original)
    await configure(client, vault)
    found = await client.get(
        "/api/wiki/search", params={"query": "双链"}, headers=HEADERS
    )
    assert found.status_code == 200, found.text
    hit = found.json()["items"][0]
    assert hit["path"] == "协作.md"
    read = await client.get(
        "/api/wiki/read", params={"doc": hit["id"]}, headers=HEADERS
    )
    assert read.status_code == 200, read.text
    assert read.json()["content"] == original
    assert read.json()["revision"] == hit["revision"]
    assert read.json()["line_start"] == 1
    assert (vault / "协作.md").read_text() == original


async def test_session_credentials_do_not_authorize_wiki_without_explicit_task(
    client, tmp_path
):
    from server.mcp_identity import mint

    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "private.md").write_text("# 私有资料\n不能从普通任务读取。")
    await configure(client, vault)
    response = await client.get(
        "/api/wiki/read",
        params={"doc": "private.md"},
        headers={"Authorization": f"Bearer {mint('ordinary-session')}"},
    )
    assert response.status_code == 403


async def test_default_search_excludes_tool_and_temporary_assets_but_can_read_them(
    client, tmp_path
):
    vault = tmp_path / "vault"
    vault.mkdir()
    files = {
        "Library/笔记.md": "# 双链\n普通文章。",
        "Library/course/tmp/extract.txt": "双链临时提取。",
        "Library/paper/static/css/readme.txt": "双链配套资源。",
        "Codex Skills/wiki/SKILL.md": "# 双链工具\n技能操作规则。",
    }
    for path, content in files.items():
        target = vault / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    await configure(client, vault)
    found = await client.get("/api/wiki/search?query=双链", headers=HEADERS)
    assert [item["path"] for item in found.json()["items"]] == ["Library/笔记.md"]
    assets = await client.get(
        "/api/wiki/documents?include_assets=true", headers=HEADERS
    )
    assert assets.json()["total"] == 4
    read = await client.get(
        "/api/wiki/read?doc=Library/course/tmp/extract.txt", headers=HEADERS
    )
    assert read.json()["content"] == "双链临时提取。"
    assert read.json()["kind"] == "asset"


async def test_wiki_mcp_discloses_query_tools_and_maintenance_entry_by_current_task(
    client, tmp_path
):
    import httpx
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    from server.mcp_identity import mint
    from server.wiki.tasks import tasks
    from tests.callback_api import start_callback_api

    vault = tmp_path / "vault"
    vault.mkdir()
    config = await configure(client, vault)
    session = await session_manager.create_session(
        config["curator_agent_id"], "工具范围", str(tmp_path)
    )
    api = await start_callback_api()
    try:
        for mode in ("read", "maintain"):
            token = tasks.open(session, mode)
            try:
                async with httpx.AsyncClient(
                    headers={
                        "Authorization": f"Bearer {mint(session.id, wiki_task_id=token)}"
                    }
                ) as transport:
                    async with streamable_http_client(
                        f"http://127.0.0.1:{api.port}/mcp/wiki/mcp",
                        http_client=transport,
                    ) as (read, write, _):
                        async with ClientSession(read, write) as peer:
                            await peer.initialize()
                            visible = {
                                tool.name for tool in (await peer.list_tools()).tools
                            }
                assert {"info", "search", "list_documents", "read", "graph"} <= visible
                assert "publish" not in visible
                assert "organize" not in visible
                assert ("maintain" in visible) == (mode == "maintain")
            finally:
                tasks.close(token)
    finally:
        await api.stop()


async def test_explicit_image_read_returns_original_bytes_and_rejects_other_assets(
    client, tmp_path
):
    import base64

    vault = tmp_path / "vault"
    vault.mkdir()
    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jL1EAAAAASUVORK5CYII="
    )
    (vault / "图.png").write_bytes(png)
    (vault / "附件.pdf").write_bytes(b"PDF is not an image to inspect")
    await configure(client, vault)
    response = await client.get("/api/wiki/image?path=图.png", headers=HEADERS)
    assert response.status_code == 200, response.text
    assert response.content == png
    assert response.headers["content-type"] == "image/png"
    excluded = await client.get("/api/wiki/image?path=附件.pdf", headers=HEADERS)
    assert excluded.status_code == 422
    escape = await client.get("/api/wiki/image?path=../secret.png", headers=HEADERS)
    assert escape.status_code == 404


async def test_graph_calculates_reverse_wikilinks_without_rewriting_existing_notes(
    client, tmp_path
):
    vault = tmp_path / "vault"
    (vault / "Library").mkdir(parents=True)
    a = "# 新笔记\n\n相关：[[旧笔记#条件|原判断]]。\n"
    b = "# 旧笔记\n\n## 条件\n原有观点，不补反链。\n"
    (vault / "Library/新笔记.md").write_text(a)
    (vault / "Library/旧笔记.md").write_text(b)
    await configure(client, vault)
    graph = await client.get("/api/wiki/graph?doc=Library/旧笔记.md", headers=HEADERS)
    assert graph.status_code == 200, graph.text
    assert {node["path"] for node in graph.json()["nodes"]} == {
        "Library/新笔记.md",
        "Library/旧笔记.md",
    }
    assert any(edge["relation"] == "wikilink" for edge in graph.json()["edges"])
    assert (vault / "Library/旧笔记.md").read_text() == b
    assert (vault / "Library/新笔记.md").read_text() == a


async def test_multiformat_reading_preserves_originals_and_records_quality(
    client, tmp_path
):
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "说明.txt").write_bytes("资料解释短词双链。".encode("gb18030"))
    (vault / "参考.html").write_text(
        "<h1>参考材料</h1><script>秘密假词</script><p>来源核验</p>"
    )
    # Even an invalid PDF stays an untouched asset; no parser should run.
    pdf_original = b"%PDF-asset deliberately not parsed"
    (vault / "扫描.pdf").write_bytes(pdf_original)
    await configure(client, vault)
    txt = await client.get(
        "/api/wiki/read", params={"doc": "说明.txt"}, headers=HEADERS
    )
    assert txt.status_code == 200, txt.text
    assert "双链" in txt.json()["content"]
    html = await client.get(
        "/api/wiki/read", params={"doc": "参考.html"}, headers=HEADERS
    )
    assert "秘密假词" not in html.json()["content"]
    pdf = await client.get(
        "/api/wiki/read", params={"doc": "扫描.pdf"}, headers=HEADERS
    )
    assert pdf.status_code == 200, pdf.text
    assert pdf.json()["pages"] == []
    assert pdf.json()["content"] == ""
    assert pdf.json()["quality"] == "excluded"
    assert (vault / "扫描.pdf").read_bytes() == pdf_original


async def test_conflicting_edits_are_retained_and_restore_creates_a_new_history_entry(
    client, tmp_path
):
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "原文.md").write_text("# 原文\n第一版\n")
    await configure(client, vault)
    original = (
        await client.get("/api/wiki/read", params={"doc": "原文.md"}, headers=HEADERS)
    ).json()
    changed = await client.put(
        "/api/wiki/document",
        headers=HEADERS,
        json={
            "path": "原文.md",
            "content": "# 原文\n人工第二版\n",
            "expected_revision": original["revision"],
            "reason": "人工编辑",
        },
    )
    assert changed.status_code == 200, changed.text
    conflict = await client.put(
        "/api/wiki/document",
        headers=HEADERS,
        json={
            "path": "原文.md",
            "content": "待处理草稿\n",
            "expected_revision": original["revision"],
            "reason": "并发编辑",
        },
    )
    assert conflict.status_code == 409
    history = (
        await client.get(
            "/api/wiki/history", params={"path": "原文.md"}, headers=HEADERS
        )
    ).json()["items"]
    assert history[0]["status"] == "conflict"
    assert history[0]["proposed_content"] == "待处理草稿\n"
    restored = await client.post(
        "/api/wiki/restore",
        headers=HEADERS,
        json={
            "change_id": history[1]["id"],
            "expected_revision": changed.json()["revision"],
            "side": "before",
        },
    )
    assert restored.status_code == 200, restored.text
    assert (vault / "原文.md").read_text() == original["content"]
    new_history = (
        await client.get(
            "/api/wiki/history", params={"path": "原文.md"}, headers=HEADERS
        )
    ).json()["items"]
    assert len(new_history) == 3
    assert new_history[0]["reason"].startswith("Restore")


async def test_curator_restructures_losslessly_and_publishes_only_current_sources(
    client, tmp_path
):
    vault = tmp_path / "vault"
    vault.mkdir()
    original = "---\naliases: [测试]\n---\n\n# 原文\n\n```python\nx = 1\n\nprint(x)\n```\n\n$$\na^2+b^2=c^2\n$$\n\n原始结论。\n"
    (vault / "原文.md").write_text(original)
    await configure(client, vault)
    headers = HEADERS
    response = await client.get(
        "/api/wiki/blocks", params={"doc": "原文.md"}, headers=headers
    )
    assert response.status_code == 200, response.text
    document = response.json()
    parts = document["blocks"]
    rewritten = await client.post(
        "/api/wiki/restructure",
        headers=headers,
        json={
            "path": "原文.md",
            "expected_revision": document["revision"],
            "order": [p["id"] for p in parts],
            "additions": [
                {
                    "before": len(parts),
                    "content": "## 阅读说明\n原始结论需要来源核验。",
                    "sources": [],
                }
            ],
            "reason": "添加阅读模板",
        },
    )
    assert rewritten.status_code == 200, rewritten.text
    revised = (vault / "原文.md").read_text()
    assert revised.startswith(original)
    assert "Weber 整理" in revised
    publish = {
        "path": "Wiki/sources/test.md",
        "content": "# 来源条目\n原文包含代码与公式。",
        "kind": "source",
        "expected_revision": None,
        "sources": [
            {
                "doc": "原文.md",
                "revision": rewritten.json()["revision"],
                "location": "全文",
            }
        ],
        "reason": "来源组织",
    }
    accepted = await client.post("/api/wiki/publish", headers=headers, json=publish)
    assert accepted.status_code == 200, accepted.text
    (vault / "原文.md").write_text(revised + "人工补充\n")
    publish["path"] = "Wiki/concepts/stale.md"
    rejected = await client.post("/api/wiki/publish", headers=headers, json=publish)
    assert rejected.status_code == 409
    found = (
        await client.get(
            "/api/wiki/search", params={"query": "来源条目"}, headers=HEADERS
        )
    ).json()
    assert found["items"][0]["stale"] is True


async def test_explicit_submissions_are_idempotent_and_jobs_can_be_cancelled_and_retried(
    client, tmp_path
):
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "资料.md").write_text("# 材料\n来源正文。")
    await configure(client, vault)
    submission = {
        "idempotency_key": "user-save-001",
        "material": "明确保存的整理需求",
        "scope": "来源与概念",
        "sources": [
            {
                "doc": "资料.md",
                "revision": (
                    await client.get(
                        "/api/wiki/read", params={"doc": "资料.md"}, headers=HEADERS
                    )
                ).json()["revision"],
                "location": "全文",
            }
        ],
    }
    first = await client.post("/api/wiki/submit", json=submission, headers=HEADERS)
    assert first.status_code == 200, first.text
    second = await client.post("/api/wiki/submit", json=submission, headers=HEADERS)
    assert second.json()["id"] == first.json()["id"]
    job_id = first.json()["id"]
    cancelled = await client.post(f"/api/wiki/jobs/{job_id}/cancel", headers=HEADERS)
    assert cancelled.json()["status"] == "cancelled"
    retried = await client.post(f"/api/wiki/jobs/{job_id}/retry", headers=HEADERS)
    assert retried.json()["status"] == "queued"


async def test_maintain_unifies_ingestion_and_records_only_its_new_target(
    client, tmp_path
):
    vault = tmp_path / "vault"
    vault.mkdir()
    old = "# 旧观点\n我的核心判断可能仅适用于这个条件。\n"
    (vault / "旧文.md").write_text(old)
    await configure(client, vault)
    request = {
        "idempotency_key": "new-note-1",
        "material": "# 新观点\n明确提交的原文，不应被改写。\n",
        "scope": "补 Key note、标签和关联",
        "target": "Library/Inbox/新观点.md",
    }
    first = await client.post("/api/wiki/maintain", json=request, headers=HEADERS)
    assert first.status_code == 200, first.text
    job = first.json()
    assert job["payload"]["new_only"] is True
    assert job["payload"]["target"] == "Library/Inbox/新观点.md"
    assert job["items"] == []
    again = await client.post("/api/wiki/maintain", json=request, headers=HEADERS)
    assert again.json()["id"] == job["id"]
    assert (vault / "旧文.md").read_text() == old
    empty = await client.post(
        "/api/wiki/maintain", json={"idempotency_key": "empty"}, headers=HEADERS
    )
    assert empty.status_code == 422


async def test_organize_preserves_native_metadata_and_only_refreshes_owned_additions(
    client, tmp_path
):
    vault = tmp_path / "vault"
    vault.mkdir()
    original = "---\ntags: [原有标签]\naliases: [旧名称]\n---\n\n# 原文\n\n我的判断可能只在条件 A 下成立。\n\n```py\nx = 1\n```\n"
    (vault / "笔记.md").write_text(original)
    await configure(client, vault)
    current = (await client.get("/api/wiki/read?doc=笔记.md", headers=HEADERS)).json()
    body = {
        "path": "笔记.md",
        "expected_revision": current["revision"],
        "key_note": "判断可能只在条件 A 下成立。",
        "tags": ["适用条件"],
        "reason": "首次整理",
    }
    added = await client.post("/api/wiki/organize", json=body, headers=HEADERS)
    assert added.status_code == 200, added.text
    first = (await client.get("/api/wiki/read?doc=笔记.md", headers=HEADERS)).json()
    assert set(first["metadata"]["tags"]) == {"原有标签", "适用条件"}
    assert first["stale"] is False
    rendered = (vault / "笔记.md").read_text()
    assert rendered.startswith(
        "---\ntags: [原有标签]\naliases: [旧名称]\n---\n\n# 原文\n"
    )
    assert "我的判断可能只在条件 A 下成立。\n\n```py\nx = 1\n```\n" in rendered
    invalid = await client.post(
        "/api/wiki/organize",
        headers=HEADERS,
        json={
            "path": "笔记.md",
            "expected_revision": first["revision"],
            "key_note": "参见 [[不存在的概念]]。",
            "reason": "broken new link",
        },
    )
    assert invalid.status_code == 422, invalid.text
    assert (vault / "笔记.md").read_text() == rendered
    body.update(
        expected_revision=first["revision"], key_note="条件 A 限定了这个可能的判断。"
    )
    refreshed = await client.post("/api/wiki/organize", json=body, headers=HEADERS)
    assert refreshed.status_code == 200, refreshed.text
    final = (vault / "笔记.md").read_text()
    assert final.count("## Key note") == 1
    assert "\n判断可能只在条件 A 下成立。" not in final
    assert "我的判断可能只在条件 A 下成立。" in final
    # Human edits to Weber's area become protected rather than silently replaced.
    final = final.replace("条件 A 限定了这个可能的判断。", "我自己校准后的要点。")
    saved = await client.put(
        "/api/wiki/document",
        headers=HEADERS,
        json={
            "path": "笔记.md",
            "content": final,
            "expected_revision": refreshed.json()["revision"],
            "reason": "人工调整",
        },
    )
    assert saved.status_code == 200
    read = (await client.get("/api/wiki/read?doc=笔记.md", headers=HEADERS)).json()
    assert read["metadata"]["needs_refresh"] is True
    body["expected_revision"] = read["revision"]
    rejected = await client.post("/api/wiki/organize", json=body, headers=HEADERS)
    assert rejected.status_code == 409
    assert (vault / "笔记.md").read_text() == final


@pytest.mark.parametrize("ingestion", [False, True])
async def test_worker_only_organizes_assigned_notes_and_rejects_writing_related_old_notes(
    client, tmp_path, monkeypatch, ingestion
):
    import asyncio

    from server.harness import HarnessEvent, get_harness
    from server.mcp_identity import mint
    from server.wiki.jobs import worker
    from tests.fake_run import FakeRunBase

    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "资料.md").write_text("# 原始资料\n双链应有可核验来源。")
    old_text = "# 旧观点\n用户自己的核心总结。\n"
    (vault / "旧文.md").write_text(old_text)
    config = await configure(client, vault)
    agent = await session_manager.db.get_agent(config["curator_agent_id"])
    backend = agent.get("backend") or "claude-code"

    attempts = []

    target = "Library/Inbox/新笔记.md" if ingestion else "资料.md"

    def create(run_config):
        headers = {
            "Authorization": f"Bearer {mint(run_config.session_id, wiki_task_id=run_config.wiki_task_id)}"
        }

        class Runtime(FakeRunBase):
            def set_idle_handler(self, handler):
                pass

            async def start(self, *args, **kwargs):
                pass

            async def stop(self):
                pass

            async def stream(self):
                found = next(
                    item
                    for item in (
                        await client.get(
                            "/api/wiki/search",
                            params={"query": "原始资料"},
                            headers=headers,
                        )
                    ).json()["items"]
                    if item["path"] == target
                )
                excerpt = await client.get(
                    "/api/wiki/read",
                    params={"doc": found["id"], "length": 1},
                    headers=headers,
                )
                premature = await client.post(
                    "/api/wiki/organize",
                    headers=headers,
                    json={
                        "path": target,
                        "expected_revision": excerpt.json()["revision"],
                        "key_note": "尚未读完就总结",
                        "reason": "coverage",
                    },
                )
                attempts.append((premature.status_code, premature.text))
                assert premature.status_code == 422, premature.text
                assert "full current note" in premature.text, premature.text
                read = await client.get(
                    "/api/wiki/read", params={"doc": found["id"]}, headers=headers
                )
                assert read.status_code == 200
                old_read = await client.get(
                    "/api/wiki/read?doc=旧文.md", headers=headers
                )
                assert old_read.status_code == 200
                body = {
                    "path": "旧文.md",
                    "expected_revision": old_read.json()["revision"],
                    "key_note": "不应写入旧文",
                    "reason": "新增材料的关联",
                }
                rejected = await client.post(
                    "/api/wiki/organize", headers=headers, json=body
                )
                assert rejected.status_code == 403, rejected.text
                body.update(
                    path=target,
                    expected_revision=read.json()["revision"],
                    key_note="双链应有可核验来源。",
                )
                result = await client.post(
                    "/api/wiki/organize", headers=headers, json=body
                )
                assert result.status_code == 200, result.text
                yield HarnessEvent(type="result", content="已完成来源发布")

        return Runtime()

    monkeypatch.setattr(get_harness(backend), "create_run", create)
    await worker.start(session_manager)
    try:
        response = await client.post(
            "/api/wiki/maintain",
            json={
                "idempotency_key": "one-note",
                **(
                    {"material": "# 原始资料\n双链应有可核验来源。\n这是本次不同的新材料。", "target": target}
                    if ingestion
                    else {"paths": [target]}
                ),
            },
            headers=HEADERS,
        )
        assert response.status_code == 200, response.text
        job_id = response.json()["id"]
        async with asyncio.timeout(5):
            while True:
                job = (
                    await client.get(f"/api/wiki/jobs/{job_id}", headers=HEADERS)
                ).json()
                if job["status"] not in {"queued", "running"}:
                    break
                await asyncio.sleep(0.01)
        assert attempts and attempts[0][0] == 422, attempts
        assert job["status"] == "completed", job
        assert job["progress"]["completed"] == 1
        assert "## Key note" in (vault / target).read_text()
        assert "双链应有可核验来源。" in (vault / target).read_text()
        assert (vault / "旧文.md").read_text() == old_text
        assert not (vault / "Wiki").exists()
        if ingestion:
            assert (vault / "资料.md").read_text() == "# 原始资料\n双链应有可核验来源。"
    finally:
        await worker.stop()


async def test_bound_app_token_can_edit_only_its_registered_wiki(
    client, tmp_path, monkeypatch
):
    from datetime import UTC, datetime

    from server.applications import ApplicationManager, app_scope_token
    from server.routers import applications as app_routes

    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "note.md").write_text("# 主库\n正文")
    config = await configure(client, vault)
    manager = ApplicationManager()
    manager.bind(session_mgr=session_manager, db=session_manager.db)
    monkeypatch.setattr(app_routes, "_manager", manager)
    try:
        now = datetime.now(UTC).isoformat()
        for app_id in ("sapphire", "other"):
            await session_manager.db.save_application(
                app_id=app_id,
                name=app_id,
                app_dir=str(tmp_path / app_id),
                created_at=now,
                updated_at=now,
                agent_id=config["curator_agent_id"],
            )
        response = await client.put(
            "/api/wiki/config",
            headers=HEADERS,
            json={
                "root": str(vault),
                "curator_agent_id": config["curator_agent_id"],
                "app_id": "sapphire",
            },
        )
        assert response.status_code == 200, response.text
        app_headers = {"Authorization": f"Bearer {app_scope_token('sapphire')}"}
        original = await client.get(
            "/apps/sapphire/wiki/read?doc=note.md", headers=app_headers
        )
        assert original.status_code == 200, original.text
        denied = await client.get(
            "/apps/other/wiki/read?doc=note.md",
            headers={"Authorization": f"Bearer {app_scope_token('other')}"},
        )
        assert denied.status_code == 403
        global_denied = await client.get(
            "/api/wiki/read?doc=note.md", headers=app_headers
        )
        assert global_denied.status_code == 401
        edited = await client.put(
            "/apps/sapphire/wiki/document",
            headers=app_headers,
            json={
                "path": "note.md",
                "content": "# 主库\n人工更新",
                "expected_revision": original.json()["revision"],
                "reason": "app 编辑",
            },
        )
        assert edited.status_code == 200, edited.text
        oversized = await client.get(
            "/apps/sapphire/wiki/read?doc=note.md&length=1000000", headers=app_headers
        )
        assert oversized.status_code == 422
    finally:
        manager.shutdown()


async def test_legacy_binary_snapshot_restore_and_new_snapshot_creation_retired(
    client, tmp_path
):
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "note.md").write_bytes(b"original\r\n")
    (vault / "image.png").write_bytes(b"\x89PNG\r\n\x1a\noriginal-image")
    await configure(client, vault)
    import json
    import uuid
    from datetime import UTC, datetime

    from server.wiki.history import WikiHistory, atomic
    from server.wiki.registry import registry

    history_store = WikiHistory(registry.store(None))
    snapshot = {"id": uuid.uuid4().hex, "created_at": datetime.now(UTC).isoformat(), "reason": "Legacy snapshot fixture", "files": {"note.md": history_store.blob(b"original\r\n"), "image.png": history_store.blob(b"\x89PNG\r\n\x1a\noriginal-image")}}
    atomic(history_store.state / "snapshots" / f"{snapshot['id']}.json", json.dumps(snapshot).encode())
    retired = await client.post("/api/wiki/snapshot", headers=HEADERS)
    assert retired.status_code == 410
    (vault / "image.png").write_bytes(b"changed-image")
    import hashlib

    expected = {"image.png": hashlib.sha256(b"changed-image").hexdigest()}
    response = await client.post(
        "/api/wiki/snapshot/restore",
        headers=HEADERS,
        json={
            "snapshot_id": snapshot["id"],
            "paths": ["image.png"],
            "expected_revisions": expected,
        },
    )
    assert response.status_code == 200, response.text
    assert (vault / "image.png").read_bytes() == b"\x89PNG\r\n\x1a\noriginal-image"
    assert (vault / "note.md").read_bytes() == b"original\r\n"
    history = (
        await client.get("/api/wiki/history?path=image.png", headers=HEADERS)
    ).json()
    assert history["items"][0]["reason"].startswith("Restore snapshot")


async def test_curator_role_cannot_be_forged_and_lowercase_bearer_is_not_human(
    client, tmp_path
):
    from server.agent_manager import AgentManager
    from server.mcp_identity import mint
    from server.wiki.tasks import tasks

    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "原文.md").write_text("# 原文\n原始知识。")
    config = await configure(client, vault)
    agent = await AgentManager(session_manager.db).create_agent(name="Weber clone")
    session = await session_manager.create_session(
        agent["id"], "普通 agent", str(tmp_path)
    )
    token = tasks.open(session, "maintain")
    headers = {"Authorization": f"bearer {mint(session.id, wiki_task_id=token)}"}
    try:
        original = await client.get("/api/wiki/read?doc=原文.md", headers=headers)
        assert original.status_code == 200, original.text
        published = await client.post(
            "/api/wiki/publish",
            headers=headers,
            json={
                "path": "Wiki/sources/forged.md",
                "content": "伪造职责",
                "kind": "source",
                "expected_revision": None,
                "sources": [
                    {
                        "doc": "原文.md",
                        "revision": original.json()["revision"],
                        "location": "全文",
                    }
                ],
                "reason": "伪造",
                "writer": config["curator_agent_id"],
            },
        )
        assert published.status_code == 403
        overwrite = await client.put(
            "/api/wiki/document",
            headers=headers,
            json={
                "path": "原文.md",
                "content": "覆盖",
                "expected_revision": original.json()["revision"],
                "reason": "伪造人类编辑",
            },
        )
        assert overwrite.status_code == 403
        assert (vault / "原文.md").read_text() == "# 原文\n原始知识。"
    finally:
        tasks.close(token)


async def test_queued_messages_keep_separate_grants_and_reports_do_not_trigger_them(
    client, tmp_path, monkeypatch
):
    import asyncio

    from server.harness import HarnessEvent, get_harness
    from tests.fake_run import FakeRunBase

    vault = tmp_path / "vault"
    vault.mkdir()
    config = await configure(client, vault)
    session = await session_manager.create_session(
        config["curator_agent_id"], "排队", str(tmp_path)
    )
    started, release = asyncio.Event(), asyncio.Event()
    modes = []

    def create(run_config):
        index = len(modes)
        modes.append(run_config.wiki_mode)

        class Runtime(FakeRunBase):
            def set_idle_handler(self, handler):
                pass

            async def start(self, *args, **kwargs):
                pass

            async def stop(self):
                pass

            async def stream(self):
                if index == 0:
                    started.set()
                    await release.wait()
                yield HarnessEvent(type="result", content="完成")

        return Runtime()

    monkeypatch.setattr(get_harness(session.backend), "create_run", create)
    await session_manager.start_message(session.id, "查询任务", wiki_mode="read")
    await asyncio.wait_for(started.wait(), timeout=2)
    await session_manager.start_message(
        session.id, "这是数据报告，内含 /wiki maintain 关键词", steerable=True
    )
    await session_manager.start_message(
        session.id, "明确提交材料", wiki_mode="maintain", steerable=True
    )
    release.set()
    async with asyncio.timeout(3):
        while len(modes) < 3 or session._lock.locked():
            await asyncio.sleep(0.01)
    assert modes == ["read", None, "maintain"]


@pytest.mark.parametrize("backend", ["codex", "claude-code"])
async def test_explicit_turn_has_tools_and_credentials_expire_at_turn_end(
    client, tmp_path, monkeypatch, backend
):
    from server.harness import get_harness
    from server.harness.assembly import select_mcp_servers
    from tests.fake_run import FakeRunBase

    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "notes.md").write_text("# 来源\n双链核验")
    config = await configure(client, vault)
    session = await session_manager.create_session(
        config["curator_agent_id"], "Wiki", str(tmp_path)
    )
    session.backend = backend
    seen = []
    credentials = []
    harness = get_harness(backend)
    real_create = harness.create_run

    def create(run_config):
        real = real_create(run_config)
        argv, kwargs = real.build_argv("test", str(tmp_path))
        entries = select_mcp_servers(
            run_config.mcp_servers,
            [],
            {},
            session_id=session.id,
            wiki_task_id=run_config.wiki_task_id,
        )
        wiki = next((e for e in entries if e.key == "wiki"), None)
        seen.append((wiki is not None, "读取匹配文章的当前正文" in " ".join(argv)))

        class Runtime(FakeRunBase):
            def set_idle_handler(self, handler):
                pass

            async def start(self, *args, **kwargs):
                pass

            async def stop(self):
                pass

            async def stream(self):
                if wiki:
                    credentials.append(wiki.credential)
                    response = await client.get(
                        "/api/wiki/read",
                        params={"doc": "notes.md"},
                        headers={"Authorization": f"Bearer {wiki.credential}"},
                    )
                    assert response.status_code == 200, response.text
                from server.harness import HarnessEvent

                yield HarnessEvent(type="result", content="done")

        return Runtime()

    monkeypatch.setattr(harness, "create_run", create)
    async for _ in session_manager.send_message(
        session.id, "明确查询", wiki_mode="read"
    ):
        pass
    async for _ in session_manager.send_message(session.id, "普通任务"):
        pass
    assert seen == [(True, True), (False, False)]
    response = await client.get(
        "/api/wiki/read",
        params={"doc": "notes.md"},
        headers={"Authorization": f"Bearer {credentials[0]}"},
    )
    assert response.status_code == 403


async def test_sapphire_existing_writer_and_async_git_share_framework_history(
    client, tmp_path, monkeypatch
):
    import asyncio
    import json
    from datetime import UTC, datetime

    from server.app_backends import backend_supervisor
    from server.applications import ApplicationManager
    from server.routers import applications as app_routes
    from server.wiki.app_bridge import shutdown

    app_dir = tmp_path / "sapphire"
    app_dir.mkdir()
    vault = tmp_path / "sapphire.data/vaults/obv/repo"
    vault.mkdir(parents=True)
    (vault / "note.md").write_text("# 原文\n保留")
    (tmp_path / "sapphire.data/registry.json").write_text(
        json.dumps({"vaults": [{"id": "obv"}]})
    )
    config = await configure(client, vault)
    manager = ApplicationManager()
    manager.bind(session_mgr=session_manager, db=session_manager.db)
    monkeypatch.setattr(app_routes, "_manager", manager)
    now = datetime.now(UTC).isoformat()
    await session_manager.db.save_application(
        app_id="sapphire",
        name="sapphire",
        app_dir=str(app_dir),
        created_at=now,
        updated_at=now,
        agent_id=config["curator_agent_id"],
    )
    bound = await client.put(
        "/api/wiki/config",
        headers=HEADERS,
        json={
            "root": str(vault),
            "curator_agent_id": config["curator_agent_id"],
            "app_id": "sapphire",
        },
    )
    assert bound.status_code == 200, bound.text
    (app_dir / "serve.py").write_text("""
import hashlib, json, os, threading, time
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
root = Path(os.environ['APP_DATA_DIR']) / 'vaults/obv/repo'
job = {'status': 'running'}
polls = 0
class H(BaseHTTPRequestHandler):
    def respond(self, status, payload):
        body = json.dumps(payload).encode()
        self.send_response(status); self.send_header('content-type','application/json'); self.end_headers(); self.wfile.write(body)
    def do_GET(self):
        global polls
        polls += 1
        self.respond(503 if polls == 1 else 200, job)
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get('content-length', 0))))
        if self.path.startswith('/api/git'):
            def mutate():
                time.sleep(.2)
                (root / 'note.md').write_text('# Git result')
                job['status'] = 'done'
            threading.Thread(target=mutate).start()
            self.respond(202, {'id': 'git-job'})
            return
        path = root / body['path']
        current = hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None
        if current != body.get('revision'):
            self.respond(409, {'error': 'conflict'}); return
        path.write_text(body['content'])
        self.respond(200, {'revision': hashlib.sha256(path.read_bytes()).hexdigest()})
    def log_message(self, *args): pass
ThreadingHTTPServer(('127.0.0.1', int(os.environ['PORT'])), H).serve_forever()
""")
    start = app_dir / "start.sh"
    start.write_text("#!/bin/sh\nexec python3 serve.py\n")
    start.chmod(0o755)
    try:
        original = (
            await client.get(
                "/api/wiki/read", params={"doc": "note.md"}, headers=HEADERS
            )
        ).json()
        saved = await client.post(
            "/apps/sapphire/api/save",
            headers=HEADERS,
            json={
                "path": "note.md",
                "content": "# 人工保存",
                "revision": original["revision"],
            },
        )
        assert saved.status_code == 200, saved.text
        conflict = await client.post(
            "/apps/sapphire/api/save",
            headers=HEADERS,
            json={
                "path": "note.md",
                "content": "# 冲突成果",
                "revision": original["revision"],
            },
        )
        assert conflict.status_code == 409
        git = await client.post(
            "/apps/sapphire/api/git", headers=HEADERS, json={"action": "pull"}
        )
        assert git.status_code == 202
        # More contenders than AnyIO's usual worker pool: lock waiting must
        # leave enough execution capacity for the owning writer to finish.
        async with asyncio.timeout(20):
            receipts = await asyncio.gather(
                *[
                    client.post(
                        "/apps/sapphire/api/save",
                        headers=HEADERS,
                        json={
                            "path": f"new-{i}.md",
                            "content": "# 新内容",
                            "revision": None,
                        },
                    )
                    for i in range(43)
                ]
            )
        assert all(r.status_code == 200 for r in receipts)
        latest = (
            await client.get(
                "/api/wiki/read", params={"doc": "note.md"}, headers=HEADERS
            )
        ).json()
        assert latest["content"] == "# Git result"
        history = (
            await client.get(
                "/api/wiki/history", params={"path": "note.md"}, headers=HEADERS
            )
        ).json()["items"]
        assert {h["reason"] for h in history} >= {
            "save",
            "git",
            "Sapphire save revision conflict",
        }
        assert any(h.get("proposed_content") == "# 冲突成果" for h in history)

        # Cancellation must join the journal worker before admitting a writer.
        import threading

        from server.wiki import app_bridge

        entered, release = threading.Event(), threading.Event()
        real_atomic = app_bridge.atomic
        paused = False

        def pause_journal(path, raw):
            nonlocal paused
            if path.name == "external-change.json" and not paused:
                paused = True
                entered.set()
                if not release.wait(5):
                    raise RuntimeError("Cancellation test journal was not released")
            real_atomic(path, raw)

        monkeypatch.setattr(app_bridge, "atomic", pause_journal)
        cancelled = asyncio.create_task(
            client.post(
                "/apps/sapphire/api/save",
                headers=HEADERS,
                json={"path": "cancelled.md", "content": "cancelled", "revision": None},
            )
        )
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            cancelled.cancel()
            next_writer = asyncio.create_task(
                client.post(
                    "/apps/sapphire/api/save",
                    headers=HEADERS,
                    json={
                        "path": "next.md",
                        "content": "next writer",
                        "revision": None,
                    },
                )
            )
            await asyncio.sleep(0.1)
            assert not next_writer.done(), (
                "Cancelled preparation released the lock before its worker joined"
            )
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await cancelled
        next_receipt = await asyncio.wait_for(next_writer, 5)
        assert next_receipt.status_code == 200, next_receipt.text
        assert not (vault / "cancelled.md").exists()
        assert (vault / "next.md").read_text() == "next writer"
        from server.wiki import registry

        assert not (registry.state(None) / "external-change.json").exists()
    finally:
        await backend_supervisor.stop_all()
        await shutdown()
        manager.shutdown()


async def test_schedule_wiki_grant_requires_verified_intent_at_creation(
    client, tmp_path, monkeypatch
):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from server.mcp_identity import mint
    from server.routers import schedules
    from server.wiki.tasks import tasks

    vault = tmp_path / "vault"
    vault.mkdir()
    await configure(client, vault)
    agent = await session_manager.db.get_system_agent()
    session = await session_manager.create_session(agent["id"])
    monkeypatch.setattr(schedules, "_db", session_manager.db)
    monkeypatch.setattr(
        schedules,
        "_runner",
        SimpleNamespace(add=AsyncMock(), next_run_at=lambda _: None),
    )
    plain = {"Authorization": f"Bearer {mint(session.id)}"}
    endpoint = f"/api/sessions/{session.id}/schedules"
    rejected = await client.post(
        endpoint,
        headers=plain,
        json={"prompt": "/wiki maintain 整理", "interval_seconds": 3600},
    )
    assert rejected.status_code == 403, rejected.text
    ordinary = await client.post(
        endpoint, headers=plain, json={"prompt": "一般检查", "interval_seconds": 3600}
    )
    assert ordinary.status_code == 201, ordinary.text
    assert ordinary.json()["wiki_mode"] is None
    human = await client.post(
        endpoint,
        headers=HEADERS,
        json={
            "prompt": "整理指定来源",
            "wiki_mode": "maintain",
            "interval_seconds": 3600,
        },
    )
    assert human.status_code == 201, human.text
    assert human.json()["wiki_mode"] == "maintain"
    token = tasks.open(session, "read")
    try:
        scoped = {"Authorization": f"Bearer {mint(session.id, None, None, token)}"}
        escalated = await client.post(
            endpoint,
            headers=scoped,
            json={"prompt": "整理", "wiki_mode": "maintain", "interval_seconds": 3600},
        )
        assert escalated.status_code == 403
        read = await client.post(
            endpoint,
            headers=scoped,
            json={"prompt": "查证", "wiki_mode": "read", "interval_seconds": 3600},
        )
        assert read.status_code == 201, read.text
        rows = await session_manager.db.load_schedules()
        assert (
            next(r for r in rows if r["id"] == read.json()["id"])["wiki_mode"] == "read"
        )
    finally:
        tasks.close(token)


async def test_bom_frontmatter_is_one_protected_block_even_with_blank_lines(
    client, tmp_path
):
    vault = tmp_path / "vault"
    vault.mkdir()
    frontmatter = "\ufeff---\r\naliases: [双链]\r\n\r\nowner: unknown\r\n---\r\n\r\n"
    original = (frontmatter + "# 原文\r\n\r\n正文保留。\r\n").encode()
    (vault / "bom.md").write_bytes(original)
    await configure(client, vault)
    block = (
        await client.get("/api/wiki/blocks", params={"doc": "bom.md"}, headers=HEADERS)
    ).json()
    assert block["blocks"][0]["content"] == frontmatter
    assert "".join(b["content"] for b in block["blocks"]).encode() == original


async def test_interrupted_organization_recovers_owned_sections_and_restore_state(
    client, tmp_path, monkeypatch
):
    from server.wiki.history import WikiHistory

    vault = tmp_path / "vault"
    vault.mkdir()
    original = "# 保全\n原文必须完整保留。\n"
    (vault / "笔记.md").write_text(original)
    await configure(client, vault)
    read = (await client.get("/api/wiki/read?doc=笔记.md", headers=HEADERS)).json()
    real_status = WikiHistory.status

    def interrupt_after_write(self, change_id, value):
        if value == "applied":
            raise RuntimeError("simulated interruption after file write")
        return real_status(self, change_id, value)

    monkeypatch.setattr(WikiHistory, "status", interrupt_after_write)
    with pytest.raises(RuntimeError, match="simulated interruption"):
        await client.post(
            "/api/wiki/organize",
            headers=HEADERS,
            json={
                "path": "笔记.md",
                "expected_revision": read["revision"],
                "key_note": "原文需要完整保全。",
                "reason": "crash recovery",
            },
        )
    monkeypatch.setattr(WikiHistory, "status", real_status)
    recovered = (await client.get("/api/wiki/read?doc=笔记.md", headers=HEADERS)).json()
    assert recovered["metadata"]["author"] == "Weber 整理"
    assert recovered["stale"] is False
    history = (
        await client.get("/api/wiki/history?path=笔记.md", headers=HEADERS)
    ).json()["items"]
    assert history[0]["status"] == "applied"
    restored = await client.post(
        "/api/wiki/restore",
        headers=HEADERS,
        json={
            "change_id": history[0]["id"],
            "expected_revision": recovered["revision"],
            "side": "before",
        },
    )
    assert restored.status_code == 200, restored.text
    assert (vault / "笔记.md").read_text() == original
    refreshed = await client.post(
        "/api/wiki/organize",
        headers=HEADERS,
        json={
            "path": "笔记.md",
            "expected_revision": restored.json()["revision"],
            "key_note": "再次整理仍要保全原文。",
            "reason": "after restore",
        },
    )
    assert refreshed.status_code == 200, refreshed.text


async def test_new_file_manifest_must_reference_actual_creation_changes(
    client, tmp_path
):
    vault = tmp_path / "vault"
    (vault / "Library").mkdir(parents=True)
    (vault / "Library/旧文.md").write_text("原有判断")
    await configure(client, vault)
    created = await client.put(
        "/api/wiki/document",
        headers=HEADERS,
        json={
            "path": "Library/新增.md",
            "content": "# 新材料\n须保全原文。",
            "expected_revision": None,
            "reason": "new note",
        },
    )
    assert created.status_code == 200, created.text
    change_id = created.json()["change_id"]
    submitted = await client.post(
        "/api/wiki/maintain",
        headers=HEADERS,
        json={
            "idempotency_key": "new-manifest",
            "paths": ["Library/新增.md"],
            "created_changes": {"Library/新增.md": change_id},
        },
    )
    assert submitted.status_code == 200, submitted.text
    assert submitted.json()["payload"]["new_only"] is True
    forged = await client.post(
        "/api/wiki/maintain",
        headers=HEADERS,
        json={
            "idempotency_key": "wrong-manifest",
            "paths": ["Library/旧文.md"],
            "created_changes": {"Library/旧文.md": change_id},
        },
    )
    assert forged.status_code == 403, forged.text
    assert (vault / "Library/旧文.md").read_text() == "原有判断"


async def test_layout_conflict_is_a_structured_proposal_not_restorable_text(
    client, tmp_path
):
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "note.md").write_text("# Note\n\nOriginal wording.\n")
    await configure(client, vault)
    old = (
        await client.get("/api/wiki/blocks", params={"doc": "note.md"}, headers=HEADERS)
    ).json()
    current = "# Note\n\nOriginal wording.\n\nHuman addition.\n"
    (vault / "note.md").write_text(current)
    response = await client.post(
        "/api/wiki/restructure",
        headers=HEADERS,
        json={
            "path": "note.md",
            "expected_revision": old["revision"],
            "order": [part["id"] for part in old["blocks"]],
            "additions": [
                {
                    "before": len(old["blocks"]),
                    "content": "Reading explanation.",
                    "sources": [],
                }
            ],
            "reason": "Stale layout",
        },
    )
    assert response.status_code == 409, response.text
    entry = (
        await client.get(
            "/api/wiki/history", params={"path": "note.md"}, headers=HEADERS
        )
    ).json()["items"][0]
    assert entry["proposed_operation"]["order"] == [
        part["id"] for part in old["blocks"]
    ]
    assert "proposed_content" not in entry
    read = (
        await client.get("/api/wiki/read", params={"doc": "note.md"}, headers=HEADERS)
    ).json()
    restored = await client.post(
        "/api/wiki/restore",
        headers=HEADERS,
        json={
            "change_id": entry["id"],
            "expected_revision": read["revision"],
            "side": "after",
        },
    )
    assert restored.status_code == 409, restored.text
    assert (vault / "note.md").read_text() == current


@pytest.mark.parametrize("operation", ["relation", "layout"])
async def test_all_new_supplement_links_are_checked_but_old_broken_links_remain(
    client, tmp_path, operation
):
    vault = tmp_path / "vault"
    vault.mkdir()
    original = "# Note\n\nKeep [old](old-missing.md) verbatim.\n"
    (vault / "note.md").write_text(original)
    (vault / "source.md").write_text("# Source\n\nRelated idea.\n")
    await configure(client, vault)
    document = (
        await client.get("/api/wiki/blocks", params={"doc": "note.md"}, headers=HEADERS)
    ).json()
    source = (
        await client.get("/api/wiki/read", params={"doc": "source.md"}, headers=HEADERS)
    ).json()
    if operation == "relation":
        endpoint = "/api/wiki/organize"
        body = {
            "path": "note.md",
            "expected_revision": document["revision"],
            "key_note": "Keep the old wording.",
            "relations": [
                {
                    "doc": source["id"],
                    "revision": source["revision"],
                    "location": "L1-L3",
                    "explanation": "See [new](new-missing.md).",
                }
            ],
            "reason": "Check relation",
        }
    else:
        endpoint = "/api/wiki/restructure"
        body = {
            "path": "note.md",
            "expected_revision": document["revision"],
            "order": [part["id"] for part in document["blocks"]],
            "additions": [
                {
                    "before": len(document["blocks"]),
                    "content": "See [[new-missing]].",
                    "sources": [],
                }
            ],
            "reason": "Check layout",
        }
    rejected = await client.post(endpoint, headers=HEADERS, json=body)
    assert rejected.status_code == 422, rejected.text
    assert (vault / "note.md").read_text() == original


@pytest.mark.parametrize("move", ["rename", "external-recovery"])
async def test_note_move_preserves_identity_ownership_and_source_relations(
    client, tmp_path, move
):
    import json

    from server.wiki.history import WikiHistory, atomic
    from server.wiki.registry import registry

    vault = tmp_path / "vault"
    (vault / "Library").mkdir(parents=True)
    original = "# Original\n\nKeep the judgement and all original words.\n"
    (vault / "Library/a.md").write_text(original)
    await configure(client, vault)
    first = (
        await client.get(
            "/api/wiki/read", params={"doc": "Library/a.md"}, headers=HEADERS
        )
    ).json()
    result = await client.post(
        "/api/wiki/organize",
        headers=HEADERS,
        json={
            "path": "Library/a.md",
            "expected_revision": first["revision"],
            "key_note": "Preserve original wording.",
            "reason": "Organize before move",
        },
    )
    assert result.status_code == 200, result.text
    organized = (
        await client.get("/api/wiki/read", params={"doc": first["id"]}, headers=HEADERS)
    ).json()
    published = await client.post(
        "/api/wiki/publish",
        headers=HEADERS,
        json={
            "path": "Library/topic.md",
            "kind": "topic",
            "content": "# Topic\n\nA derived observation.",
            "expected_revision": None,
            "sources": [
                {
                    "doc": first["id"],
                    "revision": organized["revision"],
                    "location": "Full text",
                }
            ],
            "reason": "Track stable source",
        },
    )
    assert published.status_code == 200, published.text
    store = registry.store(None)
    history = WikiHistory(store)
    if move == "rename":
        (vault / "Library/a.md").rename(vault / "Library/b.md")
    else:
        before = history.inventory()
        atomic(
            store.state / "external-change.json",
            json.dumps(
                {"before": before, "actor": "app:sapphire", "reason": "rename"}
            ).encode(),
        )
        (vault / "Library/b.md").write_bytes((vault / "Library/a.md").read_bytes())
        (vault / "Library/a.md").unlink()
        with history.locked():
            pass
    moved = (
        await client.get("/api/wiki/read", params={"doc": first["id"]}, headers=HEADERS)
    ).json()
    assert moved["path"] == "Library/b.md", moved
    assert moved["id"] == first["id"]
    assert moved["metadata"]["author"] == "Weber 整理"
    assert not moved["stale"]
    source = (
        await client.get(
            "/api/wiki/read", params={"doc": "Library/topic.md"}, headers=HEADERS
        )
    ).json()
    assert not source["stale"]
    assert source["metadata"]["sources"][0]["path"] == "Library/b.md"
    second = await client.post(
        "/api/wiki/organize",
        headers=HEADERS,
        json={
            "path": "Library/b.md",
            "expected_revision": moved["revision"],
            "key_note": "Preserve original wording with a fresh entry.",
            "reason": "Refresh after move",
        },
    )
    assert second.status_code == 200, second.text
    text = (vault / "Library/b.md").read_text()
    assert text.count("## Key note") == 1
    from server.wiki.sections import original_text

    with store.connect() as db:
        owned = db.execute(
            "SELECT regions FROM managed_notes WHERE path=?", ("Library/b.md",)
        ).fetchone()
    assert original_text(text, json.loads(owned[0])) == original
    with store.connect() as db:
        db.execute("DELETE FROM documents")
    rebuilt = (
        await client.get(
            "/api/wiki/read", params={"doc": "Library/b.md"}, headers=HEADERS
        )
    ).json()
    assert rebuilt["id"] == first["id"]


@pytest.mark.parametrize("cancel", [False, True])
async def test_ingestion_waiting_for_app_lock_keeps_loop_live_and_honors_cancel(
    client, tmp_path, cancel
):
    import asyncio
    import threading
    import time
    from types import SimpleNamespace

    from server.wiki.history import WikiHistory
    from server.wiki.jobs import WikiJobs, WikiWorker
    from server.wiki.registry import registry

    vault = tmp_path / "vault"
    vault.mkdir()
    await configure(client, vault)
    jobs = WikiJobs(None)
    job = jobs.maintain(
        "app-lock-race",
        "# Material\nKeep this original.\n",
        "organize",
        [],
        "Library/new.md",
        [],
        "human",
    )
    jobs.update(job["id"], status="running")
    acquired, release = threading.Event(), threading.Event()
    history = WikiHistory(registry.store(None))

    def app_writer():
        with history.locked():
            acquired.set()
            # Bounded watchdog makes a regression fail rather than deadlock pytest.
            release.wait(timeout=2)

    writer = asyncio.create_task(asyncio.to_thread(app_writer))
    assert await asyncio.to_thread(acquired.wait, 1)

    async def stop_after_ingest(*args, **kwargs):
        raise RuntimeError("stop after ingest")

    worker = WikiWorker()
    worker.manager = SimpleNamespace(
        db=session_manager.db, create_session=stop_after_ingest
    )

    async def app_finishes():
        await asyncio.sleep(0.05)
        if cancel:
            await jobs.cancel(job["id"], None)
        release.set()

    timer = asyncio.create_task(app_finishes())
    started = time.monotonic()
    try:
        if cancel:
            await worker.process(jobs, job["id"])
        else:
            with pytest.raises(RuntimeError, match="stop after ingest"):
                await worker.process(jobs, job["id"])
    finally:
        release.set()
        await timer
        await writer
    assert time.monotonic() - started < 1, (
        "worker blocked the loop needed to release the app lock"
    )
    if cancel:
        assert jobs.get(job["id"])["status"] == "cancelled"
        assert not (vault / "Library/new.md").exists()
    else:
        assert (
            vault / "Library/new.md"
        ).read_text() == "# Material\nKeep this original.\n"


@pytest.mark.parametrize("outcome", ["completed", "failed"])
async def test_cancelled_job_is_not_overwritten_by_racing_worker_terminal_state(
    client, tmp_path, monkeypatch, outcome
):
    import asyncio
    import threading

    from server.wiki.jobs import WikiJobs, WikiWorker

    vault = tmp_path / "vault"
    vault.mkdir()
    await configure(client, vault)
    jobs = WikiJobs(None)
    job = jobs.enqueue(
        "terminal-race", {"kind": "curation", "scope": "empty selection"}, []
    )
    loop = asyncio.get_running_loop()
    entered, release = asyncio.Event(), threading.Event()
    original = WikiJobs.update

    def pause_terminal(self, job_id, **fields):
        if fields.get("status") == outcome:
            loop.call_soon_threadsafe(entered.set)
            assert release.wait(2)
        return original(self, job_id, **fields)

    monkeypatch.setattr(WikiJobs, "update", pause_terminal)
    worker = WikiWorker()
    worker.manager = session_manager
    if outcome == "failed":

        async def fail(*args):
            raise RuntimeError("controlled worker failure")

        monkeypatch.setattr(worker, "process", fail)
    running = asyncio.create_task(worker.run(None))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        cancelled = await jobs.cancel(job["id"], None)
        assert cancelled["status"] == "cancelled"
    finally:
        release.set()
        await running
    assert jobs.get(job["id"])["status"] == "cancelled"


async def test_deleted_moved_source_is_not_replaced_by_same_text_at_its_old_path(
    client, tmp_path
):
    vault = tmp_path / "vault"
    (vault / "Library").mkdir(parents=True)
    raw = "# Source\n\nEvidence that belongs to this document.\n"
    (vault / "Library/a.md").write_text(raw)
    await configure(client, vault)
    original = (
        await client.get(
            "/api/wiki/read", params={"doc": "Library/a.md"}, headers=HEADERS
        )
    ).json()
    response = await client.post(
        "/api/wiki/publish",
        headers=HEADERS,
        json={
            "path": "Library/topic.md",
            "content": "# Topic\n\nA source-specific inference.",
            "kind": "topic",
            "expected_revision": None,
            "sources": [
                {
                    "doc": original["id"],
                    "revision": original["revision"],
                    "location": "L1-L3",
                }
            ],
            "reason": "Keep source identity",
        },
    )
    assert response.status_code == 200, response.text
    (vault / "Library/a.md").rename(vault / "Library/b.md")
    moved = (
        await client.get(
            "/api/wiki/read", params={"doc": original["id"]}, headers=HEADERS
        )
    ).json()
    assert moved["path"] == "Library/b.md"
    (vault / "Library/b.md").unlink()
    (vault / "Library/a.md").write_text(raw)
    replacement = (
        await client.get(
            "/api/wiki/read", params={"doc": "Library/a.md"}, headers=HEADERS
        )
    ).json()
    assert replacement["id"] != original["id"]
    refreshed = await client.get(
        "/api/wiki/search", params={"query": "Source"}, headers=HEADERS
    )
    assert refreshed.status_code == 200
    topic = (
        await client.get(
            "/api/wiki/read", params={"doc": "Library/topic.md"}, headers=HEADERS
        )
    ).json()
    assert topic["metadata"]["sources"][0]["doc"] == original["id"]
    assert topic["stale"] is True
