"""Explicit summaries cannot hide readable current sources or invent identity."""

import json
import os
from pathlib import Path

import pytest

from server.wiki.schema import VERSION as DURABLE_SCHEMA_VERSION
from server.wiki.store import WikiError, WikiStore
from tests import test_wiki_api as api
from tests import test_wiki_identity as identity_tests

HEADERS = api.HEADERS
client = api.client
history = identity_tests.history
INTENTS = ["question", "overview", "compare"]


@pytest.fixture
def store(history):
    return history.store


def write_note(store, relative, content):
    path = store.root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def represented_pair(store, source_path="alpha.md", source=None, summaries=2):
    write_note(
        store,
        source_path,
        source if source is not None else
        "---\ntype: Reference\n---\n# Alpha realtime study\n"
        "Alpha realtime uses video distillation under fixed frame length.\n",
    )
    for index in range(summaries):
        write_note(
            store,
            f"Summaries/summary-{index:02}.md",
            "---\ntype: Synthesis\n"
            f"x_wiki: {{representation_of: {source_path}}}\n---\n"
            f"# Realtime summary {index}\nRealtime uses video distillation.\n",
        )
    store.refresh()
    return store.read(source_path)


@pytest.mark.parametrize("intent", INTENTS)
@pytest.mark.parametrize("view", ["full", "compact"])
async def test_twenty_summaries_preserve_original_other_direction_and_comparison(
    client, tmp_path, intent, view,
):
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "alpha.md").write_text(
        "---\ntype: Reference\ntags: [distillation]\n---\n"
        "# Alpha realtime study\nAlpha realtime uses video distillation under "
        "fixed frame length. This is an original report.\n"
    )
    (vault / "beta.md").write_text(
        "---\ntype: Reference\ntags: [reconstruction]\n---\n"
        "# Beta realtime study\nBeta realtime reconstructs a monocular Rig "
        "under sufficient calibration accuracy. This is another original report.\n"
    )
    (vault / "comparison.md").write_text(
        "---\ntype: Synthesis\nsources: [{resource: alpha.md}, "
        "{resource: beta.md}]\n---\n# Comparison\nAlpha realtime and "
        "Beta realtime answer different research problems.\n"
    )
    await api.configure(client, vault)
    alpha = (await client.get(
        "/api/wiki/read", params={"doc": "alpha.md"}, headers=HEADERS,
    )).json()
    params = {"query": "realtime", "intent": intent, "view": view, "limit": 3}
    baseline = await client.get("/api/wiki/search", params=params, headers=HEADERS)
    assert baseline.status_code == 200, baseline.text
    assert {item["path"] for item in baseline.json()["items"]} == {
        "alpha.md", "beta.md", "comparison.md",
    }
    for index in range(20):
        (vault / f"summary-{index:02}.md").write_text(
            "---\ntype: Synthesis\ntags: [distillation]\n"
            "x_wiki: {representation_of: alpha.md}\n---\n"
            f"# Realtime summary {index}\nRealtime uses video distillation.\n"
        )
    from server.wiki.registry import registry

    registry.store(None).refresh()
    with registry.store(None).connect() as db:
        assert db.execute(
            "SELECT count(*) FROM wiki_edges WHERE relation='representation_of' "
            "AND target_doc=?", (alpha["id"],),
        ).fetchone()[0] == 20
    response = await client.get("/api/wiki/search", params=params, headers=HEADERS)
    assert response.status_code == 200, response.text
    found = response.json()
    assert found["total"] == 3
    assert {item["path"] for item in found["items"]} == {
        "alpha.md", "beta.md", "comparison.md",
    }
    hit = next(item for item in found["items"] if item["path"] == "alpha.md")
    assert hit["id"] == alpha["id"] and hit["revision"] == alpha["revision"]
    part = hit["sections"][0]
    read = await client.get(
        "/api/wiki/read", headers=HEADERS,
        params={"doc": hit["id"], "expected_revision": hit["revision"],
                "start": part["start"], "length": part["end"] - part["start"]},
    )
    assert read.status_code == 200, read.text
    assert "under fixed frame length" in read.json()["content"]


