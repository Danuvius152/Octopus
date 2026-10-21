"""Structural additions preserve source bytes without inventing body claims."""

import json

import pytest

from server.wiki import format, material_proof, origins
from server.wiki.curation import WikiCuration
from server.wiki.history import WikiHistory
from server.wiki.store import WikiError, WikiStore


@pytest.fixture
def history(tmp_path):
    root = tmp_path / "vault"
    root.mkdir()
    store = WikiStore(root, tmp_path / "state", "layout-contract", create=True)
    try:
        yield WikiHistory(store)
    finally:
        store.close()


HEADINGS = [
    "## 正文",
    "### Methods and limits ###",
    "   # **Evidence** and `notation`",
    "Discussion\n==========",
    "Scope and exceptions\n--------------------",
    "## One\n\n### Two",
    "A long\nsection title\n===",
    "## [Evidence](https://example.org/evidence)",
]


@pytest.mark.parametrize("heading", HEADINGS)
@pytest.mark.parametrize("newline,bom", [("\n", ""), ("\r\n", "\ufeff")])
def test_heading_addition_has_only_requested_structure_and_keeps_original_proof(
    history, heading, newline, bom
):
    original = bom + (
        "---\ncustom: 'keep spelling' # user comment\n---\n"
        "# Existing title\n\n## Key note\n\nThe useful summary is conditional.\n\n"
        "Original argument with its exception.\n\nRepeated paragraph.\n\nRepeated paragraph.\n"
    ).replace("\n", newline)
    raw = original.encode()
    history.write("Library/note.md", raw, None, "human", "create")
    curation = WikiCuration(history.store)
    current = history.store.read("Library/note.md")
    parts = curation.blocks(current["id"])["blocks"]
    before = next(
        i for i, part in enumerate(parts) if "Original argument" in part["content"]
    )
    heading = heading.replace("\n", newline)
    result = curation.restructure(
        current["path"],
        current["revision"],
        [p["id"] for p in parts],
        [{"before": before, "content": heading, "sources": []}],
        "curator",
        "Insert only a section boundary",
    )
    output = history.store.path(current["path"]).read_bytes()
    expected = original.replace(
        "Original argument", heading + newline * 2 + "Original argument", 1
    )
    assert output == expected.encode()
    assert b"<!--" not in output and "Weber" not in output.decode()
    with history.store.connect() as db:
        state = origins.get(db, current["id"])
        actor = db.execute(
            "SELECT actor FROM changes WHERE after_hash=?", (result["revision"],)
        ).fetchone()[0]
    assert actor == "curator"
    assert material_proof.reconstruct(state, current["id"], output) == raw


@pytest.mark.parametrize(
    "addition",
    [
        "A new explanation, with a condition.",
        "## Methods\n\nA new explanation.",
        "## Methods\n\n<!-- A hidden claim. -->",
        "## Methods <!-- A hidden claim. -->",
        "## Methods\n\n[ref]: https://example.org/source",
        "## [Methods][ref]\n\n[ref]: https://example.org/source",
        "```md\n## Methods\n```",
        "    ## This is indented code",
        "####### This is a paragraph",
        "> ## A quoted heading",
        "## ![New image](https://example.org/image.png)",
        "## Methods\n\n---",
    ],
)
def test_explanations_and_hidden_content_cannot_bypass_attribution(history, addition):
    original = b"# Note\n\nOriginal source with its limit.\n"
    history.write("Library/note.md", original, None, "human", "create")
    curation = WikiCuration(history.store)
    current = history.store.read("Library/note.md")
    parts = curation.blocks(current["id"])["blocks"]
    curation.restructure(
        current["path"],
        current["revision"],
        [p["id"] for p in parts],
        [{"before": len(parts), "content": addition, "sources": []}],
        "curator",
        "Add an explanation",
    )
    output = history.store.path(current["path"]).read_bytes()
    assert output.startswith(original)
    assert (
        "<!-- Weber 整理; sources: [] -->\n> Weber 整理，来源待核验\n\n"
        in output.decode()
    )
    assert addition in output.decode()
    with history.store.connect() as db:
        state = origins.get(db, current["id"])
    assert material_proof.reconstruct(state, current["id"], output) == original


