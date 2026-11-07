"""Current input facts survive restart and runtime classification."""

import json
import sqlite3
from contextlib import closing

from server.database import Database
from server.sessions.client_submissions import input_digest
from tests.accounts import create_account


async def test_current_stages_and_child_recovery_authority_survive_repeated_open(tmp_path):
    path = tmp_path / "legacy-inputs.db"
    db = Database(str(path))
    await db.initialize()
    owner, _ = await create_account(db, extra_roots=[tmp_path])
    agent = await db.get_system_agent(owner["id"])
    await db.save_session("legacy", "Legacy input", str(tmp_path), "2026-10-20T00:00:00Z",
                          agent_id=agent["id"], user_id=owner["id"])
    await db.close()
    submitted = {"content": "Original content", "attachment_ids": [], "attachments": [], "wiki_mode": "read"}
    payload = json.dumps(submitted)
    accepted = '{"accepted":true,"disposition":"accepted"}'
    with closing(sqlite3.connect(path)) as stored:
        for identity, state, body, receipt, recovery_of, reason, generation in (
            ("source", "withdrawn", payload, accepted, None, None, None),
            ("child", "pending", payload, accepted, "source", None, None),
            ("missing", "unknown", None, accepted, None, None, None),
            ("started", "running", payload, accepted, None, None, "original-generation"),
            ("unproven-result", "unknown", payload, accepted, None, None, None),
        ):
            original_payload = json.loads(body) if body is not None else None
            if recovery_of:
                original_payload["recovery_of_client_id"] = recovery_of
                body = json.dumps(original_payload)
            stored.execute(
                "INSERT INTO client_submissions(session_id,client_id,payload_sha256,request_id,payload,receipt,"
                "execution_state,recovery_of,reason_code,execution_generation) VALUES('legacy',?,?,?,?,?,?,?,?,?)",
                (identity, input_digest(original_payload or submitted), identity,
                 body, receipt, state, recovery_of, reason, generation),
            )
        stored.commit()
        original = stored.execute("SELECT client_id,payload,receipt,execution_state,recovery_of,execution_generation,payload_sha256 "
                                  "FROM client_submissions ORDER BY client_id").fetchall()
    for _ in range(2):
        reopened = Database(str(path))
        await reopened.initialize()
        try:
            source = await reopened.get_client_submission("legacy", "source")
            child = await reopened.get_client_submission("legacy", "child")
            assert source["recovered_by"] == "child" and child["recovery_of"] == "source"
            assert child["execution_state"] == "pending" and child["receipt"]["accepted"]
            assert (await reopened.get_client_submission("legacy", "missing"))["execution_state"] == "unknown"
            assert (await reopened.get_client_submission("legacy", "started"))["execution_state"] == "running"
            assert (await reopened.get_client_submission("legacy", "unproven-result"))["execution_state"] == "unknown"
            assert (await reopened.get_session_binding("legacy"))["user_id"] == owner["id"]
            async with reopened.transaction(write=False) as tx:
                assert await tx.fetch_all("SELECT client_id,payload,receipt,execution_state,recovery_of,execution_generation,payload_sha256 "
                                          "FROM client_submissions ORDER BY client_id") == original
        finally:
            await reopened.close()
    current = Database(str(path))
    await current.initialize()
    try:
        assert await current.recover_client_submissions() == frozenset({"original-generation"})
        assert (await current.get_client_submission("legacy", "child"))["execution_state"] == "withdrawn"
        started = await current.get_client_submission("legacy", "started")
        assert started["execution_state"] == "unknown" and started["execution_generation"] == "original-generation"
        assert started["payload"]["operation"]["content"] == "Original content"
        assert (await current.get_client_submission("legacy", "source"))["recovered_by"] == "child"
        await current.recover_client_submissions()
        assert await current.get_client_submission("legacy", "started") == started
    finally:
        await current.close()