@pytest.mark.parametrize("intent", INTENTS)
def test_source_returns_its_own_score_sections_and_lookup_identity(store, intent):
    alpha = represented_pair(store, summaries=20)
    lookup = store.search("realtime", intent="lookup", limit=30)
    lexical_source = next(item for item in lookup["items"] if item["id"] == alpha["id"])
    found = store.search("realtime", intent=intent)["items"]
    assert len(found) == 1
    hit = found[0]
    assert hit["id"] == alpha["id"] and hit["revision"] == alpha["revision"]
    assert hit["score"] == lexical_source["score"]
    assert hit["section"] == lexical_source["section"]
    assert hit["snippet"] == lexical_source["snippet"]
    for path in ["alpha.md", "Summaries/summary-00.md"]:
        exact = store.search(path, intent="lookup", view="compact")
        assert exact["total"] == 1 and exact["items"][0]["path"] == path
        document = store.read(path)
        assert store.search(document["id"], intent="lookup")["items"][0]["path"] == path


@pytest.mark.parametrize("intent", INTENTS)
def test_limit_one_pages_have_same_set_and_old_epoch_is_rejected(store, intent):
    alpha = represented_pair(store, summaries=20)
    write_note(store, "beta.md", "# Beta study\nRealtime uses a calibrated Rig.\n")
    write_note(store, "comparison.md", "# Comparison\nRealtime studies differ.\n")
    store.refresh()
    complete = store.search("realtime", intent=intent)
    pages = []
    page = store.search("realtime", intent=intent, limit=1)
    first_cursor = page["next_cursor"]
    assert first_cursor
    while True:
        assert page["total"] == 3
        pages.extend(item["id"] for item in page["items"])
        if page["next_cursor"] is None:
            break
        page = store.search("realtime", intent=intent, limit=1, cursor=page["next_cursor"])
    assert len(pages) == len(set(pages)) == 3
    assert set(pages) == {item["id"] for item in complete["items"]}
    assert alpha["id"] in pages
    write_note(store, "alpha.md", "# Alpha study\nRealtime still requires fixed frame length.\n")
    store.refresh()
    with pytest.raises(WikiError) as error:
        store.search("realtime", intent=intent, limit=1, cursor=first_cursor)
    assert error.value.status == 409


@pytest.mark.parametrize("intent", INTENTS)
@pytest.mark.parametrize("case", ["nonmatching", "directory", "kind", "asset", "template", "source_snapshot"])
def test_filtered_or_nonmatching_source_falls_back_to_summary(store, intent, case):
    source_path = "Library/assets/alpha.md" if case == "asset" else "Library/alpha.md"
    source = None
    if case == "nonmatching":
        source = "---\ntype: Reference\n---\n# Alpha study\nUnrelated evidence.\n"
    elif case == "template":
        source = "---\ntype: Template\n---\n# Alpha study\nRealtime template.\n"
    elif case == "source_snapshot":
        source = "---\ntype: SourceSnapshot\n---\n# Alpha study\nRealtime snapshot.\n"
    alpha = represented_pair(store, source_path, source)
    filters = {"directory": "Summaries"} if case == "directory" else {"kind": "topic"} if case == "kind" else {}
    result = store.search("realtime", intent=intent, **filters)
    assert result["total"] == 1 and len(result["items"]) == 1
    fallback = result["items"][0]
    assert fallback["path"].startswith("Summaries/") and fallback["id"] != alpha["id"]
    current = store.read(fallback["id"])
    assert fallback["revision"] == current["revision"]
    assert "fixed frame length" not in fallback["snippet"]
    assert store.search("realtime", intent=intent, **filters)["items"][0]["id"] == fallback["id"]
    if case == "asset":
        explicit = store.search("realtime", intent=intent, include_assets=True)
        assert explicit["total"] == 1 and explicit["items"][0]["id"] == alpha["id"]


