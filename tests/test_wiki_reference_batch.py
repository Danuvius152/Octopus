"""Batch identity decisions, invalidation and immutable source proof."""

import uuid

import pytest

from server.wiki import catalog
from server.wiki import store as store_module
from server.wiki.curation import WikiCuration
from server.wiki.history import WikiHistory
from server.wiki.store import WikiError, WikiStore

CLAIM = "urn:uuid:7c1fd8f1-6ff3-4c28-99bb-a0d14567663f"


@pytest.fixture
def store(tmp_path):
    root = tmp_path / "vault"
    root.mkdir()
    current = WikiStore(root, tmp_path / "state", uuid.uuid4().hex, create=True)
    try:
        yield current
    finally:
        current.close()


def write(store, path, text):
    target = store.root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text)


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("missing", ["wiki_uid_claims", "wiki_uid_dirty"])
def test_duplicate_fresh_uid_claims_have_no_first_file_winner(store, reverse, missing, monkeypatch):
    files = ["first/a.md", "last/z.md"]
    for path, title in zip(files, ["One", "Two"], strict=True):
        write(store, path, f"---\nuid: {CLAIM}\n---\n# {title}\nOriginal evidence.\n")
    if reverse:
        original_walk = store_module.os.walk

        def reversed_inventory(path, *args, **kwargs):
            rows = list(original_walk(path, *args, **kwargs))
            return iter(reversed(rows) if path == store.root else rows)

        monkeypatch.setattr(store_module.os, "walk", reversed_inventory)
    store.refresh()
    with store.connect() as db:
        assert db.execute("SELECT doc_id FROM uid_aliases WHERE uid=?", (CLAIM,)).fetchone() is None
    for path in files:
        metadata = store.read(path)["metadata"]
        assert metadata.get("uid") != CLAIM and metadata.get("uid_conflict")
    # Claims are a derived projection. Rebuilding them must still collect the
    # complete set before deciding an unowned alias.
    with store.connect() as db:
        db.execute("DROP TABLE " + missing)
    store.close()
    reopened = WikiStore(store.root, store.state, store.vault_id, health=store.health)
    try:
        with reopened.connect() as db:
            assert db.execute("SELECT 1 FROM uid_aliases WHERE uid=?", (CLAIM,)).fetchone() is None
    finally:
        reopened.close()


@pytest.mark.parametrize("source_first", [False, True])
def test_forward_uid_source_is_resolved_in_the_first_complete_batch(store, source_first):
    source, target = ("a-source.md", "z-target.md") if source_first else ("z-source.md", "a-target.md")
    write(store, source, f"---\nsources:\n  - resource: {CLAIM}\n---\n# Consumer\nConditional claim.\n")
    write(store, target, f"---\nuid: {CLAIM}\n---\n# Evidence\nOriginal source.\n")
    store.refresh()
    expected = store.read(target)["id"]
    dependency = store.read(source)["metadata"]["source_refs"][0]
    assert dependency["doc"] == expected


@pytest.mark.parametrize("relation", ["source", "navigation"])
def test_absent_exact_doc_reference_resolves_when_identity_appears_in_later_batch(store, relation):
    target = uuid.uuid5(uuid.UUID(store.vault_id), "future.md").hex
    consumer = (f"---\nsources:\n  - doc: {target}\n---\n# Consumer\n" if relation == "source"
                else f"# Consumer\n\nSee [[{target}]].\n")
    write(store, "consumer.md", consumer)
    store.refresh()
    document = store.read("consumer.md")
    with store.connect() as db:
        assert not db.execute("SELECT 1 FROM document_identities WHERE id=?", (target,)).fetchone()
        edge = db.execute("SELECT target_doc,status FROM wiki_edges WHERE source_doc=?", (document["id"],)).fetchone()
        assert edge[:] == (None, "unresolved")
        assert db.execute("SELECT namespace,key FROM wiki_edge_lookups WHERE source_doc=?", (document["id"],)).fetchall()[0][:] == ("doc", target)
    # Creating through the filesystem assigns the known first-index identity;
    # no consumer edit or cache rebuild should be required to wake its edge.
    write(store, "future.md", "# Future evidence\nOriginal words.\n")
    store.refresh()
    with store.connect() as db:
        edge = db.execute("SELECT target_doc,status FROM wiki_edges WHERE source_doc=?", (document["id"],)).fetchone()
        assert edge[:] == (target, "resolved")
        assert db.execute("SELECT pending FROM catalog_state WHERE singleton=1").fetchone()[0] == 0
    assert store.read("consumer.md")["revision"] == document["revision"]
    assert store.read("future.md")["id"] == target
    store.close()
    reopened = WikiStore(store.root, store.state, store.vault_id, health=store.health)
    try:
        edge = reopened.graph("consumer.md")["edges"][0]
        assert (edge["to"], edge["status"]) == (target, "resolved")
        if relation == "source":
            assert reopened.read("consumer.md")["metadata"]["source_refs"][0]["doc"] == target
    finally:
        reopened.close()


