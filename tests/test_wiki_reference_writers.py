"""Actual writers validate final links, metadata and literal relation targets."""

import uuid
from urllib.parse import quote

import pytest

from server.wiki.contracts import PatchRevision
from server.wiki.curation import WikiCuration
from server.wiki.history import WikiHistory
from server.wiki.revise import revise
from server.wiki.store import WikiError, WikiStore


@pytest.fixture
def store(tmp_path):
    root = tmp_path / "vault"
    root.mkdir()
    instance = WikiStore(root, tmp_path / "state", uuid.uuid4().hex, create=True)
    try:
        yield instance
    finally:
        instance.close()


def note(store, path, text):
    WikiHistory(store).write(path, text.encode(), None, "human", "create")
    return store.read(path)


def organize(store, *, modern=False, **fields):
    current = store.read("Library/note.md")
    key_note = fields.pop("key_note", None)
    return WikiCuration(store).organize(
        current["path"], current["revision"], key_note,
        None, fields.pop("relations", []), None, "human", "check references",
        reuse_existing_summary=key_note is None,
        frontmatter_patch={} if modern else None, **fields,
    )


def patch(store, edits, **metadata):
    document = WikiCuration(store).blocks("Library/note.md")
    parts = document["blocks"]
    changes = []
    for before, after in edits:
        part = next(part for part in parts if before in part["content"])
        start = part["content"].index(before)
        changes.append({"locator": part["locator"], "expected_block_hash": part["sha256"],
                        "start": start, "end": start + len(before),
                        "expected_text": before, "replacement": after, "kind": "correction"})
    body = PatchRevision(doc=document["id"], expected_revision=document["revision"],
                         operation="patch", reason="correct the selected words", edits=changes, **metadata)
    return revise(WikiCuration(store), body, "human", None)


@pytest.mark.parametrize("modern", [False, True])
@pytest.mark.parametrize("filename", ["a#b.md", "a%23b.md", "a?b.md", "甲 😀.md"])
def test_relation_writers_encode_literal_paths_and_keep_checked_sources(store, modern, filename):
    note(store, "Library/a.md", "# Decoy\n\n## b.md\nWrong evidence.\n")
    if filename != "a#b.md":
        note(store, "Library/a#b.md", "# Another decoy\nWrong evidence.\n")
    target = note(store, "Library/" + filename, "# Target\nEvidence.\n")
    source = note(store, "Library/note.md", "# Note\nOriginal words.\n")
    organize(store, modern=modern, relations=[{
        "doc": target["id"], "revision": target["revision"], "location": "L1-L2",
        "explanation": "Supporting evidence in this exact file.",
    }])
    current = store.read(source["id"])
    navigation = [edge for edge in store.graph(source["id"])["edges"]
                  if edge["relation"] == "wikilink" and edge["from"] == source["id"]]
    assert len(navigation) == 1
    assert navigation[0]["to"] == target["id"] and navigation[0]["status"] == "resolved"
    assert navigation[0]["resource"] == quote(target["path"], safe="/")
    assert current["metadata"]["sources"][0]["doc"] == target["id"]
    assert current["metadata"]["source_refs"][0]["doc"] == target["id"]


@pytest.mark.parametrize("link", ["[go](#h)", "[[#H]]"])
def test_self_fragment_survives_first_index_reindex_and_reopen(store, link):
    text = "# Note\n\n## H\n\n" + link + "\n"
    current = note(store, "Library/note.md", text)

    def check(instance):
        edges = instance.graph(current["id"])["edges"]
        assert len(edges) == 1
        assert edges[0]["to"] == current["id"] and edges[0]["status"] == "resolved"

    check(store)
    WikiHistory(store).write(current["path"], (text + "\nMore context.\n").encode(),
                             current["revision"], "human", "append")
    check(store)
    store.close()
    reopened = WikiStore(store.root, store.state, store.vault_id, health=store.health)
    try:
        check(reopened)
    finally:
        reopened.close()


@pytest.mark.parametrize("modern", [False, True])
@pytest.mark.parametrize("collision", [False, True])
def test_organize_validates_new_alias_from_final_metadata(store, modern, collision):
    original = "# Note\nOriginal words.\n"
    note(store, "Library/note.md", original)
    if collision:
        note(store, "Library/other.md", "---\naliases: [NewAlias]\n---\n# Other\nOther words.\n")
        with pytest.raises(WikiError, match="New local.*ambiguous"):
            organize(store, modern=modern, key_note="See [[NewAlias]].", aliases=["NewAlias"])
        assert store.path("Library/note.md").read_text() == original
        assert "NewAlias" not in store.read("Library/note.md")["metadata"].get("aliases", [])
    else:
        organize(store, modern=modern, key_note="See [[NewAlias]].", aliases=["NewAlias"])
        current = store.read("Library/note.md")
        assert any(edge["to"] == current["id"] and edge["status"] == "resolved"
                   for edge in store.graph(current["id"])["edges"])


