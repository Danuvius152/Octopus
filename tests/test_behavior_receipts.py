"""Crash boundaries must not turn uncertain browser admission into replay."""

import asyncio
import hashlib
import json

from server import deps
from server.database import Database
from server.sessions import SessionManager
from server.sessions.client_submissions import submit_client_message
from server.users import UserManager
from tests.accounts import create_account
from tests.test_submission_recovery import _submit_paused
from tests.test_submission_recovery import offline_harness as offline_harness


async def test_submission_and_legacy_receipt_survive_database_reopen(tmp_path, monkeypatch):
    path = str(tmp_path / "receipts.db")
    db = Database(path)
    await db.initialize()
    await create_account(db, extra_roots=[tmp_path])
    manager = SessionManager()
    await manager.initialize(db)
    agent = await db.get_system_agent()
    session = await manager.create_session(agent["id"], "receipt", str(tmp_path), backend="codex")
    first = await _submit_paused(manager, session.id, "once", "accepted")
    unknown_payload = json.dumps({"content": "uncertain", "attachment_ids": [], "wiki_mode": None}, sort_keys=True, separators=(",", ":"))
    # A pre-upgrade reservation has no saved input or acceptance evidence.
    async with db.transaction(write=True) as tx:
        await tx.execute(
            "INSERT INTO client_submissions(session_id,client_id,payload_sha256,request_id) VALUES(?,?,?,?)",
            (session.id, "uncertain", hashlib.sha256(unknown_payload.encode()).hexdigest(), "reserved-request"),
        )
    await db.close()
    reopened = Database(path)
    await reopened.initialize()
    deps.set_user_manager(UserManager(reopened))
    restored = SessionManager()
    await restored.initialize(reopened)
    try:
        duplicate = await submit_client_message(restored, session.id, "once", [], "accepted", wiki_mode=None)
        assert duplicate["request_id"] == first["request_id"]
        assert duplicate["accepted"] is first["accepted"] is True
        assert duplicate["execution"]["state"] == "withdrawn"
        assert duplicate["input"]["content"] == "once"
        unknown = await submit_client_message(restored, session.id, "uncertain", [], "uncertain", wiki_mode=None)
        assert unknown["state"] == "unknown" and unknown["request_id"] == "reserved-request"
        assert restored.dispatch_snapshot(session.id)["items"] == []
    finally:
        await reopened.close()


async def test_concurrent_duplicate_submission_has_one_admission(tmp_path, monkeypatch):
    db = Database(":memory:")
    await db.initialize()
    await create_account(db, extra_roots=[tmp_path])
    manager = SessionManager()
    await manager.initialize(db)
    agent = await db.get_system_agent()
    session = await manager.create_session(agent["id"], "concurrent", str(tmp_path), backend="codex")
    stopped = asyncio.Event()

    async def pause_original(event):
        if event.get("type") == "dequeued" and event.get("session_id") == session.id:
            await manager.interrupt(session.id)
            stopped.set()

    manager.on_broadcast("pause-original-submission", pause_original)
    try:
        results = await asyncio.gather(*(submit_client_message(manager, session.id, "once", [], "same", wiki_mode=None) for _ in range(2)))
        assert len({row["request_id"] for row in results}) == 1
        assert any(row["accepted"] for row in results)
        await asyncio.wait_for(stopped.wait(), 5)
        await manager.await_session_execution(session.id, timeout=5)
        assert len(manager.dispatch_snapshot(session.id)["items"]) == 1
    finally:
        await db.close()
