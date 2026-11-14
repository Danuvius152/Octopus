"""Heading allocation upgrades preserve old unique citations across consumers."""

import json
import uuid
from urllib.parse import quote

import pytest

from server.wiki import catalog, format, search_index
from server.wiki.curation import WikiCuration
from server.wiki.history import WikiHistory
from server.wiki.links import source_span
from server.wiki.store import WikiError, WikiStore


@pytest.fixture
def store(tmp_path):
    root = tmp_path / "vault"
    (root / "Library").mkdir(parents=True)
    instance = WikiStore(root, tmp_path / "state", uuid.uuid4().hex, create=True)
    try:
        yield instance
    finally:
        instance.close()


@pytest.mark.parametrize("titles,slugs,preserved", [
    (["Evidence", "Evidence-1", "Evidence", "Evidence"],
     ["evidence", "evidence-1-1", "evidence-3", "evidence-2"], {"evidence": 0, "evidence-2": 3}),
    (["Evidence-1", "Evidence", "Evidence", "Evidence"],
     ["evidence-1-1", "evidence", "evidence-3", "evidence-2"], {"evidence": 1, "evidence-2": 3}),
    (["Evidence", "Evidence", "Evidence-1", "Evidence-2"],
     ["evidence", "evidence-3", "evidence-1-1", "evidence-2"], {"evidence": 0, "evidence-2": 3}),
    (["证据", "证据-1", "证据", "证据-2", "证据"],
     ["证据", "证据-1-1", "证据-3", "证据-2-1", "证据-4"], {"证据": 0}),
    (["Evidence", "Evidence-1", "Evidence", "Evidence-1-1", "Evidence"],
     ["evidence", "evidence-1-2", "evidence-3", "evidence-1-1", "evidence-2"],
     {"evidence": 0, "evidence-1-1": 3, "evidence-2": 4}),
    (["Evidence", "Evidence", "Evidence"],
     ["evidence", "evidence-1", "evidence-2"], {"evidence": 0, "evidence-1": 1, "evidence-2": 2}),
])
@pytest.mark.parametrize("nested", [False, True])
def test_new_addresses_are_unique_and_old_unique_addresses_keep_their_sections(store, titles, slugs, preserved, nested):
    prefix = "> " if nested else ""
    text = "# Target\n\n" + "\n".join(
        f"{prefix}## {title}\n{prefix}Marker {index}.\n" for index, title in enumerate(titles)
    )
    target = store.root / "Library/target.md"
    target.write_text(text)
    source = store.read("Library/target.md")
    headings = format.parse(text).headings[1:]
    assert [heading["slug"] for heading in headings] == slugs
    assert len(set(slugs)) == len(slugs)
    for slug, index in preserved.items():
        assert store.read(source["id"], location="#" + slug)["start"] == headings[index]["start"]
    content = "# Consumer\n\n" + "\n".join(f"[Section {index}](target.md#{quote(slug)})" for index, slug in enumerate(slugs))
    curation = WikiCuration(store)
    published = curation.publish("Library/consumer.md", content, "concept", None, None, "human", "Cite each unique section")
    edges = store.graph(published["path"])["edges"]
    assert len(edges) == len(titles)
    assert all(edge["to"] == source["id"] and edge["status"] == "resolved" for edge in edges)
    for index, slug in enumerate(slugs):
        read = store.read(source["id"], location="#" + slug)
        assert read["start"] == headings[index]["start"]
        assert f"Marker {index}." in read["content"]
        span = source_span(text, "#" + slug)
        assert span is not None and text[slice(*span)] == read["content"]
    consumer = store.read(published["path"])
    curation.organize(consumer["path"], consumer["revision"], "Cite the original sections.", None,
        [{"doc": source["id"], "revision": source["revision"], "location": "#" + slug,
          "explanation": f"Original marker {index}."} for index, slug in enumerate(slugs)],
        None, "human", "Verify each source locator")
    assert target.read_text() == text


def test_reserved_ambiguity_cannot_fall_back_to_its_unique_display_title(store):
    text = "# Target\n\n## Evidence\nFirst.\n\n## Evidence-1\nNatural suffix.\n\n## Evidence\nRepeated.\n\n## Evidence\nFourth.\n"
    (store.root / "Library/target.md").write_text(text)
    source = store.read("Library/target.md")
    curation = WikiCuration(store)
    content = "# Consumer\n\n[Ambiguous](target.md#evidence-1)\n"
    with pytest.raises(WikiError):
        curation.publish("Library/consumer.md", content, "concept", None, None, "human", "Do not guess")
    WikiHistory(store).write("Library/imported.md", content.encode(), None, "human", "Import old unresolved citation")
    assert store.graph("Library/imported.md")["edges"][0]["status"] == "ambiguous"
    for location in ("#evidence-1", "#Evidence-1", "Evidence"):
        with pytest.raises(WikiError, match="missing or ambiguous"):
            store.read(source["id"], location=location)
        assert source_span(text, location) is None
    # A bare unique display title is a different query from its old fragment.
    assert "Natural suffix." in store.read(source["id"], location="Evidence-1")["content"]
    current = store.read("Library/imported.md")
    with pytest.raises(WikiError, match="existing line, heading or block"):
        curation.organize(current["path"], current["revision"], "A relation.", None,
            [{"doc": source["id"], "revision": source["revision"], "location": "#evidence-1", "explanation": "Ambiguous old evidence"}],
            None, "human", "Do not accept ambiguous source locator")


