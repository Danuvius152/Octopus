"""Current public Wiki behavior and repeated reopening preserve durable facts."""

import sqlite3
from contextlib import closing
from pathlib import Path
from urllib.parse import unquote, urlsplit

from server.wiki import grants, job_events, maintenance_inputs, protocols, schema
from server.wiki.jobs import WikiJobs
from server.wiki.store import WikiStore
from tests.test_wiki_format_admission import registered_account as registered_account
from tests.wiki_current_storage import facts


async def test_current_owned_format_shares_the_complete_current_contract(registered_account):
    root, state, registration, owner = registered_account
    path = state / "index.sqlite3"
    with closing(sqlite3.connect(path)) as db:
        before = facts(db)
    original_body = (root / "source.md").read_bytes()
    original_config = (state / "config.json").read_bytes()
    with closing(WikiStore(root, state, registration["vault_id"], defer_recovery=True)) as store:
        jobs = WikiJobs(owner, store=store, registration=registration)
        with store.connect() as db:
            assert db.execute("PRAGMA user_version").fetchone()[0] == schema.VERSION
            assert db.execute("SELECT 1 FROM sqlite_schema WHERE name='wiki_schema'").fetchone() is None
            assert facts(db) == before
            assert maintenance_inputs.require(db)
            grants.require(db)
            job_events.require(db)
        assert protocols.registration_preflight(state, complete=True)["vault_id"] == registration["vault_id"]
        current = store.read("source.md")
        requester = job_events.Requester(owner, "human", "format-browser", "format-request", "format-client")
        accepted = jobs.enqueue("format-contract", {"kind": "curation"}, [{"doc": current["id"], "path": current["path"],
                                "revision": current["revision"], "status": "pending"}], requester=requester)
        assert accepted["grant"]["vault_id"] == registration["vault_id"]
        assert accepted["grant"]["selected_documents"] == [{"doc_id": current["id"], "path_at_admission": "source.md",
                                                          "observed_revision": current["revision"], "current_path": "source.md"}]
        with store.connect() as db:
            subscriber = db.execute("SELECT id FROM wiki_job_subscribers WHERE job_id=? AND owner=?", (accepted["id"], owner)).fetchone()[0]
            event = db.execute("SELECT event_id FROM wiki_job_deliveries WHERE subscriber_id=?", (subscriber,)).fetchone()[0]
            job_events.acknowledge(db, event, subscriber, "delivered")
        assert jobs.enqueue("format-contract", {"kind": "curation"}, [{"doc": current["id"], "path": current["path"],
                            "revision": current["revision"], "status": "pending"}], requester=requester)["id"] == accepted["id"]
        with store.connect() as db:
            assert db.execute("SELECT acknowledged,outcome FROM wiki_job_deliveries WHERE event_id=? AND subscriber_id=?",
                              (event, subscriber)).fetchone()[:] == (1, "delivered")
    assert (root / "source.md").read_bytes() == original_body
    assert (state / "config.json").read_bytes() == original_config


async def test_healthy_current_reopen_does_not_run_bootstrap_or_change_nonce(registered_account, monkeypatch):
    root, state, registration, owner = registered_account
    path = state / "index.sqlite3"
    with closing(sqlite3.connect(path)) as db:
        original_nonce = db.execute("SELECT rebuild_nonce FROM maintenance_input_state").fetchone()[0]
        original_version = db.execute("PRAGMA user_version").fetchone()[0]
    preparation = []
    connect = sqlite3.connect

    def observed(*args, **kwargs):
        db = connect(*args, **kwargs)
        raw = str(args[0])
        location = unquote(urlsplit(raw).path) if raw.startswith("file:") else raw
        if location != ":memory:" and Path(location).resolve() == path:
            def trace(sql):
                normalized = " ".join(sql.lower().split())
                if (normalized.startswith(("create ", "alter ", "drop ")) and "if not exists" not in normalized) or (
                        "wiki_schema" in normalized and normalized.startswith(("insert ", "update ", "delete ", "replace "))) or (
                        "from wiki_schema where version=" in normalized):
                    preparation.append(sql)
            db.set_trace_callback(trace)
        return db

    monkeypatch.setattr(sqlite3, "connect", observed)
    with closing(WikiStore(root, state, registration["vault_id"], defer_recovery=True)) as store:
        WikiJobs(owner, store=store, registration=registration)
        WikiJobs(owner, store=store, registration=registration)
        assert preparation == []
        with store.connect() as db:
            assert db.execute("SELECT rebuild_nonce FROM maintenance_input_state").fetchone()[0] == original_nonce
            assert db.execute("PRAGMA user_version").fetchone()[0] == original_version