@pytest.mark.parametrize("portable_first", [False, True])
def test_default_and_portable_uid_claims_are_decided_together(store, portable_first):
    portable, default = ("a-portable.md", "z-default.md") if portable_first else ("z-portable.md", "a-default.md")
    default_id = uuid.uuid5(uuid.UUID(store.vault_id), default).hex
    contested = "urn:uuid:" + str(uuid.UUID(default_id))
    write(store, portable, f"---\nuid: {contested}\n---\n# Portable\nOriginal words.\n")
    write(store, default, "# Default identity\nOriginal words.\n")
    store.refresh()
    with store.connect() as db:
        assert db.execute("SELECT doc_id FROM uid_aliases WHERE uid=?", (contested,)).fetchone() is None
    # With no uncontested alias, exact doc identity remains available.
    assert store.read(default_id)["metadata"].get("uid") is None
    assert store.read(portable)["metadata"].get("uid") != contested


def test_later_portable_claim_cannot_take_an_existing_durable_uid_owner(store):
    write(store, "owner.md", f"---\nuid: {CLAIM}\n---\n# Owner\nEvidence.\n")
    store.refresh()
    owner = store.read("owner.md")["id"]
    write(store, "later.md", f"---\nuid: {CLAIM}\n---\n# Later\nOther evidence.\n")
    store.refresh()
    with store.connect() as db:
        assert db.execute("SELECT doc_id FROM uid_aliases WHERE uid=?", (CLAIM,)).fetchone()[0] == owner
    assert store.read("later.md")["metadata"].get("uid_conflict")


def test_alias_ambiguity_is_invalidated_after_every_addition_and_deletion(store):
    write(store, "a.md", "---\naliases: [Shared]\n---\n# A\nEvidence.\n")
    write(store, "consumer.md", "# Consumer\n\nSee [[Shared]].\n")
    store.refresh()
    a = store.read("a.md")["id"]

    def edge():
        return store.graph("consumer.md")["edges"][0]

    assert edge()["to"] == a and edge()["status"] == "resolved"
    write(store, "b.md", "---\naliases: [Shared]\n---\n# B\nOther evidence.\n")
    store.refresh()
    assert edge()["to"] is None and edge()["status"] == "ambiguous"
    (store.root / "b.md").unlink()
    store.refresh()
    assert edge()["to"] == a and edge()["status"] == "resolved"
    (store.root / "a.md").unlink()
    store.refresh()
    assert edge()["to"] is None and edge()["status"] == "unresolved"


@pytest.mark.parametrize("edit", ["equivalent_spelling", "locator_only", "remove_and_readd"])
def test_source_proof_survives_target_move_path_reuse_and_reference_edit(store, edit):
    history = WikiHistory(store)
    history.write("a.md", b"# A\nOriginal evidence.\n", None, "human", "create")
    a = store.read("a.md")
    before = f"---\nsources:\n  - resource: a.md\n    revision: {a['revision']}\n    location: L1-L2\n---\n# Consumer\nConditional claim.\n"
    history.write("consumer.md", before.encode(), None, "human", "create")
    assert store.read("consumer.md")["metadata"]["source_refs"][0]["doc"] == a["id"]
    (store.root / "a.md").rename(store.root / "moved.md")
    store.refresh()
    history.write("a.md", b"# B\nDifferent evidence at the old path.\n", None, "human", "create")
    b = store.read("a.md")
    assert b["id"] != a["id"]
    if edit == "remove_and_readd":
        current = store.read("consumer.md")
        history.write("consumer.md", b"# Consumer\nConditional claim.\n", current["revision"], "human", "remove reference")
        after = before
    elif edit == "locator_only":
        after = before.replace("L1-L2", "L2")
    else:
        after = before.replace("resource: a.md", "resource: ./a.md")
    current = store.read("consumer.md")
    history.write("consumer.md", after.encode(), current["revision"], "human", "edit reference evidence")
    dependency = store.read("consumer.md")["metadata"]["source_refs"][0]
    assert dependency["doc"] == a["id"]
    assert dependency["doc"] != b["id"]


