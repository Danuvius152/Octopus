"""Catalog batches retain navigation facts, not every source's full AST."""

import json
import uuid
import weakref

import pytest

from server.wiki import catalog, curation, format, resolver
from server.wiki.curation import WikiCuration
from server.wiki.references import local_references
from server.wiki.store import WikiError, WikiStore


class _ParseSentinel:
    pass


class _ParseLifetimes:
    def __init__(self, monkeypatch):
        self.references = {name: [] for name in ("formats", "tokens", "environments")}
        self.peaks = dict.fromkeys(self.references, 0)
        original_parse = format.parse

        def observe_parse(text):
            parsed = original_parse(text)
            token_sentinel, env_sentinel = _ParseSentinel(), _ParseSentinel()
            pending = list(parsed.tokens)
            while pending:
                token = pending.pop()
                token.meta["ast_lifetime_sentinel"] = token_sentinel
                pending.extend(token.children or [])
            parsed.env["ast_lifetime_sentinel"] = env_sentinel
            for name, value in zip(self.references, (parsed, token_sentinel, env_sentinel), strict=True):
                self.references[name].append(weakref.ref(value))
                self.peaks[name] = max(self.peaks[name], sum(reference() is not None for reference in self.references[name]))
            return parsed

        monkeypatch.setattr(format, "parse", observe_parse)

    def assert_released(self):
        for name, references in self.references.items():
            assert self.peaks[name] <= 2, f"Only current source and temporary target may own {name}"
            assert all(reference() is None for reference in references), f"Retained {name} after operation"


@pytest.fixture
def store(tmp_path):
    root = tmp_path / "vault"
    root.mkdir()
    instance = WikiStore(root, tmp_path / "state", uuid.uuid4().hex, create=True)
    try:
        yield instance
    finally:
        instance.close()


def test_catalog_releases_each_complete_source_parse_without_losing_relations(tmp_path, monkeypatch):
    root = tmp_path / "vault"
    root.mkdir()
    for index in range(20):
        following = (index + 1) % 20
        (root / f"note-{index:02}.md").write_text(
            f"# Note {index}\n\n## Local\n\nEvidence paragraph.\n\n"
            f"[Self](#local) [Next](note-{following:02}.md#local) "
            f"[Missing](note-{following:02}.md#absent)\n"
        )
    store = WikiStore(root, tmp_path / "state", uuid.uuid4().hex, create=True)
    try:
        lifetimes = _ParseLifetimes(monkeypatch)
        with store.connect() as db:
            db.execute("INSERT OR IGNORE INTO catalog_dirty SELECT id FROM documents")
            db.execute("UPDATE catalog_state SET pending=1")
            catalog.finish(db)
        lifetimes.assert_released()
        for index in range(20):
            path = f"note-{index:02}.md"
            source_id = store.resolve_identity(path)["id"]
            edges = [edge for edge in store.graph(path)["edges"] if edge["from"] == source_id]
            assert [(edge["resource"], edge["location"], edge["status"]) for edge in edges] == [
                ("#local", "local", "resolved"),
                (f"note-{(index + 1) % 20:02}.md#local", "local", "resolved"),
                (f"note-{(index + 1) % 20:02}.md#absent", "absent", "missing_anchor"),
            ]
    finally:
        store.close()


def test_overlay_anchor_facts_do_not_replace_indexed_or_other_candidate_versions(store):
    (store.root / "note.md").write_text("# Original\n")
    store.refresh()
    with store.connect() as db:
        row = dict(db.execute("SELECT * FROM documents WHERE path='note.md'").fetchone())
        row["metadata"] = json.loads(row["metadata"])
        targets = resolver.TargetDocuments(db)

        def status(anchor, overlay=None):
            reference = next(local_references(f"[Note](note.md#{anchor})"))
            return resolver.resolve(db, reference, ".", overlay=overlay, documents=targets).status

        assert status("original") == "resolved"
        first = row | {"text": "# Candidate\n"}
        second = row | {"text": "# Replacement\n"}
        assert status("candidate", first) == "resolved"
        assert status("candidate", second) == "missing_anchor"
        assert status("replacement", second) == "resolved"
        assert status("original") == "resolved"
        assert status("candidate") == "missing_anchor"
        assert set(targets.document(row["id"]).keys()) == {"id", "path", "revision"}


def test_curation_releases_target_parses_and_proves_real_originals_only_when_needed(store, monkeypatch):
    for index in range(20):
        (store.root / f"note-{index}.md").write_text("# Original\n")
    store.refresh()
    # The catalog copy cannot stand in for an original during write validation.
    with store.connect() as db:
        db.execute("UPDATE documents SET text='# Catalog copy\n'")
    reads = []
    original_read = curation.read_document_bytes
    lifetimes = _ParseLifetimes(monkeypatch)

    def observe_read(path, **kwargs):
        reads.append(path.name)
        return original_read(path, **kwargs)

    monkeypatch.setattr(curation, "read_document_bytes", observe_read)
    curator = WikiCuration(store)
    curator.validate_links("candidate.md", "\n".join(f"[Note](note-{index}.md)" for index in range(20)))
    assert reads == []
    candidate = "\n".join(f"[Note](note-{index}.md#original)" for index in range(20))
    curator.validate_links("candidate.md", candidate + "\n" + candidate)
    assert reads == [f"note-{index}.md" for index in range(20)]
    lifetimes.assert_released()
    with pytest.raises(WikiError, match="New local anchor does not exist"):
        curator.validate_links("candidate.md", "[Wrong](note-0.md#catalog-copy)")
