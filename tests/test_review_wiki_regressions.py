"""Behavior regressions for review F01–F05, using isolated current vaults."""

import json
import uuid

import pytest
from markdown_it import MarkdownIt

from server.wiki import format, portable_metadata, source_bindings
from server.wiki.curation import WikiCuration
from server.wiki.history import WikiHistory
from server.wiki.references import local_references
from server.wiki.store import WikiError
from tests.test_wiki_reference_writers import note
from tests.test_wiki_reference_writers import store as store
from tests.wiki_job_runtime import client as client
from tests.wiki_job_runtime import jobs as jobs


def related_note(store):
    source = note(store, "Library/source.md", "# Source\nEvidence.\n")
    target = note(store, "Library/note.md", "# Note\nOriginal.\n")
    relation = {"doc": source["id"], "revision": source["revision"],
                "location": "L1-L2", "explanation": "Keep this explanation"}
    WikiCuration(store).organize(target["path"], target["revision"], "Summary", None,
                                [relation], None, "human", "add relation")
    return store.read(target["id"]), relation


@pytest.mark.parametrize("mode", ["omitted", "clear", "replace", "edited"])
def test_f01_organize_preserves_omitted_relations_and_requires_review_of_edits(store, mode):
    target, relation = related_note(store)
    before = store.path(target["path"]).read_bytes()
    relations = None if mode in {"omitted", "edited"} else [] if mode == "clear" else [
        relation | {"explanation": "Replacement explanation"}]
    if mode == "edited":
        before = before.replace(b"Keep this explanation", b"Human explanation")
        store.path(target["path"]).write_bytes(before)
        target = store.read(target["id"])
        with pytest.raises(WikiError, match="user edited") as error:
            WikiCuration(store).organize(target["path"], target["revision"], None, ["updated"],
                                        relations, None, "human", "update tags")
        assert error.value.status == 409
        assert store.path(target["path"]).read_bytes() == before
        return
    WikiCuration(store).organize(target["path"], target["revision"], None, ["updated"],
                                relations, None, "human", "update tags")
    current = store.path(target["path"]).read_text()
    assert "Original." in current and "#updated" in current
    assert ("Keep this explanation" in current) == (mode == "omitted")
    assert ("Replacement explanation" in current) == (mode == "replace")
    if mode == "omitted":
        with store.connect() as db:
            managed = json.loads(db.execute("SELECT regions FROM managed_notes WHERE path=?", (target["path"],)).fetchone()[0])
        assert next(part["raw"] for part in managed if part["role"] == "relations").encode() in before


async def test_f01_http_omission_and_explicit_clear_reach_writer_distinctly(jobs, client):
    target, _ = related_note(jobs.store)
    response = await client.post("/api/wiki/organize", headers=client.headers, json={
        "path": target["path"], "expected_revision": target["revision"],
        "tags": ["updated"], "reason": "tags only",
    })
    assert response.status_code == 200, response.text
    assert "Keep this explanation" in jobs.store.path(target["path"]).read_text()
    current = jobs.store.read(target["id"])
    response = await client.post("/api/wiki/organize", headers=client.headers, json={
        "path": target["path"], "expected_revision": current["revision"],
        "relations": [], "reason": "explicitly clear relations",
    })
    assert response.status_code == 200, response.text
    assert "Keep this explanation" not in jobs.store.path(target["path"]).read_text()


def test_f01_mcp_preserves_omitted_relations(monkeypatch):
    from server.mcp_servers import wiki

    calls = []
    monkeypatch.setattr(wiki, "call", lambda *args, **kwargs: calls.append(kwargs["body"]) or {})
    wiki.organize("Library/note.md", "revision", "tags only", tags=["updated"])
    wiki.organize("Library/note.md", "revision", "clear", relations=[])
    assert calls[0]["relations"] is None
    assert calls[1]["relations"] == []


