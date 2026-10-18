"""One transaction-local readiness predicate for reads and maintenance."""

DOCUMENT_ERROR = "quality='error' OR json_extract(metadata, '$.frontmatter_error') IS NOT NULL"


def summary(db, observed_pending):
    index = db.execute("SELECT * FROM index_state WHERE singleton=1").fetchone()
    stream = db.execute("SELECT * FROM filesystem_state WHERE singleton=1").fetchone()
    diagnostics = [index["error"]] if index["error"] else []
    durable_error = None
    if stream:
        durable_error = stream["uncertain"] or (
            "Filesystem reception is incomplete; durable receipt required" if stream["armed_epoch"] else None
        )
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
