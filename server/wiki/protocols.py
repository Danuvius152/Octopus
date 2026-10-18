"""Durable protocol markers are never recreated as disposable projections."""

import sqlite3


def unavailable():
    from .store import WikiError

    return WikiError("Wiki protocol ledger is missing or incompatible; durable state retained", 503)


def initialize(db):
    db.execute("CREATE TABLE wiki_protocols(name TEXT PRIMARY KEY NOT NULL,version INTEGER NOT NULL)")
    db.execute("INSERT INTO wiki_protocols VALUES('registration',1)")


def require(db, name, version=1):
    columns = [(row[1], row[2].upper(), row[3], row[4], row[5], row[6])
               for row in db.execute("PRAGMA table_xinfo(wiki_protocols)")]
    if columns != [("name", "TEXT", 1, None, 1, 0), ("version", "INTEGER", 1, None, 0, 0)]:
        raise unavailable()
    row = db.execute("SELECT version FROM wiki_protocols WHERE name=?", (name,)).fetchone()
    if row is None or row[0] != version:
        raise unavailable()


def registration_preflight(state, *, legacy=False):
    """Read-only check before a config upgrade can replace any file bytes."""
    path = state / "index.sqlite3"
    if not path.exists():
        return
    try:
        with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as db:
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "wiki_schema" not in tables:
                raise unavailable()
            version = db.execute("SELECT max(version) FROM wiki_schema").fetchone()[0] or 0
            if version >= 10:
                require(db, "registration")
            if legacy and (version >= 10 or "wiki_protocols" in tables):
                raise unavailable()
    except sqlite3.DatabaseError as error:
        raise unavailable() from error
