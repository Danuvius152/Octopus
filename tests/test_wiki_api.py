"""Local knowledge behavior at its authenticated HTTP boundary."""

import pytest

from server.config import settings
from server.main import app
from tests import wiki_api
from tests.wiki_api import client as client
from tests.wiki_api import configure


async def test_malformed_portable_metadata_is_evidence_not_executable(client, tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "bad.md").write_text(
        '<!-- octopus-wiki {"kind": [], "sources": ["bad"]} -->\n# 双链\n资料'
    )
    await configure(client, vault)
    found = await client.get(
        "/api/wiki/search", params={"query": "双链"}, headers=client.headers
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
        await client.get("/api/wiki/read", params={"doc": "source.md"}, headers=client.headers)
    ).json()
    published = await client.post(
        "/api/wiki/publish",
        headers=client.headers,
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
        "/api/wiki/read", params={"doc": "Wiki/concepts/claim.md"}, headers=client.headers
    )
    assert read.status_code == 200, read.text
    assert read.json()["stale"] is True


async def test_read_reuses_current_bytes_for_content_and_metadata(client, tmp_path, monkeypatch):
    from pathlib import Path

    vault = tmp_path / "vault"
    vault.mkdir()
    article = vault / "note.md"
    article.write_text("# 当前文章\n观点及其适用条件。\n")
    await configure(client, vault)
    opened = []
    original_open = Path.open

    def observe_open(path, mode="r", *args, **kwargs):
        if path == article and "r" in mode:
            opened.append(mode)
        return original_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", observe_open)
    response = await client.get(
        "/api/wiki/read", params={"doc": "note.md"}, headers=client.headers
    )
    assert response.status_code == 200, response.text
    assert response.json()["content"] == "# 当前文章\n观点及其适用条件。\n"
    assert opened == ["rb"]


async def test_unchanged_navigation_does_not_scan_the_vault(client, tmp_path, monkeypatch):
    import os
    from pathlib import Path

    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "selected.md").write_text("# 精确对象\n只有这一篇相关。\n")
    unrelated = vault / "unrelated.md"
    unrelated.write_text("# 无关文档\n不必检查。\n")
    await configure(client, vault)
    initial = await client.get(
        "/api/wiki/search", params={"query": "精确对象"}, headers=client.headers
    )
    assert initial.status_code == 200, initial.text
    walks, unrelated_stats = [], []
    original_walk, original_stat = os.walk, Path.stat

    def observe_walk(path, *args, **kwargs):
        if Path(path) == vault:
            walks.append(path)
        return original_walk(path, *args, **kwargs)

    def observe_stat(path, *args, **kwargs):
        if path == unrelated:
            unrelated_stats.append(path)
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(os, "walk", observe_walk)
    monkeypatch.setattr(Path, "stat", observe_stat)
    result = await client.get(
        "/api/wiki/search", params={"query": "精确对象"}, headers=client.headers
    )
    assert result.status_code == 200, result.text
    assert [item["path"] for item in result.json()["items"]] == ["selected.md"]
    assert walks == []
    assert unrelated_stats == []


async def test_index_epoch_reports_a_single_document_save(client, tmp_path, monkeypatch):
    from pathlib import Path

    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "note.md").write_text("# 原观点\n原有条件。\n")
    unrelated = vault / "other.md"
    unrelated.write_text("# 无关\n无需读取。\n")
    await configure(client, vault)
    before = (await client.get("/api/wiki/info", headers=client.headers)).json()
    current = (await client.get("/api/wiki/read", params={"doc": "note.md"}, headers=client.headers)).json()
    opened = []
    original_open = Path.open

    def observe_open(path, mode="r", *args, **kwargs):
        if path == unrelated and "r" in mode:
            opened.append(path)
        return original_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", observe_open)
    saved = await client.put("/api/wiki/document", headers=client.headers, json={
        "path": "note.md", "content": "# 原观点\n补上明确条件。\n",
        "expected_revision": current["revision"], "reason": "保存单篇",
    })
    assert saved.status_code == 200, saved.text
    after = (await client.get("/api/wiki/info", headers=client.headers)).json()
    assert after["index_epoch"] > before["index_epoch"]
    assert after["index_status"] == "ready"
    assert after["pending_changes"] == 0
    assert opened == []
    found = await client.get("/api/wiki/search", params={"query": "明确条件"}, headers=client.headers)
    assert found.json()["items"][0]["revision"] == saved.json()["revision"]
    assert found.json()["index_epoch"] == after["index_epoch"]