@pytest.mark.parametrize("quality", ["error", "excluded"])
def test_index_quality_excludes_unreadable_source_without_disguising_summary(store, quality):
    alpha = represented_pair(store)
    # Model extraction/index recovery states, even if old lexical rows remain.
    with store.connect() as db:
        db.execute("UPDATE documents SET quality=? WHERE id=?", (quality, alpha["id"]))
    result = store.search("realtime", intent="question")
    assert result["total"] == 1
    fallback = result["items"][0]
    assert fallback["id"] != alpha["id"] and fallback["path"].startswith("Summaries/")
    assert fallback["revision"] == store.read(fallback["id"])["revision"]


def test_metadata_match_without_available_body_falls_back_to_summary(store):
    alpha = represented_pair(
        store, source="---\ntype: Reference\ntitle: Alpha realtime\n---\n \t\n",
    )
    result = store.search("realtime", intent="question")
    assert result["total"] == 1 and result["items"][0]["id"] != alpha["id"]
    assert result["items"][0]["path"].startswith("Summaries/")


@pytest.mark.parametrize("intent", INTENTS)
@pytest.mark.parametrize("view", ["compact", "full"])
@pytest.mark.parametrize("body", [
    "# Alpha realtime\n\n",
    "Alpha realtime\n==============\n\n",
    "<!-- report pending -->\n",
    "## 相关笔记\n[[other]]\n",
    "## 整理说明\nRealtime bookkeeping only.\n",
    "## 整理说明\n```text\nRealtime bookkeeping only.\n```\n",
    "# Alpha realtime " + "title " * 2500 + "\n",
    "<!-- " + "Realtime pending " * 1000 + " -->\n",
    "<h1>Alpha realtime</h1>\n<div> </div>\n",
    "<span><script>Realtime secret.</script></span>\n",
    "<span><style>Realtime hidden.</style></span>\n",
    "<noscript>Realtime hidden.</noscript>\n",
    "<span><noscript>`Realtime literal`</noscript></span>\n",
    "<span><noscript>![Realtime](diagram.png)</noscript></span>\n",
    "# ![Realtime](diagram.png)\n",
    "<h1><img src=\"diagram.png\"></h1>\n",
    "<head><title>Realtime metadata.</title></head>\n",
    "<noscript>\n\n```text\nRealtime secret.\n```\n\n</noscript>\n",
    "<noscript>\n\n    Realtime secret.\n\n</noscript>\n",
    "<noscript>\n\n## Hidden title\n\n```text\nRealtime secret.\n```\n\n</noscript>\n",
    "<noscript>\n\n```text\n" + "Realtime secret. " * 1200 + "\n```\n\n</noscript>\n",
    "<h2>整理说明</h2>\n<p>Realtime bookkeeping only.</p>\n",
    "<h2>相关笔记</h2>\n<p>Realtime links only.</p>\n",
    "<h2>整理说明</h2>\n\n### Child\n\n```text\nRealtime bookkeeping.\n```\n![](diagram.png)\n",
    "## 相关笔记\n\n> ## 正文\n> Realtime linked note.\n\nRealtime bookkeeping.\n",
    "## 整理说明\n\n- ## 正文\n  Realtime linked note.\n\nRealtime bookkeeping.\n",
    " \t\n",
], ids=["atx-title", "setext-title", "comment", "related", "curator", "curator-code",
        "long-title", "long-comment", "html-title", "inline-script", "inline-style",
        "noscript", "hidden-inline-code", "hidden-markdown-image", "heading-image",
        "html-heading-image", "html-head", "hidden-fence", "hidden-indented-code",
        "hidden-heading-fence", "hidden-long-fence", "html-curator", "html-related",
        "html-curator-child-code-media", "quoted-body-does-not-release-related",
        "list-body-does-not-release-curator", "whitespace"])
