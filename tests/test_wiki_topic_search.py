"""Independently labeled topic navigation through the real Store and index."""

import uuid

import pytest

from server.wiki.store import WikiStore
from tests import wiki_api

client = wiki_api.client

DOCUMENTS = {
    "one.md": "# Implementation notes\n\n数字人的表情驱动是这项研究的对象，实验比较了两种控制方案。\n",
    "two.md": "# Engine integration\nQuasar++ 用于数字人的实时驱动，研究评估控制延迟。\n",
    "three.md": "# Other runtime\nQuasar# 用于数字人的实时驱动，研究评估控制延迟。\n",
    "four.md": "# Video methods\n实时生成的视频使用蒸馏压缩教师模型。\n",
    "five.md": "# Project report\nVidu 的技术报告讨论缓存。\n",
    "six.md": "# Learning conditions\nFeedback improves retention.\n",
    "generic.md": "# Writing\n论文的一般写作方法与研究计划。\n",
    "letters.md": "# Unrelated notes\n\n数字用于编号。人负责签字。研究普通表格的排版。\n",
    "far.md": "# Separate subjects\n数字人用于展示。" + "另一件事情需要单独记录。" * 80 + "研究纸张的生产。\n",
    "chapters.md": "# Separate chapters\n## Display\n数字人用于展览。\n## Paper\n研究纸张的生产。\n",
    "metadata.md": "# Feedback retention evidence\nInstallation instructions.\n",
}


@pytest.fixture(scope="module")
def topic_store(tmp_path_factory):
    directory = tmp_path_factory.mktemp("topic-search-corpus")
    root = directory / "vault"
    root.mkdir()
    for path, text in DOCUMENTS.items():
        (root / path).write_text(text)
    store = WikiStore(root, directory / "state", str(uuid.uuid4()), create=True)
    try:
        yield store
    finally:
        store.close()


@pytest.mark.parametrize("intent", [None, "question", "overview"])
@pytest.mark.parametrize("view", ["full", "compact"])
@pytest.mark.parametrize("query,expected,unrelated,unrelated_ratio", [
    ("数字人研究", {"one.md", "two.md", "three.md"}, {"far.md"}, 0.25),
    ("Quasar++数字人研究", {"two.md"}, set(), 0),
    ("实时视频蒸馏", {"four.md"}, set(), 0),
    ("那篇 Vidu 论文", {"five.md"}, set(), 0),
])
def test_labeled_topic_recall_has_body_support_and_no_unrelated_top_k(topic_store, query, expected, unrelated, unrelated_ratio, intent, view):
    # Labels come from the prose above, never from the production tokenizer.
    result = topic_store.search(query, intent=intent, view=view)
    paths = [item["path"] for item in result["items"]]
    # Whole concepts in one section can still describe unrelated subjects.
    # Keep that false positive labeled; relevance is not lexical coverage.
    assert set(paths) == expected | unrelated
    assert result["total"] == len(expected | unrelated)
    assert len(set(paths) & unrelated) / len(paths) == unrelated_ratio
    assert all(path in expected for path in paths[:3])  # unrelated@3 = 0
    assert result["match_mode"] == "lexical_partial"
    assert "Complete term coverage returned zero" in result["match_explanation"]
    for hit in result["items"]:
        part = hit["sections"][0]
        evidence = topic_store.read(hit["id"], start=part["start"],
                                    length=part["end"] - part["start"],
                                    expected_revision=hit["revision"])
        assert evidence["content"]
        assert evidence["content"] in DOCUMENTS[hit["path"]]
        if hit["path"] == "one.md":
            assert "数字人的表情驱动是这项研究的对象" in evidence["content"]
        if view == "full":
            assert hit["section"] == part
        assert hit["path"] in expected | unrelated


@pytest.mark.parametrize("intent", ["lookup", "compare"])
def test_topic_relaxation_does_not_change_other_intents(topic_store, intent):
    result = topic_store.search("数字人研究", intent=intent)
    assert result["total"] == 0 and result["items"] == []
    assert result["match_mode"] != "lexical_partial"