async def test_okf_metadata_is_indexed_without_rewriting_the_article(client, tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    original = (
        "---\ntype: Reference\ntitle: 精确显示题名\n"
        "description: 保留条件与立场的文章。\nsources: []\n"
        "generated: {by: imported/model, at: 2026-01-01T00:00:00Z}\n"
        "x_wiki: {profile: article-v1, body_dialect: obsidian}\n"
        "private_extension: {keep: true}\n---\n# 原作者题名\n"
        "## Key note\n有条件的观点。\n```markdown\n## 正文\n示例标题\n```\n"
        "## 正文\n原作者的风格与理由，不应重写。\n"
    )
    article = vault / "note.md"
    article.write_text(original)
    await configure(client, vault)
    found = await client.get("/api/wiki/search", params={"query": "精确显示题名"}, headers=client.headers)
    assert found.status_code == 200, found.text
    item = found.json()["items"][0]
    assert item["title"] == "精确显示题名"
    assert item["metadata"]["doc_role"] == "reference"
    assert item["metadata"]["description"] == "保留条件与立场的文章。"
    assert item["metadata"]["generated"]["at"] == "2026-01-01T00:00:00Z"
    assert [part["role"] for part in item["metadata"]["body_sections"]].count("body") == 2
    assert article.read_text() == original


async def test_portable_uid_is_an_alias_and_collision_does_not_replace_identity(client, tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    uid = "urn:uuid:54b89d70-8829-40fb-ae52-3c99001293fe"
    (vault / "first.md").write_text(f"---\ntype: Reference\nuid: {uid}\n---\n# 第一篇\n原观点。\n")
    await configure(client, vault)
    first = (await client.get("/api/wiki/read", params={"doc": "first.md"}, headers=client.headers)).json()
    by_uid = await client.get("/api/wiki/read", params={"doc": uid}, headers=client.headers)
    assert by_uid.status_code == 200, by_uid.text
    assert by_uid.json()["id"] == first["id"]
    (vault / "second.md").write_text(f"---\ntype: Reference\nuid: {uid}\n---\n# 第二篇\n另一立场。\n")
    second = (await client.get("/api/wiki/read", params={"doc": "second.md"}, headers=client.headers)).json()
    assert second["id"] != first["id"]
    assert second["metadata"]["uid_conflict"]["existing_doc"] == first["id"]
    again = await client.get("/api/wiki/read", params={"doc": uid}, headers=client.headers)
    assert again.json()["path"] == "first.md"


async def test_publish_preserves_portable_sources_when_runtime_sources_are_omitted(client, tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    await configure(client, vault)
    content = "---\nsources:\n  - {id: article, resource: 'https://example.com/article'}\ncustom: retained\n---\n# Explanation\nThe article describes a conditional conclusion.[^article]\n"
    result = await client.post("/api/wiki/publish", headers=client.headers, json={"path": "Library/explanation.md", "content": content, "kind": "source", "expected_revision": None, "reason": "保留用户提供的实际出处"})
    assert result.status_code == 200, result.text
    current = (await client.get("/api/wiki/read", params={"doc": "Library/explanation.md"}, headers=client.headers)).json()
    assert current["metadata"]["sources"] == [{"id": "article", "resource": "https://example.com/article"}]
    assert current["metadata"]["source_refs"][0]["status"] == "external"
    assert "custom: retained" in (vault / "Library/explanation.md").read_text()


async def test_okf_local_source_is_a_direct_dependency_and_can_be_rechecked(client, tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "source.md").write_text("# 依据\n只在条件 A 下成立。\n")
    await configure(client, vault)
    source = (await client.get("/api/wiki/read", params={"doc": "source.md"}, headers=client.headers)).json()
    (vault / "summary.md").write_text(
        "---\ntype: Synthesis\nsources:\n"
        f"  - {{id: basis, resource: source.md, revision: '{source['revision']}', location: L2-L2}}\n"
        "---\n# 解释\n保留条件 A。[^basis]\n"
    )
    summary = (await client.get("/api/wiki/read", params={"doc": "summary.md"}, headers=client.headers)).json()
    assert summary["stale"] is False
    graph = await client.get("/api/wiki/graph", params={"doc": summary["id"]}, headers=client.headers)
    assert any(edge["to"] == source["id"] and edge["relation"] == "derived_from" for edge in graph.json()["edges"])
    (vault / "source.md").write_text("# 依据\n新增了条件 B。\n")
    after = (await client.get("/api/wiki/read", params={"doc": summary["id"]}, headers=client.headers)).json()
    assert after["stale"] is True
    assert after["metadata"]["sources"][0]["resource"] == "source.md"


async def test_article_normalization_preserves_words_unknown_yaml_and_existing_sources(client, tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    original = "\ufeff---\r\ncustom: {keep: '原值'} # 我的字段\r\nsources: [{resource: 'https://example.org/article'}]\r\n---\r\n# 我的判断\r\n\r\n我认为这很糟，但只在条件 A 下。😀\r\n\r\n```python\r\nprint('原样')\r\n```\r\n"
    (vault / "note.md").write_bytes(original.encode())
    await configure(client, vault)
    before = (await client.get("/api/wiki/read", params={"doc": "note.md"}, headers=client.headers)).json()
    result = await client.post("/api/wiki/organize", headers=client.headers, json={
        "path": "note.md", "expected_revision": before["revision"], "key_note": "判断限于条件 A。",
        "frontmatter_patch": {"type": "Reference", "title": "我的判断", "description": "带有条件的个人判断", "x_wiki": {"profile": "article-v1", "body_dialect": "obsidian"}},
        "reason": "按最新文章约定规范，保留原话",
    })
    assert result.status_code == 200, result.text
    raw = (vault / "note.md").read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf---\r\n")
    assert "custom: {keep: '原值'} # 我的字段\r\n".encode() in raw
    assert original.split("# 我的判断\r\n", 1)[1].encode() in raw
    current = (await client.get("/api/wiki/read", params={"doc": "note.md"}, headers=client.headers)).json()
    assert current["metadata"]["sources"] == [{"resource": "https://example.org/article"}]
    assert current["metadata"]["doc_role"] == "reference"
    assert current["metadata"]["x_wiki"]["profile"] == "article-v1"
    assert current["metadata"]["generated"]["by"] == "human"
    assert not list(vault.rglob("*.source.md.txt"))


async def test_targeted_revision_changes_only_selected_raw_words_after_human_edit(client, tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    original = "\ufeff---\r\ncustom: keep\r\n---\r\n# 判断\r\n\r\n我认为这很糟，只在条件 A 下。😀 单位为 10 ms。\r\n\r\n同样的单位为 10 ms，但这是另一个例子。\r\n"
    note = vault / "note.md"
    note.write_bytes(original.encode())
    await configure(client, vault)
    listed = await client.get("/api/wiki/blocks", params={"doc": "note.md", "view": "locators"}, headers=client.headers)
    assert listed.status_code == 200
    body = listed.json()
    assert all("content" not in part for part in body["blocks"])
    selected = (await client.get("/api/wiki/blocks", params={"doc": "note.md", "ids": "1"}, headers=client.headers)).json()
    part = selected["blocks"][0]
    start = part["content"].index("10 ms")
    response = await client.post("/api/wiki/revise", headers=client.headers, json={
        "doc": body["id"], "expected_revision": body["revision"], "operation": "patch", "reason": "确认单位应为秒",
        "edits": [{"locator": part["locator"], "expected_block_hash": part["sha256"], "start": start, "end": start + 5, "expected_text": "10 ms", "replacement": "10 s", "kind": "correction"}],
    })
    assert response.status_code == 200, response.text
    assert note.read_bytes() == original.replace("10 ms", "10 s", 1).encode()
    assert response.json()["changed_ranges"][0]["expected_text"] == "10 ms"
    assert response.json()["original_preserved"] is False
    assert response.json()["receipt_id"]
    conflict = await client.post("/api/wiki/revise", headers=client.headers, json={
        "doc": body["id"], "expected_revision": body["revision"], "operation": "no_change", "reason": "旧版本不能确认",
    })
    assert conflict.status_code == 409


@pytest.mark.parametrize("missing_location", [False, True])
async def test_confirm_sources_updates_only_checked_dependencies_and_keeps_body(client, tmp_path, missing_location):
    vault = tmp_path / "vault"
    vault.mkdir()
    for name in ("a", "b"):
        (vault / f"{name}.md").write_text(f"# {name}\n原条件。\n")
    await configure(client, vault)
    inputs = [(await client.get("/api/wiki/read", params={"doc": f"{name}.md"}, headers=client.headers)).json() for name in ("a", "b")]
    body = "# 综合\n\n我认为有条件成立；保留反例。\n"
    front = "---\ntype: Synthesis\nsources:\n" + "".join(f"  - {{id: {name}, resource: {name}.md, revision: '{item['revision']}', location: L2-L2}}\n" for name, item in zip(("a", "b"), inputs, strict=True)) + "---\n"
    if missing_location:
        front = front.replace(", location: L2-L2", "", 1)
    (vault / "summary.md").write_text(front + body)
    for name in ("a", "b"):
        (vault / f"{name}.md").write_text(f"# {name}\n追加条件。\n")
    summary = (await client.get("/api/wiki/read", params={"doc": "summary.md"}, headers=client.headers)).json()
    current = (await client.get("/api/wiki/read", params={"doc": "a.md"}, headers=client.headers)).json()
    other = (await client.get("/api/wiki/read", params={"doc": "b.md"}, headers=client.headers)).json()
    before = (vault / "summary.md").read_bytes()
    rejected = await client.post("/api/wiki/revise", headers=client.headers, json={"doc": summary["id"], "expected_revision": summary["revision"], "operation": "confirm_sources", "checks": [{"resource": "a.md", "doc": other["id"], "revision": other["revision"], "location": "L2-L2"}], "reason": "Cannot rebind a dependency to another document"})
    assert rejected.status_code == 422, rejected.text
    assert (vault / "summary.md").read_bytes() == before
    result = await client.post("/api/wiki/revise", headers=client.headers, json={"doc": summary["id"], "expected_revision": summary["revision"], "operation": "confirm_sources", "checks": [{"resource": "a.md", "doc": current["id"], "revision": current["revision"], "location": "L2-L2"}], "reason": "已重读 a，原解释仍适用"})
    assert result.status_code == 200, result.text
    after = (await client.get("/api/wiki/read", params={"doc": summary["id"]}, headers=client.headers)).json()
    assert result.json()["revision"] == summary["revision"] and not result.json()["changed"]
    assert (vault / "summary.md").read_bytes() == before
    assert after["metadata"]["sources"][0]["revision"] == inputs[0]["revision"]
    assert after["metadata"]["source_refs"][0]["confirmed_revision"] == current["revision"]
    assert after["metadata"]["source_refs"][0]["confirmation_receipt_id"] == result.json()["receipt_id"]
    assert after["metadata"]["sources"][1]["revision"] == inputs[1]["revision"]
    assert after["stale"] is True
    assert (vault / "summary.md").read_text().split("---\n", 2)[2] == body


@pytest.mark.parametrize("combined", [False, True])
async def test_confirm_sources_distinguishes_positions_of_one_resource(client, tmp_path, combined):
    import yaml

    vault = tmp_path / "vault"
    vault.mkdir()
    source_path = vault / "source.md"
    source_path.write_text("# Source\n\n## First\nOld first condition.\n\n## Second\nOld second condition.\n")
    (vault / "other.md").write_text("# Other\nAn unrelated source.\n")
    await configure(client, vault)
    source = (await client.get("/api/wiki/read", params={"doc": "source.md"})).json()
    refs = [{"doc": source["id"], "resource": source["id"], "revision": source["revision"],
             "location": location, "custom": "keep"} for location in ("First", "Second")]
    body = "# Interpretation\n\nThe existing explanation still applies, with its conditions.\n"
    note = vault / "note.md"
    note.write_text("---\n" + yaml.safe_dump({"sources": refs, "custom": "keep"}) + "---\n" + body)
    source_path.write_text(source_path.read_text() + "\nAdditional context.\n")
    current = (await client.get("/api/wiki/read", params={"doc": "source.md"})).json()
    other = (await client.get("/api/wiki/read", params={"doc": "other.md"})).json()
    summary = (await client.get("/api/wiki/read", params={"doc": "note.md"})).json()
    check = {"resource": source["id"], "doc": source["id"], "revision": current["revision"], "location": "First"}
    request = {"doc": summary["id"], "expected_revision": summary["revision"], "operation": "confirm_sources", "reason": "Both original explanations still apply"}
    before = note.read_bytes()
    for checks in ([check | {"location": "Missing"}], [check, check], [check | {"doc": other["id"], "revision": other["revision"]}]):
        rejected = await client.post("/api/wiki/revise", json=request | {"checks": checks})
        assert rejected.status_code == 422, rejected.text
        assert note.read_bytes() == before
    checks = [check | {"location": "Second"}, check] if combined else [check]
    result = await client.post("/api/wiki/revise", json=request | {"checks": checks})
    assert result.status_code == 200, result.text
    after = (await client.get("/api/wiki/read", params={"doc": "note.md"})).json()
    if not combined:
        assert [ref.get("confirmed_revision") for ref in after["metadata"]["source_refs"]] == [current["revision"], None]
        assert after["stale"]
        result = await client.post("/api/wiki/revise", json=request | {"expected_revision": after["revision"], "checks": [check | {"location": "Second"}]})
        assert result.status_code == 200, result.text
        after = (await client.get("/api/wiki/read", params={"doc": "note.md"})).json()
    assert not after["stale"]
    assert note.read_bytes() == before
    assert after["metadata"]["sources"] == summary["metadata"]["sources"]
    assert [ref["confirmed_revision"] for ref in after["metadata"]["source_refs"]] == [current["revision"]] * 2
    front, preserved = note.read_text().split("---\n")[1:]
    assert yaml.safe_load(front)["custom"] == "keep" and preserved == body


async def test_compact_lookup_and_domain_search_use_index_only_and_respect_roles(client, tmp_path, monkeypatch):
    from pathlib import Path

    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "paper.md").write_text("---\ntype: Reference\ntitle: 视频蒸馏\ndescription: 讨论延迟和保真\naliases: [Vidu S2]\n---\n# 视频蒸馏\n数字人实时视频方法。\n")
    (vault / "template.md").write_text("---\ntype: Template\ntitle: 数字人模板\n---\n# 数字人模板\n数字人数字人数字人\n")
    await configure(client, vault)
    opens = []
    original = Path.open

    def observe(path, mode="r", *args, **kwargs):
        if path.is_relative_to(vault) and "r" in mode:
            opens.append(path)
        return original(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", observe)
    looked = await client.get("/api/wiki/search", params={"query": "Vidu S2", "intent": "lookup", "view": "compact"}, headers=client.headers)
    assert looked.status_code == 200, looked.text
    card = looked.json()["items"][0]
    assert card["doc_role"] == "reference"
    assert card["description"] == "讨论延迟和保真"
    assert card["freshness"] == "indexed"
    assert "metadata" not in card and "pages" not in card
    assert looked.json()["match_mode"] == "exact"
    found = (await client.get("/api/wiki/search", params={"query": "数字人", "intent": "overview", "view": "compact"}, headers=client.headers)).json()
    assert [item["path"] for item in found["items"]] == ["paper.md"]
    template = (await client.get("/api/wiki/search", params={"query": "template.md", "intent": "lookup", "view": "compact"}, headers=client.headers)).json()
    assert template["items"][0]["doc_role"] == "template"
    assert not opens


async def test_read_many_merges_overlaps_and_reports_changed_missing_and_budget(client, tmp_path, monkeypatch):
    from pathlib import Path

    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "a.md").write_text("# A\n😀甲乙丙丁戊己庚辛壬癸\n")
    (vault / "b.md").write_text("# B\n另一篇文章\n")
    await configure(client, vault)
    before = (await client.get("/api/wiki/read", params={"doc": "b.md"}, headers=client.headers)).json()
    (vault / "b.md").write_text("# B\n后来人工修改\n")
    # Consume the notification before measuring the batch's current reads.
    await client.get("/api/wiki/info", headers=client.headers)
    opens = []
    original = Path.open

    def observe(path, mode="r", *args, **kwargs):
        if path == vault / "a.md" and mode == "rb":
            opens.append(path)
        return original(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", observe)
    response = await client.post("/api/wiki/read-many", headers=client.headers, json={"items": [
        {"doc": "a.md", "start": 4, "length": 4}, {"doc": "a.md", "start": 6, "length": 4},
        {"doc": "b.md", "expected_revision": before["revision"]}, {"doc": "absent.md"},
        {"doc": "a.md", "start": 10, "length": 6},
    ], "budget_chars": 6})
    assert response.status_code == 200, response.text
    result = response.json()
    assert [item["status"] for item in result["items"]] == ["ok", "ok", "changed", "not_found", "budget_exhausted"]
    assert result["ranges"][0]["content"] == "😀甲乙丙丁戊"
    assert result["ranges"][0]["start"] == 4 and result["ranges"][0]["end"] == 10
    assert result["characters_returned"] == 6
    assert result["items"][-1]["next_start"] == 10
    assert len(opens) == 1


async def test_indexed_links_resolve_used_references_and_report_changed_heading_anchors(client, tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "a.md").write_text("# 原标题\n观点。\n")
    related = "# 导航\n[实际链接][used]\n\n[used]: a.md#原标题\n[unused]: absent.md\n\n`[[fake]]`\n"
    (vault / "b.md").write_text(related)
    await configure(client, vault)
    before = (await client.get("/api/wiki/graph", params={"doc": "b.md"}, headers=client.headers)).json()
    assert len(before["edges"]) == 1
    assert before["edges"][0]["status"] == "resolved"
    document = (await client.get("/api/wiki/blocks", params={"doc": "a.md"}, headers=client.headers)).json()
    part = document["blocks"][0]
    result = await client.post("/api/wiki/revise", headers=client.headers, json={"doc": document["id"], "expected_revision": document["revision"], "operation": "patch", "reason": "明确更正题名", "edits": [{"locator": part["locator"], "expected_block_hash": part["sha256"], "start": 2, "end": 5, "expected_text": "原标题", "replacement": "新标题", "kind": "correction"}]})
    assert result.status_code == 200, result.text
    after = (await client.get("/api/wiki/graph", params={"doc": "b.md"}, headers=client.headers)).json()
    assert after["edges"][0]["status"] == "missing_anchor"
    assert (vault / "b.md").read_text() == related


@pytest.mark.parametrize("extension", ["verified: {by: human, extra: !!binary YQ==}", "verified: &review {by: human, extra: *review}"])
async def test_unprojectable_optional_metadata_does_not_block_original_navigation(client, tmp_path, extension):
    vault = tmp_path / "vault"
    vault.mkdir()
    raw = ("---\ntype: Reference\n" + extension + "\n---\n# 可读原文\n思想和风格原样保留。\n").encode()
    (vault / "note.md").write_bytes(raw)
    await configure(client, vault)
    current = await client.get("/api/wiki/read", params={"doc": "note.md"}, headers=client.headers)
    assert current.status_code == 200, current.text
    assert current.json()["metadata"]["frontmatter_error"]
    assert (vault / "note.md").read_bytes() == raw


@pytest.mark.parametrize("spacing", ["\n", ""])
@pytest.mark.parametrize("replacement", ["一段全新的概括。\n", "我认为这是一段新总结，省略了原来的论证和反例。\n"])
async def test_revise_rejects_replacing_an_entire_prose_block_with_a_new_summary(client, tmp_path, spacing, replacement):
    vault = tmp_path / "vault"
    vault.mkdir()
    prose = "我认为很糟糕，但只在条件 A 下。理由是实验没有控制条件。反例 B 不能被省略。\n"
    original = "---\ntype: Reference\n---\n# 我自己的判断\n" + spacing + prose
    (vault / "note.md").write_text(original)
    await configure(client, vault)
    document = (await client.get("/api/wiki/blocks", params={"doc": "note.md"}, headers=client.headers)).json()
    part = document["blocks"][-1]
    start = part["content"].index(prose)
    response = await client.post("/api/wiki/revise", headers=client.headers, json={"doc": document["id"], "expected_revision": document["revision"], "operation": "patch", "reason": "禁止用新摘要替代原论述", "edits": [{"locator": part["locator"], "expected_block_hash": part["sha256"], "start": start, "end": start + len(prose), "expected_text": prose, "replacement": replacement, "kind": "correction"}]})
    assert response.status_code == 422
    assert (vault / "note.md").read_text() == original


@pytest.mark.parametrize("heading", ["# Bad title\n\n", "Bad title\n=========\n\n"])
async def test_revise_allows_explicit_title_correction_without_replacing_the_prose(client, tmp_path, heading):
    vault = tmp_path / "vault"
    vault.mkdir()
    original = heading + "Actual prose preserves the author's judgment and conditions.\n"
    (vault / "note.md").write_text(original)
    await configure(client, vault)
    document = (await client.get("/api/wiki/blocks", params={"doc": "note.md"}, headers=client.headers)).json()
    part = document["blocks"][0]
    start = part["content"].index("Bad title")
    response = await client.post("/api/wiki/revise", headers=client.headers, json={"doc": document["id"], "expected_revision": document["revision"], "operation": "patch", "reason": "明确纠正题名", "edits": [{"locator": part["locator"], "expected_block_hash": part["sha256"], "start": start, "end": start + 9, "expected_text": "Bad title", "replacement": "Good heading", "kind": "correction"}]})
    assert response.status_code == 200, response.text
    assert (vault / "note.md").read_text() == original.replace("Bad title", "Good heading", 1)


async def test_graph_matches_real_basename_ambiguity_and_dotted_aliases(client, tmp_path):
    vault = tmp_path / "vault"
    for folder, title in (("a", "A"), ("b", "B")):
        (vault / folder).mkdir(parents=True)
        (vault / folder / "target.md").write_text(f"# {title}\nDistinct judgment.\n")
    (vault / "source.md").write_text("---\naliases: [Vidu v2.0]\n---\n# Source\nKnown explanation.\n")
    (vault / "myunique.md").write_text("# Different\nSuffix is not a basename.\n")
    (vault / "linked.md").write_text("# Links\n[[target]]\n[[Vidu v2.0]]\n[[unique]]\n")
    await configure(client, vault)
    result = (await client.get("/api/wiki/graph", params={"doc": "linked.md"}, headers=client.headers)).json()
    edges = {edge["resource"]: edge for edge in result["edges"]}
    assert edges["target"]["to"] is None and edges["target"]["status"] == "ambiguous"
    assert edges["Vidu v2.0"]["status"] == "resolved"
    assert edges["unique"]["to"] is None and edges["unique"]["status"] == "unresolved"
    current = (await client.get("/api/wiki/read", params={"doc": "linked.md"}, headers=client.headers)).json()
    accepted = await client.put("/api/wiki/document", headers=client.headers, json={"path": "linked.md", "content": "# Links\n[[Vidu v2.0]]\n", "expected_revision": current["revision"], "reason": "同一真实别名通过写入校验和导航解析"})
    assert accepted.status_code == 200, accepted.text


@pytest.mark.parametrize("suffix", [".MD", ".MARKDOWN"])
async def test_wikilink_basename_keeps_supported_case_variants_of_markdown_suffixes(client, tmp_path, suffix):
    vault = tmp_path / "vault"
    (vault / "nested").mkdir(parents=True)
    (vault / "nested" / ("Foo" + suffix)).write_text("# Different display title\nOriginal judgment.\n")
    (vault / "linked.md").write_text("# Links\n[[Foo]]\n")
    await configure(client, vault)
    graph = (await client.get("/api/wiki/graph", params={"doc": "linked.md"}, headers=client.headers)).json()
    assert graph["edges"][0]["status"] == "resolved"
    current = (await client.get("/api/wiki/read", params={"doc": "linked.md"}, headers=client.headers)).json()
    saved = await client.put("/api/wiki/document", headers=client.headers, json={"path": "linked.md", "content": "# Links\n[[Foo]] is preserved.\n", "expected_revision": current["revision"], "reason": "保留受支持文件后缀"})
    assert saved.status_code == 200, saved.text


async def test_alias_removal_recomputes_navigation_and_distinguishes_ambiguity(client, tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "a.md").write_text("---\naliases: [OldAlias]\n---\n# Shared\n观点 A。\n")
    (vault / "b.md").write_text("# Shared\n观点 B。\n")
    (vault / "linked.md").write_text("# 链接\n[[OldAlias]]\n[[Shared]]\n")
    await configure(client, vault)
    before = (await client.get("/api/wiki/graph", params={"doc": "linked.md"}, headers=client.headers)).json()
    assert next(edge for edge in before["edges"] if edge["resource"] == "Shared")["status"] == "ambiguous"
    current = (await client.get("/api/wiki/read", params={"doc": "a.md"}, headers=client.headers)).json()
    saved = await client.put("/api/wiki/document", headers=client.headers, json={"path": "a.md", "content": "---\naliases: [NewAlias]\n---\n# Shared\n观点 A。\n", "expected_revision": current["revision"], "reason": "修改别名"})
    assert saved.status_code == 200, saved.text
    after = (await client.get("/api/wiki/graph", params={"doc": "linked.md"}, headers=client.headers)).json()
    removed = next(edge for edge in after["edges"] if edge["resource"] == "OldAlias")
    assert removed["to"] is None and removed["status"] == "unresolved"


async def test_compare_keeps_each_named_method_when_a_comparison_page_exists(client, tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "alpha.md").write_text("# Alpha 方法\nAlpha 用视频蒸馏减少延迟，条件是固定帧长。\n")
    (vault / "beta.md").write_text("# Beta 方法\nBeta 重建单目 Rig，条件是标定精度足够。\n")
    (vault / "map.md").write_text("# 比较概览\nAlpha 与 Beta 用于不同研究问题。\n")
    await configure(client, vault)
    response = await client.get("/api/wiki/search", params={"query": "Alpha Beta", "intent": "compare", "view": "compact", "limit": 3}, headers=client.headers)
    assert response.status_code == 200, response.text
    assert {item["path"] for item in response.json()["items"]} == {"alpha.md", "beta.md", "map.md"}


async def test_user_searches_chinese_short_words_and_reads_revisioned_original(
    client, tmp_path
):
    vault = tmp_path / "vault"
    vault.mkdir()
    original = "---\naliases: [协作]\n---\n# Agent 协作\n\n双链支持来源核验。\n"
    (vault / "协作.md").write_text(original)
    await configure(client, vault)
    found = await client.get(
        "/api/wiki/search", params={"query": "双链"}, headers=client.headers
    )
    assert found.status_code == 200, found.text
    hit = found.json()["items"][0]
    assert hit["path"] == "协作.md"
    read = await client.get(
        "/api/wiki/read", params={"doc": hit["id"]}, headers=client.headers
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
    agent = await wiki_api.session_manager.db.get_system_agent()
    session = await wiki_api.session_manager.create_session(agent["id"], "Ordinary Wiki refusal")
    response = await client.get(
        "/api/wiki/read",
        params={"doc": "private.md"},
        headers={"Authorization": f"Bearer {mint(session.id, user_id=session.user_id)}"},
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
    found = await client.get("/api/wiki/search?query=双链", headers=client.headers)
    assert [item["path"] for item in found.json()["items"]] == ["Library/笔记.md"]
    assets = await client.get(
        "/api/wiki/documents?include_assets=true", headers=client.headers
    )
    assert assets.json()["total"] == 2
    read = await client.get(
        "/api/wiki/read?doc=Library/course/tmp/extract.txt", headers=client.headers
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
    session = await wiki_api.session_manager.create_session(
        config["curator_agent_id"], "工具范围", str(tmp_path)
    )
    api = await start_callback_api(readiness_token=mint(session.id, user_id=session.user_id))
    try:
        for mode in ("read", "maintain"):
            token = tasks.open(session, mode)
            try:
                async with httpx.AsyncClient(
                    headers={
                        "Authorization": f"Bearer {mint(session.id, user_id=session.user_id, wiki_task_id=token)}"
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
                assert "read_many" in visible
                assert "publish" not in visible
                assert "organize" not in visible
                assert ("maintain" in visible) == (mode == "maintain")
            finally:
                tasks.close(token)
    finally:
        await api.stop()


async def test_mcp_curator_executes_new_contracts_through_real_http(client, tmp_path, monkeypatch):
    import json

    import httpx
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    from tests.wiki_job_runtime import worker_callback

    vault = tmp_path / "vault"
    (vault / "Library").mkdir(parents=True)
    (vault / "Library/note.md").write_text("# 当前文章\n\n观点和条件完整。\n")
    config = await configure(client, vault)
    async with worker_callback(config, ["Library/note.md"], client=client) as (worker, api):
        monkeypatch.setattr(settings, "port", api.port)
        async with httpx.AsyncClient(headers=worker.headers) as transport:
            async with streamable_http_client(f"http://127.0.0.1:{api.port}/mcp/wiki/mcp", http_client=transport) as (reader, writer, _):
                async with ClientSession(reader, writer) as peer:
                    await peer.initialize()
                    visible = {tool.name for tool in (await peer.list_tools()).tools}
                    assert {"read_many", "revise", "load_guide"} <= visible
                    found = await peer.call_tool("search", {"query": "Library/note.md", "intent": "lookup", "view": "compact"})
                    payload = json.loads(found.content[0].text)
                    assert "items" in payload, payload
                    card = payload["items"][0]
                    assert card["freshness"] == "indexed" and "metadata" not in card
                    evidence = await peer.call_tool("read_many", {"items": [{"doc": card["id"], "expected_revision": card["revision"]}], "budget_chars": 100})
                    assert json.loads(evidence.content[0].text)["ranges"][0]["content"].endswith("观点和条件完整。\n")
                    result = await peer.call_tool("revise", {"request": {"doc": card["id"], "expected_revision": card["revision"], "operation": "no_change", "reason": "已经满足当前范围"}})
                    receipt = json.loads(result.content[0].text)
                    assert receipt["changed"] is False and receipt["receipt_id"]


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
    response = await client.get("/api/wiki/image?path=图.png", headers=client.headers)
    assert response.status_code == 200, response.text
    assert response.content == png
    assert response.headers["content-type"] == "image/png"
    excluded = await client.get("/api/wiki/image?path=附件.pdf", headers=client.headers)
    assert excluded.status_code == 422
    escape = await client.get("/api/wiki/image?path=../secret.png", headers=client.headers)
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
    graph = await client.get("/api/wiki/graph?doc=Library/旧笔记.md", headers=client.headers)
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
        "/api/wiki/read", params={"doc": "说明.txt"}, headers=client.headers
    )
    assert txt.status_code == 200, txt.text
    assert "双链" in txt.json()["content"]
    html = await client.get(
        "/api/wiki/read", params={"doc": "参考.html"}, headers=client.headers
    )
    assert "秘密假词" not in html.json()["content"]
    pdf = await client.get(
        "/api/wiki/read", params={"doc": "扫描.pdf"}, headers=client.headers
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
        await client.get("/api/wiki/read", params={"doc": "原文.md"}, headers=client.headers)
    ).json()
    changed = await client.put(
        "/api/wiki/document",
        headers=client.headers,
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
        headers=client.headers,
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
            "/api/wiki/history", params={"path": "原文.md"}, headers=client.headers
        )
    ).json()["items"]
    assert history[0]["status"] == "conflict"
    assert history[0]["proposed_content"] == "待处理草稿\n"
    restored = await client.post(
        "/api/wiki/restore",
        headers=client.headers,
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
            "/api/wiki/history", params={"path": "原文.md"}, headers=client.headers
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
    headers = client.headers
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
            "/api/wiki/search", params={"query": "来源条目"}, headers=client.headers
        )
    ).json()
    assert found["items"][0]["stale"] is True


@pytest.mark.parametrize("operation", ["read_many", "submit"])
@pytest.mark.parametrize("caller", ["http", "app"])
@pytest.mark.parametrize("serve_frontend", [False, True])
async def test_retired_wiki_urls_are_unregistered_without_effects(
    client, tmp_path, monkeypatch, operation, caller, serve_frontend
):
    from datetime import UTC, datetime

    from fastapi.staticfiles import StaticFiles
    from starlette.routing import Mount

    from server.applications import ApplicationManager, app_scope_token
    from server.routers import applications as app_routes

    routes = [route for route in app.router.routes if getattr(route, "name", None) != "spa"]
    monkeypatch.setattr(app.router, "routes", routes)
    if serve_frontend:
        frontend = tmp_path / "frontend"
        frontend.mkdir()
        (frontend / "index.html").write_text("<html>isolated frontend</html>")
        routes.append(Mount("/", app=StaticFiles(directory=frontend, html=True), name="spa"))

    vault = tmp_path / "vault"
    vault.mkdir()
    original = "# 当前资料\n旧URL不能创建任务或改写来源。\n"
    (vault / "note.md").write_text(original)
    config = await configure(client, vault)
    prefix, headers = "/api/wiki", client.headers
    if caller == "app":
        manager = ApplicationManager()
        manager.bind(session_mgr=wiki_api.session_manager, db=wiki_api.session_manager.db)
        monkeypatch.setattr(app_routes, "_manager", manager)
        now = datetime.now(UTC).isoformat()
        await wiki_api.session_manager.db.save_application(
            app_id="sapphire", name="sapphire", app_dir=str(tmp_path / "sapphire"),
            created_at=now, updated_at=now, agent_id=config["curator_agent_id"],
            user_id=(await wiki_api.session_manager.db.get_agent(config["curator_agent_id"]))["user_id"],
        )
        bound = await client.put("/api/wiki/config", json={
            "root": str(vault), "curator_agent_id": config["curator_agent_id"], "app_id": "sapphire",
        })
        assert bound.status_code == 200, bound.text
        prefix = "/apps/sapphire/wiki"
        headers = {"Authorization": f"Bearer {app_scope_token('sapphire')}"}
    before = (await client.get("/api/wiki/jobs")).json()
    original_files = {path.relative_to(vault).as_posix(): path.read_bytes()
                      for path in vault.rglob("*") if path.is_file()}
    body = {"items": [{"doc": "note.md"}]} if operation == "read_many" else {
        "idempotency_key": "retired-entry", "material": "不能受理旧入口", "scope": "旧操作",
    }
    result = await client.post(f"{prefix}/{operation}", json=body, headers=headers)
    # The root static mount rejects non-GET methods before looking up a file.
    expected_status = 405 if serve_frontend and caller == "http" else 404
    assert result.status_code == expected_status, result.text
    assert (await client.get("/api/wiki/jobs")).json() == before
    assert (vault / "note.md").read_text() == original
    assert {path.relative_to(vault).as_posix(): path.read_bytes()
            for path in vault.rglob("*") if path.is_file()} == original_files
    assert not any(getattr(route, "path", None) in {"/api/wiki/read_many", "/api/wiki/submit"}
                   for route in app.routes)


async def test_current_maintenance_is_idempotent_and_can_be_cancelled_verified_and_retried(
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
                        "/api/wiki/read", params={"doc": "资料.md"}, headers=client.headers
                    )
                ).json()["revision"],
                "location": "全文",
            }
        ],
    }
    first = await client.post("/api/wiki/maintain", json=submission, headers=client.headers)
    assert first.status_code == 200, first.text
    second = await client.post("/api/wiki/maintain", json=submission, headers=client.headers)
    assert second.json()["id"] == first.json()["id"]
    conflict = await client.post("/api/wiki/maintain", json={**submission, "material": "改变原请求"}, headers=client.headers)
    assert conflict.status_code == 409, conflict.text
    job_id = first.json()["id"]
    cancelled = await client.post(f"/api/wiki/jobs/{job_id}/cancel", headers=client.headers)
    assert cancelled.json()["status"] == "cancelled"
    verified = await client.post(f"/api/wiki/jobs/{job_id}/verify", json={}, headers=client.headers)
    assert verified.status_code == 200, verified.text
    assert verified.json()["settlement_phase"] == "confirmed"
    retried = await client.post(f"/api/wiki/jobs/{job_id}/retry", headers=client.headers)
    assert retried.status_code == 200, retried.text
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
    first = await client.post("/api/wiki/maintain", json=request, headers=client.headers)
    assert first.status_code == 200, first.text
    job = first.json()
    assert job["payload"]["new_only"] is True
    assert job["payload"]["target"] == "Library/Inbox/新观点.md"
    assert job["items"] == []
    again = await client.post("/api/wiki/maintain", json=request, headers=client.headers)
    assert again.json()["id"] == job["id"]
    assert (vault / "旧文.md").read_text() == old
    empty = await client.post(
        "/api/wiki/maintain", json={"idempotency_key": "empty"}, headers=client.headers
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
    current = (await client.get("/api/wiki/read?doc=笔记.md", headers=client.headers)).json()
    body = {
        "path": "笔记.md",
        "expected_revision": current["revision"],
        "key_note": "判断可能只在条件 A 下成立。",
        "tags": ["适用条件"],
        "reason": "首次整理",
    }
    added = await client.post("/api/wiki/organize", json=body, headers=client.headers)
    assert added.status_code == 200, added.text
    first = (await client.get("/api/wiki/read?doc=笔记.md", headers=client.headers)).json()
    assert set(first["metadata"]["tags"]) == {"原有标签", "适用条件"}
    assert first["stale"] is False
    rendered = (vault / "笔记.md").read_text()
    assert rendered.startswith(
        "---\ntags: [原有标签]\naliases: [旧名称]\n---\n\n# 原文\n"
    )
    assert "我的判断可能只在条件 A 下成立。\n\n```py\nx = 1\n```\n" in rendered
    invalid = await client.post(
        "/api/wiki/organize",
        headers=client.headers,
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
    refreshed = await client.post("/api/wiki/organize", json=body, headers=client.headers)
    assert refreshed.status_code == 200, refreshed.text
    final = (vault / "笔记.md").read_text()
    assert final.count("## Key note") == 1
    assert "\n判断可能只在条件 A 下成立。" not in final
    assert "我的判断可能只在条件 A 下成立。" in final
    # Human edits to Weber's area become protected rather than silently replaced.
    final = final.replace("条件 A 限定了这个可能的判断。", "我自己校准后的要点。")
    saved = await client.put(
        "/api/wiki/document",
        headers=client.headers,
        json={
            "path": "笔记.md",
            "content": final,
            "expected_revision": refreshed.json()["revision"],
            "reason": "人工调整",
        },
    )
    assert saved.status_code == 200
    read = (await client.get("/api/wiki/read?doc=笔记.md", headers=client.headers)).json()
    assert read["metadata"]["needs_refresh"] is True
    body["expected_revision"] = read["revision"]
    rejected = await client.post("/api/wiki/organize", json=body, headers=client.headers)
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
    agent = await wiki_api.session_manager.db.get_agent(config["curator_agent_id"])
    backend = agent.get("backend") or "claude-code"

    attempts = []

    target = "Library/Inbox/新笔记.md" if ingestion else "资料.md"

    def create(run_config):
        headers = {
            "Authorization": f"Bearer {mint(run_config.session_id, user_id=run_config.user_id, wiki_task_id=run_config.wiki_task_id)}"
        }

        class Runtime(FakeRunBase):
            def set_idle_handler(self, handler):
                pass

            async def start(self, *args, **kwargs):
                pass

            async def stop(self):
                pass

            async def interrupt(self):
                await self.stop()

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
    await worker.start(wiki_api.session_manager)
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
            headers=client.headers,
        )
        assert response.status_code == 200, response.text
        job_id = response.json()["id"]
        # File, index and history work stay real; this checks authority and
        # coverage rather than completion latency under full-suite contention.
        async with asyncio.timeout(10):
            while True:
                job = (
                    await client.get(f"/api/wiki/jobs/{job_id}", headers=client.headers)
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
    manager.bind(session_mgr=wiki_api.session_manager, db=wiki_api.session_manager.db)
    monkeypatch.setattr(app_routes, "_manager", manager)
    try:
        now = datetime.now(UTC).isoformat()
        for app_id in ("sapphire", "other"):
            await wiki_api.session_manager.db.save_application(
                app_id=app_id,
                name=app_id,
                app_dir=str(tmp_path / app_id),
                created_at=now,
                updated_at=now,
                agent_id=config["curator_agent_id"],
                user_id=(await wiki_api.session_manager.db.get_agent(config["curator_agent_id"]))["user_id"],
            )
        response = await client.put(
            "/api/wiki/config",
            headers=client.headers,
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
        compact = await client.get("/apps/sapphire/wiki/read", params={"doc": "note.md", "view": "compact"}, headers=app_headers)
        assert compact.status_code == 200, compact.text
        assert compact.json()["content"] == original.json()["content"]
        assert "metadata" in original.json() and "metadata" not in compact.json()
        invalid = await client.get("/apps/sapphire/wiki/read", params={"doc": "note.md", "view": "invalid"}, headers=app_headers)
        assert invalid.status_code == 422
        navigation = await client.get("/apps/sapphire/wiki/search", params={"query": "note.md", "intent": "lookup", "view": "compact"}, headers=app_headers)
        assert navigation.status_code == 200, navigation.text
        assert navigation.json()["items"][0]["freshness"] == "indexed"
        batch = await client.post("/apps/sapphire/wiki/read-many", json={"items": [{"doc": "note.md", "length": 2}], "budget_chars": 2}, headers=app_headers)
        assert batch.status_code == 200, batch.text
        assert batch.json()["characters_returned"] == 2
        compact_batch = await client.post("/apps/sapphire/wiki/read-many", json={"items": [{"doc": "note.md", "length": 2}], "budget_chars": 2, "view": "compact"}, headers=app_headers)
        assert compact_batch.status_code == 200, compact_batch.text
        assert compact_batch.json()["ranges"] == batch.json()["ranges"]
        assert "metadata" not in compact_batch.json()["documents"][0]
        maintenance = {"idempotency_key": "bound-app-maintain", "paths": ["note.md"]}
        queued = await client.post("/apps/sapphire/wiki/maintain", json=maintenance, headers=app_headers)
        assert queued.status_code == 200, queued.text
        again = await client.post("/apps/sapphire/wiki/maintain", json=maintenance, headers=app_headers)
        assert again.status_code == 200 and again.json()["id"] == queued.json()["id"]
        conflict = await client.post("/apps/sapphire/wiki/maintain", json={**maintenance, "scope": "不同请求"}, headers=app_headers)
        assert conflict.status_code == 409, conflict.text
        before_denial = (await client.get("/api/wiki/jobs")).json()
        blocked = await client.post("/apps/other/wiki/maintain", json=maintenance,
                                    headers={"Authorization": f"Bearer {app_scope_token('other')}"})
        assert blocked.status_code == 403, blocked.text
        assert (await client.get("/api/wiki/jobs")).json() == before_denial
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

    history_store = WikiHistory(registry.store((await wiki_api.session_manager.db.get_system_agent())["user_id"]))
    snapshot = {"id": uuid.uuid4().hex, "created_at": datetime.now(UTC).isoformat(), "reason": "Legacy snapshot fixture", "files": {"note.md": history_store.blob(b"original\r\n"), "image.png": history_store.blob(b"\x89PNG\r\n\x1a\noriginal-image")}}
    atomic(history_store.state / "snapshots" / f"{snapshot['id']}.json", json.dumps(snapshot).encode())
    retired = await client.post("/api/wiki/snapshot", headers=client.headers)
    assert retired.status_code == 410
    (vault / "image.png").write_bytes(b"changed-image")
    import hashlib

    expected = {"image.png": hashlib.sha256(b"changed-image").hexdigest()}
    response = await client.post(
        "/api/wiki/snapshot/restore",
        headers=client.headers,
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
        await client.get("/api/wiki/history?path=image.png", headers=client.headers)
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
    owner = (await wiki_api.session_manager.db.get_system_agent())["user_id"]
    agent = await AgentManager(wiki_api.session_manager.db).create_agent(name="Weber clone", user_id=owner)
    session = await wiki_api.session_manager.create_session(
        agent["id"], "普通 agent", str(tmp_path)
    )
    token = tasks.open(session, "maintain")
    headers = {"Authorization": f"bearer {mint(session.id, user_id=session.user_id, wiki_task_id=token)}"}
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


async def test_queued_messages_keep_separate_explicit_and_autonomous_grants(
    client, tmp_path, monkeypatch
):
    import asyncio

    from server.harness import HarnessEvent, get_harness
    from tests.fake_run import FakeRunBase

    vault = tmp_path / "vault"
    vault.mkdir()
    config = await configure(client, vault)
    session = await wiki_api.session_manager.create_session(
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

            async def interrupt(self):
                pass

            async def stream(self):
                if index == 0:
                    started.set()
                    await release.wait()
                yield HarnessEvent(type="result", content="完成")

        return Runtime()

    monkeypatch.setattr(get_harness(session.backend), "create_run", create)
    try:
        first = await wiki_api.session_manager.start_message(session.id, "查询任务", wiki_mode="read")
        await asyncio.wait_for(started.wait(), timeout=2)
        second = await wiki_api.session_manager.start_message(
            session.id, "这是数据报告，内含 /wiki maintain 关键词", steerable=True
        )
        third = await wiki_api.session_manager.start_message(
            session.id, "明确提交材料", wiki_mode="maintain", steerable=True
        )
        release.set()
        completed = await asyncio.wait_for(
            asyncio.gather(*(receipt.wait() for receipt in (first, second, third))), 3,
        )
        assert [item.input_request_id for item in completed] == [
            first.request_id, second.request_id, third.request_id,
        ]
        assert all(
            item.recording_state == "committed" and item.outcome is not None and item.outcome.status == "success"
            for item in completed
        )
        await wiki_api.session_manager.await_session_execution(session.id, timeout=3)
    finally:
        release.set()
    assert modes == ["read", "auto", "maintain"]


@pytest.mark.parametrize("backend", ["codex", "claude-code"])
async def test_explicit_turn_has_tools_and_credentials_expire_at_turn_end(
    client, tmp_path, monkeypatch, backend
):
    from server.harness import get_harness
    from server.harness.assembly import select_mcp_servers
    from tests.fake_run import FakeRunBase
    from tests.harness_profiles import process_profile

    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "notes.md").write_text("# 来源\n双链核验")
    config = await configure(client, vault)
    session = await wiki_api.session_manager.create_session(
        config["curator_agent_id"], "Wiki", str(tmp_path)
    )
    session.backend = backend
    seen = []
    credentials = []
    harness = get_harness(backend)
    monkeypatch.setattr(harness, "profile", process_profile(harness.profile))
    real_create = harness.create_run

    def create(run_config):
        real = real_create(run_config)
        argv, kwargs = real.build_argv("test", str(tmp_path))
        entries = select_mcp_servers(
            run_config.mcp_servers,
            [],
            {},
            session_id=session.id,
            user_id=session.user_id,
            wiki_task_id=run_config.wiki_task_id,
        )
        wiki = next((e for e in entries if e.key == "wiki"), None)
        from server.wiki.workflow import BOOTSTRAP

        if backend == "codex":
            prompt = real._make_context("test", str(tmp_path), None, None).system_prompt
        else:
            prompt = argv[argv.index("--append-system-prompt") + 1]
        seen.append((wiki is not None, BOOTSTRAP in prompt))

        class Runtime(FakeRunBase):
            def set_idle_handler(self, handler):
                pass

            async def start(self, *args, **kwargs):
                pass

            async def stop(self):
                pass

            async def interrupt(self):
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
    import asyncio

    for prompt, mode in [("明确查询", "read"), ("普通任务", None), ("只读本轮", "read")]:
        receipt = await wiki_api.session_manager.start_message(session.id, prompt, wiki_mode=mode)
        assert receipt.accepted
        assert (await asyncio.wait_for(receipt.wait(), 5)).outcome.status == "success"
    assert seen == [(True, True), (True, True), (True, True)]
    assert len(set(credentials)) == 3
    for credential in credentials:
        response = await client.get(
            "/api/wiki/read",
            params={"doc": "notes.md"},
            headers={"Authorization": f"Bearer {credential}"},
        )
        assert response.status_code == 403


async def test_sapphire_existing_writer_and_async_git_share_framework_history(
    client, tmp_path, monkeypatch
):
    import asyncio
    import json
    from datetime import UTC, datetime

    from anyio.to_thread import current_default_thread_limiter

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
    manager.bind(session_mgr=wiki_api.session_manager, db=wiki_api.session_manager.db)
    monkeypatch.setattr(app_routes, "_manager", manager)
    now = datetime.now(UTC).isoformat()
    await wiki_api.session_manager.db.save_application(
        app_id="sapphire",
        name="sapphire",
        app_dir=str(app_dir),
        created_at=now,
        updated_at=now,
        agent_id=config["curator_agent_id"],
        user_id=(await wiki_api.session_manager.db.get_agent(config["curator_agent_id"]))["user_id"],
    )
    bound = await client.put(
        "/api/wiki/config",
        headers=client.headers,
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
from urllib.parse import parse_qs, urlsplit
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
        if parse_qs(urlsplit(self.path).query).get('vault', [''])[0] != 'obv':
            self.respond(404, {'error': 'vault not found'}); return
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
        current = hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else ''
        if body.get('revision') is None:
            self.respond(400, {'error': 'revision required'}); return
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
                "/api/wiki/read", params={"doc": "note.md"}, headers=client.headers
            )
        ).json()
        from server.wiki import registry
        from server.wiki import store as document_storage

        owner = (await wiki_api.session_manager.db.get_agent(config["curator_agent_id"]))["user_id"]
        store = registry.store(owner)
        with store.connect() as db:
            counts = [db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                      for table in ("changes", "operation_receipts", "job_publications")]
        with monkeypatch.context() as budget:
            budget.setattr(document_storage, "MAX_DOCUMENT_BYTES", 128)
            refused = await client.post(
                "/apps/sapphire/api/save", params={"vault": "obv"}, headers=client.headers,
                json={"path": "note.md", "content": "x" * 129, "revision": original["revision"]},
            )
            assert refused.status_code == 413, refused.text
            assert (vault / "note.md").read_text() == original["content"]
            assert not (store.state / "external-change.json").exists()
            with store.connect() as db:
                assert [db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                        for table in ("changes", "operation_receipts", "job_publications")] == counts
        saved = await client.post(
            "/apps/sapphire/api/save",
            params={"vault": "obv"},
            headers=client.headers,
            json={
                "path": "note.md",
                "content": "# 人工保存",
                "revision": original["revision"],
            },
        )
        assert saved.status_code == 200, saved.text
        conflict = await client.post(
            "/apps/sapphire/api/save",
            params={"vault": "obv"},
            headers=client.headers,
            json={
                "path": "note.md",
                "content": "# 冲突成果",
                "revision": original["revision"],
            },
        )
        assert conflict.status_code == 409
        git = await client.post(
            "/apps/sapphire/api/git", params={"vault": "obv"}, headers=client.headers, json={"action": "pull"}
        )
        assert git.status_code == 202
        # Saturate a real, smaller AnyIO pool. More contenders than capacity
        # still exposes a lock waiter starving its owning writer, without 43
        # unrelated document writes and their growing index/history setup.
        limiter = current_default_thread_limiter()
        monkeypatch.setattr(limiter, "total_tokens", 2)
        contenders = limiter.total_tokens + 3
        async with asyncio.timeout(20):
            receipts = await asyncio.gather(
                *[
                    client.post(
                        "/apps/sapphire/api/save",
                        params={"vault": "obv"},
                        headers=client.headers,
                        json={
                            "path": f"new-{i}.md",
                            "content": "# 新内容",
                            "revision": "",
                        },
                    )
                    for i in range(contenders)
                ]
            )
        assert all(r.status_code == 200 for r in receipts)
        latest = (
            await client.get(
                "/api/wiki/read", params={"doc": "note.md"}, headers=client.headers
            )
        ).json()
        assert latest["content"] == "# Git result"
        history = (
            await client.get(
                "/api/wiki/history", params={"path": "note.md"}, headers=client.headers
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
                params={"vault": "obv"},
                headers=client.headers,
                json={"path": "cancelled.md", "content": "cancelled", "revision": ""},
            )
        )
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            cancelled.cancel()
            next_writer = asyncio.create_task(
                client.post(
                    "/apps/sapphire/api/save",
                    params={"vault": "obv"},
                    headers=client.headers,
                    json={
                        "path": "next.md",
                        "content": "next writer",
                        "revision": "",
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

        assert not (registry.state((await wiki_api.session_manager.db.get_system_agent())["user_id"]) / "external-change.json").exists()
    finally:
        await backend_supervisor.stop_all()
        await shutdown()
        manager.shutdown()


async def test_schedule_contract_requires_live_scope_or_verified_human_intent_at_creation(
    client, tmp_path, monkeypatch
):
    from server.mcp_identity import mint
    from server.routers import schedules
    from server.scheduler import ScheduleRunner
    from server.wiki.tasks import tasks

    vault = tmp_path / "vault"
    vault.mkdir()
    await configure(client, vault)
    agent = await wiki_api.session_manager.db.get_system_agent()
    session = await wiki_api.session_manager.create_session(agent["id"])
    monkeypatch.setattr(schedules, "_db", wiki_api.session_manager.db)
    runner = ScheduleRunner(wiki_api.session_manager, wiki_api.session_manager.db)
    monkeypatch.setattr(schedules, "_runner", runner)
    plain = {"Authorization": f"Bearer {mint(session.id, user_id=session.user_id)}"}
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
    assert ordinary.status_code == 403, ordinary.text
    human = await client.post(
        endpoint,
        headers=client.headers,
        json={
            "prompt": "整理指定来源",
            "wiki_mode": "maintain",
            "interval_seconds": 3600,
        },
    )
    assert human.status_code == 201, human.text
    assert human.json()["wiki_mode"] == "maintain"
    saved = await wiki_api.session_manager.db.get_schedule_definition(human.json()["id"])
    assert saved["wiki_mode"] == "maintain"
    token = tasks.open(session, "read")
    try:
        scoped = {"Authorization": f"Bearer {mint(session.id, user_id=session.user_id, wiki_task_id=token)}"}
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
        assert read.status_code == 403, read.text
        # A task-only token cannot invent the committed request/run contract.
        # Real live caller inheritance is exercised by knowledge_schedules.
    finally:
        tasks.close(token)
        await runner.shutdown()


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
        await client.get("/api/wiki/blocks", params={"doc": "bom.md"}, headers=client.headers)
    ).json()
    assert block["blocks"][0]["content"] == frontmatter
    assert "".join(b["content"] for b in block["blocks"]).encode() == original


async def test_interrupted_organization_recovers_owned_sections_and_restore_state(
    client, tmp_path, monkeypatch
):
    from server.service_health import ServiceHealth
    from server.wiki.history import WikiHistory
    from server.wiki.registry import registry

    vault = tmp_path / "vault"
    vault.mkdir()
    original = "# 保全\n原文必须完整保留。\n"
    (vault / "笔记.md").write_text(original)
    await configure(client, vault)
    read = (await client.get("/api/wiki/read?doc=笔记.md", headers=client.headers)).json()
    real_status = WikiHistory.status

    def interrupt_after_write(self, change_id, value):
        if value == "applied":
            raise RuntimeError("simulated interruption after file write")
        return real_status(self, change_id, value)

    monkeypatch.setattr(WikiHistory, "status", interrupt_after_write)
    with pytest.raises(RuntimeError, match="simulated interruption"):
        await client.post(
            "/api/wiki/organize",
            headers=client.headers,
            json={
                "path": "笔记.md",
                "expected_revision": read["revision"],
                "key_note": "原文需要完整保全。",
                "reason": "crash recovery",
            },
        )
    monkeypatch.setattr(WikiHistory, "status", real_status)
    # Simulate the new service generation that owns startup recovery. Ordinary
    # reads and locks in the old generation do not replay prepared effects.
    registry.close()
    registry.bind_health(ServiceHealth())
    recovered = (await client.get("/api/wiki/read?doc=笔记.md", headers=client.headers)).json()
    assert recovered["metadata"]["author"] == "Weber 整理"
    assert recovered["stale"] is False
    history = (
        await client.get("/api/wiki/history?path=笔记.md", headers=client.headers)
    ).json()["items"]
    assert history[0]["status"] == "applied"
    restored = await client.post(
        "/api/wiki/restore",
        headers=client.headers,
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
        headers=client.headers,
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
        headers=client.headers,
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
        headers=client.headers,
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
        headers=client.headers,
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
        await client.get("/api/wiki/blocks", params={"doc": "note.md"}, headers=client.headers)
    ).json()
    current = "# Note\n\nOriginal wording.\n\nHuman addition.\n"
    (vault / "note.md").write_text(current)
    response = await client.post(
        "/api/wiki/restructure",
        headers=client.headers,
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
            "/api/wiki/history", params={"path": "note.md"}, headers=client.headers
        )
    ).json()["items"][0]
    assert entry["proposed_operation"]["order"] == [
        part["id"] for part in old["blocks"]
    ]
    assert "proposed_content" not in entry
    read = (
        await client.get("/api/wiki/read", params={"doc": "note.md"}, headers=client.headers)
    ).json()
    restored = await client.post(
        "/api/wiki/restore",
        headers=client.headers,
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
        await client.get("/api/wiki/blocks", params={"doc": "note.md"}, headers=client.headers)
    ).json()
    source = (
        await client.get("/api/wiki/read", params={"doc": "source.md"}, headers=client.headers)
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
    rejected = await client.post(endpoint, headers=client.headers, json=body)
    assert rejected.status_code == 422, rejected.text
    assert (vault / "note.md").read_text() == original


@pytest.mark.parametrize("move", ["rename", "external-recovery"])
async def test_note_move_preserves_identity_ownership_and_source_relations(
    client, tmp_path, move
):
    import json
    import uuid

    from server.wiki.history import WikiHistory, atomic
    from server.wiki.registry import registry

    vault = tmp_path / "vault"
    (vault / "Library").mkdir(parents=True)
    original = "# Original\n\nKeep the judgement and all original words.\n"
    (vault / "Library/a.md").write_text(original)
    await configure(client, vault)
    first = (
        await client.get(
            "/api/wiki/read", params={"doc": "Library/a.md"}, headers=client.headers
        )
    ).json()
    result = await client.post(
        "/api/wiki/organize",
        headers=client.headers,
        json={
            "path": "Library/a.md",
            "expected_revision": first["revision"],
            "key_note": "Preserve original wording.",
            "reason": "Organize before move",
        },
    )
    assert result.status_code == 200, result.text
    organized = (
        await client.get("/api/wiki/read", params={"doc": first["id"]}, headers=client.headers)
    ).json()
    published = await client.post(
        "/api/wiki/publish",
        headers=client.headers,
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
    store = registry.store((await wiki_api.session_manager.db.get_system_agent())["user_id"])
    history = WikiHistory(store)
    if move == "rename":
        (vault / "Library/a.md").rename(vault / "Library/b.md")
    else:
        with history.locked():
            paths = ["Library/a.md", "Library/b.md"]
            entry = {
                "version": 2,
                "id": uuid.uuid4().hex,
                "source_binding_protocol": 1,
                "before": history.capture_external(paths),
                "paths": paths,
                "actor": "app:sapphire",
                "reason": "rename",
            }
            atomic(store.state / "external-change.json", json.dumps(entry).encode())
            (vault / "Library/b.md").write_bytes((vault / "Library/a.md").read_bytes())
            (vault / "Library/a.md").unlink()
            history.recover()
    moved = (
        await client.get("/api/wiki/read", params={"doc": first["id"]}, headers=client.headers)
    ).json()
    assert moved["path"] == "Library/b.md", moved
    assert moved["id"] == first["id"]
    assert moved["metadata"]["author"] == "Weber 整理"
    assert not moved["stale"]
    source = (
        await client.get(
            "/api/wiki/read", params={"doc": "Library/topic.md"}, headers=client.headers
        )
    ).json()
    assert not source["stale"]
    assert source["metadata"]["sources"][0]["path"] == "Library/b.md"
    second = await client.post(
        "/api/wiki/organize",
        headers=client.headers,
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
            "/api/wiki/read", params={"doc": "Library/b.md"}, headers=client.headers
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
    owner = (await wiki_api.session_manager.db.get_system_agent())["user_id"]
    jobs = WikiJobs(owner)
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
    history = WikiHistory(registry.store(owner))

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
        db=wiki_api.session_manager.db, create_session=stop_after_ingest
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
    jobs = WikiJobs((await wiki_api.session_manager.db.get_system_agent())["user_id"])
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
    worker.manager = wiki_api.session_manager
    if outcome == "failed":

        async def fail(*args):
            raise RuntimeError("controlled worker failure")

        monkeypatch.setattr(worker, "process", fail)
    running = asyncio.create_task(worker.run(jobs.user_id))
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
            "/api/wiki/read", params={"doc": "Library/a.md"}, headers=client.headers
        )
    ).json()
    response = await client.post(
        "/api/wiki/publish",
        headers=client.headers,
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
            "/api/wiki/read", params={"doc": original["id"]}, headers=client.headers
        )
    ).json()
    assert moved["path"] == "Library/b.md"
    (vault / "Library/b.md").unlink()
    (vault / "Library/a.md").write_text(raw)
    replacement = (
        await client.get(
            "/api/wiki/read", params={"doc": "Library/a.md"}, headers=client.headers
        )
    ).json()
    assert replacement["id"] != original["id"]
    refreshed = await client.get(
        "/api/wiki/search", params={"query": "Source"}, headers=client.headers
    )
    assert refreshed.status_code == 200
    topic = (
        await client.get(
            "/api/wiki/read", params={"doc": "Library/topic.md"}, headers=client.headers
        )
    ).json()
    assert topic["metadata"]["sources"][0]["doc"] == original["id"]
    assert topic["stale"] is True