def test_non_evidence_source_does_not_hide_readable_summary(store, intent, view, body):
    alpha = represented_pair(
        store, source="---\ntype: Reference\ntitle: Alpha realtime\n---\n" + body,
        summaries=1,
    )
    result = store.search("realtime", intent=intent, view=view, limit=1)
    assert result["total"] == 1 and result["next_offset"] is None
    hit = result["items"][0]
    assert hit["id"] != alpha["id"] and hit["path"] == "Summaries/summary-00.md"
    assert hit["revision"] == store.read(hit["id"])["revision"]
    section = hit["sections"][0]
    actual = store.read(hit["id"], start=section["start"],
                        length=section["end"] - section["start"],
                        expected_revision=hit["revision"])
    assert "Realtime uses video distillation" in actual["content"]
    # Exact lookup can still locate the original, even if it has no evidence.
    lookup = store.search(alpha["id"], intent="lookup", view=view)
    assert lookup["items"][0]["id"] == alpha["id"]


@pytest.mark.parametrize("intent", INTENTS)
@pytest.mark.parametrize("view", ["full", "compact"])
@pytest.mark.parametrize("body", [
    "42\n",
    "$$\\sum_{i=1}^{2} i = 3$$\n",
    "```json\n{}\n```\n",
    "```text\n<!-- literal code, not an invisible comment -->\n```\n",
    "## Key note\nRealtime retains a fixed frame length condition.\n",
    "<p>Realtime retains a fixed frame length condition.</p>\n",
    "> Realtime retains a fixed frame length condition.\n",
    "![Realtime](diagram.png)\n",
    "<img src=\"diagram.png\">\n",
    "<div><img src=\"diagram.png\"></div>\n",
    "<audio src=\"sample.ogg\"></audio>\n",
    "<video><source src=\"sample.mp4\"></video>\n",
    "<span><script>hidden.</script></span> Realtime visible conditions.\n",
    "```html\n<script>Realtime literal code.</script>\n```\n",
    "<noscript>\n\n```text\nHidden.\n```\n\n</noscript>\n\nRealtime visible conditions.\n",
    "<h2>整理说明</h2><p>Bookkeeping.</p>\n<h2>正文</h2><p>Realtime actual conditions.</p>\n",
    "<h2>相关笔记</h2><p>Links.</p>\n<h2>Key note</h2><p>Realtime actual conditions.</p>\n",
    "```html\n<h2>整理说明</h2><p>Realtime literal code.</p>\n```\n",
    "> ## 整理说明\n> Quoted note.\n\nRealtime actual body with fixed frame length.\n",
    "- ## 相关笔记\n  A quoted link.\n\nRealtime actual body with fixed frame length.\n",
    "> - ## 整理说明\n>   Quoted note.\n\n```json\n{}\n```\n![](diagram.png)\n",
], ids=["numeric", "formula", "punctuation-code", "literal-comment-code",
        "key-note", "html-body", "quoted-body", "markdown-image", "html-image",
        "wrapped-html-image", "audio", "video-source", "visible-after-hidden", "literal-script-code",
        "visible-after-hidden-fence", "html-body-after-curator", "html-keynote-after-related", "literal-heading-code",
        "quoted-curator-does-not-hide-body", "list-related-does-not-hide-body", "nested-quote-list-with-code-media"])
def test_visible_body_key_note_math_and_code_keep_original_identity(store, intent, view, body):
    alpha = represented_pair(
        store, source="---\ntype: Reference\ntitle: Alpha realtime\n---\n" + body,
        summaries=1,
    )
    result = store.search("realtime", intent=intent, view=view)
    assert result["total"] == 1 and result["items"][0]["id"] == alpha["id"]
    actual = store.read(alpha["id"], expected_revision=result["items"][0]["revision"])
    assert actual["content"].endswith(body)