@pytest.mark.parametrize("query", ["虚拟人物调研", "数字人调研", "feedback retention missing evidence"])
def test_lexical_topic_recall_does_not_invent_synonyms_or_drop_multiple_terms(topic_store, query):
    assert topic_store.search(query)["items"] == []


@pytest.mark.parametrize("view", ["full", "compact"])
def test_relaxed_pages_keep_the_same_filtered_total_and_explanation(topic_store, view):
    first = topic_store.search("数字人研究", view=view, limit=1)
    second = topic_store.search("数字人研究", view=view, limit=1, cursor=first["next_cursor"])
    empty = topic_store.search("数字人研究", view=view, limit=1, offset=100)
    assert first["total"] == second["total"] == empty["total"] == 4
    assert first["items"][0]["path"] != second["items"][0]["path"]
    assert empty["items"] == [] and empty["next_cursor"] is None
    assert first["match_explanation"] == second["match_explanation"] == empty["match_explanation"]
    assert topic_store.search("数字人研究", directory="missing")["total"] == 0


@pytest.mark.parametrize("view", ["full", "compact"])
def test_complete_coverage_and_empty_pages_do_not_trigger_relaxation(topic_store, view):
    # Full coverage can span chapters. It remains distinct from a phrase and
    # prevents topic relaxation even when the requested page is empty.
    result = topic_store.search("数字人 研究", view=view, offset=100)
    assert result["total"] > 0 and result["items"] == []
    assert result["match_mode"] == "lexical"
    assert "not necessarily a contiguous phrase" in result["match_explanation"]
    complete = topic_store.search("数字人 研究", view=view)
    assert "chapters.md" in {item["path"] for item in complete["items"]}


def test_ordered_name_and_hyphen_equivalent_rank_before_scattered_full_coverage(tmp_path):
    root = tmp_path / "vault"
    root.mkdir()
    (root / "phrase.md").write_text("# Distribution alignment\nSelf Forcing matches the student rollout distribution.\n")
    (root / "hyphen.md").write_text("# Trajectory training\nSelf-Forcing uses generated trajectories for training.\n")
    (root / "scattered.md").write_text("# Self reference\nSelf correction precedes teacher forcing in this unrelated exercise.\n")
    store = WikiStore(root, tmp_path / "state", str(uuid.uuid4()), create=True)
    try:
        result = store.search("Self Forcing")
        paths = [item["path"] for item in result["items"]]
        assert set(paths[:2]) == {"phrase.md", "hyphen.md"}
        assert "scattered.md" in paths  # Complete coverage stays discoverable.
        assert result["match_mode"] == "lexical"
    finally:
        store.close()


@pytest.mark.parametrize("query,text,expected_body", [
    ("Nimbus 推理复现研究",
     "# Nimbus\n接口和权重说明。\n## 推理复现的验收\n比较相同输入的逐帧数值，固定像素采样与后处理条件。\n",
     "比较相同输入的逐帧数值"),
    ("代码执行协议",
     "# Runtime interfaces\n代码工具通过双向协议发送请求，由隔离进程执行命令并回传状态。\n",
     "隔离进程执行命令"),
], ids=["named-subsection", "separated-compound"])
def test_object_identity_and_subsection_context_support_natural_topics(tmp_path, query, text, expected_body):
    root = tmp_path / "vault"
    root.mkdir()
    (root / "supported.md").write_text(text)
    (root / "other.md").write_text("# Other engine\n## 推理复现的验收\n比较数值并固定采样。\n")
    store = WikiStore(root, tmp_path / "state", str(uuid.uuid4()), create=True)
    try:
        result = store.search(query)
        assert [hit["path"] for hit in result["items"]] == ["supported.md"]
        hit = result["items"][0]
        part = hit["section"]
        assert expected_body in store.read(hit["id"], start=part["start"],
                                            length=part["end"] - part["start"],
                                            expected_revision=hit["revision"])["content"]
    finally:
        store.close()


def test_frontmatter_title_does_not_turn_document_heading_into_body_support(tmp_path):
    root = tmp_path / "vault"
    root.mkdir()
    (root / "metadata.md").write_text(
        "---\ntitle: Installation manual\n---\n# Feedback retention tool\nInstallation instructions.\n"
    )
    store = WikiStore(root, tmp_path / "state", str(uuid.uuid4()), create=True)
    try:
        assert store.search("feedback retention evidence")["items"] == []
    finally:
        store.close()


