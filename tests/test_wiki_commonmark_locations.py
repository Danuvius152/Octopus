"""Current CommonMark locations agree across reading and controlled writes."""

import uuid
from types import SimpleNamespace

import pytest

from server.wiki.curation import WikiCuration
from server.wiki.jobs import WikiJobs
from server.wiki.links import source_span
from server.wiki.store import WikiError, WikiStore
from server.wiki.tasks import tasks
from tests import test_wiki_api as wiki_api

client = wiki_api.client

NESTED_HEADING_SECTIONS = [
    pytest.param(
        "# Source\n\n"
        "> ## Evidence\n> Supported claim.\n> ### Details\n> Necessary exception.\n"
        "> ## Sibling\n> Other conditions.\n\nOutside paragraph.\n",
        [
            ("#evidence", (10, 80), "> ## Evidence\n> Supported claim.\n> ### Details\n> Necessary exception.\n"),
            ("#sibling", (80, 113), "> ## Sibling\n> Other conditions.\n"),
        ],
        id="quoted",
    ),
    pytest.param(
        "# Source\n\n"
        "- ## Evidence\n  Supported claim.\n  ### Details\n  Necessary exception.\n"
        "- ## Sibling\n  Other conditions.\n\nOutside paragraph.\n",
        [
            ("#evidence", (10, 80), "- ## Evidence\n  Supported claim.\n  ### Details\n  Necessary exception.\n"),
            ("#sibling", (80, 114), "- ## Sibling\n  Other conditions.\n\n"),
        ],
        id="list",
    ),
]


@pytest.fixture
def store(tmp_path):
    root = tmp_path / "vault"
    (root / "Library").mkdir(parents=True)
    instance = WikiStore(root, tmp_path / "state", uuid.uuid4().hex, create=True)
    try:
        yield instance
    finally:
        instance.close()


@pytest.mark.parametrize("heading,slug", [
    ("# Topic", "topic"), ("Topic\n=====", "topic"), ("Topic\n-----", "topic"),
    ("   # Topic ###", "topic"), ("> # Topic", "topic"), ("- # Topic", "topic"),
    ("# [Topic](https://example.org)", "topic"), ("# **Topic**", "topic"),
    ("# `Topic`", "topic"), ("Topic\nsection\n====", "topic-section"),
])
def test_publication_links_to_current_commonmark_heading(store, heading, slug):
    original = (heading + "\n\nOriginal section content.\n").encode()
    target = store.root / "Library/target.md"
    target.write_bytes(original)
    document = store.read("Library/target.md")

    published = WikiCuration(store).publish(
        "Library/new.md", f"# New\n\n[Topic](target.md#{slug})\n", "concept",
        None, None, "human", "Link an existing CommonMark heading",
    )

    assert store.read(published["path"])["content"].endswith(f"[Topic](target.md#{slug})\n")
    edge = store.graph(published["path"])["edges"][0]
    assert (edge["to"], edge["location"], edge["status"]) == (document["id"], slug, "resolved")
    assert target.read_bytes() == original


def test_duplicate_chinese_heading_slug_selects_the_same_source_section_and_link(store):
    text = (
        "# Target\n\n## 中文 标题\n\nFirst conditions.\n\n"
        "## 中文 标题\n\nSecond conditions.\n\n### Details\n\nNested evidence.\n\n"
        "## Limits\n\nOther conditions.\n"
    )
    target = store.root / "Library/target.md"
    target.write_bytes(text.encode())
    source = store.read("Library/target.md")
    curation = WikiCuration(store)
    published = curation.publish(
        "Library/new.md", "# New\n\n[Second](target.md#中文-标题-1)\n", "concept",
        None, None, "human", "Link the second heading",
    )
    current = store.read(published["path"])

    curation.organize(
        current["path"], current["revision"], "A conditional relation.", None,
        [{"doc": source["id"], "revision": source["revision"], "location": "#中文-标题-1",
          "explanation": "The second section contains the relevant conditions."}],
        None, "human", "Cite the same section as the link",
    )

    start, end = source_span(text, "#中文-标题-1")
    assert text[start:end] == "## 中文 标题\n\nSecond conditions.\n\n### Details\n\nNested evidence.\n\n"
    assert source_span(text, "中文 标题") is None
    edge = next(edge for edge in store.graph(published["path"])["edges"] if edge["relation"] == "markdown_link")
    assert (edge["to"], edge["location"], edge["status"]) == (source["id"], "中文-标题-1", "resolved")
    assert target.read_bytes() == text.encode()