@pytest.mark.parametrize("intent", INTENTS)
@pytest.mark.parametrize("view", ["full", "compact"])
@pytest.mark.parametrize("suffix", ["html", "htm"])
@pytest.mark.parametrize("body,usable", [
    ("<h1>Alpha realtime</h1>", False),
    ("<span><noscript>Realtime hidden.</noscript></span>", False),
    ('<h1><img src="diagram.png"></h1>', False),
    ('<img src="diagram.png">', True),
    ('<div><img src="diagram.png"></div>', True),
    ("<p>Realtime preserves fixed frame length.</p>", True),
    ("<h2>整理说明</h2><p>Realtime bookkeeping only.</p>", False),
    ("<h2>相关笔记</h2><p>Realtime links only.</p>", False),
], ids=["heading", "hidden", "heading-image", "image-only", "wrapped-image", "paragraph", "curator", "related"])
def test_physical_html_structure_selects_source_without_changing_read_coordinates(
    store, intent, view, suffix, body, usable,
):
    from server.wiki.extract import HTMLText

    path = "alpha-realtime." + suffix
    alpha = represented_pair(store, source_path=path, source=body, summaries=1)
    result = store.search("realtime", intent=intent, view=view, limit=1)
    assert result["total"] == 1
    hit = result["items"][0]
    assert (hit["id"] == alpha["id"]) is usable
    assert hit["path"] == (path if usable else "Summaries/summary-00.md")
    assert hit["revision"] == store.read(hit["id"])["revision"]
    assert store.search(alpha["id"], intent="lookup", view=view)["items"][0]["id"] == alpha["id"]
    # Qualification uses original structure; the evidence text still has the
    # established extractor's exact code-point coordinates.
    legacy = HTMLText()
    legacy.feed(body)
    read = store.read(alpha["id"], expected_revision=alpha["revision"])
    assert read["content"] == "".join(legacy.parts).strip()
    assert read["characters"] == len(read["content"])
    assert read["metadata"]["html_body_readable"] is usable
    if usable and "img" in body:
        assert read["metadata"]["html_media_refs"] == [{"tag": "img", "src": "diagram.png"}]


@pytest.mark.parametrize("legacy_version", ["sections-v4", "sections-v5", "sections-v6"])
def test_html_upgrade_reextracts_missing_structure_without_rewriting_history(history, legacy_version):
    from server.wiki.extract import HTML_BODY_VERSION
    from server.wiki.history import WikiHistory

    store = history.store
    path = "alpha-realtime.html"
    before = b"<p>Realtime original conditions.</p>"
    created = history.write(path, before, None, "human", "create source")
    edited = history.write(path, b"<h1>Alpha realtime</h1>", created["revision"], "human", "human edit")
    alpha = represented_pair(store, source_path=path, source="<h1>Alpha realtime</h1>", summaries=1)
    tables = ["changes", "change_retention", "change_management", "operation_receipts",
              "managed_notes", "document_identities"]
    with store.connect() as db:
        metadata = json.loads(db.execute("SELECT metadata FROM documents WHERE id=?", (alpha["id"],)).fetchone()[0])
        for key in ("html_body_version", "html_body_readable", "html_media_refs"):
            metadata.pop(key)
        db.execute("UPDATE documents SET metadata=? WHERE id=?", (json.dumps(metadata), alpha["id"]))
        db.execute("UPDATE search_documents SET version=?", (legacy_version,))
        db.execute("UPDATE search_sections SET readable_body=1 WHERE doc_id=?", (alpha["id"],))
        durable = {table: [tuple(row) for row in db.execute("SELECT * FROM " + table + " ORDER BY rowid")] for table in tables}
    root, state, vault_id = store.root, store.state, store.vault_id
    store.close()
    upgraded = WikiStore(root, state, vault_id)
    try:
        # No explicit refresh: startup/event compensation must handle both an
        # old layout and a cached unchanged HTML file lacking structure data.
        current = upgraded.read(alpha["id"])
        assert (current["id"], current["revision"], current["content"]) == (alpha["id"], alpha["revision"], alpha["content"])
        assert current["metadata"]["html_body_version"] == HTML_BODY_VERSION
        assert current["metadata"]["html_body_readable"] is False
        found = upgraded.search("realtime", intent="question")
        assert found["total"] == 1 and found["items"][0]["path"] == "Summaries/summary-00.md"
        with upgraded.connect() as db:
            assert durable == {table: [tuple(row) for row in db.execute("SELECT * FROM " + table + " ORDER BY rowid")] for table in tables}
            assert db.execute("SELECT max(version) FROM wiki_schema").fetchone()[0] == DURABLE_SCHEMA_VERSION
        WikiHistory(upgraded).restore(edited["change_id"], current["revision"], "before", "human")
        assert upgraded.path(path).read_bytes() == before
        assert upgraded.read(alpha["id"])["revision"] == created["revision"]
    finally:
        upgraded.close()


