"""Original submission evidence survives terminal publication and reopening."""

import pytest

from server.database import Database
from server.sessions import SessionManager
from server.submission_models import NativeInputEvidence
from tests.accounts import create_account


@pytest.fixture
async def native_input(tmp_path):
    db = Database(str(tmp_path / "inputs.db"))
    await db.initialize()
    await create_account(db, extra_roots=[tmp_path])
    manager = SessionManager()
    await manager.initialize(db)
    agent = await db.get_system_agent()
    session = await manager.create_session(agent["id"], "Native input", str(tmp_path), backend="codex")
    await db.admit_client_submission(
        session.id, "browser-input", "digest", "request-1",
        payload={"content": "Keep this exact input", "attachment_ids": [], "order": 1}, receipt={"accepted": True},
    )
    assert await db.claim_client_submission(session.id, "request-1", "request-1", "generation-1")
    try:
        yield db, session.id
    finally:
        await manager.close_session_owners()
        await db.close()


async def test_successful_terminal_cannot_erase_unknown_first_input(native_input):
    db, session_id = native_input
    evidence = NativeInputEvidence(
        attempt_id="attempt-1", input_id="request-1", rpc_id="rpc-4",
        thread_id="native-thread", state="unknown",
    )
    await db.record_native_input(session_id, "request-1", evidence)
    await db.record_terminal(
        session_id=session_id, request_id="request-1", seq=0, role="system", type="result",
        content={"request_id": "request-1", "status": "success"}, created_at="2026-11-01T00:00:00Z",
    )
    row = await db.get_client_submission(session_id, "browser-input")
    assert row["execution_state"] == "unknown"
    assert row["delivery_state"] == "unknown"
    assert row["payload"]["operation"]["content"] == "Keep this exact input"
    assert row["native_inputs"] == [evidence.model_dump()]
    result = await db.read_session_result(session_id, "request-1")
    assert result.outcome is None and result.missing == "input_unconfirmed"
    rows, _ = await db.list_recoverable_submissions(session_id)
    assert [item["client_id"] for item in rows] == ["browser-input"]


@pytest.mark.parametrize("state,expected", [("not_sent", "withdrawn"), ("unknown", "unknown"), ("accepted", "unknown")])
async def test_restart_uses_native_input_evidence_without_replaying(native_input, state, expected):
    db, session_id = native_input
    evidence = NativeInputEvidence(attempt_id="attempt-1", input_id="request-1", state=state,
        rpc_id="rpc-1" if state != "not_sent" else None, thread_id="native-thread",
        turn_id="turn-1" if state == "accepted" else None,
        acceptance_source="start_response" if state == "accepted" else None)
    await db.record_native_input(session_id, "request-1", evidence)
    await db.recover_client_submissions()
    row = await db.get_client_submission(session_id, "browser-input")
    assert row["execution_state"] == expected
    assert row["native_inputs"] == [evidence.model_dump()]
    assert row["payload"]["operation"]["content"] == "Keep this exact input"
    assert (await db.load_sessions())[0]["claude_session_id"] == "native-thread"


@pytest.mark.parametrize("change", ["new_attempt", "changed_rpc", "send_again"])
async def test_closed_submission_rejects_new_sending_or_unrelated_proof(native_input, change):
    db, session_id = native_input
    original = NativeInputEvidence(attempt_id="attempt-1", input_id="request-1", rpc_id="rpc-1",
                                   thread_id="native-thread", state="unknown")
    await db.record_native_input(session_id, "request-1", original)
    await db.record_terminal(session_id=session_id, request_id="request-1", seq=0, role="system", type="result",
                            content={"request_id": "request-1", "status": "interrupted"}, created_at="2026-11-01T00:00:00Z")
    proof = original.model_copy(update={"state": "accepted", "turn_id": "turn-1", "acceptance_source": "start_response"})
    if change == "new_attempt":
        proof = proof.model_copy(update={"attempt_id": "another-attempt"})
    elif change == "changed_rpc":
        proof = proof.model_copy(update={"rpc_id": "another-rpc"})
    else:
        proof = original
    with pytest.raises(ValueError):
        await db.record_native_input(session_id, "request-1", proof)
    row = await db.get_client_submission(session_id, "browser-input")
    assert row["native_inputs"] == [original.model_dump()]
    assert row["execution_state"] == "unknown"
