"""Fork capacity readers and copying use the same projected, frozen messages."""

from server.database import Database
from server.sessions import SessionManager
from server.submission_models import NativeInputEvidence
from tests.accounts import create_account


async def test_fork_copy_installs_the_verified_input_projection(tmp_path):
    db = Database(str(tmp_path / "fork.db"))
    await db.initialize()
    await create_account(db, extra_roots=[tmp_path])
    manager = SessionManager()
    await manager.initialize(db)
    try:
        agent = await db.get_system_agent()
        parent = await manager.create_session(agent["id"], "Parent", str(tmp_path), backend="codex")
        await db.admit_client_submission(parent.id, "original", "digest", "request",
            payload={"content": "你好", "attachment_ids": [], "order": 1}, receipt={"accepted": True})
        await db.claim_client_submission(parent.id, "request", "request", "generation")
        await db.record_native_input(parent.id, "request", NativeInputEvidence(
            input_id="request", attempt_id="attempt"))
        await db.append_message(session_id=parent.id, seq=0, role="user", type="text", content="你好",
            tool_input={"request_context": {"request_id": "request"}})
        snapshot = await db.snapshot_fork_messages(parent.id, 0)
        projected = snapshot.messages[0]["tool_input"]["copied_input"]
        assert projected["source_session_id"] == parent.id
        assert projected["native_inputs"][0]["state"] == "not_sent"

        # Late original evidence must not change an already verified payload.
        await db.record_native_input(parent.id, "request", NativeInputEvidence(
            input_id="request", attempt_id="attempt", thread_id="native", rpc_id="rpc", state="unknown"))
        await db.create_fork_session(fork_id="fork", name="Fork", working_dir=str(tmp_path),
            created_at="2026-11-10T00:00:00Z", parent_id=parent.id, backend="codex",
            agent_id=agent["id"], credential_id=None, resume_id=None, fork_after_seq=0,
            message_snapshot=snapshot)
        copied = await db.load_messages("fork")
        assert copied == snapshot.messages
        assert copied[0]["tool_input"]["copied_input"] == projected
        # A chained fork keeps the original input's provenance, not a new grant.
        again = await db.snapshot_fork_messages("fork", 0)
        assert again.messages == copied
    finally:
        await manager.close_session_owners()
        await db.close()