def test_human_body_removal_updates_fallback_without_rebinding_source(store):
    alpha = represented_pair(store, summaries=1)
    assert store.search("realtime")["items"][0]["id"] == alpha["id"]
    write_note(store, "alpha.md", "# Alpha realtime\n<!-- pending revision -->\n")
    store.refresh()
    current = store.read(alpha["id"])
    assert current["id"] == alpha["id"] and current["revision"] != alpha["revision"]
    result = store.search("realtime", intent="question")
    assert result["total"] == 1 and result["items"][0]["path"] == "Summaries/summary-00.md"


@pytest.mark.parametrize("legacy_version", ["sections-v5", "sections-v6"])
def test_markdown_old_v5_qualification_is_rebuilt_without_touching_durable_state(history, legacy_version):
    from server.wiki import search_index

    store = history.store
    source = "# Alpha realtime\n<span><script>Realtime secret.</script></span>\n"
    created = history.write("alpha.md", source.encode(), None, "human", "create source")
    alpha = represented_pair(store, source=source, summaries=1)
    tables = ["changes", "change_retention", "change_management", "operation_receipts",
              "managed_notes", "document_identities"]
    with store.connect() as db:
        # Persisted qualification produced by the previous v5 inline rule.
        # It must be invalidated by the new algorithm's derived version.
        db.execute("UPDATE search_documents SET version=?", (legacy_version,))
        db.execute("UPDATE search_sections SET readable_body=1 WHERE doc_id=?", (alpha["id"],))
        durable = {table: [tuple(row) for row in db.execute("SELECT * FROM " + table + " ORDER BY rowid")] for table in tables}
    root, state, vault_id = store.root, store.state, store.vault_id
    store.close()
    upgraded = WikiStore(root, state, vault_id)
    try:
        assert upgraded.search("realtime", intent="question")["items"][0]["path"] == "Summaries/summary-00.md"
        current = upgraded.read(alpha["id"], expected_revision=created["revision"])
        assert current["content"] == source and current["revision"] == alpha["revision"]
        assert upgraded.path("alpha.md").read_bytes() == source.encode()
        with upgraded.connect() as db:
            assert {row[0] for row in db.execute("SELECT version FROM search_documents")} == {search_index.VERSION}
            assert {row[0] for row in db.execute("SELECT readable_body FROM search_sections WHERE doc_id=?", (alpha["id"],))} == {0}
            assert durable == {table: [tuple(row) for row in db.execute("SELECT * FROM " + table + " ORDER BY rowid")] for table in tables}
            assert db.execute("SELECT max(version) FROM wiki_schema").fetchone()[0] == DURABLE_SCHEMA_VERSION
        upgraded.refresh()
        assert upgraded.search("realtime", intent="question")["items"][0]["path"] == "Summaries/summary-00.md"
    finally:
        upgraded.close()