@pytest.mark.parametrize("heading,role", [("相关笔记", "related_notes"), ("整理说明", "curator_note")])
def test_auxiliary_subheadings_do_not_supply_relaxed_body_context(tmp_path, heading, role):
    root = tmp_path / "vault"
    root.mkdir()
    (root / "auxiliary.md").write_text(
        f"# Other subject\nUnrelated argument.\n## {heading}\n### Feedback retention\nSee another note.\n"
    )
    (root / "supported.md").write_text(
        "# Learning conditions\n## Feedback retention\nCompare results under the same spacing conditions.\n"
    )
    store = WikiStore(root, tmp_path / "state", str(uuid.uuid4()), create=True)
    try:
        auxiliary = store.read("auxiliary.md")
        assert any(part["title"] == "Feedback retention" and part["role"] == role
                   for part in auxiliary["metadata"]["body_sections"])
        with store.connect() as db:
            section = db.execute(
                "SELECT section_role,readable_body FROM search_sections WHERE doc_id=? AND title=?",
                (auxiliary["id"], "Feedback retention"),
            ).fetchone()
        assert tuple(section) == (role, 0)
        for intent in [None, "question", "overview"]:
            for view in ["full", "compact"]:
                result = store.search("feedback retention evidence", intent=intent, view=view)
                assert [item["path"] for item in result["items"]] == ["supported.md"]
                assert result["match_mode"] == "lexical_partial"
        # Complete term coverage and exact identities keep their original
        # metadata/navigation semantics; only relaxed body support is gated.
        complete = store.search("feedback retention")
        assert auxiliary["id"] in {item["id"] for item in complete["items"]}
        assert complete["match_mode"] == "lexical"
        for identity in ["auxiliary.md", auxiliary["id"]]:
            assert store.search(identity, intent="lookup")["items"][0]["id"] == auxiliary["id"]
    finally:
        store.close()


def test_overlapping_dictionary_words_keep_the_remaining_topic_and_later_concepts(tmp_path):
    root = tmp_path / "vault"
    root.mkdir()
    (root / "supported.md").write_text("# Findings\n中国科学院的研究比较了两种实验设计。\n")
    (root / "noise.md").write_text("# Other context\n中国科学史介绍古代器物。\n")
    store = WikiStore(root, tmp_path / "state", str(uuid.uuid4()), create=True)
    try:
        assert [item["path"] for item in store.search("中国科学院研究")["items"]] == ["supported.md"]
    finally:
        store.close()


@pytest.mark.parametrize("padding,recalled", [("english padding " * 100, True), ("english padding " * 800, False)],
                         ids=["within-original-window", "beyond-original-window"])
def test_relaxed_scope_uses_original_text_windows_and_discloses_lexical_limits(tmp_path, padding, recalled):
    root = tmp_path / "vault"
    root.mkdir()
    text = "# Separate subjects\n数字人用于展示。" + padding + "研究纸张的生产。\n"
    (root / "note.md").write_text(text)
    store = WikiStore(root, tmp_path / "state", str(uuid.uuid4()), create=True)
    try:
        result = store.search("数字人研究")
        assert bool(result["items"]) is recalled
        assert "6000 original characters" in result["match_explanation"]
        if recalled:
            part = result["items"][0]["section"]
            content = text[part["start"]:part["end"]]
            assert len(content) <= 6000 and "数字人" in content and "研究" in content
            assert "semantic relevance" in result["match_explanation"]
    finally:
        store.close()


async def test_search_http_preserves_relaxation_explanation_and_current_body(client, tmp_path):
    root = tmp_path / "vault"
    root.mkdir()
    (root / "one.md").write_text(DOCUMENTS["one.md"])
    await wiki_api.configure(client, root)
    response = await client.get("/api/wiki/search", params={"query": "数字人研究", "view": "compact"})
    assert response.status_code == 200
    page = response.json()
    assert page["total"] == 1 and page["match_mode"] == "lexical_partial"
    assert "body section" in page["match_explanation"]
    assert page["items"][0]["path"] == "one.md"
