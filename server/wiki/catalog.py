"""Indexed direct dependencies and navigation; no transitive evidence claims."""

from pathlib import PurePosixPath

from . import format, resolver, source_bindings, source_confirmations, uids
from .links import note_name
from .references import ParsedReference, local_references

VERSION = "direct-links-v4"


def initialize(db):
    names_missing = resolver.initialize(db)
    uid_missing = uids.initialize(db)
    available = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    db.execute("""CREATE TABLE IF NOT EXISTS wiki_edges(
        source_doc TEXT NOT NULL, ordinal INTEGER NOT NULL, target_doc TEXT,
        resource TEXT NOT NULL, relation TEXT NOT NULL, location TEXT NOT NULL,
        recorded_revision TEXT, label TEXT, lookup_key TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'unresolved',
        PRIMARY KEY(source_doc,ordinal))""")
    db.execute("CREATE INDEX IF NOT EXISTS wiki_edge_target ON wiki_edges(target_doc)")
    db.execute("""CREATE TRIGGER IF NOT EXISTS wiki_edge_delete AFTER DELETE ON documents
        BEGIN DELETE FROM wiki_edges WHERE source_doc=old.id; END""")
    db.execute("""CREATE TABLE IF NOT EXISTS wiki_edge_lookups(
        source_doc TEXT NOT NULL,ordinal INTEGER NOT NULL,namespace TEXT NOT NULL,key TEXT NOT NULL,
        PRIMARY KEY(source_doc,ordinal,namespace,key))""")
    db.execute("CREATE INDEX IF NOT EXISTS wiki_reverse_lookup ON wiki_edge_lookups(namespace,key,source_doc)")
    db.execute("CREATE TABLE IF NOT EXISTS catalog_dirty(source_doc TEXT PRIMARY KEY)")
    db.execute("""CREATE TABLE IF NOT EXISTS catalog_state(
        singleton INTEGER PRIMARY KEY CHECK(singleton=1),version TEXT NOT NULL,pending INTEGER NOT NULL)""")
    state = db.execute("SELECT version FROM catalog_state WHERE singleton=1").fetchone()
    changed = names_missing or uid_missing or not {"wiki_edges", "wiki_edge_lookups", "catalog_dirty", "catalog_state"}.issubset(available) or state is None or state[0] != VERSION
    if changed:
        db.execute("INSERT OR REPLACE INTO catalog_state VALUES(1,?,1)", (VERSION,))
        db.execute("INSERT OR IGNORE INTO catalog_dirty SELECT id FROM documents")
    db.execute("""CREATE TRIGGER IF NOT EXISTS wiki_reference_delete BEFORE DELETE ON documents
        BEGIN
            INSERT OR IGNORE INTO catalog_dirty SELECT source_doc FROM wiki_edges WHERE target_doc=old.id;
            INSERT OR IGNORE INTO catalog_dirty
                SELECT l.source_doc FROM wiki_edge_lookups l JOIN wiki_lookup_names n
                ON l.namespace=n.namespace AND l.key=n.key WHERE n.doc_id=old.id;
            INSERT OR IGNORE INTO wiki_uid_dirty SELECT uid FROM wiki_uid_claims WHERE doc_id=old.id;
            DELETE FROM wiki_uid_claims WHERE doc_id=old.id;
            DELETE FROM wiki_edge_lookups WHERE source_doc=old.id;
            UPDATE catalog_state SET pending=1 WHERE singleton=1;
        END""")
    return changed


def name_matches(db, name):
    return db.execute("SELECT doc_id FROM wiki_lookup_names WHERE namespace='name' AND key=? ORDER BY doc_id", (name,)).fetchall()


def dirty_keys(db, keys):
    for namespace, key_value in keys:
        db.execute("INSERT OR IGNORE INTO catalog_dirty SELECT source_doc FROM wiki_edge_lookups WHERE namespace=? AND key=?", (namespace, key_value))


def index(db, row, metadata):
    """Collect names and all impacted sources; resolve only after the batch."""
    dirty_keys(db, resolver.index(db, row, metadata) | {("doc", row["id"])})
    db.execute("INSERT OR IGNORE INTO catalog_dirty VALUES(?)", (row["id"],))
    db.execute("INSERT OR IGNORE INTO catalog_dirty SELECT source_doc FROM wiki_edges WHERE target_doc=?", (row["id"],))
    db.execute("UPDATE catalog_state SET pending=1 WHERE singleton=1")


def finish(db):
    state = db.execute("SELECT pending FROM catalog_state WHERE singleton=1").fetchone()
    if state and not state[0] and not db.execute("SELECT 1 FROM catalog_dirty LIMIT 1").fetchone() and not db.execute("SELECT 1 FROM wiki_uid_dirty LIMIT 1").fetchone():
        return
    dirty_keys(db, (("uid", value) for value in uids.finish(db)))
    for source in db.execute("SELECT source_doc FROM catalog_dirty ORDER BY source_doc").fetchall():
        row = db.execute("SELECT d.*,m.metadata AS managed_metadata FROM documents d LEFT JOIN managed_notes m ON m.path=d.path WHERE d.id=?", (source[0],)).fetchone()
        if row is not None:
            import json

            metadata = json.loads(row["metadata"]) | json.loads(row["managed_metadata"] or "{}")
            resolve_edges(db, row, metadata)
        else:
            source_bindings.retire(db, source[0])
            db.execute("DELETE FROM wiki_edges WHERE source_doc=?", (source[0],))
            db.execute("DELETE FROM wiki_edge_lookups WHERE source_doc=?", (source[0],))
        db.execute("DELETE FROM catalog_dirty WHERE source_doc=?", (source[0],))
    db.execute("UPDATE catalog_state SET pending=0 WHERE singleton=1")