def test_existing_index_upgrade_preserves_identity_revision_and_history(history):
    store = history.store
    created = history.write("alpha.md", b"# Alpha realtime\n", None, "human", "create source")
    alpha = represented_pair(store, source="# Alpha realtime\n", summaries=1)
    with store.connect() as db:
        db.execute("ALTER TABLE search_sections DROP COLUMN readable_body")
        db.execute("UPDATE search_documents SET version='sections-v4'")
        epoch = db.execute("SELECT epoch FROM index_state").fetchone()[0]
    root, state, vault_id = store.root, store.state, store.vault_id
    store.close()
    upgraded = WikiStore(root, state, vault_id)
    try:
        current = upgraded.read(alpha["id"])
        assert (current["id"], current["revision"]) == (alpha["id"], alpha["revision"])
        result = upgraded.search("realtime", intent="question")
        assert result["total"] == 1 and result["items"][0]["path"] == "Summaries/summary-00.md"
        with upgraded.connect() as db:
            assert db.execute("SELECT epoch FROM index_state").fetchone()[0] > epoch
            assert db.execute("SELECT count(*) FROM changes WHERE id=? AND status='applied'",
                              (created["change_id"],)).fetchone()[0] == 1
            assert db.execute("SELECT max(version) FROM wiki_schema").fetchone()[0] == DURABLE_SCHEMA_VERSION
    finally:
        upgraded.close()


def test_missing_target_and_same_path_replacement_do_not_rebind_representation(store):
    alpha = represented_pair(store)
    with store.connect() as db:
        assert {row[0] for row in db.execute(
            "SELECT target_doc FROM wiki_edges WHERE relation='representation_of'",
        )} == {alpha["id"]}
    store.path("alpha.md").unlink()
    store.refresh()
    missing = store.search("realtime", intent="question")
    assert missing["total"] == 1 and missing["items"][0]["id"] != alpha["id"]
    write_note(store, "alpha.md", "# Replacement realtime\nRealtime unrelated new source.\n")
    store.refresh()
    replacement = store.read("alpha.md")
    assert replacement["id"] != alpha["id"]
    result = store.search("realtime", intent="question")
    assert result["total"] == 2
    assert replacement["id"] in {item["id"] for item in result["items"]}
    assert any(item["path"].startswith("Summaries/") for item in result["items"])
    with store.connect() as db:
        assert {row[0] for row in db.execute(
            "SELECT target_doc FROM wiki_edges WHERE relation='representation_of'",
        )} == {alpha["id"]}


def test_renamed_source_is_selected_by_stable_identity(store):
    alpha = represented_pair(store)
    store.path("alpha.md").rename(store.path("renamed.md"))
    store.refresh()
    result = store.search("realtime", intent="question")
    assert result["total"] == 1
    hit = result["items"][0]
    assert hit["path"] == "renamed.md" and hit["id"] == alpha["id"]
    assert hit["revision"] == alpha["revision"]
    assert "fixed frame length" in store.read(hit["id"])["content"]


def test_unresolved_relations_keep_independent_summary_identities(store):
    for index in range(2):
        write_note(
            store, f"summary-{index}.md",
            "---\ntype: Synthesis\nx_wiki: {representation_of: missing.md}\n---\n"
            f"# Summary {index}\nRealtime short summary.\n",
        )
    store.refresh()
    result = store.search("realtime", intent="question")
    assert result["total"] == 2
    assert {item["path"] for item in result["items"]} == {"summary-0.md", "summary-1.md"}


