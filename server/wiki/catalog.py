"""Indexed direct dependencies and navigation; no transitive evidence claims."""

import posixpath
import re
from pathlib import PurePosixPath
from urllib.parse import unquote, urlsplit

from .links import anchor_exists, local_links, note_name

VERSION = "direct-links-v2"


def initialize(db):
    db.execute("""CREATE TABLE IF NOT EXISTS wiki_edges(
        source_doc TEXT NOT NULL, ordinal INTEGER NOT NULL, target_doc TEXT,
        resource TEXT NOT NULL, relation TEXT NOT NULL, location TEXT NOT NULL,
        recorded_revision TEXT, label TEXT, lookup_key TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'unresolved',
        PRIMARY KEY(source_doc,ordinal))""")
    db.execute("CREATE INDEX IF NOT EXISTS wiki_edge_target ON wiki_edges(target_doc)")
    db.execute("CREATE INDEX IF NOT EXISTS wiki_edge_lookup ON wiki_edges(lookup_key)")
    db.execute("""CREATE TRIGGER IF NOT EXISTS wiki_edge_delete AFTER DELETE ON documents
        BEGIN DELETE FROM wiki_edges WHERE source_doc=old.id; END""")


def name_matches(db, name):
    return db.execute("""WITH named AS (SELECT *, CASE
        WHEN lower(substr(path,-3))='.md' THEN substr(path,1,length(path)-3)
        WHEN lower(substr(path,-9))='.markdown' THEN substr(path,1,length(path)-9)
        ELSE path END AS name_path FROM documents)
        SELECT id FROM named WHERE title=?
        OR name_path=? OR substr(name_path,-length(?))=?
        OR EXISTS(SELECT 1 FROM json_each(metadata,'$.aliases') WHERE value=?)
        OR EXISTS(SELECT 1 FROM managed_notes m,json_each(m.metadata,'$.aliases') a
            WHERE m.path=named.path AND a.value=?)""", (name, name, "/" + name, "/" + name, name, name)).fetchall()


def resolve(db, resource, parent, *, wiki=False):
    if re.fullmatch(r"[0-9a-f]{32}", resource):
        return db.execute("SELECT id FROM document_identities WHERE id=?", (resource,)).fetchone()
    if resource.casefold().startswith("urn:uuid:"):
        return db.execute("SELECT doc_id FROM uid_aliases WHERE uid=?", (resource.casefold(),)).fetchone()
    try:
        url = urlsplit(resource)
    except ValueError:
        return None
    if url.scheme or url.netloc:
        return None
    path = unquote(url.path)
    candidates = [posixpath.normpath(posixpath.join(parent, path))]
    if wiki:
        candidates.append(path.lstrip("/"))
    for candidate in candidates:
        if candidate.startswith(("../", "/")) or candidate == "..":
            continue
        for suffix in ("", ".md", ".markdown"):
            row = db.execute("SELECT id FROM documents WHERE path=?", (candidate + suffix,)).fetchone()
            if row:
                return row
    if wiki and "/" not in path:
        found = name_matches(db, note_name(path))
        if len(found) == 1:
            return found[0]
    return None


def key(resource):
    try:
        return note_name(unquote(urlsplit(resource).path)).casefold()
    except ValueError:
        return ""


def edge_status(db, resource, target, location, relation):
    try:
        external = bool(urlsplit(resource).scheme and not resource.startswith("urn:uuid:"))
    except ValueError:
        return "invalid_resource"
    if external:
        return "external"
    if target is None:
        if relation == "wikilink" and "/" not in resource:
            if len(name_matches(db, note_name(unquote(urlsplit(resource).path)))) > 1:
                return "ambiguous"
        return "unresolved"
    row = db.execute("SELECT text FROM documents WHERE id=?", (target,)).fetchone()
    if row is None:
        return "missing"
    if location and relation in {"wikilink", "markdown_link"} and not anchor_exists(row[0], location):
        return "missing_anchor"
    return "resolved"


