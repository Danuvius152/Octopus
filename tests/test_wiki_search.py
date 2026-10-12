"""Observable lexical relevance, current evidence and derived-index recovery."""

import json
import uuid
from pathlib import Path

import pytest

from server.wiki.history import WikiHistory
from server.wiki.store import WikiError, WikiStore
from tests import test_wiki_api as wiki_api

HEADERS = wiki_api.HEADERS
client = wiki_api.client
configure = wiki_api.configure

DOCUMENTS = {
    "Library/Research/双链.md": "---\naliases: [双向链接, bi-directional links]\ntags: [知识组织]\n---\n# 双链\n\n## Key note\n双链让笔记之间的关系可追踪。\n\n## 来源核验\n双链支持来源核验，反向关联从索引得到。\n",
    "Library/Research/学习方法.md": "# 学习方法\n\n"
    + "练习应该配合反馈。\n" * 1800
    + "\n## 检索练习的条件\nRetrieval practice improves retention when feedback follows recall.\n检索练习需要延迟反馈。\n",
    "Library/Research/机器学习.md": "---\naliases: [SSL, 自监督学习]\n---\n# Self-supervised learning\n\n自监督学习使用数据自身构造监督信号。\n",
    "Library/Research/人机交互.md": "---\naliases: [HCI, human-computer interaction]\n---\n# 人机交互\n\nHCI 研究交互中的可用性。\n",
    "Library/Research/泛泛关联.md": "# 一般笔记\n学习、数学、AI 与双向。没有具体研究关系。\n",
    "Library/Research/噪声.md": "# 双字与链条\n双字不是双链。研究时不要只看链条。\n",
    "Library/Me/information.md": "# 个人信息\n用户的职业是合成测试工程师。\n",
    "Codex Skills/demo/SKILL.md": "# 双链\n双链工具的操作说明。\n",
}
CASES = [
    ("双链", "Library/Research/双链.md"),
    ("双链如何核验来源？", "Library/Research/双链.md"),
    ("双向链接", "Library/Research/双链.md"),
    ("bi-directional links", "Library/Research/双链.md"),
    ("Retrieval practice", "Library/Research/学习方法.md"),
    ("检索练习需要什么反馈？", "Library/Research/学习方法.md"),
    ("自监督学习", "Library/Research/机器学习.md"),
    ("SSL", "Library/Research/机器学习.md"),
    ("self supervised learning", "Library/Research/机器学习.md"),
    ("HCI 的可用性", "Library/Research/人机交互.md"),
    ("HCI可用性", "Library/Research/人机交互.md"),
    ("HCI的可用性", "Library/Research/人机交互.md"),
    ("human-computer interaction", "Library/Research/人机交互.md"),
    ("Library/Research/学习方法.md", "Library/Research/学习方法.md"),
]


@pytest.mark.parametrize("query", ["vidu实时报告", "Vidu实时报告", "Ｖｉｄｕ实时报告"])
def test_mixed_script_terms_do_not_require_spaces(store, query):
    path = store.root / "vidu.md"
    path.write_text("# Vidu 实时报告\nVidu 的实时视频实验与报告。\n")
    result = store.search(query)
    assert result["items"][0]["path"] == "vidu.md"
    assert result["query_terms"] == ["vidu", "实时报告"]


@pytest.mark.parametrize("query,noise", [
    ("数字人", "数字是测量指标。\n## 参与者\n人负责记录。"),
    ("自监督学习", "自己设置条件。\n## 监督\n监督不同实验。\n## 学习\n学习别的方法。"),
])
def test_bare_chinese_concept_does_not_match_disconnected_known_words(store, query, noise):
    (store.root / "碎片.md").write_text("# 无关材料\n" + noise)
    assert not any(item["path"] == "碎片.md" for item in store.search(query)["items"])
    (store.root / "完整术语.md").write_text("# 方法\n这里讨论" + query + "的具体条件。\n")
    hit = next(item for item in store.search(query)["items"] if item["path"] == "完整术语.md")
    assert query in store.read(hit["id"], start=hit["section"]["start"], length=hit["section"]["end"] - hit["section"]["start"])["content"]


def test_partial_question_retains_distinctive_indexed_object(store):
    (store.root / "vidu.md").write_text("# Vidu 研究\nVidu 的技术报告。\n")
    (store.root / "泛泛论文.md").write_text("# 论文讨论\n论文的一般写作方法。\n")
    result = store.search("那篇 Vidu 论文")
    assert result["match_mode"] == "lexical_partial"
    assert result["items"][0]["path"] == "vidu.md"
    assert {item["path"] for item in result["items"]} == {"vidu.md", "泛泛论文.md"}