@pytest.mark.parametrize("intent", INTENTS)
def test_sources_related_and_independent_concept_synthesis_are_not_merged_or_demoted(store, intent):
    represented_pair(store)
    write_note(store, "beta.md", "# Beta\nRealtime uses a calibrated Rig. [[alpha]]\n")
    write_note(
        store, "comparison.md", "---\ntype: Synthesis\n"
        "sources: [{resource: alpha.md}, {resource: beta.md}]\n---\n"
        "# Realtime comparison\nRealtime realtime realtime realtime realtime realtime comparison.\n",
    )
    write_note(
        store, "concept.md", "---\ntype: Concept\n"
        "sources: [{resource: alpha.md}]\n---\n# Realtime concept\n"
        "Realtime realtime realtime realtime realtime realtime concept.\n"
        "## 相关笔记\n[[alpha]] [[beta]]\n",
    )
    store.refresh()
    result = store.search("realtime", intent=intent)
    assert result["total"] == 4
    assert {item["path"] for item in result["items"]} == {
        "alpha.md", "beta.md", "comparison.md", "concept.md",
    }
    lexical = {item["id"]: item for item in store.search("realtime", intent="lookup")["items"]}
    for hit in result["items"]:
        assert hit["score"] == lexical[hit["id"]]["score"]
    if intent == "question":
        # Independent synthesis/concept evidence retains its ordinary rank.
        assert result["items"][0]["path"] in {"concept.md", "comparison.md"}


def test_chains_and_cycles_use_direct_groups_without_recursive_closure(store):
    for name, target in {"alpha": "beta", "beta": "gamma", "gamma": None,
                         "cycle-a": "cycle-b", "cycle-b": "cycle-a"}.items():
        front = f"---\nx_wiki: {{representation_of: {target}.md}}\n---\n" if target else ""
        write_note(store, name + ".md", front + f"# {name}\nRealtime evidence for {name}.\n")
    store.refresh()
    result = store.search("realtime", intent="question")
    assert result["total"] == 4
    assert {item["path"] for item in result["items"]} == {
        "alpha.md", "gamma.md", "cycle-a.md", "cycle-b.md",
    }


def test_source_keeps_cross_section_coverage_after_summary_deduplication(store):
    alpha = represented_pair(
        store, source="# Alpha study\n## Realtime\nRealtime requires fixed frame length.\n"
        "## Calibration\nCalibration accuracy controls Rig quality.\n",
    )
    write_note(
        store, "Summaries/summary-00.md",
        "---\nx_wiki: {representation_of: alpha.md}\n---\n"
        "# Summary\nRealtime calibration.\n",
    )
    store.refresh()
    result = store.search("realtime calibration", intent="question")
    assert result["total"] == 1
    hit = result["items"][0]
    assert hit["id"] == alpha["id"] and len(hit["sections"]) == 2
    reads = [store.read(hit["id"], start=part["start"], length=part["end"] - part["start"],
                        expected_revision=hit["revision"])["content"] for part in hit["sections"]]
    assert any("fixed frame length" in text for text in reads)
    assert any("Calibration accuracy" in text for text in reads)


def test_hot_representation_queries_do_not_walk_stat_or_read_vault_files(store, monkeypatch):
    alpha = represented_pair(store, summaries=20)
    for index in range(30):
        write_note(store, f"unrelated-{index}.md", "# Unrelated\nNo matching evidence.\n")
    store.refresh()
    original_walk, original_stat, original_open = os.walk, Path.stat, Path.open
    accesses = []

    def walk(path, *args, **kwargs):
        if Path(path).is_relative_to(store.root):
            accesses.append(("walk", str(path)))
        return original_walk(path, *args, **kwargs)

    def stat(path, *args, **kwargs):
        if path != store.root and path.is_relative_to(store.root):
            accesses.append(("stat", str(path)))
        return original_stat(path, *args, **kwargs)

    def opened(path, mode="r", *args, **kwargs):
        if path.is_relative_to(store.root) and "r" in mode:
            accesses.append(("read", str(path)))
        return original_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(os, "walk", walk)
    monkeypatch.setattr(Path, "stat", stat)
    monkeypatch.setattr(Path, "open", opened)
    for intent in INTENTS:
        for view in ["compact", "full"]:
            result = store.search("realtime", intent=intent, view=view, limit=1)
            assert result["total"] == 1 and result["items"][0]["id"] == alpha["id"]
    assert accesses == []
