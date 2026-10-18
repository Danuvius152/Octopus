"""Collect a batch's original UID claims before assigning durable aliases."""

import json
import uuid


def initialize(db):
    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    missing = not {"wiki_uid_claims", "wiki_uid_dirty"}.issubset(tables)
    db.execute("""CREATE TABLE IF NOT EXISTS wiki_uid_claims(
        doc_id TEXT NOT NULL,kind TEXT NOT NULL,uid TEXT NOT NULL,original TEXT,
        PRIMARY KEY(doc_id,kind))""")
    db.execute("CREATE INDEX IF NOT EXISTS wiki_uid_claimants ON wiki_uid_claims(uid,doc_id)")
    db.execute("CREATE TABLE IF NOT EXISTS wiki_uid_dirty(uid TEXT PRIMARY KEY)")
    return missing


def observe(db, doc_id, metadata):
    claimed = metadata.pop("uid", None)
    metadata.pop("portable_uid", None)
    previous = {row[0] for row in db.execute("SELECT uid FROM wiki_uid_claims WHERE doc_id=?", (doc_id,))}
    claims = [(doc_id, "default", "urn:uuid:" + str(uuid.UUID(doc_id)), claimed)]
    if claimed:
        metadata["portable_uid"] = claimed
        try:
            if not claimed.casefold().startswith("urn:uuid:"):
                raise ValueError("UID must be a UUID URN")
            canonical = "urn:uuid:" + str(uuid.UUID(claimed[9:]))
            claims.append((doc_id, "portable", canonical, claimed))
        except (ValueError, AttributeError):
            metadata["uid_error"] = "Invalid UUID URN; original field retained"
    db.execute("DELETE FROM wiki_uid_claims WHERE doc_id=?", (doc_id,))
    db.executemany("INSERT INTO wiki_uid_claims VALUES(?,?,?,?)", claims)
    db.executemany("INSERT OR IGNORE INTO wiki_uid_dirty VALUES(?)",
                   [(uid,) for uid in previous | {item[2] for item in claims}])


def finish(db):
    """Existing aliases never transfer; fresh conflicting claims have no owner."""
    dirty = {row[0] for row in db.execute("SELECT uid FROM wiki_uid_dirty")}
    if not dirty:
        return dirty
    affected = set()
    ordered = db.execute("""SELECT d.uid FROM wiki_uid_dirty d ORDER BY
        EXISTS(SELECT 1 FROM wiki_uid_claims c WHERE c.uid=d.uid AND c.kind='portable') DESC,d.uid""").fetchall()
    for item in ordered:
        uid = item[0]
        claimants = {row[0] for row in db.execute("SELECT doc_id FROM wiki_uid_claims WHERE uid=?", (uid,))}
        owner = db.execute("SELECT doc_id FROM uid_aliases WHERE uid=?", (uid,)).fetchone()
        if owner is None and len(claimants) == 1:
            db.execute("INSERT INTO uid_aliases VALUES(?,?)", (uid, next(iter(claimants))))
        affected.update(claimants)
        if owner:
            affected.add(owner[0])
    for doc_id in sorted(affected):
        row = db.execute("SELECT metadata FROM documents WHERE id=?", (doc_id,)).fetchone()
        if row is None:
            continue
        metadata = json.loads(row[0])
        for field in ("uid", "uid_conflict", "portable_uid"):
            metadata.pop(field, None)
        claimed = db.execute("SELECT original FROM wiki_uid_claims WHERE doc_id=? AND kind='default'", (doc_id,)).fetchone()
        claimed = claimed[0] if claimed else None
        preferred = db.execute("SELECT uid FROM uid_aliases WHERE doc_id=? ORDER BY rowid LIMIT 1", (doc_id,)).fetchone()
        if preferred:
            metadata["uid"] = preferred[0]
        if claimed and (not preferred or claimed != preferred[0]):
            metadata["portable_uid"] = claimed
        conflicts = []
        for claim in db.execute("SELECT uid FROM wiki_uid_claims WHERE doc_id=? ORDER BY kind DESC", (doc_id,)):
            owner = db.execute("SELECT doc_id FROM uid_aliases WHERE uid=?", (claim[0],)).fetchone()
            if owner and owner[0] == doc_id:
                continue
            claimants = sorted({item[0] for item in db.execute("SELECT doc_id FROM wiki_uid_claims WHERE uid=?", (claim[0],))})
            if owner or len(claimants) > 1:
                conflicts.append({"uid": claim[0], "existing_doc": owner[0] if owner else None,
                                  "claiming_docs": claimants})
        if conflicts:
            metadata["uid_conflict"] = conflicts[0]
        db.execute("UPDATE documents SET metadata=? WHERE id=?", (json.dumps(metadata, ensure_ascii=False), doc_id))
    db.execute("DELETE FROM wiki_uid_dirty")
    return dirty