def test_encoded_fragment_keeps_unique_display_title_fallback(store):
    text = "# Target\n\n## Condition %23\nLiteral percent evidence.\n"
    (store.root / "Library/target.md").write_text(text)
    source = store.read("Library/target.md")
    href = "target.md#" + quote("Condition %23", safe="")
    result = WikiCuration(store).publish("Library/consumer.md", f"# Consumer\n\n[Evidence]({href})\n", "concept", None, None, "human", "Keep encoded display compatibility")
    assert store.graph(result["path"])["edges"][0]["status"] == "resolved"
    assert store.read(source["id"], location="#Condition %23")["content"] == "## Condition %23\nLiteral percent evidence.\n"


def test_fragment_prefix_preserves_literal_hash_in_display_title_across_consumers(store):
    text = "# Target\n\n## abc\nFirst.\n\n## #abc\nHash evidence.\n"
    target = store.root / "Library/target.md"
    target.write_text(text)
    source = store.read("Library/target.md")
    curation = WikiCuration(store)
    published = curation.publish("Library/consumer.md", "# Consumer\n\n[Hash title](target.md#%23abc)\n",
        "concept", None, None, "human", "Cite the literal hash title")
    edge = store.graph(published["path"])["edges"][0]
    assert edge["to"] == source["id"] and edge["status"] == "resolved"
    expected = "## #abc\nHash evidence.\n"
    assert store.read(source["id"], location="##abc")["content"] == expected
    span = source_span(text, "##abc")
    assert span is not None and text[slice(*span)] == expected
    consumer = store.read(published["path"])
    curation.organize(consumer["path"], consumer["revision"], "Cite the hash section.", None,
        [{"doc": source["id"], "revision": source["revision"], "location": "##abc",
          "explanation": "Original hash evidence."}], None, "human", "Keep the same source section")
    assert target.read_text() == text


def test_html_anchor_existence_remains_separate_from_heading_section_reads(store):
    text = '# Target\n\n## Name %\nFirst.\n\n## Name %\nSecond.\n\n<a id="Name %"></a>\n'
    (store.root / "Library/target.md").write_text(text)
    curation = WikiCuration(store)
    href = "target.md#" + quote("Name %", safe="")
    published = curation.publish("Library/consumer.md", f"# Consumer\n\n[HTML anchor]({href})\n", "concept", None, None, "human", "Keep HTML id existence")
    assert store.graph(published["path"])["edges"][0]["status"] == "resolved"
    with pytest.raises(WikiError, match="missing or ambiguous"):
        store.read("Library/target.md", location="#Name %")


def test_old_derived_versions_rebuild_without_rewriting_original_or_durable_facts(store):
    history = WikiHistory(store)
    text = "# Target\n\n## Evidence\nFirst.\n\n## Evidence-1\nNatural.\n\n## Evidence\nThird.\n\n## Evidence\nFourth.\n"
    history.write("Library/target.md", text.encode(), None, "human", "Original target")
    target = store.read("Library/target.md")
    WikiCuration(store).publish("Library/consumer.md", "# Consumer\n\n[Old unique](target.md#evidence-2)\n", "concept",
        [{"doc": target["id"], "revision": target["revision"], "location": "L1"}], None, "human", "Retain source declaration")
    history.write("Library/imported.md", b"# Import\n\n[Old collision](target.md#evidence-1)\n", None, "human", "Existing old link")
    files = {path: path.read_bytes() for path in store.root.rglob("*.md")}
    durable_tables = ("changes", "change_retention", "managed_notes", "material_origins", "document_identities")
    with store.connect() as db:
        durable = {table: [tuple(row) for row in db.execute(f"SELECT * FROM {table} ORDER BY 1")] for table in durable_tables}
        chapters = [tuple(row)[1:] for row in db.execute("SELECT * FROM search_sections ORDER BY doc_id,start")]
        documents = {row["path"]: (row["revision"], json.loads(row["metadata"]).get("sources")) for row in db.execute("SELECT * FROM documents")}
        # Model the old derived projection; these are caches, never ledgers.
        db.execute("UPDATE documents SET metadata=json_set(metadata,'$.format_version','okf-parser-v11')")
        db.execute("UPDATE catalog_state SET version='direct-links-v4'")
        db.execute("UPDATE wiki_edges SET status='resolved'")
    store.close()
    reopened = WikiStore(store.root, store.state, store.vault_id)
    try:
        assert reopened.graph("Library/imported.md")["edges"][0]["status"] == "ambiguous"
        assert "Fourth." in reopened.read(target["id"], location="#evidence-2")["content"]
        with reopened.connect() as db:
            assert db.execute("SELECT version FROM catalog_state").fetchone()[0] == catalog.VERSION
            assert {json.loads(row[0])["format_version"] for row in db.execute("SELECT metadata FROM documents")} == {format.VERSION}
            assert {row[0] for row in db.execute("SELECT version FROM search_documents")} == {search_index.VERSION}
            assert [tuple(row)[1:] for row in db.execute("SELECT * FROM search_sections ORDER BY doc_id,start")] == chapters
            assert {row["path"]: (row["revision"], json.loads(row["metadata"]).get("sources")) for row in db.execute("SELECT * FROM documents")} == documents
            for table in durable_tables:
                assert [tuple(row) for row in db.execute(f"SELECT * FROM {table} ORDER BY 1")] == durable[table]
        assert all(path.read_bytes() == raw for path, raw in files.items())
    finally:
        reopened.close()
