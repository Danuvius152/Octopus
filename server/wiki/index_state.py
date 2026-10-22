"""One transaction-local readiness predicate for reads and maintenance."""

DOCUMENT_ERROR = "quality='error' OR json_extract(metadata, '$.frontmatter_error') IS NOT NULL"


def maintenance_blockers(db, identifiers):
    """Only current source occurrences extend a target's health boundary."""
    if not identifiers:
        return set()
    requested = ",".join("(?)" for _ in identifiers)
    return {row[0] for row in db.execute(f"""WITH RECURSIVE
        requested(doc) AS (VALUES {requested}),
        dependencies(root,doc) AS (
            SELECT doc,doc FROM requested UNION
            SELECT prior.root,b.target_doc FROM dependencies prior
            JOIN source_binding_head h ON h.source_doc=prior.doc
            JOIN source_binding_observations o ON o.id=h.observation_id
            JOIN json_each(o.occurrences) occurrence
            JOIN source_bindings b ON b.id=json_extract(occurrence.value,'$.binding_id')
            WHERE b.policy='bound')
        SELECT DISTINCT dependency.root FROM dependencies dependency
        LEFT JOIN documents d ON d.id=dependency.doc
        WHERE d.id IS NULL OR d.quality='error' OR length(d.text)=0
          OR json_extract(d.metadata,'$.frontmatter_error') IS NOT NULL""", tuple(identifiers))}


def summary(db, observed_pending):
    index = db.execute("SELECT * FROM index_state WHERE singleton=1").fetchone()
    stream = db.execute("SELECT * FROM filesystem_state WHERE singleton=1").fetchone()
    diagnostics = [index["error"]] if index["error"] else []
    durable_error = None
    if stream:
        durable_error = stream["uncertain"] or (
            "Filesystem reception is incomplete; durable receipt required" if stream["armed_epoch"] else None
        )
    from . import recovery_state

    recovery = recovery_state.active(db)
    if recovery is not None:
        durable_error = "Filesystem recovery is unfinished: " + recovery["phase"]
    if durable_error:
        diagnostics.append(durable_error)
    pending = max(int(observed_pending), stream["received_seq"] - stream["applied_seq"] if stream else 0,
                  int(bool(durable_error)))
    catalog = db.execute("SELECT pending FROM catalog_state WHERE singleton=1").fetchone()
    pending = max(pending, catalog[0] if catalog else 0)
    errors = db.execute(f"SELECT count(*) FROM documents WHERE {DOCUMENT_ERROR}").fetchone()[0]
    return {
        "index_epoch": index["epoch"], "index_checked_at": index["checked_at"],
        "pending_changes": pending, "index_error_count": errors + len(diagnostics),
        "index_status": "unavailable" if diagnostics else "updating" if pending else "partial" if errors else "ready",
        "index_diagnostics": diagnostics,
    }
