"""Source rechecks are durable operation facts, not another article version."""

import json

from . import source_bindings


def read(db, identifiers):
    """Latest checks for each current source occurrence, in the caller's snapshot."""
    if not identifiers:
        return {}
    rows = db.execute("""SELECT r.id,r.doc_id,r.details,o.id AS observation,o.occurrences
        FROM operation_receipts r JOIN documents d ON d.id=r.doc_id
        JOIN source_binding_head h ON h.source_doc=d.id
        JOIN source_binding_observations o ON o.id=h.observation_id
        WHERE r.doc_id IN (""" + ",".join("?" for _ in identifiers) + """)
        AND r.operation='confirm_sources' AND r.change_id IS NULL
        AND r.input_revision=d.revision AND r.output_revision=d.revision
        AND o.revision=d.revision AND h.pending_plan IS NULL
        ORDER BY r.rowid DESC""", list(identifiers))
    result = {}
    try:
        for row in rows:
            confirmation = json.loads(row["details"]).get("source_confirmation")
            if confirmation is None:  # Earlier confirmations rewrote source metadata.
                continue
            if confirmation["observation"] != row["observation"]:
                continue
            entries = json.loads(row["occurrences"])
            for check in confirmation["checks"]:
                ordinal = check["ordinal"]
                entry = entries[ordinal]
                if (ordinal < 0 or entry["occurrence_id"] != check["occurrence"]
                        or entry["binding_id"] != check["binding"]
                        or not all(isinstance(check[key], str) for key in ("doc", "revision", "location"))):
                    raise ValueError("source check does not belong to this occurrence")
                result.setdefault((row["doc_id"], ordinal), {
                    "confirmed_revision": check["revision"],
                    "confirmed_location": check["location"],
                    "confirmation_receipt_id": row["id"],
                })
    except (ValueError, TypeError, KeyError, IndexError, RecursionError) as error:
        raise source_bindings.unavailable("source confirmation is missing or incompatible") from error
    return result


def status(policy, recorded, current, confirmation=None):
    """Identity resolution still wins over a recorded or subsequently checked version."""
    if policy in {"external", "unknown"}:
        return policy
    if current is None:
        return "missing"
    checked = confirmation["confirmed_revision"] if confirmation else recorded
    return "version_unknown" if not checked else "indexed" if checked == current else "changed"


def is_stale(reference):
    """A missing source invalidates both original and subsequently checked evidence."""
    return reference["status"] == "changed" or (reference["status"] == "missing"
        and bool(reference.get("confirmed_revision") or reference.get("revision")))
