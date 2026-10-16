"""Rebuildable exact original-material hashes; current files decide reuse."""

import json

from . import material_proof, origins
from .sections import original_text
from .store import WikiError, asset_reason, revision


def initialize(db):
    db.execute("""CREATE TABLE IF NOT EXISTS raw_materials (
      doc_id TEXT PRIMARY KEY,path TEXT NOT NULL,revision TEXT NOT NULL,
      original_hash TEXT NOT NULL,ownership TEXT NOT NULL
    )""")
    db.execute(
        "CREATE INDEX IF NOT EXISTS raw_material_hash ON raw_materials(original_hash)"
    )
    db.execute("""CREATE TRIGGER IF NOT EXISTS raw_material_delete
      AFTER DELETE ON documents BEGIN
      DELETE FROM raw_materials WHERE doc_id=old.id; END""")


def original(raw, owned):
    if owned is None:
        return raw
    try:
        return original_text(raw.decode("utf-8"), json.loads(owned)).encode("utf-8")
    except (UnicodeError, WikiError):
        # Human edits and untrusted control comments remain material bytes.
        return raw


def ownership(regions, encoded_origin):
    """A derived cache also depends on private proof changes, not just regions."""
    if encoded_origin is None:
        return regions or ""
    return json.dumps([2, regions or "", revision(encoded_origin.encode())])


def current_material(db, row, raw, regions):
    verified = material_proof.reconstruct(origins.get(db, row["id"]), row["id"], raw)
    return original(raw, regions) if verified is None else verified


def index(db, row, raw):
    if asset_reason(row["path"]):
        db.execute("DELETE FROM raw_materials WHERE doc_id=?", (row["id"],))
        return
    owned = db.execute(
        "SELECT regions FROM managed_notes WHERE path=?", (row["path"],)
    ).fetchone()
    regions = owned[0] if owned else None
    origin = db.execute("SELECT state FROM material_origins WHERE doc_id=?", (row["id"],)).fetchone()
    db.execute(
        "INSERT OR REPLACE INTO raw_materials VALUES(?,?,?,?,?)",
        (
            row["id"],
            row["path"],
            row["revision"],
            revision(current_material(db, row, raw, regions)),
            ownership(regions, origin[0] if origin else None),
        ),
    )


def find(store, raw):
    """Caller holds the writer lock. Recheck candidate bytes before reusing."""
    store.refresh()
    with store.connect() as db:
        candidates = db.execute(
            "SELECT doc_id,path FROM raw_materials WHERE original_hash=? ORDER BY path",
            (revision(raw),),
        ).fetchall()
    for candidate in candidates:
        path = candidate["path"]
        try:
            with store.connect() as db:
                row = store._index(db, path)
                live = store.path(path).read_bytes()
                if row["id"] != candidate["doc_id"] or revision(live) != row["revision"]:
                    continue
                owned = db.execute(
                    "SELECT regions FROM managed_notes WHERE path=?", (path,)
                ).fetchone()
                matches = current_material(db, row, live, owned[0] if owned else None) == raw
        except (FileNotFoundError, WikiError) as exc:
            if isinstance(exc, WikiError) and exc.status != 404:
                raise
            continue
        if matches:
            return store.public(row)
    return None