def test_partial_english_terms_keep_more_relevant_body_candidates(store):
    (store.root / "research.md").write_text("# Learning conditions\nFeedback improves retention.\n")
    (store.root / "tool.md").write_text("# Feedback tool\nInstallation instructions.\n")
    result = store.search("feedback retention evidence")
    assert result["match_mode"] == "lexical_partial"
    paths = [item["path"] for item in result["items"]]
    assert paths[0] == "research.md"
    assert paths.index("research.md") < paths.index("tool.md")
    assert "Library/Research/学习方法.md" in paths


def test_equal_total_coverage_prefers_body_evidence_before_latin_title_signal(store):
    (store.root / "research.md").write_text("# Spacing study\nFeedback improves retention.\n")
    (store.root / "tool.md").write_text("# Feedback retention tool\nInstallation instructions.\n")
    result = store.search("feedback retention evidence")
    assert result["match_mode"] == "lexical_partial"
    paths = [item["path"] for item in result["items"]]
    assert paths[0] == "research.md" and paths.index("research.md") < paths.index("tool.md")
    assert "Library/Research/学习方法.md" in paths


def test_missing_fts_rows_are_repaired_before_publication_is_indexed(store):
    (store.root / "note.md").write_text("# Evidence\nUNIQUEFTSCHECK is recorded.\n")
    document = store.read("note.md")
    with store.connect() as db:
        db.execute("DELETE FROM search_fts")
    with store.connect() as db:
        store.require_indexed(db, "note.md")
    assert store.search("UNIQUEFTSCHECK")["items"][0]["id"] == document["id"]


def test_refresh_repairs_missing_fts_for_unchanged_files(store):
    (store.root / "note.md").write_text("# Evidence\nUNIQUEFTSREFRESH is recorded.\n")
    store.refresh()
    with store.connect() as db:
        db.execute("DELETE FROM search_fts")
    assert store.search("UNIQUEFTSREFRESH")["items"][0]["path"] == "note.md"


def test_directory_scope_is_literal_and_applied_before_ranking_and_pagination(store):
    selected = store.root / "Library/Me/nested/概念.md"
    selected.parent.mkdir(parents=True)
    selected.write_text("# 双链\n个人材料中的双链观点。\n")
    result = store.search("双链", directory="Library/Me", limit=1)
    assert result["total"] == 1 and result["next_offset"] is None
    assert result["items"][0]["path"] == "Library/Me/nested/概念.md"
    assert store.search("双链", directory="Library/M%") ["items"] == []


def populate(vault):
    for relative, content in DOCUMENTS.items():
        path = vault / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)


@pytest.fixture
def store(tmp_path):
    root = tmp_path / "vault"
    root.mkdir()
    populate(root)
    return WikiStore(root, tmp_path / "state", str(uuid.uuid4()))


@pytest.mark.parametrize("query,expected", CASES)
def test_relevance_and_current_chapter_evidence(store, query, expected):
    result = store.search(query)
    assert result["index_status"] == "ready"
    assert result["items"][0]["path"] == expected
    hit = result["items"][0]
    section = hit["section"]
    evidence = store.read(
        hit["id"],
        start=section["start"],
        length=section["end"] - section["start"],
        expected_revision=hit["revision"],
    )
    assert evidence["line_start"] == section["line_start"]
    assert evidence["line_end"] == section["line_end"]
    assert evidence["content"] == DOCUMENTS[expected][section["start"] : section["end"]]
    if "反馈" in query or "Retrieval" in query:
        assert section["start"] > 16000
        assert "feedback" in evidence["content"]


def test_whole_term_fallback_does_not_match_arbitrary_chinese_fragments(store):
    assert store.search("超弦拓扑")["items"] == []
    assert store.search("双向链接")["items"][0]["path"] == "Library/Research/双链.md"


def test_key_note_hit_reads_the_matching_section_instead_of_short_appendix(store):
    (store.root / "摘要长文.md").write_text(
        "# 研究长文\n## Key note\n量子退相干说明环境耦合的影响。\n"
        + "这里讨论实验条件与测量限制。\n" * 500
        + "## appendix\n附件无相关论述。\n"
    )
    hit = store.search("量子退相干")["items"][0]
    assert hit["path"] == "摘要长文.md"
    part = hit["section"]
    evidence = store.read(hit["id"], start=part["start"], length=part["end"] - part["start"], expected_revision=hit["revision"])
    assert part["title"] == "Key note"
    assert "量子退相干" in evidence["content"]


