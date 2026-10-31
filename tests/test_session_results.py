"""Committed results use original input/carrier and terminal-attempt evidence."""

import pytest

from server.database import Database
from tests.accounts import create_account


@pytest.fixture
async def database():
    db = Database(":memory:")
    await db.initialize()
    await create_account(db)
    agent = await db.get_system_agent()
    await db.save_session("session", "Results", ".", "time", agent_id=agent["id"], user_id=agent["user_id"])
    try:
        yield db
    finally:
        await db.close()


@pytest.mark.parametrize("attempt_id,expected", [("final", (" one ", "", "two\n")), (None, ("obsolete", " one ", "", "two\n"))])
async def test_terminal_selects_ordered_fragments_and_preserves_empty_and_whitespace(database, attempt_id, expected):
    for seq, (request, attempt, text) in enumerate([
        ("original", "failed", "obsolete"), ("other", "final", "unrelated"),
        ("original", "final", " one "), ("original", "final", ""), ("original", "final", "two\n"),
    ]):
        await database.append_message(session_id="session", seq=seq, role="assistant", type="text", content=text,
            tool_input={"attempt_context": {"request_id": request, "attempt_id": attempt}})
    await database.record_terminal(session_id="session", request_id="original", seq=5, role="system", type="result",
        content={"kind": "outcome", "request_id": "original", "status": "failed", "message": "partial effects"},
        created_at="time", cost=.25,
        tool_input={"attempt_context": {"request_id": "original", "attempt_id": attempt_id}})
    result = await database.read_session_result("session", "original")
    assert result.outcome.status == "failed" and result.missing is None
    assert result.attempt_id == attempt_id and result.fragments == expected
    assert result.cost == .25


async def test_confirmed_inline_input_reads_actual_carrier_but_unknown_input_does_not(database):
    for request in ("confirmed", "unknown"):
        await database.admit_client_submission("session", request, request, request,
            payload={"content": request}, receipt={"accepted": True})
        await database.claim_client_submission("session", request, owner="carrier", generation="test")
        await database.set_steer_delivery("session", request, confirmed=request == "confirmed")
    await database.append_message(session_id="session", seq=0, role="assistant", type="text", content="carrier body",
        tool_input={"attempt_context": {"request_id": "carrier", "attempt_id": "attempt"}})
    await database.record_terminal(session_id="session", request_id="carrier", seq=1, role="system", type="result",
        content={"kind": "outcome", "request_id": "carrier", "status": "success"}, created_at="time",
        tool_input={"attempt_context": {"request_id": "carrier", "attempt_id": "attempt"}})
    confirmed = await database.read_session_result("session", "confirmed")
    assert confirmed.input_request_id == "confirmed" and confirmed.request_id == "carrier"
    assert confirmed.fragments == ("carrier body",)
    unknown = await database.read_session_result("session", "unknown")
    assert unknown.outcome is None and unknown.missing == "input_unconfirmed"


async def test_missing_terminal_is_distinct_from_a_legitimate_empty_answer(database):
    missing = await database.read_session_result("session", "missing")
    assert missing.missing == "terminal_missing" and missing.outcome is None
    await database.record_terminal(session_id="session", request_id="empty", seq=0, role="system", type="result",
        content={"kind": "outcome", "request_id": "empty", "status": "success"}, created_at="time")
    empty = await database.read_session_result("session", "empty")
    assert empty.missing is None and empty.fragments == () and empty.outcome.status == "success"
    deleted = await database.read_session_result("deleted", "empty")
    assert deleted.missing == "session_missing"