@pytest.mark.parametrize(
    "original", ["```text\nUnclosed code", "<!--\nUnclosed comment"]
)
def test_structure_swallowed_by_surrounding_context_is_rejected_before_write(
    history, original
):
    created = history.write(
        "Library/note.md", original.encode(), None, "human", "create"
    )
    curation = WikiCuration(history.store)
    parts = curation.blocks("Library/note.md")["blocks"]
    with pytest.raises(WikiError, match="remain headings"):
        curation.restructure(
            "Library/note.md",
            created["revision"],
            [p["id"] for p in parts],
            [{"before": len(parts), "content": "## Evidence", "sources": []}],
            "curator",
            "Insert a real heading",
        )
    assert history.store.path("Library/note.md").read_bytes() == original.encode()


@pytest.mark.parametrize("ending", ["", "\n", "\n\n", "\r\n"])
def test_headings_at_eof_use_only_needed_separators_and_still_render(history, ending):
    original = "Original paragraph." + ending
    created = history.write(
        "Library/note.md", original.encode(), None, "human", "create"
    )
    curation = WikiCuration(history.store)
    parts = curation.blocks("Library/note.md")["blocks"]
    curation.restructure(
        "Library/note.md",
        created["revision"],
        [p["id"] for p in parts],
        [
            {"before": len(parts), "content": "## Evidence", "sources": []},
            {"before": len(parts), "content": "Scope\n-----", "sources": []},
        ],
        "curator",
        "Insert headings",
    )
    output = history.store.path("Library/note.md").read_bytes().decode()
    newline = "\r\n" if ending == "\r\n" else "\n"
    missing = 2 - ending.count("\n")
    assert (
        output
        == original + newline * missing + "## Evidence" + newline * 2 + "Scope\n-----"
    )
    assert [h["title"] for h in format.parse(output).headings] == ["Evidence", "Scope"]


def test_layout_covers_all_original_blocks_beyond_navigation_page_limits(history):
    original = (
        "# Long note\n\n"
        + "\n\n".join(f"Paragraph {i}: condition and exception." for i in range(80))
        + "\n"
    )
    created = history.write(
        "Library/note.md", original.encode(), None, "human", "create"
    )
    curation = WikiCuration(history.store)
    parts = curation.blocks("Library/note.md", limit=100, budget_chars=96_000)["blocks"]
    assert len(parts) > 30
    curation.restructure(
        "Library/note.md",
        created["revision"],
        [p["id"] for p in parts],
        [{"before": len(parts), "content": "## Further work", "sources": []}],
        "curator",
        "Keep the full document",
    )
    output = history.store.path("Library/note.md").read_bytes()
    assert output == (original + "\n## Further work").encode()


def test_heading_sources_remain_in_private_receipt_without_visible_body_claims(history):
    created = history.write(
        "Library/note.md", b"Original paragraph.\n", None, "human", "create"
    )
    source = history.write(
        "Library/source.md", b"# Source\n\nEvidence.\n", None, "human", "create"
    )
    curation = WikiCuration(history.store)
    parts = curation.blocks("Library/note.md")["blocks"]
    refs = [
        {"doc": "Library/source.md", "revision": source["revision"], "location": "L3"}
    ]
    result = curation.restructure(
        "Library/note.md",
        created["revision"],
        [p["id"] for p in parts],
        [{"before": len(parts), "content": "## Evidence", "sources": refs}],
        "curator",
        "Insert a sourced boundary",
    )
    with history.store.connect() as db:
        receipt = db.execute(
            "SELECT details FROM operation_receipts WHERE id=?", (result["receipt_id"],)
        ).fetchone()[0]
    details = json.loads(receipt)["additions"]
    assert details[0]["kind"] == "structure"
    assert details[0]["sources"][0]["revision"] == source["revision"]
    assert "Weber" not in history.store.path("Library/note.md").read_text()


@pytest.mark.parametrize(
    "separator", ["\u2028", "\u2029", "\x85", "\v", "\f", "\x1c", "\x1d", "\x1e"]
)
@pytest.mark.parametrize("heading", ["## Inserted", "Inserted\n--------"])
@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_unicode_non_commonmark_separators_keep_real_heading_spans_and_source(
    history, separator, heading, newline
):
    original = f"# Top{newline}{newline}left{separator}right{newline}"
    created = history.write(
        "Library/note.md", original.encode(), None, "human", "create"
    )
    curation = WikiCuration(history.store)
    parts = curation.blocks("Library/note.md")["blocks"]
    heading = heading.replace("\n", newline)
    curation.restructure(
        "Library/note.md",
        created["revision"],
        [p["id"] for p in parts],
        [{"before": len(parts), "content": heading, "sources": []}],
        "curator",
        "Insert a heading after a retained Unicode character",
    )
    output = history.store.path("Library/note.md").read_bytes().decode()
    assert output == original + newline + heading
    current = history.store.read("Library/note.md")
    inserted = next(
        h for h in format.parse(output).headings if h["title"] == "Inserted"
    )
    assert (inserted["start"], inserted["end"]) == (output.index(heading), len(output))
    assert current["metadata"]["body_sections"][-1]["start"] == output.index(heading)
    with history.store.connect() as db:
        state = origins.get(db, current["id"])
    assert (
        material_proof.reconstruct(state, current["id"], output.encode())
        == original.encode()
    )