@pytest.mark.parametrize("text,sections", NESTED_HEADING_SECTIONS)
def test_nested_heading_source_sections_stop_at_their_container_and_siblings(text, sections):
    for location, span, passage in sections:
        assert source_span(text, location) == span
        assert text[slice(*span)] == passage


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
@pytest.mark.parametrize("prefix", ["", "\ufeff", "\ufeff---\ntitle: Topic\n---\n"])
def test_raw_heading_locations_retain_bom_and_crlf_coordinates(store, newline, prefix):
    text = (prefix + "# Topic\n\nEvidence\n--------\n\nOnly under condition A.\n").replace("\n", newline)
    raw = text.encode()
    target = store.root / "Library/target.md"
    target.write_bytes(raw)
    document = store.read("Library/target.md")
    blocks = WikiCuration(store).blocks(document["id"])["blocks"]

    assert source_span(text, "#topic") == (len(prefix.replace("\n", newline)), len(text))
    evidence = "Evidence" + newline + "--------" + newline * 2 + "Only under condition A." + newline
    assert source_span(text, "#evidence") == (text.index("Evidence"), len(text))
    assert text[slice(*source_span(text, "#evidence"))] == evidence
    assert "".join(block["content"] for block in blocks) == text
    assert all(text[block["start"]:block["end"]] == block["content"] for block in blocks)
    assert blocks[-1]["title"] == "Evidence"
    assert target.read_bytes() == raw


def test_navigation_uses_the_current_h2_and_h3_titles_while_roles_inherit(store):
    text = (
        "# Topic\n\n## Evidence\n\nCalibrated finding.\n\n"
        "### Conditions\n\nSpecific assumptions.\n\n"
        "## 相关笔记\n\n### Background\n\nUnrelated navigation.\n"
    )
    (store.root / "Library/target.md").write_text(text)
    document = store.read("Library/target.md")

    assert [(section["title"], section["role"]) for section in document["metadata"]["body_sections"]] == [
        ("Topic", "body"), ("Evidence", "body"), ("Conditions", "body"),
        ("相关笔记", "related_notes"), ("Background", "related_notes"),
    ]
    for query, title, passage in [
        ("Calibrated finding", "Evidence", "## Evidence\n\nCalibrated finding.\n\n"),
        ("Specific assumptions", "Conditions", "### Conditions\n\nSpecific assumptions.\n\n"),
    ]:
        hit = store.search(query)["items"][0]
        section = hit["section"]
        assert section["title"] == title
        assert store.read(hit["id"], start=section["start"], length=section["end"] - section["start"])["content"] == passage


def test_explicit_html_and_complete_list_block_anchors_remain_linkable(store):
    text = (
        "# Target\n\n<a id='Exact-ID'></a>\n\n<div><a name='legacy'></a></div>\n\n"
        "- Necessary condition A.\n\n  Supporting argument.\n\n- Conditional conclusion.\n\n^claim\n\n"
        "Other paragraph.\n"
    )
    target = store.root / "Library/target.md"
    target.write_bytes(text.encode())
    source = store.read("Library/target.md")
    published = WikiCuration(store).publish(
        "Library/new.md",
        "# New\n\n[Exact](target.md#Exact-ID) [Legacy](target.md#legacy) [Claim](target.md#^claim)\n",
        "concept", None, None, "human", "Preserve explicit anchor rules",
    )

    assert [(edge["to"], edge["status"]) for edge in store.graph(published["path"])["edges"]] == [(source["id"], "resolved")] * 3
    assert text[slice(*source_span(text, "^claim"))] == "- Necessary condition A.\n\n  Supporting argument.\n\n- Conditional conclusion.\n\n^claim\n\n"
    with pytest.raises(WikiError, match="New local anchor does not exist"):
        WikiCuration(store).validate_links("Library/new.md", "[Case matters](target.md#exact-id)")
    assert target.read_bytes() == text.encode()


def test_block_location_stops_before_a_consumed_reference_definition(store):
    text = "# Target\n\nConditional claim. ^claim\n\n[unused]: absent.md\n"
    (store.root / "Library/target.md").write_text(text)
    store.read("Library/target.md")

    assert text[slice(*source_span(text, "^claim"))] == "Conditional claim. ^claim\n\n"


def test_block_id_must_end_the_original_line_after_heading_markup(store):
    text = "# Topic ^claim ###\n\nOriginal content.\n"
    (store.root / "Library/target.md").write_text(text)
    store.read("Library/target.md")

    with pytest.raises(WikiError, match="New local anchor does not exist"):
        WikiCuration(store).validate_links("Library/new.md", "[Claim](target.md#^claim)")
    assert source_span(text, "^claim") is None


