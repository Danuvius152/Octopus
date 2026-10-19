"""Crash boundaries must not turn uncertain browser admission into replay."""

import asyncio
import hashlib
import json

from server.database import Database
from server.sessions import SessionManager
from server.sessions.client_submissions import submit_client_message


async def test_submission_reservation_and_receipt_survive_database_reopen(tmp_path, monkeypatch):
    path = str(tmp_path / "receipts.db")
    db = Database(path)
    await db.initialize()
    manager = SessionManager()
    await manager.initialize(db)
    agent = await db.get_system_agent()
    session = await manager.create_session(agent["id"], "receipt", str(tmp_path), backend="codex")
    monkeypatch.setattr(manager, "_schedule_dispatch", lambda session: False)
    first = await submit_client_message(manager, session.id, "once", [], "accepted", wiki_mode=None)
    unknown_payload = json.dumps({"content": "uncertain", "attachment_ids": [], "wiki_mode": None}, sort_keys=True, separators=(",", ":"))
    await db.reserve_client_submission(session.id, "uncertain", hashlib.sha256(unknown_payload.encode()).hexdigest(), "reserved-request")
    await db.close()
    reopened = Database(path)
    await reopened.initialize()
    restored = SessionManager()
    await restored.initialize(reopened)
    try:
        duplicate = await submit_client_message(restored, session.id, "once", [], "accepted", wiki_mode=None)
        assert duplicate == first
        unknown = await submit_client_message(restored, session.id, "uncertain", [], "uncertain", wiki_mode=None)
        assert unknown["state"] == "unknown" and unknown["request_id"] == "reserved-request"
        assert restored.get_session(session.id)._pending_queue == []
    finally:
        await reopened.close()


async def test_concurrent_duplicate_submission_has_one_admission(tmp_path, monkeypatch):
    db = Database(":memory:")
    await db.initialize()
    manager = SessionManager()
    await manager.initialize(db)
    agent = await db.get_system_agent()
    session = await manager.create_session(agent["id"], "concurrent", str(tmp_path), backend="codex")
    monkeypatch.setattr(manager, "_schedule_dispatch", lambda session: False)
    try:
        results = await asyncio.gather(*(submit_client_message(manager, session.id, "once", [], "same", wiki_mode=None) for _ in range(2)))
        assert len({row["request_id"] for row in results}) == 1
        assert any(row["accepted"] for row in results)
        assert len(session._pending_queue) == 1
    finally:
        await db.close()