@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"])
def test_current_heading_ranges_and_search_evidence_share_markdown_lines(newline):
    from server.wiki import search_index

    text = (
        "# Source\n\nleft\u2028right\n\n## Evidence\n\nOnly under condition A.\n"
    ).replace("\n", newline)
    heading = next(h for h in format.parse(text).headings if h["title"] == "Evidence")
    start = text.index("## Evidence")
    assert (heading["start"], heading["end"]) == (
        start,
        start + len("## Evidence") + len(newline),
    )
    section = next(
        p for p in search_index.sections(text, "Source") if p["start"] == start
    )
    assert section["start"] == start and section["end"] == len(text)
    assert section["readable_body"] is True
    assert text[section["start"] : section["end"]].endswith(
        "Only under condition A." + newline
    )


def test_old_parser_index_refresh_preserves_document_bytes_identity_and_durable_history(
    history,
):
    from server.wiki import search_index

    store = history.store
    raw = (
        b"# Source\n\nleft\xe2\x80\xa8right\n\n## Evidence\n\nOnly under condition A.\n"
    )
    history.write("Library/note.md", raw, None, "human", "create")
    current = store.read("Library/note.md")
    curation = WikiCuration(store)
    parts = curation.blocks(current["id"])["blocks"]
    curation.restructure(
        current["path"],
        current["revision"],
        [p["id"] for p in parts],
        [{"before": len(parts), "content": "## Follow-up", "sources": []}],
        "curator",
        "Add a structural boundary",
    )
    current = store.read(current["id"])
    raw_after = store.path(current["path"]).read_bytes()
    tables = [
        "changes",
        "change_retention",
        "change_management",
        "change_material_origins",
        "material_origins",
        "operation_receipts",
        "managed_notes",
        "document_identities",
        "uid_aliases",
    ]
    with store.connect() as db:
        metadata = json.loads(
            db.execute(
                "SELECT metadata FROM documents WHERE id=?", (current["id"],)
            ).fetchone()[0]
        )
        metadata["format_version"] = "okf-parser-v4"
        metadata["body_sections"] = [
            {"start": 0, "end": 1, "role": "body", "title": "outdated"}
        ]
        db.execute(
            "UPDATE documents SET metadata=? WHERE id=?",
            (json.dumps(metadata), current["id"]),
        )
        db.execute("UPDATE search_documents SET version='sections-v7'")
        db.execute(
            "UPDATE search_sections SET start=0,end=1,readable_body=0 WHERE doc_id=?",
            (current["id"],),
        )
        durable = {
            name: [
                tuple(row)
                for row in db.execute("SELECT * FROM " + name + " ORDER BY rowid")
            ]
            for name in tables
        }
    root, state, vault_id = store.root, store.state, store.vault_id
    store.close()
    upgraded = WikiStore(root, state, vault_id)
    try:
        read = upgraded.read(current["id"])
        assert (read["id"], read["revision"], read["metadata"]["uid"]) == (
            current["id"],
            current["revision"],
            current["metadata"]["uid"],
        )
        assert upgraded.path(current["path"]).read_bytes() == raw_after
        assert read["metadata"]["format_version"] == format.VERSION
        assert read["metadata"]["body_sections"][-1]["start"] == read["content"].index(
            "## Follow-up"
        )
        with upgraded.connect() as db:
            evidence = db.execute(
                "SELECT start,end,readable_body FROM search_sections WHERE doc_id=? AND start=?",
                (current["id"], read["content"].index("## Evidence")),
            ).fetchone()
            assert tuple(evidence) == (
                read["content"].index("## Evidence"),
                read["content"].index("## Follow-up"),
                1,
            )
            assert (
                db.execute(
                    "SELECT version FROM search_documents WHERE doc_id=?",
                    (current["id"],),
                ).fetchone()[0]
                == search_index.VERSION
            )
            assert durable == {
                name: [
                    tuple(row)
                    for row in db.execute("SELECT * FROM " + name + " ORDER BY rowid")
                ]
                for name in tables
            }
            assert (
                material_proof.reconstruct(
                    origins.get(db, current["id"]), current["id"], raw_after
                )
                == raw
            )
    finally:
        upgraded.close()