def index(db, row, metadata):
    old = {(edge["resource"], edge["relation"]): edge["target_doc"] for edge in db.execute("SELECT * FROM wiki_edges WHERE source_doc=?", (row["id"],))}
    db.execute("DELETE FROM wiki_edges WHERE source_doc=?", (row["id"],))
    parent = PurePosixPath(row["path"]).parent.as_posix()
    refs = []
    for ref in metadata.get("sources", []):
        resource = ref.get("resource") or ref.get("doc") or ref.get("path")
        if isinstance(resource, str):
            refs.append((resource, "derived_from", ref.get("location", ""), ref.get("revision"), ref.get("id")))
    representation = metadata.get("x_wiki", {}).get("representation_of")
    if isinstance(representation, str):
        refs.append((representation, "representation_of", "", None, None))
    refs.extend((target, relation, anchor, None, None) for target, anchor, relation in local_links(row["text"][metadata.get("body_start", 0):]) if target)
    for ordinal, (resource, relation, location, recorded, label) in enumerate(refs):
        target = old.get((resource, relation)) if relation in {"derived_from", "representation_of"} else None
        resolved = resolve(db, resource, parent, wiki=relation != "markdown_link") if target is None else None
        target = target or (resolved[0] if resolved else None)
        location = location if isinstance(location, str) else ""
        db.execute("INSERT INTO wiki_edges VALUES(?,?,?,?,?,?,?,?,?,?)", (row["id"], ordinal, target, resource, relation, location, recorded if isinstance(recorded, str) else None, label if isinstance(label, str) else None, key(resource), edge_status(db, resource, target, location, relation)))
    # A new/renamed note can resolve previously missing links. Resolved source
    # identities remain bound through deletion and path reuse.
    keys = {note_name(row["path"]).casefold(), row["title"].casefold(), *(value.casefold() for value in metadata.get("aliases", []))}
    old_names = db.execute("SELECT title,path,aliases FROM search_documents WHERE doc_id=?", (row["id"],)).fetchone()
    if old_names:
        import json

        keys.update(value.casefold() for value in (old_names["title"], note_name(old_names["path"]), *json.loads(old_names["aliases"])))
    for lookup in keys:
        for edge in db.execute("SELECT e.*,d.path FROM wiki_edges e JOIN documents d ON d.id=e.source_doc WHERE e.lookup_key=? AND (e.target_doc IS NULL OR e.relation IN ('wikilink','markdown_link'))", (lookup,)).fetchall():
            resolved = resolve(db, edge["resource"], PurePosixPath(edge["path"]).parent.as_posix(), wiki=edge["relation"] != "markdown_link")
            target = resolved[0] if resolved else None
            db.execute("UPDATE wiki_edges SET target_doc=?,status=? WHERE source_doc=? AND ordinal=?", (target, edge_status(db, edge["resource"], target, edge["location"], edge["relation"]), edge["source_doc"], edge["ordinal"]))
    for edge in db.execute("SELECT * FROM wiki_edges WHERE target_doc=?", (row["id"],)).fetchall():
        target = row["id"]
        if edge["relation"] in {"wikilink", "markdown_link"}:
            parent_path = db.execute("SELECT path FROM documents WHERE id=?", (edge["source_doc"],)).fetchone()[0]
            resolved = resolve(db, edge["resource"], PurePosixPath(parent_path).parent.as_posix(), wiki=edge["relation"] == "wikilink")
            target = resolved[0] if resolved else None
        db.execute("UPDATE wiki_edges SET target_doc=?,status=? WHERE source_doc=? AND ordinal=?", (target, edge_status(db, edge["resource"], target, edge["location"], edge["relation"]), edge["source_doc"], edge["ordinal"]))


def dependencies(db, doc_id):
    result = []
    for row in db.execute("SELECT e.*,d.path,d.revision FROM wiki_edges e LEFT JOIN documents d ON d.id=e.target_doc WHERE e.source_doc=? AND e.relation='derived_from' ORDER BY e.ordinal", (doc_id,)):
        external = edge_status(db, row["resource"], row["target_doc"], row["location"], row["relation"]) == "external"
        status = "external" if external else "missing" if row["path"] is None else "version_unknown" if not row["recorded_revision"] else "changed" if row["revision"] != row["recorded_revision"] else "indexed"
        result.append({"doc": row["target_doc"], "path": row["path"], "resource": row["resource"], "id": row["label"], "revision": row["recorded_revision"], "current_revision": row["revision"], "location": row["location"], "status": status})
    return result


def graph(store, doc_id, offset=0, limit=100, cursor=None):
    from .retrieval import continuation, pagination

    offset, epoch, fingerprint = pagination(store, cursor, offset, {"graph": doc_id})
    with store.connect() as db:
        total = db.execute("SELECT count(*) FROM wiki_edges WHERE source_doc=? OR target_doc=?", (doc_id, doc_id)).fetchone()[0]
        selected = db.execute("SELECT e.*,d.revision FROM wiki_edges e LEFT JOIN documents d ON d.id=e.target_doc WHERE e.source_doc=? OR e.target_doc=? ORDER BY e.source_doc,e.ordinal LIMIT ? OFFSET ?", (doc_id, doc_id, limit, offset)).fetchall()
        ids = {doc_id, *(edge["source_doc"] for edge in selected), *(edge["target_doc"] for edge in selected if edge["target_doc"])}
        nodes = [store.navigation(row) for identifier in sorted(ids) if (row := db.execute("SELECT * FROM documents WHERE id=?", (identifier,)).fetchone()) is not None]
        edges = [{"from": edge["source_doc"], "to": edge["target_doc"], "resource": edge["resource"], "revision": edge["recorded_revision"] or edge["revision"], "location": edge["location"], "relation": edge["relation"], "status": edge["status"]} for edge in selected]
    next_offset = offset + limit if offset + limit < total else None
    return {"nodes": nodes, "edges": edges, "total": total, "next_offset": next_offset, "next_cursor": continuation(next_offset, epoch, fingerprint), **store.index_state()}