@pytest.mark.parametrize("text", ["瑟是一种乐器，缇是一种颜色。", "瑟是一种乐器。", "缇是一种颜色。"])
def test_unknown_chinese_name_does_not_match_disconnected_characters(store, text):
    (store.root / "名称噪声.md").write_text("# 名称\n" + text)
    assert store.search("瑟缇")["items"] == []
    (store.root / "真正名称.md").write_text("# 游戏人物\n这里介绍瑟缇的技能。\n")
    hit = store.search("瑟缇")["items"][0]
    assert hit["path"] == "真正名称.md"
    assert "瑟缇" in store.read(hit["id"], start=hit["section"]["start"], length=hit["section"]["end"] - hit["section"]["start"])["content"]


@pytest.mark.parametrize("term", ["abcdefgh", "自监督学习", "瑟缇"])
def test_long_unbroken_paragraph_keeps_terms_across_chunk_boundary(store, term):
    text = "# A\n" + "a " * 2996 + term + " end"
    (store.root / "边界.md").write_text(text)
    hit = next(item for item in store.search(term)["items"] if item["path"] == "边界.md")
    assert hit["path"] == "边界.md"
    part = hit["section"]
    evidence = store.read(hit["id"], start=part["start"], length=part["end"] - part["start"], expected_revision=hit["revision"])
    assert term in evidence["content"]
    assert evidence["content"] == text[part["start"] : part["end"]]


@pytest.mark.parametrize("title", ["比较研究", "Retrieval Interleaving comparison"])
def test_comparison_covers_query_groups_across_chapters_before_pagination(store, title):
    (store.root / "比较研究.md").write_text(
        f"# {title}\n## Retrieval\nRetrieval improves recall.\n"
        "## Interleaving\nInterleaving contrasts exercises.\n"
    )
    (store.root / "词汇表.md").write_text("# 词汇表\nRetrieval Interleaving are words.\n")
    result = store.search("Retrieval Interleaving")
    assert result["match_mode"] == "lexical"
    assert {item["path"] for item in result["items"]} == {"比较研究.md", "词汇表.md"}
    hit = next(item for item in result["items"] if item["path"] == "比较研究.md")
    assert len(hit["sections"]) == 2
    read = [store.read(hit["id"], start=part["start"], length=part["end"] - part["start"], expected_revision=hit["revision"])["content"] for part in hit["sections"]]
    assert any("Retrieval" in text for text in read)
    assert any("Interleaving" in text for text in read)
    pages = [store.search("Retrieval Interleaving", limit=1, offset=offset) for offset in range(3)]
    assert all(page["total"] == 2 for page in pages)
    assert len({item["id"] for page in pages for item in page["items"]}) == 2


def test_document_deduplication_precedes_pagination(store):
    long = store.root / "多章节.md"
    long.write_text(
        "# 多章节\n"
        + "\n".join(f"## 双链章节 {n}\n双链章节的细节。" for n in range(40))
    )
    result = store.search("双链", limit=1)
    paths = []
    offset = 0
    while True:
        page = store.search("双链", limit=1, offset=offset)
        paths.extend(item["path"] for item in page["items"])
        if page["next_offset"] is None:
            break
        offset = page["next_offset"]
    assert len(paths) == len(set(paths)) == result["total"]
    assert store.search("双链", offset=100)["total"] == result["total"]


def test_incremental_search_does_not_parse_or_read_unreturned_documents(
    store, monkeypatch
):
    store.refresh()
    original = Path.read_bytes
    reads = []

    def watch(path):
        if path.is_relative_to(store.root):
            reads.append(path.relative_to(store.root).as_posix())
        return original(path)

    monkeypatch.setattr(Path, "read_bytes", watch)
    result = store.search("SSL", limit=1)
    assert result["items"][0]["path"] == "Library/Research/机器学习.md"
    assert reads == ["Library/Research/机器学习.md"]


def test_directory_filter_and_pagination_only_read_the_requested_page(store, monkeypatch):
    for name in ("a", "b", "c"):
        (store.root / "Library/Me" / f"{name}.md").write_text(f"# {name}\n合成个人材料。\n")
    (store.root / "Library/Me_too").mkdir()
    (store.root / "Library/Me_too/ignored.md").write_text("# A\n目录外。\n")
    store.refresh()
    reads = []
    original = Path.read_bytes

    def watch(path):
        if path.is_relative_to(store.root):
            reads.append(path.relative_to(store.root).as_posix())
        return original(path)

    monkeypatch.setattr(Path, "read_bytes", watch)
    page = store.documents(directory="Library/Me", limit=1, offset=1)
    assert page["total"] == 4
    assert page["next_offset"] == 2
    assert reads == [page["items"][0]["path"]] == ["Library/Me/b.md"]
    assert store.documents(directory="Library/Me", limit=1, offset=10)["total"] == 4
    assert reads == ["Library/Me/b.md"]