def resolve_edges(db, row, metadata):
    db.execute("DELETE FROM wiki_edges WHERE source_doc=?", (row["id"],))
    db.execute("DELETE FROM wiki_edge_lookups WHERE source_doc=?", (row["id"],))
    parent = PurePosixPath(row["path"]).parent.as_posix()
    parsed = format.parse(row["text"])
    refs = [(ParsedReference.restore(entry["reference"]), entry.get("recorded_revision"), entry.get("label"), resolved)
            for entry, resolved in source_bindings.index(db, row, metadata)]
    refs.extend((reference, None, None, resolver.resolve(db, reference, parent, source_doc=row["id"], source_parse=parsed))
                for reference in local_references(row["text"], parsed=parsed))
    for ordinal, (reference, recorded, label, resolved) in enumerate(refs):
        resource, relation, location = reference.raw_resource, reference.relation, reference.fragment
        db.execute("INSERT INTO wiki_edges VALUES(?,?,?,?,?,?,?,?,?,?)", (row["id"], ordinal, resolved.target, resource, relation, location, recorded if isinstance(recorded, str) else None, label if isinstance(label, str) else None, note_name(reference.value).casefold(), resolved.status))
        db.executemany("INSERT OR IGNORE INTO wiki_edge_lookups VALUES(?,?,?,?)",
                       [(row["id"], ordinal, namespace, value) for namespace, value in resolved.keys])


def dependencies(db, doc_id):
    result = []
    rows = db.execute("SELECT e.*,d.path,d.revision FROM wiki_edges e LEFT JOIN documents d ON d.id=e.target_doc WHERE e.source_doc=? AND e.relation='derived_from' ORDER BY e.ordinal", (doc_id,)).fetchall()
    confirmations = source_confirmations.read(db, [doc_id]) if rows else {}
    for row in rows:
        confirmation = confirmations.get((doc_id, row["ordinal"]))
        status = source_confirmations.status(row["status"], row["recorded_revision"], row["revision"], confirmation)
        result.append({"doc": row["target_doc"], "path": row["path"], "resource": row["resource"], "id": row["label"], "revision": row["recorded_revision"], "current_revision": row["revision"], "location": row["location"], "status": status, **(confirmation or {})})
    return result


def graph(store, doc_id, offset=0, limit=100, cursor=None, *, model_response=False):
    from .retrieval import continuation, pagination

    with store.connect() as db:
        db.execute("BEGIN")
        offset, epoch, fingerprint = pagination(store, cursor, offset, {"graph": doc_id}, db=db)
        total = db.execute("SELECT count(*) FROM wiki_edges WHERE source_doc=? OR target_doc=?", (doc_id, doc_id)).fetchone()[0]
        selected = db.execute("SELECT e.*,d.revision FROM wiki_edges e LEFT JOIN documents d ON d.id=e.target_doc WHERE e.source_doc=? OR e.target_doc=? ORDER BY e.source_doc,e.ordinal LIMIT ? OFFSET ?", (doc_id, doc_id, limit, offset)).fetchall()
        ids = {doc_id, *(edge["source_doc"] for edge in selected), *(edge["target_doc"] for edge in selected if edge["target_doc"])}
        nodes = [store.navigation(row, db=db) for identifier in sorted(ids) if (row := db.execute("SELECT d.*,m.metadata AS managed_metadata FROM documents d LEFT JOIN managed_notes m ON m.path=d.path WHERE d.id=?", (identifier,)).fetchone()) is not None]
        edges = [{"from": edge["source_doc"], "to": edge["target_doc"], "resource": edge["resource"], "revision": edge["recorded_revision"] or edge["revision"], "location": edge["location"], "relation": edge["relation"], "status": edge["status"]} for edge in selected]
        state = store._index_state(db)
    next_offset = offset + limit if offset + limit < total else None
    result = {"nodes": nodes, "edges": edges, "total": total, "next_offset": next_offset, "next_cursor": continuation(next_offset, epoch, fingerprint), **state}
    if model_response:
        from .model_response import fit_prefix, presentation

        header = presentation(result)

        def build(count):
            following = offset + count if offset + count < total else None
            kept = edges[:count]
            identifiers = {doc_id, *(edge["from"] for edge in kept), *(edge["to"] for edge in kept)}
            return header | {"edges": kept, "nodes": [node for node in header["nodes"] if node["id"] in identifiers],
                             "returned_count": count, "next_offset": following,
                             "next_cursor": continuation(following, epoch, fingerprint)}

        return fit_prefix(build, len(edges))
    return result
