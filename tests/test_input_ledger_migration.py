"""Legacy admission evidence is readable without inventing executable input."""

import json
import sqlite3

from server.database import Database


async def test_legacy_stages_and_forward_recovery_links_survive_repeated_open(tmp_path):
    path = tmp_path / "legacy-inputs.db"
    db = Database(str(path))
    await db.initialize()
    await db.save_session("legacy", "Legacy input", str(tmp_path), "2026-10-20T00:00:00Z")
    await db.close()
    payload = json.dumps({"content": "Original content", "attachment_ids": [], "attachments": [], "wiki_mode": None})
    accepted = '{"accepted":true,"disposition":"accepted"}'
    with sqlite3.connect(path) as legacy:
        legacy.execute("DROP INDEX messages_terminal_request")
        legacy.execute("ALTER TABLE messages DROP COLUMN terminal_request_id")
        for identity, state, body, receipt, recovery_of, recovered_by in (
            ("source", "withdrawn", payload, accepted, None, "child"),
            ("child", "queued", payload, accepted, None, None),
            ("refused-recovery", "rejected", payload, '{"accepted":false}', "source", None),
            ("missing", "queued", None, accepted, None, None),
            ("started", "claimed", payload, accepted, None, None),
            ("unproven-result", "completed", payload, accepted, None, None),
        ):
            legacy.execute(
                "INSERT INTO client_submissions(session_id,client_id,payload_sha256,request_id,payload,receipt,"
                "execution_state,recovery_of,recovered_by) VALUES('legacy',?,?,?,?,?,?,?,?)",
                (identity, "digest", identity, body, receipt, state, recovery_of, recovered_by),
            )
    for _ in range(2):
        reopened = Database(str(path))
        await reopened.initialize()
        try:
            source = await reopened.get_client_submission("legacy", "source")
            child = await reopened.get_client_submission("legacy", "child")
            refused = await reopened.get_client_submission("legacy", "refused-recovery")
            assert source["recovered_by"] == "child" and child["recovery_of"] == "source"
            assert child["execution_state"] == "pending" and child["receipt"]["accepted"]
            assert refused["recovery_of"] is None and refused["reason_code"] == "legacy_rejected"
            assert not refused["receipt"]["accepted"]
            assert (await reopened.get_client_submission("legacy", "missing"))["execution_state"] == "unknown"
            assert (await reopened.get_client_submission("legacy", "started"))["execution_state"] == "running"
            assert (await reopened.get_client_submission("legacy", "unproven-result"))["execution_state"] == "unknown"
        finally:
            await reopened.close()
