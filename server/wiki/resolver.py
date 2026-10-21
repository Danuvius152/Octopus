"""Connection-local indexed resolution of already parsed references."""

import json
from dataclasses import dataclass

from .format import DocumentFormat
from .links import anchor_exists, note_name
from .references import candidate_keys


@dataclass(frozen=True)
class Resolution:
    target: str | None
    status: str
    keys: tuple[tuple[str, str], ...]


def initialize(db):
    missing = db.execute("SELECT 1 FROM sqlite_master WHERE name='wiki_lookup_names'").fetchone() is None
    db.execute("""CREATE TABLE IF NOT EXISTS wiki_lookup_names(
        namespace TEXT NOT NULL,key TEXT NOT NULL,doc_id TEXT NOT NULL,
        PRIMARY KEY(namespace,key,doc_id))""")
    db.execute("CREATE INDEX IF NOT EXISTS wiki_lookup_document ON wiki_lookup_names(doc_id)")
    db.execute("""CREATE TRIGGER IF NOT EXISTS wiki_lookup_delete AFTER DELETE ON documents
        BEGIN DELETE FROM wiki_lookup_names WHERE doc_id=old.id; END""")
    return missing


def names(row, metadata):
    result = {("path", row["path"]), ("name", note_name(row["path"])), ("name", row["title"])}
    for source in (json.loads(row["metadata"]) if isinstance(row["metadata"], str) else row["metadata"], metadata):
        result.update(("name", value) for value in source.get("aliases", []) if isinstance(value, str))
    return result


def index(db, row, metadata):
    previous = {(item[0], item[1]) for item in db.execute("SELECT namespace,key FROM wiki_lookup_names WHERE doc_id=?", (row["id"],))}
    current = names(row, metadata)
    db.execute("DELETE FROM wiki_lookup_names WHERE doc_id=?", (row["id"],))
    db.executemany("INSERT INTO wiki_lookup_names VALUES(?,?,?)", [(namespace, key, row["id"]) for namespace, key in current])
    return previous | current


def resolve(db, reference, parent, *, source_doc=None, overlay=None, check_anchor=True, source_parse: DocumentFormat | None = None):
    """Resolve indexed identities; source_parse belongs to the current source text."""
    keys = candidate_keys(reference, parent, source_doc)
    if reference.namespace == "external":
        return Resolution(None, "external", keys)
    if reference.namespace in {"unknown", "invalid"}:
        return Resolution(None, reference.diagnostic or reference.namespace, keys)
    overlay_id = overlay["id"] if overlay is not None else None
    overlay_names = names(overlay, overlay["metadata"]) if overlay is not None else set()
    selected: list[str] = []
    for namespace, key in keys:
        if namespace == "doc":
            targets = [item[0] for item in db.execute("SELECT id FROM document_identities WHERE id=?", (key,))]
            if overlay_id == key:
                targets = [key]
        elif namespace == "uid":
            targets = [item[0] for item in db.execute("SELECT doc_id FROM uid_aliases WHERE uid=?", (key,))]
            if not targets and overlay is not None and overlay["metadata"].get("uid") == key:
                targets = [overlay_id]
        else:
            targets = [item[0] for item in db.execute("SELECT doc_id FROM wiki_lookup_names WHERE namespace=? AND key=?", (namespace, key)) if item[0] != overlay_id]
            if (namespace, key) in overlay_names:
                targets.append(overlay_id)
        if reference.identities:
            # Every explicit identity must resolve and agree; never fall back
            # to a display path or a later successful identity.
            if len(targets) != 1:
                return Resolution(None, "identity_conflict" if selected else "unresolved", keys)
            selected.extend(targets)
            continue
        if len(targets) > 1:
            return Resolution(None, "ambiguous", keys)
        if targets:
            selected = targets
            break
    if len(set(selected)) > 1:
        return Resolution(None, "identity_conflict", keys)
    target = selected[0] if selected else None
    if target is None:
        return Resolution(None, "unresolved", keys)
    current = overlay if target == overlay_id else db.execute("SELECT text FROM documents WHERE id=?", (target,)).fetchone()
    if current is None:
        return Resolution(target, "missing", keys)
    if check_anchor and reference.fragment and reference.relation in {"wikilink", "markdown_link"} and not anchor_exists(current["text"], reference.fragment, parsed=source_parse if target == source_doc else None):
        return Resolution(target, "missing_anchor", keys)
    return Resolution(target, "resolved", keys)
