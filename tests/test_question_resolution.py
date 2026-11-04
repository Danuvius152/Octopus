"""All answer producers share validation and retain trusted resolution facts."""

import asyncio

import pytest

from server.config import settings
from server.mcp_identity import mint
from server.sessions.commands import process_command
from tests.test_delegation_restart_security import wait_live_scope
from tests.test_delegation_work import work_runtime as work_runtime
from tests.test_question_lifecycle import QUESTIONS
from tests.test_question_lifecycle import question_host as question_host


async def test_answer_receipt_is_recoverable_by_original_command_after_question_retirement(question_host):
    manager, session, client, _ = question_host
    qid = await manager.create_pending_question(session.id, QUESTIONS)
    url = f"/api/sessions/{session.id}/questions/{qid}/result"
    pending = await client.get(url)
    assert pending.status_code == 200 and pending.json()["state"] == "pending"
    command = {"type": "answer_question", "session_id": session.id, "client_command_id": "lost-answer-ack",
               "request_id": None, "attempt_id": None, "question_id": qid, "answers": [{"selected": ["A"]}]}
    assert (await process_command(manager, command, session.user_id))["accepted"]
    # Retire the live question; a missing pending record is not the receipt.
    await manager.interrupt(session.id)
    result = await client.get(url)
    assert result.status_code == 200
    assert result.json()["state"] == "resolved"
    assert result.json()["resolution"]["client_command_id"] == "lost-answer-ack"
    assert result.json()["resolution"]["disposition"] == "answered"
    assert (await client.get(f"/api/sessions/{session.id}/questions/missing/result")).status_code == 404


@pytest.mark.parametrize("entry", ["http", "ws"])
@pytest.mark.parametrize("answers", [[], [{}], [{"selected": ["missing"]}],
    [{"selected": ["A", "A"]}], [{"selected": ["A", "B"]}],
    [{"selected": ["A"], "text": "also text"}], [{"text": "  "}],
])
async def test_invalid_answer_does_not_consume_question_or_fail_owner(question_host, entry, answers):
    manager, session, client, events = question_host
    qid = await manager.create_pending_question(session.id, QUESTIONS)
    if entry == "http":
        rejected = await client.post(f"/api/sessions/{session.id}/questions/{qid}/answer", json={"answers": answers})
        assert rejected.status_code == 400
        assert "question" in rejected.json()["detail"].lower()
    else:
        rejected = await process_command(manager, {
            "type": "answer_question", "session_id": session.id, "client_command_id": "invalid",
            "request_id": None, "attempt_id": None, "question_id": qid, "answers": answers,
        }, session.user_id)
        assert not rejected["accepted"] and rejected["code"] == "invalid_command"
    assert manager.has_pending_question(session.id, qid)
    assert not manager.session_owner_failed(session.id)
    assert not any(event["type"] == "question_answer" for event in events)
    accepted = await client.post(f"/api/sessions/{session.id}/questions/{qid}/answer",
        json={"answers": [{"selected": ["A"], "text": ""}]})
    assert accepted.status_code == 200
    assert await manager.wait_for_question_answer(session.id, qid, timeout=.1) == "Q: Choose\nA: A"


async def test_structure_and_account_source_are_identical_in_history_and_live(question_host):
    manager, session, client, events = question_host
    form = [
        {"question": "Colors?", "multiSelect": True, "options": [{"label": "A"}, {"label": "B"}]},
        {"question": "Details?", "options": []},
    ]
    qid = await manager.create_pending_question(session.id, form)
    published = asyncio.Event()
    async def observe(event):
        if event["type"] == "question_answer":
            published.set()
    manager.on_broadcast("resolution", observe)
    response = await client.post(f"/api/sessions/{session.id}/questions/{qid}/answer", json={
        "answers": [{"selected": ["B", "A"], "text": ""}, {"selected": [], "text": "  details  "}],
        "source": {"kind": "system"},
    })
    assert response.status_code == 200
    await asyncio.wait_for(published.wait(), 1)
    detail = (await client.get(f"/api/sessions/{session.id}")).json()
    saved = next(row for row in detail["messages"] if row["type"] == "question_answer")
    live = next(event for event in events if event["type"] == "question_answer")
    resolution = saved["tool_input"]["question_resolution"]
    assert live["tool_input"] == saved["tool_input"]
    assert resolution["version"] == 1 and resolution["disposition"] == "answered"
    assert resolution["question_id"] == qid
    assert resolution["source"] == {"kind": "account", "user_id": session.user_id}
    assert resolution["answers"] == [
        {"question_index": 0, "selected": [{"option_index": 1, "label": "B"}, {"option_index": 0, "label": "A"}], "text": None},
        {"question_index": 1, "selected": [], "text": "  details  "},
    ]
    assert saved["content"] == "Q: Colors?\nA: B, A\n\nQ: Details?\nA:   details  "


