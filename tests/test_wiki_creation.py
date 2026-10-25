"""First registration publishes one complete ledger or no ledger at all."""

import sqlite3
from collections import Counter
from contextlib import closing

import pytest

from server.config import settings
from server.storage_verification import verify_wiki
from server.wiki import maintenance_inputs, schema
from server.wiki.registry import WikiRegistry
from server.wiki.store import WikiError


def test_registration_builds_the_complete_projection_once(tmp_path, monkeypatch):
    state, root = tmp_path / "state", tmp_path / "vault"
    root.mkdir()
    monkeypatch.setattr(settings, "wiki_dir", str(state))
    connect = sqlite3.connect
    created = Counter()

    def observed(*args, **kwargs):
        db = connect(*args, **kwargs)

        def trace(sql):
            words = sql.lower().split()
            if words[:2] == ["create", "table"]:
                name = words[2].split("(")[0]
                if name in maintenance_inputs.TABLES:
                    created[name] += 1

        if str(args[0]) != ":memory:":
            db.set_trace_callback(trace)
        return db

    monkeypatch.setattr(sqlite3, "connect", observed)
    registry = WikiRegistry()
    try:
        registration = registry.configure(None, root, "curator")
        store = registry.store(None)
        with store.connect() as db:
            assert verify_wiki(db, registration) == schema.VERSION
        assert created == dict.fromkeys(maintenance_inputs.TABLES, 1)
    finally:
        registry.close()


@pytest.mark.parametrize("boundary", [
    'create table "jobs"',
    "create trigger maintenance_evidence_job_owner",
    f"pragma user_version={schema.VERSION}",
])
def test_failed_creation_rolls_back_before_registration_can_publish(tmp_path, monkeypatch, boundary):
    state, root = tmp_path / "state", tmp_path / "vault"
    root.mkdir()
    (root / "source.md").write_text("# Retained source\nOriginal words.\n")
    monkeypatch.setattr(settings, "wiki_dir", str(state))
    connect = sqlite3.connect
    rolled_back = []

    class InterruptedConnection(sqlite3.Connection):
        def execute(self, sql, *args, **kwargs):
            if " ".join(sql.lower().split()).startswith(boundary):
                raise RuntimeError("interrupted initial creation")
            return super().execute(sql, *args, **kwargs)

        def rollback(self):
            super().rollback()
            rolled_back.append((self.execute("PRAGMA user_version").fetchone()[0],
                                list(self.execute("SELECT name FROM sqlite_schema"))))

    def interrupted(*args, **kwargs):
        return connect(*args, **kwargs, factory=InterruptedConnection)

    registry = WikiRegistry()
    try:
        with monkeypatch.context() as patch:
            patch.setattr(sqlite3, "connect", interrupted)
            with pytest.raises(RuntimeError, match="interrupted initial creation"):
                registry.configure(None, root, "curator")
        assert rolled_back == [(0, [])]
        assert not (state / "index.sqlite3").exists()
        assert not (state / "config.json").exists()
        assert not list(state.glob(".index-*"))
        with pytest.raises(WikiError, match="No local wiki"):
            registry.store(None)
        registration = registry.configure(None, root, "curator")
        assert registry.store(None).read("source.md")["title"] == "Retained source"
        with closing(connect(state / "index.sqlite3")) as db:
            db.row_factory = sqlite3.Row
            assert verify_wiki(db, registration) == schema.VERSION
        assert (root / "source.md").read_text() == "# Retained source\nOriginal words.\n"
    finally:
        registry.close()
