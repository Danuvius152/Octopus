"""Represent supported format12 only after proving the frozen complete layout."""

import sqlite3
from contextlib import closing
from pathlib import Path

from server.wiki.schema import _catalog


def use_supported_baseline12(db):
    if not db.in_transaction:
        raise ValueError("A baseline fixture requires its explicit preparation transaction")
    with_jobs = db.execute("SELECT 1 FROM sqlite_schema WHERE name='jobs' AND type='table'").fetchone() is not None
    fixture = "wiki-baseline-v12-with-jobs.sql" if with_jobs else "wiki-baseline-v12.sql"
    with closing(sqlite3.connect(":memory:")) as reference:
        reference.row_factory = sqlite3.Row
        reference.executescript((Path(__file__).parent / "fixtures" / fixture).read_text())
        assert _catalog(db) == _catalog(reference), "Retained facts require the independently frozen complete format12"
        versions = [tuple(row) for row in reference.execute("SELECT * FROM wiki_schema ORDER BY version")]
    db.execute("DELETE FROM wiki_schema")
    db.executemany("INSERT INTO wiki_schema VALUES(?,?)", versions)