async def test_delegated_answer_keeps_runtime_and_original_relationship(work_runtime):
    manager, delegations, parent, starts = work_runtime
    parent = await manager.create_session(parent.agent_id, "Runtime answerer", parent.working_dir, backend="codex")
    await manager.start_message(parent.id, "held parent")
    caller = await wait_live_scope(await asyncio.wait_for(starts.get(), 2))
    rec = await delegations.start_delegation(parent_session_id=parent.id, agent_name="Worker",
        request="held child", scope=caller)
    child_scope = await wait_live_scope(await asyncio.wait_for(starts.get(), 2))
    question_id = await manager.create_pending_question(rec.delegation_id, QUESTIONS, scope=child_scope)
    token = mint(caller.session_id, user_id=caller.user_id, run_lease_id=caller.run_lease_id, work_id=caller.work_id)
    url = f"/api/sessions/{parent.id}/delegations/{rec.delegation_id}/answer"
    client = manager.work_test_client
    rejected = await client.post(url, headers={"Authorization": f"Bearer {token}"},
        json={"question_id": question_id, "answers": [{"selected": ["A", "B"]}]})
    assert rejected.status_code == 400 and manager.has_pending_question(rec.delegation_id, question_id)
    for selected in (False, 0, "", {}):
        rejected = await client.post(url, headers={"Authorization": f"Bearer {token}"},
            json={"question_id": question_id, "answers": [{"selected": selected, "text": "choice"}]})
        assert rejected.status_code == 400 and manager.has_pending_question(rec.delegation_id, question_id)
    accepted = await client.post(url, headers={"Authorization": f"Bearer {token}"},
        json={"question_id": question_id, "answers": [{"text": "  parent choice  "}]})
    assert accepted.status_code == 200
    saved = next(row for row in await manager.db.load_messages(rec.delegation_id) if row["type"] == "question_answer")
    resolution = saved["tool_input"]["question_resolution"]
    assert resolution["source"] == {
        "kind": "agent", "user_id": parent.user_id, "session_id": parent.id,
        "run_lease_id": caller.run_lease_id, "delegation_id": rec.delegation_id,
        "delegation_work_id": rec.work_id, "parent_session_id": parent.id,
    }
    assert resolution["request_id"] and resolution["attempt_id"] and resolution["work_id"] == rec.work_id
    assert saved["content"] == "Q: Choose\nA: parent choice"


async def test_expiry_and_explicit_closure_keep_distinct_dispositions(question_host, monkeypatch):
    manager, session, client, events = question_host
    monkeypatch.setattr(settings, "ask_user_question_timeout_seconds", .02)
    expired = await manager.create_pending_question(session.id, QUESTIONS)
    assert await manager.wait_for_question_answer(session.id, expired, timeout=1) == manager.AUTO_ANSWER_TEXT
    timeout = next(row for row in await manager.db.load_messages(session.id) if row["type"] == "question_answer")
    fact = timeout["tool_input"]["question_resolution"]
    assert fact["disposition"] == "expired" and fact["source"] == {"kind": "system"} and fact["answers"] is None
    monkeypatch.setattr(settings, "ask_user_question_timeout_seconds", 0)
    closed = await manager.create_pending_question(session.id, QUESTIONS)
    assert (await client.delete(f"/api/sessions/{session.id}/questions/{closed}")).status_code == 200
    row = next(row for row in await manager.db.load_messages(session.id) if row["type"] == "question_closed")
    assert row["tool_input"]["reason"] == "cancelled"
    fact = row["tool_input"]["question_resolution"]
    assert fact["disposition"] == "closed" and fact["source"] == {"kind": "account", "user_id": session.user_id}


async def test_legacy_answer_is_read_without_invented_structure_or_source(question_host):
    manager, session, client, _ = question_host
    await manager.db.append_message(session_id=session.id, seq=0, role="user", type="question_answer",
        content="Q: Legacy\nA: A", tool_use_id="old-form")
    saved = (await client.get(f"/api/sessions/{session.id}")).json()["messages"][0]
    assert saved["content"] == "Q: Legacy\nA: A" and saved["tool_input"] is None


async def test_recording_failure_after_claim_never_publishes_or_returns_an_answer(question_host, monkeypatch):
    from server.sessions.owner import SessionOwnerUnavailable

    manager, session, _, events = question_host
    qid = await manager.create_pending_question(session.id, [{"question": "Free text", "options": None}])
    append = manager.db.append_message

    async def fail_resolution(**message):
        if message["type"] == "question_answer":
            raise ValueError("recording rejected")
        return await append(**message)

    monkeypatch.setattr(manager.db, "append_message", fail_resolution)
    with pytest.raises(SessionOwnerUnavailable):
        await manager.answer_question(session.id, qid, [{"text": "keep original"}])
    assert manager.session_owner_failed(session.id)
    assert not any(event["type"] == "question_answer" for event in events)
    assert not any(row["type"] == "question_answer" for row in await manager.db.load_messages(session.id))
    with pytest.raises(ValueError, match="recording is unavailable"):
        await manager.wait_for_question_answer(session.id, qid)
