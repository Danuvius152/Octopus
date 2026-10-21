"""Current-body projections share one parse without caching mutable tokens."""

import uuid

import pytest
from markdown_it import MarkdownIt

from server.wiki import catalog, format, search_index
from server.wiki.curation import WikiCuration
from server.wiki.extract import extract
from server.wiki.references import local_references
from server.wiki.store import WikiStore

TEXT = "---\ntitle: Fixture\n---\n# Fixture\n\nVisible body [self](#fixture).\n"


def count_parses(monkeypatch):
    calls = []
    original = MarkdownIt.parse

    def counted(self, source, env=None):
        calls.append(source)
        return original(self, source, env)

    monkeypatch.setattr(MarkdownIt, "parse", counted)
    return calls


@pytest.mark.parametrize("entry", ["construct", "refresh", "index", "search", "prepared"])
def test_indexing_shares_current_parse_but_catalog_reads_current_body(tmp_path, monkeypatch, entry):
    root = tmp_path / "vault"
    root.mkdir()
    store = None
    try:
        if entry != "construct":
            store = WikiStore(root, tmp_path / "state", str(uuid.uuid4()), create=True)
        (root / "note.md").write_text(TEXT)
        if entry == "prepared":
            extracted = extract(TEXT.encode(), ".md")
            prepared = {"extracted": extracted, "parsed": format.parse(extracted["text"])}
        calls = count_parses(monkeypatch)
        if entry == "construct":
            store = WikiStore(root, tmp_path / "state", str(uuid.uuid4()), create=True)
        elif entry == "refresh":
            store.refresh()
        elif entry == "search":
            assert store.search("Visible")["items"][0]["path"] == "note.md"
        else:
            with store.connect() as db:
                store._index(db, "note.md", prepared=prepared if entry == "prepared" else None)
        # One shared format/search parse, and one current-text catalog parse.
        assert calls == [TEXT[format.metadata_end(TEXT):]] * (1 if entry == "prepared" else 2)
        with store.connect() as db:
            assert db.execute("SELECT count(*) FROM wiki_edges").fetchone()[0] == 1
            # A later catalog batch owns its own current text, not old tokens.
            later = TEXT + "\n[second](#fixture)\n"
            db.execute("UPDATE documents SET text=?", (later,))
            db.execute("INSERT OR IGNORE INTO catalog_dirty SELECT id FROM documents")
            calls.clear()
            catalog.finish(db)
            assert calls == [later[format.metadata_end(later):]]
            assert db.execute("SELECT count(*) FROM wiki_edges").fetchone()[0] == 2
    finally:
        if store is not None:
            store.close()


def test_reference_default_parses_once_and_explicit_body_start_is_preserved(monkeypatch):
    text = "---\n[metadata](ignored.md)\n---\n[body][ref] [[a%23b.md#%23]]\n\n[ref]: a%2523b.md#%2523\n"
    calls = count_parses(monkeypatch)
    references = list(local_references(text))
    assert calls == [text[format.metadata_end(text):]]
    assert [(ref.raw_resource, ref.value, ref.fragment) for ref in references] == [
        ("a%2523b.md#%2523", "a%23b.md", "%23"),
        ("a%23b.md#%23", "a#b.md", "#"),
    ]
    calls.clear()
    explicit = list(local_references(text, body_start=0))
    assert calls == [text]
    assert explicit[0].value == "ignored.md"
    parsed = format.parse(text)
    calls.clear()
    assert list(local_references(text, body_start=0, parsed=parsed)) == explicit
    assert calls == [text]


@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"])
@pytest.mark.parametrize("prefix", ["", "\ufeff", "---\ntitle: Test\n---\n", "\ufeff---\ntitle: [broken\n---\n"])
def test_rendering_keeps_reference_and_section_projections_in_original_coordinates(newline, prefix):
    text = (prefix + "# Fixture\n\nleft\u2028right\n\n## Evidence\n\n"
            "![visible *alt*][image] [local][target] [[甲%20乙.md]] [^foot]\n\n"
            "[image]: image.png\n[target]: local.md#Evidence\n").replace("\n", newline)
    parsed = format.parse(text)
    images = [child for token in parsed.tokens for child in token.children or [] if child.type == "image"]
    assert images[0].attrGet("alt") == ""
    before = list(local_references(text, parsed=parsed))
    first = search_index.sections(text, "Fixture", parsed=parsed)
    assert images[0].attrGet("alt") == "visible alt"
    assert search_index.sections(text, "Fixture", parsed=parsed) == first
    assert search_index.sections(text, "Fixture") == first
    assert list(local_references(text, parsed=parsed)) == before == list(local_references(text))
    assert [ref.value for ref in before] == ["image.png", "local.md", "甲 乙.md"]
    evidence = next(part for part in first if part["start"] == text.index("## Evidence"))
    assert evidence["start"] == text.index("## Evidence")
    assert evidence["end"] == len(text) and evidence["readable_body"] is True
    assert parsed.footnotes == ["foot"]
    # The existing YAML boundary accepts LF/CRLF, while bare CR stays prose.
    assert (parsed.error is not None) == ("broken" in prefix and newline != "\r")
    fresh = format.parse(text)
    assert fresh.tokens is not parsed.tokens and fresh.env is not parsed.env
    assert next(child for token in fresh.tokens for child in token.children or [] if child.type == "image").attrGet("alt") == ""


def test_link_validation_reuses_candidate_parse_for_metadata(tmp_path, monkeypatch):
    root = tmp_path / "vault"
    root.mkdir()
    (root / "note.md").write_text(TEXT)
    store = WikiStore(root, tmp_path / "state", str(uuid.uuid4()), create=True)
    try:
        candidate = TEXT + "\nAdded [self](#fixture).\n"
        calls = count_parses(monkeypatch)
        WikiCuration(store).validate_links("note.md", candidate)
        assert calls == [candidate[format.metadata_end(candidate):]]
    finally:
        store.close()
