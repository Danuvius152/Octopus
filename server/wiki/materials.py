"""Rebuildable exact original-material hashes; current files decide reuse."""

import json

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


def index(db, row, raw):
    if asset_reason(row["path"]):
        db.execute("DELETE FROM raw_materials WHERE doc_id=?", (row["id"],))
        return
    owned = db.execute(
        "SELECT regions FROM managed_notes WHERE path=?", (row["path"],)
    ).fetchone()
    regions = owned[0] if owned else None
    db.execute(
        "INSERT OR REPLACE INTO raw_materials VALUES(?,?,?,?,?)",
        (
            row["id"],
            row["path"],
            row["revision"],
            revision(original(raw, regions)),
            regions or "",
        ),
    )


def find(store, raw):
    """Caller holds the writer lock. Recheck candidate bytes before reusing."""
    store.refresh()
    with store.connect() as db:
        candidates = db.execute(
            "SELECT path FROM raw_materials WHERE original_hash=? ORDER BY path",
            (revision(raw),),
        ).fetchall()
    for candidate in candidates:
        path = candidate["path"]
        with store.connect() as db:
            row = store._index(db, path)
            owned = db.execute(
                "SELECT regions FROM managed_notes WHERE path=?", (path,)
            ).fetchone()
        if original(store.path(path).read_bytes(), owned[0] if owned else None) == raw:
            return store.public(row)
    return None