@pytest.mark.parametrize("opening,not_closing,closing", [
    ("```text", "```not-a-close", "```"),
    ("~~~text", "~~~not-a-close", "~~~~"),
    ("```text", "~~~", "```"),
    ("~~~text", "```", "~~~"),
    ("````text", "```", "`````"),
    ("   ```text", "   ```not-a-close", "   ```   "),
])
def test_f02_restructure_preserves_actual_code_boundary_and_content(store, opening, not_closing, closing):
    raw = f"# Note\n\n{opening}\ncode one\n{not_closing}\n\ncode two\n{closing}\n\nOutside.\n"
    target = note(store, "Library/note.md", raw)
    curation = WikiCuration(store)
    parts = curation.blocks(target["id"])["blocks"]
    code = next(part for part in parts if "code one" in part["content"])
    assert "code two" in code["content"] and closing in code["content"]
    assert len(parts) == 3
    order = [parts[0]["id"], parts[2]["id"], parts[1]["id"]]
    curation.restructure(target["path"], target["revision"], order, [], "human", "move complete code block")
    after = store.path(target["path"]).read_text()
    parser = MarkdownIt("commonmark")
    def signatures(text):
        return [(token.content, token.info, token.markup) for token in parser.parse(text) if token.type == "fence"]
    assert signatures(after) == signatures(raw)
    assert any(token.type == "inline" and token.content == "Outside." for token in parser.parse(after))


def test_f02_markdown_code_ranges_keep_existing_multiline_math_indivisible(store):
    math = "$$formula\n\ncontinued math\n$$\n\n"
    target = note(store, "Library/math.md", "# Note\n\n" + math + "Outside.\n")
    parts = WikiCuration(store).blocks(target["id"])["blocks"]
    assert len(parts) == 3
    assert parts[1]["content"] == math


@pytest.mark.parametrize("stage", ["before_ingest", "after_ingest"])
async def test_f03_failed_material_without_item_exposes_retry_and_reuses_ingestion(jobs, client, stage):
    job = jobs.maintain("retry-" + stage, "# Input\nRetain this material.\n", "Review", [], None, [], "human")
    assert jobs.next_queued() == job["id"]
    original = jobs.ingest(job["id"]) if stage == "after_ingest" else None
    failed = jobs.update(job["id"], status="failed", error="before item recording", session_id=None)
    assert failed["items"] == []
    response = await client.get(f"/api/wiki/jobs/{job['id']}/details", headers=client.headers)
    assert response.status_code == 200, response.text
    assert response.json()["actions"]["retry"] is True
    retried = jobs.retry(job["id"])
    assert retried["status"] == "queued"
    assert jobs.next_queued() == job["id"]
    ingested = jobs.ingest(job["id"])
    if original:
        assert ingested["id"] == original["id"] and ingested["revision"] == original["revision"]
    assert len(list(jobs.store.path("Library/Inbox").glob("*.md"))) == 1


@pytest.mark.parametrize("boundary", ["running", "settlement", "scope", "retirement"])
async def test_f03_material_retry_still_obeys_existing_gates(jobs, monkeypatch, boundary):
    from server.wiki.job_details import actions

    job = jobs.maintain("gates-" + boundary, "# Input\nMaterial.\n", "Review", [], None, [], "human")
    job = jobs.update(job["id"], status="failed", error="before ingestion", session_id=None)
    if boundary == "running":
        job["status"] = "running"
    elif boundary == "settlement":
        job["settlement_phase"] = "attention"
    elif boundary == "scope":
        job["grant"] = None
    else:
        def active(_):
            raise WikiError("Original writer is still active", 409)
        monkeypatch.setattr(jobs, "require_retired", active)
    assert actions(jobs, job)["retry"] is False


@pytest.mark.parametrize("wrapper", ["```html\n{}\n```", "    {}", "`{}`", "\\{}", "<script>\n{}\n</script>",
                                     "<textarea>\n{}\n</textarea>", "<title>{}</title>", "<iframe>\n{}\n</iframe>"])
def test_f04_code_examples_cannot_claim_metadata_or_durable_source_and_uid(store, wrapper):
    uid = "urn:uuid:" + str(uuid.uuid4())
    payload = {"title": "Example title", "aliases": ["Example alias"], "uid": uid,
               "sources": [{"resource": "Missing.md", "revision": "example"}]}
    comment = "<!-- octopus-wiki " + json.dumps(payload) + " -->"
    text = "# Real title\n\n" + wrapper.format(comment) + "\n"
    example = note(store, "Library/example.md", text)
    assert example["title"] == "Real title"
    assert "Example alias" not in example["metadata"].get("aliases", [])
    assert not example["metadata"].get("sources")
    with pytest.raises(WikiError):
        store.read(uid)
    with store.connect() as db:
        assert not source_bindings.metadata(db, example["id"], example["path"], text.encode(), None).get("sources")
    WikiHistory(store).write(example["path"], b"# Real title\nExample removed.\n", example["revision"], "human", "remove example")
    actual = note(store, "Library/actual.md", f"---\nuid: '{uid}'\n---\n# Actual owner\n")
    assert store.read(uid)["id"] == actual["id"]
    assert not actual["metadata"].get("uid_conflict")