def test_document_name_lookup_uses_a_setext_title_instead_of_a_code_heading(store):
    text = "Actual title\n============\n\nOriginal content.\n\n```md\n# Fake title\n```\n"
    target = store.root / "Library/target.md"
    target.write_text(text)
    document = store.read("Library/target.md")
    curation = WikiCuration(store)
    candidate = text + "\n[[Actual title]]\n"
    curation.validate_links(document["path"], candidate, candidate=candidate)

    published = curation.publish(
        "Library/new.md", "# New\n\n[[Actual title]]\n", "concept",
        None, None, "human", "Resolve a current CommonMark document title",
    )

    assert document["title"] == "Actual title"
    assert store.graph(published["path"])["edges"][0]["to"] == document["id"]
    assert target.read_bytes() == text.encode()


def test_article_normalization_keeps_an_existing_setext_title_in_place(store):
    original = b"Actual title\n============\n\nOriginal content.\n"
    target = store.root / "Library/target.md"
    target.write_bytes(original)
    document = store.read("Library/target.md")

    WikiCuration(store).organize(
        document["path"], document["revision"], "A conditional opening point.", None,
        [], None, "human", "Adapt the existing CommonMark article",
        frontmatter_patch={"type": "Reference", "x_wiki": {"profile": "article-v1", "body_dialect": "markdown"}},
    )

    content = store.read(document["id"])["content"]
    assert "\n# " not in content
    assert "Actual title\n============\n\n## Key note\n" in content
    assert target.read_bytes().endswith(b"## \xe6\xad\xa3\xe6\x96\x87\n\n\nOriginal content.\n")


async def test_setext_source_section_requires_the_complete_current_cited_read(client, tmp_path):
    from server.mcp_identity import mint

    root = tmp_path / "vault"
    (root / "Library").mkdir(parents=True)
    (root / "Library/note.md").write_text("# Note\n\nOriginal claims.\n")
    original = (
        b"# Source\n\nIntro.\n\nEvidence\n--------\n\nSupported claim.\n\n"
        b"### Details\n\nNecessary exception.\n\nLimits\n------\n\nOther conditions.\n"
    )
    source_file = root / "Library/source.md"
    source_file.write_bytes(original)
    config = await wiki_api.configure(client, root)
    target = (await client.get("/api/wiki/read", params={"doc": "Library/note.md"}, headers=client.headers)).json()
    source = (await client.get("/api/wiki/read", params={"doc": "Library/source.md"}, headers=client.headers)).json()
    jobs = WikiJobs((await wiki_api.session_manager.db.get_system_agent())["user_id"])
    job = jobs.maintain("commonmark-source", "", "organize", [target["path"]], None, [], "human")
    session = SimpleNamespace(id="commonmark-worker", user_id=jobs.user_id, agent_id=config["curator_agent_id"])
    jobs.update(job["id"], status="running", session_id=session.id)
    tasks.assign(session.id, job["id"], job["items"])
    token = tasks.open(session, "maintain")
    headers = {"Authorization": f"Bearer {mint(session.id, user_id=session.user_id, wiki_task_id=token)}"}
    body = {
        "path": target["path"], "expected_revision": target["revision"],
        "key_note": "The condition and exception remain linked.", "reason": "Cite the current section",
        "relations": [{"doc": source["id"], "revision": source["revision"], "location": "#evidence",
                       "explanation": "The evidence section explains the condition and its exception."}],
    }
    try:
        await client.get("/api/wiki/read", params={"doc": target["id"]}, headers=headers)
        start = source["content"].index("Evidence")
        await client.get("/api/wiki/read", params={"doc": source["id"], "start": start, "length": len("Evidence\n--------\n")}, headers=headers)
        unread = await client.post("/api/wiki/organize", json=body, headers=headers)
        assert unread.status_code == 422 and "cited current passage" in unread.text
        end = source["content"].index("Limits")
        passage = await client.get("/api/wiki/read", params={"doc": source["id"], "start": start, "length": end - start}, headers=headers)
        assert passage.status_code == 200, passage.text
        linked = await client.post("/api/wiki/organize", json=body, headers=headers)
        assert linked.status_code == 200, linked.text
        assert source_file.read_bytes() == original
    finally:
        tasks.close(token)
        tasks.unassign(session.id)


