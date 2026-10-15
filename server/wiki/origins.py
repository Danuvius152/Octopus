"""Private, durable material-origin state shared by writers and recovery.

This layer treats proof payloads as opaque. A writer without a verified
composition keeps the historical state and invalidates its current claim.
No origin state is projected into document metadata or public history.
"""

import json

from .store import WikiError

UNCHANGED = object()


def get(db, doc_id):
    row = db.execute(
        "SELECT state FROM material_origins WHERE doc_id=?", (doc_id,)
    ).fetchone()
    if row is None:
        return None
    try:
        state = json.loads(row[0])
        if not isinstance(state, dict):
            raise ValueError("expected an object")
        return state
    except (ValueError, TypeError) as exc:
        raise WikiError("Invalid material-origin state; durable record retained", 503) from exc


def put(db, doc_id, state):
    if state is None:
        db.execute("DELETE FROM material_origins WHERE doc_id=?", (doc_id,))
    else:
        db.execute(
            "INSERT OR REPLACE INTO material_origins(doc_id,state) VALUES(?,?)",
            (doc_id, json.dumps(state, ensure_ascii=False, allow_nan=False)),
        )


def invalidated(state, doc_id, digest, reason):
    if state is None:
        return None
    # Preserve unknown proof versions verbatim; change only the outer claim.
    return state | {
        "doc_id": doc_id, "revision": digest, "status": "invalid",
        "invalidated_reason": reason,
    }


def bound(state, doc_id, digest):
    if state is not None and (
        not isinstance(state, dict)
        or state.get("doc_id") != doc_id
        or state.get("revision") != digest
    ):
        raise WikiError("Material-origin state does not match this identity/revision", 409)
    return state


def preimage(state, doc_id, digest):
    """Keep opaque history without certifying an unobserved external edit."""
    if state is not None and not isinstance(state, dict):
        raise WikiError("Invalid origin preimage; recovery record retained", 503)
    if state is not None and (
        state.get("doc_id") != doc_id or state.get("revision") != digest
    ):
        return invalidated(state, doc_id, digest, "Origin did not verify the actual preimage")
    return state


def record(
    db, change_id, doc_id, before_hash, after_hash, *,
    before_state=UNCHANGED, after_state=UNCHANGED,
):
    previous = preimage(
        get(db, doc_id) if before_state is UNCHANGED else before_state,
        doc_id, before_hash,
    )
    if after_state is UNCHANGED:
        if (previous is not None and before_hash == after_hash
                and previous.get("doc_id") == doc_id
                and previous.get("revision") == after_hash):
            following = previous
        else:
            following = invalidated(
                previous, doc_id, after_hash, "Writer did not compose a material proof",
            )
    else:
        following = bound(after_state, doc_id, after_hash)
    db.execute(
        "INSERT INTO change_material_origins(change_id,before_state,after_state) VALUES(?,?,?)",
        (change_id, json.dumps(previous, ensure_ascii=False, allow_nan=False),
         json.dumps(following, ensure_ascii=False, allow_nan=False)),
    )


def change_state(db, change_id, side):
    if side not in {"before", "after"}:
        raise ValueError("Invalid origin recovery side")
    row = db.execute(
        f"SELECT {side}_state FROM change_material_origins WHERE change_id=?",
        (change_id,),
    ).fetchone()
    return json.loads(row[0]) if row and row[0] is not None else None


def apply(db, change_id):
    row = db.execute(
        """SELECT c.path,c.after_hash,r.document_id,i.path AS current_path
        FROM changes c JOIN change_retention r ON r.change_id=c.id
        LEFT JOIN document_identities i ON i.id=r.document_id
        WHERE c.id=? AND EXISTS(
          SELECT 1 FROM change_material_origins o WHERE o.change_id=c.id)""",
        (change_id,),
    ).fetchone()
    if row is None or row["current_path"] not in {row["path"], None}:
        # The deletion half of a move must not revoke the new path's claim.
        return
    following = change_state(db, change_id, "after")
    put(db, row["document_id"], bound(following, row["document_id"], row["after_hash"]))


def observe(db, doc_id, digest):
    state = get(db, doc_id)
    if state is not None and state.get("revision") != digest:
        put(db, doc_id, invalidated(
            state, doc_id, digest, "Observed revision has no controlled origin transition",
        ))


def detach(db, path):
    identity = db.execute(
        "SELECT id FROM document_identities WHERE path=?", (path,),
    ).fetchone()
    if identity:
        state = get(db, identity[0])
        if state is not None and state.get("revision") is not None:
            put(db, identity[0], invalidated(state, identity[0], None, "Document binding ended"))