@pytest.mark.parametrize("inline", [False, True])
def test_f04_real_html_comments_supply_same_metadata_to_index_and_source_ledger(store, inline):
    uid = "urn:uuid:" + str(uuid.uuid4())
    source = note(store, "Library/source.md", "# Source\nEvidence.\n")
    payload = {"title": "Portable title", "aliases": ["Portable alias"], "uid": uid,
               "sources": [{"doc": source["id"], "revision": source["revision"], "location": "L1-L2"}]}
    comment = "<!-- octopus-wiki " + json.dumps(payload) + " -->"
    text = "# Real title\n\n" + ("Visible " if inline else "") + comment + "\n"
    document = note(store, "Library/actual.md", text)
    assert document["title"] == "Portable title"
    assert store.read(uid)["id"] == document["id"]
    assert "Portable alias" in document["metadata"]["aliases"]
    with store.connect() as db:
        assert source_bindings.metadata(db, document["id"], document["path"], text.encode(), None)["sources"] == payload["sources"]


TEXT_CONTAINERS = ["script", "style", "textarea", "title", "xmp", "iframe", "noembed", "noframes"]
CONTAINER_OPENINGS = ["<{tag}>", "<{tag} />", "<{tag}/>", '<{tag} data-example="a > b" />']


def container_example(tag, opening, uid):
    payload = {"title": "Example metadata", "uid": uid, "aliases": ["Example alias"]}
    comment = "<!-- octopus-wiki " + json.dumps(payload) + " -->"
    return ("# Real title\n\n" + opening.format(tag=tag.upper()) + "\n"
            + "</unrelated>\n" + comment + "\n</" + tag.upper() + " >\n")


@pytest.mark.parametrize("tag", TEXT_CONTAINERS)
@pytest.mark.parametrize("opening", CONTAINER_OPENINGS)
def test_f04_nonvoid_text_containers_keep_comment_examples_literal_until_the_real_close(tag, opening):
    text = container_example(tag, opening, "urn:uuid:" + str(uuid.uuid4()))
    assert portable_metadata.from_document(format.parse(text)) == {}


# Every spelling is checked above. Keep real UID ownership checks for raw-text
# and RCDATA containers, including both self-closing forms and a quoted '>'.
@pytest.mark.parametrize("tag,opening", [
    ("script", CONTAINER_OPENINGS[0]), ("textarea", CONTAINER_OPENINGS[1]),
    ("title", CONTAINER_OPENINGS[2]), ("xmp", CONTAINER_OPENINGS[3]),
])
def test_f04_literal_container_metadata_cannot_reserve_a_durable_uid(store, tag, opening):
    uid = "urn:uuid:" + str(uuid.uuid4())
    text = container_example(tag, opening, uid)
    document = note(store, "Library/text-container.md", text)
    assert document["title"] == "Real title"
    assert "Example alias" not in document["metadata"].get("aliases", [])
    with pytest.raises(WikiError):
        store.read(uid)
    # An example must not reserve its UID for a later real document.
    actual = note(store, "Library/actual-container-uid.md", f"---\nuid: '{uid}'\n---\n# Actual owner\n")
    assert store.read(uid)["id"] == actual["id"]
    assert not actual["metadata"].get("uid_conflict")


def container_following_metadata(tag, closed, uid):
    example = '<!-- octopus-wiki {"title":"Example metadata"} -->'
    real = "<!-- octopus-wiki " + json.dumps({"title": "Real metadata", "uid": uid}) + " -->"
    text = "# Real title\n\n<" + tag + " />\n" + example + "\n"
    if closed:
        text += "</" + tag + ">\n\n"
    return text + real + "\n"


@pytest.mark.parametrize("tag", TEXT_CONTAINERS)
@pytest.mark.parametrize("closed", [False, True])
def test_f04_text_container_closes_only_at_its_end_tag_and_preserves_following_real_metadata(tag, closed):
    uid = "urn:uuid:" + str(uuid.uuid4())
    text = container_following_metadata(tag, closed, uid)
    assert portable_metadata.from_document(format.parse(text)) == ({"title": "Real metadata", "uid": uid} if closed else {})


@pytest.mark.parametrize("tag,closed", [("style", False), ("textarea", True), ("title", False), ("iframe", True)])
def test_f04_container_close_controls_durable_metadata_and_uid(store, tag, closed):
    uid = "urn:uuid:" + str(uuid.uuid4())
    text = container_following_metadata(tag, closed, uid)
    document = note(store, "Library/container-close.md", text)
    assert document["title"] == ("Real metadata" if closed else "Real title")
    if closed:
        assert store.read(uid)["id"] == document["id"]
    else:
        with pytest.raises(WikiError):
            store.read(uid)