@pytest.mark.parametrize("text,sections", NESTED_HEADING_SECTIONS)
async def test_nested_source_requires_the_complete_current_section_but_no_outside_read(client, tmp_path, text, sections):
    from server.mcp_identity import mint

    root = tmp_path / "vault"
    (root / "Library").mkdir(parents=True)
    target_file = root / "Library/note.md"
    target_original = b"# Note\n\nOriginal claims.\n"
    target_file.write_bytes(target_original)
    source_file = root / "Library/source.md"
    original = text.encode()
    source_file.write_bytes(original)
    config = await wiki_api.configure(client, root)
    target_response = await client.get("/api/wiki/read", params={"doc": "Library/note.md"}, headers=client.headers)
    source_response = await client.get("/api/wiki/read", params={"doc": "Library/source.md"}, headers=client.headers)
    assert target_response.status_code == 200, target_response.text
    assert source_response.status_code == 200, source_response.text
    target, source = target_response.json(), source_response.json()
    jobs = WikiJobs((await wiki_api.session_manager.db.get_system_agent())["user_id"])
    job = jobs.maintain("commonmark-nested-source", "", "organize", [target["path"]], None, [], "human")
    session = SimpleNamespace(id="commonmark-nested-worker", user_id=jobs.user_id, agent_id=config["curator_agent_id"])
    jobs.update(job["id"], status="running", session_id=session.id)
    tasks.assign(session.id, job["id"], job["items"])
    token = tasks.open(session, "maintain")
    headers = {"Authorization": f"Bearer {mint(session.id, user_id=session.user_id, wiki_task_id=token)}"}
    location, (start, end), expected_passage = sections[-1]
    body = {
        "path": target["path"], "expected_revision": target["revision"],
        "key_note": "The nested source supplies the applicable condition.", "reason": "Cite only the nested section",
        "relations": [{"doc": source["id"], "revision": source["revision"], "location": location,
                       "explanation": "The nested section states the applicable condition."}],
    }
    try:
        target_read = await client.get(
            "/api/wiki/read", params={"doc": target["id"], "expected_revision": target["revision"]}, headers=headers,
        )
        assert target_read.status_code == 200, target_read.text
        for length in (13, 21):
            partial = await client.get(
                "/api/wiki/read",
                params={"doc": source["id"], "expected_revision": source["revision"], "start": start, "length": length},
                headers=headers,
            )
            assert partial.status_code == 200, partial.text
            assert partial.json()["revision"] == source["revision"]
            assert partial.json()["content"] == expected_passage[:length]
            unread = await client.post("/api/wiki/organize", json=body, headers=headers)
            assert unread.status_code == 422 and "cited current passage" in unread.text
            assert target_file.read_bytes() == target_original
        passage = await client.get(
            "/api/wiki/read",
            params={"doc": source["id"], "expected_revision": source["revision"], "start": start, "length": end - start},
            headers=headers,
        )
        assert passage.status_code == 200, passage.text
        read = passage.json()
        assert (read["start"], read["end"], read["next_start"]) == (start, end, end)
        assert read["revision"] == source["revision"]
        assert read["content"] == expected_passage
        linked = await client.post("/api/wiki/organize", json=body, headers=headers)
        assert linked.status_code == 200, linked.text
        assert source_file.read_bytes() == original
    finally:
        tasks.close(token)
        tasks.unassign(session.id)


@pytest.mark.parametrize(
    "example,anchor",
    [
        ("```md\nexample\n```still-code\n# Fake\n```\n", "fake"),
        ("    # Fake\n", "fake"),
        ("```md\nexample\n```still-code\n<a id='fake'></a>\n```\n", "fake"),
        ("    <a name='fake'></a>\n", "fake"),
        ("```md\nexample\n```still-code\nClaim. ^fake\n```\n", "^fake"),
        ("    Claim. ^fake\n", "^fake"),
        ("`code\nClaim. ^fake\nend`\n", "^fake"),
    ],
)
def test_publication_rejects_anchors_that_only_exist_in_code(store, example, anchor):
    original = ("# Top\n\n" + example).encode()
    target = store.root / "Library/target.md"
    target.write_bytes(original)
    store.read("Library/target.md")

    with pytest.raises(WikiError, match="New local anchor does not exist"):
        WikiCuration(store).publish(
            "Library/new.md", f"# New\n\n[Fake](target.md#{anchor})\n", "concept",
            None, None, "human", "Reject a code example as a citation",
        )

    assert not (store.root / "Library/new.md").exists()
    assert source_span(original.decode(), anchor if anchor.startswith("^") else "#" + anchor) is None
    assert target.read_bytes() == original
