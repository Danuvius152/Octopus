"""Rebuildable exact original-material hashes; current files decide reuse."""

import json

from . import material_proof, origins
from .formats import is_markdown_maintenance
from .sections import original_text
from .store import (
    WikiError,
    asset_reason,
    read_document_bytes,
    require_document_bytes,
    revision,
)


def initialize(db):
    missing = db.execute("SELECT 1 FROM sqlite_schema WHERE name='raw_materials'").fetchone() is None
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
    return missing


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
    # Compute from the current bytes and proof on every read. Only the write
    # can be omitted; an unchanged revision alone cannot certify the proof.
    values = (
        row["id"], row["path"], row["revision"],
        revision(current_material(db, row, raw, regions)),
        ownership(regions, origin[0] if origin else None),
    )
    previous = db.execute("SELECT doc_id,path,revision,original_hash,ownership FROM raw_materials WHERE doc_id=?",
                          (row["id"],)).fetchone()
    if previous is not None and tuple(previous) == values:
        return
    db.execute(
        "INSERT OR REPLACE INTO raw_materials VALUES(?,?,?,?,?)",
        values,
    )


def find(store, raw, *, markdown_only=False):
    """Caller holds the writer lock. Recheck candidate bytes before reusing."""
    require_document_bytes(raw, "submitted material")
    store.synchronize()
    found = _find_current(store, raw, markdown_only=markdown_only)
    if found is not None:
        return found
    # A miss still needs compensation: an existing file can change through
    # an outside hardlink without notifying this vault's directory watches.
    store.refresh()
    return _find_current(store, raw, markdown_only=markdown_only)


def _find_current(store, raw, *, markdown_only):
    with store.connect() as db:
        candidates = db.execute(
            "SELECT doc_id,path FROM raw_materials WHERE original_hash=? ORDER BY path",
            (revision(raw),),
        ).fetchall()
    for candidate in candidates:
        path = candidate["path"]
        if markdown_only and not is_markdown_maintenance(path):
            continue
        try:
            with store.connect() as db:
                row = store._index(db, path)
                live = read_document_bytes(store.path(path), label=path)
                if row["id"] != candidate["doc_id"] or revision(live) != row["revision"]:
                    continue
                owned = db.execute(
                    "SELECT regions FROM managed_notes WHERE path=?", (path,)
                ).fetchone()
                matches = current_material(db, row, live, owned[0] if owned else None) == raw
        except (FileNotFoundError, WikiError) as exc:
            if isinstance(exc, WikiError) and exc.status not in {404, 413}:
                raise
            continue
        if matches:
            return store.public(row)
    return None