def test_navigation_records_absent_and_losing_candidates_for_priority_changes(store):
    write(store, "elsewhere.md", "---\naliases: [Target]\n---\n# Elsewhere\nEvidence.\n")
    write(store, "Library/consumer.md", "# Consumer\n\nSee [[Target]].\n")
    store.refresh()
    fallback = store.read("elsewhere.md")["id"]
    consumer = store.read("Library/consumer.md")["id"]
    with store.connect() as db:
        keys = {(row[0], row[1]) for row in db.execute("SELECT namespace,key FROM wiki_edge_lookups WHERE source_doc=?", (consumer,))}
    assert {("path", "Library/Target.md"), ("path", "Target.md"), ("name", "Target")} <= keys
    assert store.graph(consumer)["edges"][0]["to"] == fallback
    write(store, "Library/Target.md", "# Higher priority\nEvidence.\n")
    store.refresh()
    assert store.graph(consumer)["edges"][0]["to"] == store.read("Library/Target.md")["id"]
    (store.root / "Library/Target.md").unlink()
    store.refresh()
    assert store.graph(consumer)["edges"][0]["to"] == fallback


def test_managed_alias_add_remove_and_target_anchor_edits_invalidate_navigation(store):
    write(store, "a.md", "# A\n\n## Good\nEvidence.\n")
    write(store, "consumer.md", "# Consumer\n\nSee [[Owned]] and [detail](a.md#good).\n")
    store.refresh()
    curation = WikiCuration(store)
    current = store.read("a.md")
    curation.organize("a.md", current["revision"], None, None, [], None,
                      "human", "add alias", aliases=["Owned"], reuse_existing_summary=True)
    edges = {edge["resource"]: edge for edge in store.graph("consumer.md")["edges"]}
    assert edges["Owned"]["to"] == current["id"]
    current = store.read("a.md")
    curation.organize("a.md", current["revision"], None, None, [], None,
                      "human", "remove alias", aliases=[], reuse_existing_summary=True)
    raw = (store.root / "a.md").read_text().replace("## Good", "## Changed")
    write(store, "a.md", raw)
    store.refresh()
    edges = {edge["resource"]: edge for edge in store.graph("consumer.md")["edges"]}
    assert edges["Owned"]["to"] is None and edges["Owned"]["status"] == "unresolved"
    assert edges["a.md#good"]["status"] == "missing_anchor"


def test_persistent_graph_pending_resumes_and_missing_reverse_cache_rebuilds(store):
    write(store, "a.md", "# A\nEvidence.\n")
    write(store, "consumer.md", "# Consumer\n\nSee [[a]].\n")
    store.refresh()
    target = store.read("a.md")["id"]
    consumer = store.read("consumer.md")["id"]
    with store.connect() as db:
        db.execute("INSERT OR IGNORE INTO catalog_dirty VALUES(?)", (consumer,))
        db.execute("UPDATE catalog_state SET pending=1")
    assert store.index_state()["index_status"] == "updating"
    store.synchronize()
    assert store.index_state()["index_status"] == "ready"
    with store.connect() as db:
        db.execute("DROP TABLE wiki_edge_lookups")
    store.close()
    reopened = WikiStore(store.root, store.state, store.vault_id, health=store.health)
    try:
        assert reopened.graph(consumer)["edges"][0]["to"] == target
        assert reopened.index_state()["index_status"] == "ready"
        with reopened.connect() as db:
            assert db.execute("SELECT 1 FROM wiki_edge_lookups WHERE source_doc=?", (consumer,)).fetchone()
    finally:
        reopened.close()


def test_graph_failure_rolls_back_before_acknowledging_filesystem_receipts(store, monkeypatch):
    write(store, "a.md", "# A\nEvidence.\n")
    write(store, "consumer.md", "# Consumer\n\nSee [[a]].\n")
    store.refresh()
    original = store.read("a.md")
    resolve = catalog.resolve_edges

    def fail(db, row, metadata):
        if row["path"] == "consumer.md":
            # Exercise a classified logical query error. Unknown/physical
            # storage failures use the subprocess exit contract instead.
            db.execute('SELECT * FROM "controlled graph publication failure"')
        return resolve(db, row, metadata)

    write(store, "a.md", "# A\nNew evidence.\n")
    with monkeypatch.context() as patch:
        patch.setattr(catalog, "resolve_edges", fail)
        with pytest.raises(WikiError, match="controlled graph publication failure"):
            store.refresh()
    with store.connect() as db:
        assert db.execute("SELECT revision FROM documents WHERE id=?", (original["id"],)).fetchone()[0] == original["revision"]
    assert store.index_state()["index_status"] == "unavailable"
    assert store.index_state()["pending_changes"] > 0
    store.refresh()
    assert store.index_state()["index_status"] == "ready"
    assert store.read("a.md")["revision"] != original["revision"]