def test_document_source_filters_follow_managed_override_and_current_identity(store):
    source = store.search("SSL")["items"][0]
    note = store.root / "引用.md"
    note.write_text("# 引用\n正文。\n<!-- octopus-wiki " + json.dumps({"sources": [{"doc": "native.txt", "path": "native.txt", "revision": "missing", "location": "text"}]}) + " -->\n")
    store.refresh()
    assert store.documents(source="native.txt")["items"][0]["path"] == "引用.md"
    with store.connect() as db:
        db.execute("INSERT INTO managed_notes VALUES(?,?,?,?,?)", (
            "引用.md", "base", "rendered", "[]",
            json.dumps({"sources": [{"doc": source["id"], "revision": source["revision"]}]}),
        ))
    assert store.documents(source="native.txt")["items"] == []
    assert store.documents(source=source["id"])["total"] == 1
    moved = store.root / "移动后的来源.md"
    (store.root / source["path"]).rename(moved)
    assert store.documents(source="移动后的来源.md")["items"][0]["path"] == "引用.md"


def test_current_revision_and_direct_rename_delete_refresh(store):
    initial = store.search("HCI")["items"][0]
    path = store.root / initial["path"]
    moved = path.with_name("交互改名.md")
    path.rename(moved)
    hit = store.search("HCI")["items"][0]
    assert hit["id"] == initial["id"]
    assert hit["path"].endswith("交互改名.md")
    moved.write_text("# 新内容\n工作记忆容量的限制。\n")
    with pytest.raises(WikiError, match="changed"):
        store.read(hit["id"], expected_revision=hit["revision"])
    assert store.search("HCI")["items"] == []
    assert store.search("工作记忆")["items"][0]["id"] == hit["id"]
    moved.unlink()
    assert store.search("工作记忆")["items"] == []


def test_rebuilding_only_derived_tables_preserves_identity_ownership_and_history(store):
    history = WikiHistory(store)
    hit = store.search("SSL")["items"][0]
    with store.connect() as db:
        db.execute(
            "INSERT INTO managed_notes VALUES(?,?,?,?,?)",
            (
                hit["path"],
                hit["revision"],
                hit["revision"],
                "[]",
                json.dumps({"tags": ["可靠标签"]}),
            ),
        )
    history.record(hit["path"], None, b"original", "human", "preserve audit", "applied")
    store.rebuild_search()
    assert store.search("SSL")["items"][0]["id"] == hit["id"]
    assert store.search("可靠标签")["items"][0]["path"] == hit["path"]
    assert history.history(hit["path"])["items"][0]["reason"] == "preserve audit"
    with store.connect() as db:
        assert db.execute("SELECT count(*) FROM managed_notes").fetchone()[0] == 1
        db.execute("DROP TABLE search_fts")
    reconstructed = WikiStore(store.root, store.state, store.vault_id)
    assert reconstructed.search("SSL")["items"][0]["id"] == hit["id"]


def test_extraction_failure_is_reported_separately_from_absence(store, monkeypatch):
    import server.wiki.store as module

    def broken(*args):
        raise ValueError("Controlled extractor failure")

    monkeypatch.setattr(module, "extract", broken)
    (store.root / "broken.txt").write_text("evidence")
    result = store.search("完全不存在的概念")
    assert result["index_status"] == "partial"
    assert any(
        error["path"] == "broken.txt" and "Controlled" in error["error"]
        for error in result["index_errors"]
    )


async def test_personal_directory_and_search_api_preserve_current_evidence(
    client, tmp_path
):
    vault = tmp_path / "vault"
    vault.mkdir()
    populate(vault)
    await configure(client, vault)
    located = await client.get(
        "/api/wiki/documents", params={"directory": "Library/Me"}, headers=HEADERS
    )
    assert located.status_code == 200
    assert located.json()["total"] == 1
    doc = located.json()["items"][0]
    read = await client.get(
        "/api/wiki/read", params={"doc": doc["id"]}, headers=HEADERS
    )
    assert "合成测试工程师" in read.json()["content"]
    assert "研究方向" not in read.json()["content"]
    response = await client.get(
        "/api/wiki/search", params={"query": "HCI"}, headers=HEADERS
    )
    assert response.status_code == 200, response.text
    assert response.json()["items"][0]["section"]["start"] >= 0
    assert response.json()["match_mode"] == "lexical"
