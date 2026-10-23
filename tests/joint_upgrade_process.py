"""Isolated ticket50 process: real exits leave both SQLite owners' evidence."""

import asyncio
import json
import os
import sqlite3
import subprocess
import sys
import traceback
from pathlib import Path

from server.config import settings


async def main(configuration, phase):
    values = json.loads(configuration.read_text())
    for name, value in values.items():
        setattr(settings, name, value)
    root = configuration.parent.resolve()
    from server.service_installation import InstallationOwner, _paths

    # Assert the resolved installation range, including provider homes, before
    # acquiring ownership or executing any migration. The parent sets the two
    # provider environment overrides under this same root.
    assert all(Path(value).resolve().is_relative_to(root) for value in _paths(settings).values())
    popen = subprocess.Popen

    def no_model(command, *args, **kwargs):
        if isinstance(command, (list, tuple)) and Path(command[0]).name in {"codex", "claude"}:
            (root / "model-started").touch()
            raise AssertionError("Mechanical recovery must not run a model")
        return popen(command, *args, **kwargs)

    subprocess.Popen = no_model
    from server.bootstrap import bootstrap_first_account
    from server.database import Database
    from server.session_manager import session_manager
    from server.users import UserManager
    from server.wiki import account_upgrade, maintenance_inputs
    from server.wiki.history import WikiHistory
    from server.wiki.jobs import WikiJobs
    from server.wiki.registry import registry
    from tests.wiki_schema_fixtures import remove_future_job_events

    owner = InstallationOwner(settings)
    owner.acquire()
    db = Database(settings.db_path)
    if phase == "input":
        migrate = db._apply_migrations

        async def interrupt_inputs():
            await migrate()
            os._exit(71)

        db._apply_migrations = interrupt_inputs
    await db.initialize()
    registry.bind_health(db.health)
    if phase == "seed":
        agent = await db.get_system_agent()
        await db.save_session("legacy", "Ordinary completed delegation", settings.default_working_dir,
                              "2026-10-20", agent_id=agent["id"], origin="delegation", backend="codex")
        await db.append_message("legacy", 0, "assistant", "result",
                                json.dumps({"request_id": "exact", "status": "succeeded"}), terminal_request_id="exact")
        memory = Path(settings.agents_dir) / agent["id"] / "memory"
        memory.mkdir(parents=True, exist_ok=True)
        (memory / "MEMORY.md").write_text("Retain the original account memory.\n")
        vault = root / "vault"
        (vault / "Library").mkdir(parents=True)
        registry.configure(None, vault, agent["id"])
        store = registry.store(None)
        history = WikiHistory(store)
        history.write("Library/source.md", b"# Source\nOriginal supporting evidence.\n", None, "human", "create source")
        source = store.read("Library/source.md")
        raw = ("---\nsources:\n  - resource: source.md\n    revision: " + source["revision"] +
               "\n    location: L1-L2\n---\n# Note\nOriginal conditions.\n").encode()
        first = history.write("Library/note.md", raw, None, "human", "original")
        applied = raw + b"\nAlready applied effect, never replay.\n"
        effect = history.write("Library/note.md", applied, first["revision"], "human", "applied before migration")
        note = store.read("Library/note.md")
        WikiJobs(None)
        legacy_item = {"doc": note["id"], "path": note["path"], "revision": first["revision"],
                       "status": "attention", "output": note["path"], "output_revision": effect["revision"],
                       "recording_failure": {"attempt": 1, "session_id": "legacy", "request_id": "exact", "opaque": ["keep", 7]},
                       "execution_unknown": {"original": True}}
        raw_items = json.dumps([legacy_item])
        with store.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            remove_future_job_events(connection)
            connection.execute("INSERT INTO jobs VALUES('old-job','old-key','old-hash','{}','completed_with_attention',?,NULL,'legacy','old','old')", (raw_items,))
            connection.execute("INSERT INTO job_requests VALUES('old-key','old-hash','old-job')")
            connection.execute("INSERT INTO operation_receipts VALUES('old-receipt','old-job',?,?,?,?,?,'patch','Original effect',?,'old','{}')",
                               (note["id"], note["id"], note["path"], first["revision"], effect["revision"], effect["change_id"]))
            connection.execute("UPDATE maintenance_input_state SET version='maintenance-inputs-v7'")
        (root / "seed.json").write_text(json.dumps({"note": note, "source": source, "raw_items": raw_items,
                                                     "effect": effect, "memory_agent": agent["id"]}))
        registry.close()
        await db.close()
        payload = json.dumps({"content": "Original accepted input", "attachment_ids": [], "attachments": [], "wiki_mode": None})
        accepted = json.dumps({"accepted": True, "disposition": "staged"})
        with sqlite3.connect(settings.db_path) as connection:
            for identity, state, body, receipt, successor in (
                ("staged", "reserved", payload, accepted, None),
                ("refused", "rejected", payload, '{"accepted":false}', None),
                ("empty-receipt", "reserved", payload, '{}', None),
                ("missing", "queued", None, accepted, None),
                ("unknown", "completed", payload, accepted, None),
                ("started", "claimed", payload, accepted, None),
                ("exact", "completed", payload, accepted, None),
                ("original", "withdrawn", payload, accepted, "successor"),
                ("successor", "queued", payload, accepted, None),
                ("bg:bg:stable", "queued", json.dumps(json.loads(payload) | {"source": "bg", "delivery_id": "bg:stable"}), accepted, None),
            ):
                connection.execute("INSERT INTO client_submissions(session_id,client_id,payload_sha256,request_id,payload,receipt,execution_state,recovered_by) VALUES('legacy',?,'retained-digest',?,?,?,?,?)",
                                   (identity, identity, body, receipt, state, successor))
        owner.close()
        return

    if phase == "wiki":
        initialize = maintenance_inputs.initialize

        def interrupt_wiki(connection):
            initialize(connection)
            os._exit(72)

        maintenance_inputs.initialize = interrupt_wiki
        registry.store(None)
        raise AssertionError("Wiki migration interruption was not reached")

    await session_manager.initialize(db)
    users = UserManager(db)
    if phase == "account":
        account_upgrade.publish = lambda *_: os._exit(73)
    elif phase == "reentry":
        save = account_upgrade.save

        def interrupt_published(journal):
            save(journal)
            if journal["phase"] == "reopening":
                os._exit(74)

        account_upgrade.save = interrupt_published
    if await db.count_users():
        user = await users.authenticate("owner", "password1")
        if account_upgrade.pending_upgrade() is not None:
            await account_upgrade.resume_upgrade(db, user, session_manager)
    else:
        await bootstrap_first_account(db, username="owner", password="password1", session_manager=session_manager)
        user = await users.authenticate("owner", "password1")
    assert phase in {"finish", "reopen"}, "The requested crash phase was not reached"
    store = registry.store(user["id"])
    jobs = WikiJobs(user["id"])
    seed = json.loads((root / "seed.json").read_text())
    current = store.read(seed["note"]["id"])
    assert current["revision"] != seed["note"]["revision"]
    assert current["metadata"]["source_refs"][0]["doc"] == seed["source"]["id"]
    job = jobs.get("old-job")
    assert job["items"][0]["status"] == "unknown"
    assert job["items"][0]["legacy_evidence"]["recording_failure"]["opaque"] == ["keep", 7]
    assert job["receipts"][0]["id"] == "old-receipt"
    assert job["receipts"][0]["change_id"] == seed["effect"]["change_id"]
    assert seed["effect"]["change_id"] in {item["id"] for item in WikiHistory(store).history(current["path"])["items"]}
    with store.connect() as connection:
        assert connection.execute("SELECT items FROM jobs WHERE id='old-job'").fetchone()[0] == seed["raw_items"]
        assert connection.execute("SELECT version FROM maintenance_input_state").fetchone()[0] == maintenance_inputs.VERSION
        assert {row[0] for row in connection.execute("SELECT doc_id FROM maintenance_job_members WHERE job_id='old-job'")} == {current["id"]}
        assert connection.execute("SELECT count(*) FROM operation_receipts WHERE job_id='old-job'").fetchone()[0] == 1
    states = {}
    for identity, expected in {"staged": "withdrawn", "refused": "withdrawn", "empty-receipt": "withdrawn",
                               "missing": "unknown", "unknown": "unknown", "started": "unknown", "exact": "done",
                               "original": "withdrawn", "successor": "withdrawn"}.items():
        row = await db.get_client_submission("legacy", identity)
        assert row["execution_state"] == expected, (identity, row)
        states[identity] = expected
    assert (await db.get_client_submission("legacy", "original"))["recovered_by"] == "successor"
    assert (await db.get_client_submission("legacy", "successor"))["recovery_of"] == "original"
    returned = await db.get_source_submission("bg", "bg:stable")
    assert returned["execution_state"] == "withdrawn" and returned["request_id"] == "bg:bg:stable"
    session = next(row for row in await db.load_sessions(user_id=user["id"]) if row["id"] == "legacy")
    assert session["user_id"] == user["id"] and not session["archived"]
    assert len(await db.load_messages("legacy")) == 1
    from server.workspace import paths_for

    assert (paths_for(user["id"]).agents / seed["memory_agent"] / "memory/MEMORY.md").read_text() == "Retain the original account memory.\n"
    assert not (registry.state(None) / "config.json").exists()
    assert account_upgrade.pending_upgrade() is None
    (root / (phase + ".json")).write_text(json.dumps({"user": user["id"], "revision": current["revision"], "states": states,
                                                      "job_version": job["state_version"], "receipts": job["receipts"]}))
    await session_manager.close_session_owners()
    registry.close()
    await db.close()
    owner.close()
    if phase == "reopen":
        from server.service_installation import configure_service, read_service_profile

        for _ in range(2):
            configure_service(settings, unit="octopus-joint-upgrade-test.service", manager="user")
            profile = read_service_profile(settings)
            assert profile.paths == _paths(settings)


if __name__ == "__main__":
    try:
        asyncio.run(main(Path(sys.argv[1]), sys.argv[2]))
    except Exception:
        # aiosqlite's live worker otherwise hides the original assertion behind
        # the parent's process timeout. This is a crash-only test subprocess.
        traceback.print_exc()
        os._exit(1)