@pytest.mark.parametrize("collision", [False, True])
@pytest.mark.parametrize("remove", [False, True])
def test_organize_uses_planned_managed_aliases(store, collision, remove):
    note(store, "Library/note.md", "# Note\nOriginal words.\n")
    organize(store, aliases=["OwnedAlias"])
    other = None
    if collision:
        other = note(store, "Library/other.md", "---\naliases: [OwnedAlias]\n---\n# Other\nEvidence.\n")
    before = store.path("Library/note.md").read_bytes()
    fields = {"aliases": []} if remove else {}
    rejected = not collision if remove else collision
    if rejected:
        with pytest.raises(WikiError, match="New local"):
            organize(store, key_note="See [[OwnedAlias]].", **fields)
        assert store.path("Library/note.md").read_bytes() == before
    else:
        organize(store, key_note="See [[OwnedAlias]].", **fields)
        current = store.read("Library/note.md")
        target = other["id"] if remove else current["id"]
        assert any(edge["to"] == target and edge["status"] == "resolved"
                   for edge in store.graph(current["id"])["edges"])


@pytest.mark.parametrize("collision", [False, True])
@pytest.mark.parametrize("mode", ["retain", "new", "remove"])
def test_revise_overlay_uses_retained_replaced_or_removed_managed_alias(store, collision, mode):
    note(store, "Library/note.md", "# Note\n\nKept words PENDING and reason.\n")
    alias = "NewAlias" if mode == "new" else "OwnedAlias"
    if mode != "new":
        organize(store, aliases=[alias])
    other = None
    if collision:
        other = note(store, "Library/other.md", f"---\naliases: [{alias}]\n---\n# Other\nEvidence.\n")
    before = store.path("Library/note.md").read_bytes()
    metadata = {} if mode == "retain" else {"aliases": [alias] if mode == "new" else []}
    rejected = not collision if mode == "remove" else collision
    if rejected:
        with pytest.raises(WikiError, match="New local"):
            patch(store, [("PENDING", f"[[{alias}]]")], **metadata)
        assert store.path("Library/note.md").read_bytes() == before
    else:
        patch(store, [("PENDING", f"[[{alias}]]")], **metadata)
        current = store.read("Library/note.md")
        target = other["id"] if mode == "remove" else current["id"]
        edges = [edge for edge in store.graph(current["id"])["edges"] if edge["from"] == current["id"]]
        assert len(edges) == 1 and edges[0]["to"] == target and edges[0]["status"] == "resolved"
        assert (alias in current["metadata"].get("aliases", [])) == (mode != "remove")


@pytest.mark.parametrize("body,edits", [
    ("Kept words [[TargetA]] and reason.", [("TargetA", "Missing")]),
    ("Kept [go](TargetA.md#good) and reason.", [("good", "absent")]),
    ("Kept words LEFTMissingRIGHT and reason.", [("LEFT", "[["), ("RIGHT", "]]")]),
])
def test_sparse_edits_validate_links_assembled_in_final_candidate(store, body, edits):
    note(store, "Library/TargetA.md", "# TargetA\n\n## Good\nEvidence.\n")
    original = "# Note\n\n" + body + "\n"
    note(store, "Library/note.md", original)
    with pytest.raises(WikiError, match="New local"):
        patch(store, edits)
    assert store.path("Library/note.md").read_text() == original


def test_sparse_edit_keeps_unchanged_broken_link_and_code_is_not_a_new_link(store):
    original = "# Note\n\nKept [[OldMissing]] and `PENDING` as an example.\n"
    note(store, "Library/note.md", original)
    patch(store, [("PENDING", "[[MissingInCode]]")])
    assert store.path("Library/note.md").read_text() == original.replace("PENDING", "[[MissingInCode]]")


@pytest.mark.parametrize("collision", [False, True])
def test_restructure_validates_after_applying_new_alias(store, collision):
    original = "# Note\n\nOriginal words with their limits.\n"
    current = note(store, "Library/note.md", original)
    if collision:
        note(store, "Library/other.md", "---\naliases: [NewAlias]\n---\n# Other\nEvidence.\n")
    curation = WikiCuration(store)
    parts = curation.blocks(current["id"])["blocks"]

    def restructure():
        return curation.restructure(
            current["path"], current["revision"], [part["id"] for part in parts],
            [{"before": len(parts), "content": "See [[NewAlias]].", "sources": []}],
            "human", "explain the self reference", aliases=["NewAlias"],
        )

    if collision:
        with pytest.raises(WikiError, match="New local.*ambiguous"):
            restructure()
        assert store.path(current["path"]).read_text() == original
    else:
        restructure()
        assert "Original words with their limits." in store.path(current["path"]).read_text()
        assert any(edge["to"] == current["id"] and edge["status"] == "resolved"
                   for edge in store.graph(current["id"])["edges"])


def test_candidate_preserves_portable_native_alias_projection(store):
    content = '<!-- octopus-wiki {"aliases": ["PortableAlias"]} -->\n# Note\n\nSee [[PortableAlias]].\n'
    result = WikiCuration(store).publish(
        "Library/note.md", content, "concept", None, None, "human", "publish",
    )
    current = store.read(result["path"])
    assert current["metadata"]["aliases"] == ["PortableAlias"]
    assert any(edge["to"] == current["id"] and edge["status"] == "resolved"
               for edge in store.graph(current["id"])["edges"])