def test_f04_parser_projection_refresh_keeps_current_bytes_uid_and_source_observations(store):
    from server.wiki.format import VERSION

    uid = "urn:uuid:" + str(uuid.uuid4())
    raw = '# Real title\n\n<!-- octopus-wiki ' + json.dumps({"uid": uid}) + ' -->\n'
    document = note(store, "Library/current.md", raw)
    with store.connect() as db:
        aliases = [tuple(row) for row in db.execute("SELECT * FROM uid_aliases")]
        observation = source_bindings.current(db, document["id"])
        metadata = document["metadata"] | {"format_version": "okf-parser-v8"}
        db.execute("UPDATE documents SET metadata=? WHERE id=?", (json.dumps(metadata), document["id"]))
    store.refresh()
    current = store.read(uid)
    assert current["id"] == document["id"]
    assert current["metadata"]["format_version"] == VERSION
    assert store.path(document["path"]).read_bytes() == raw.encode()
    with store.connect() as db:
        assert [tuple(row) for row in db.execute("SELECT * FROM uid_aliases")] == aliases
        assert source_bindings.current(db, document["id"]) == observation


@pytest.mark.parametrize("literal", [r"\[\[Missing\]\]", r"\[[Missing]]", r"[\[Missing]]", r"[[Missing\]\]]",
                                     "&#91;&#91;Missing&#93;&#93;", "&lbrack;&lbrack;Missing&rbrack;&rbrack;", "`[[Missing]]`"])
def test_f05_literal_wikilink_examples_publish_without_edges(store, literal):
    text = "# Literal\n\n" + literal + "\n"
    assert list(local_references(text)) == []
    WikiCuration(store).publish("Library/literal.md", text, "concept", [], None, "human", "literal syntax")
    document = store.read("Library/literal.md")
    assert not store.graph(document["id"])["edges"]


def test_f05_real_wikilinks_still_create_edges_and_missing_targets_are_rejected(store):
    target = note(store, "Library/Target.md", "# Target\nEvidence.\n")
    text = "# Links\n\n" + r"\[\[Missing\]\] &#91;&#91;Missing&#93;&#93; [[Target]]" + "\n"
    assert [reference.value for reference in local_references(text)] == ["Target"]
    WikiCuration(store).publish("Library/links.md", text, "concept", [], None, "human", "real and literal syntax")
    document = store.read("Library/links.md")
    edges = store.graph(document["id"])["edges"]
    assert len(edges) == 1 and edges[0]["to"] == target["id"]
    with pytest.raises(WikiError, match="New local link") as error:
        WikiCuration(store).publish("Library/missing.md", "# Missing\n\n[[Absent]]\n", "concept", [], None, "human", "actual link")
    assert error.value.status == 422


@pytest.mark.parametrize("spelling,filename", [
    ("[[Rock &amp; Roll]]", "Rock & Roll"),
    (r"[[A\&B]]", "A&B"),
    (r"[[A\[B]]", "A[B"),
])
def test_f05_literal_delimiters_allow_decoded_target_contents(store, spelling, filename):
    target = note(store, "Library/" + filename + ".md", "# Target\nEvidence.\n")
    text = "# Literal delimiters\n\n" + spelling + "\n"
    assert [reference.value for reference in local_references(text)] == [filename]
    WikiCuration(store).publish("Library/links.md", text, "concept", [], None, "human", "decoded target")
    document = store.read("Library/links.md")
    edges = store.graph(document["id"])["edges"]
    assert len(edges) == 1 and edges[0]["to"] == target["id"]


def test_f05_escaped_opening_brackets_do_not_hide_a_later_literal_link():
    text = "&#91;&#91;example [[Real]]"
    assert [reference.value for reference in local_references(text)] == ["Real"]


def test_f05_reference_capture_preserves_commonmark_rendering_and_image_labels():
    from server.wiki.format import _markdown

    text = (r"\[\[Literal\]\] [[Real]] &#91;&#91;Entity&#93;&#93;" + "\n\n"
            "[[Title]](Target.md) ![[[Image label]]](image.png)\n")
    assert _markdown.render(text) == MarkdownIt("commonmark").render(text)
    assert [(reference.relation, reference.value) for reference in local_references(text)] == [
        ("wikilink", "Real"), ("markdown_link", "Target.md"), ("markdown_link", "image.png"),
    ]
