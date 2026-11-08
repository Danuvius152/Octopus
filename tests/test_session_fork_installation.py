"""Only a fully prepared fork is available to public session commands."""

from __future__ import annotations

import asyncio

import pytest

from server.harness import get_harness
from server.sessions.questions import QuestionClosedError
from tests.session_fork_support import installation as installation


@pytest.mark.parametrize("full_copy", [False, True])
async def test_fork_candidate_is_unavailable_until_its_original_artifacts_are_prepared(
    installation, monkeypatch, full_copy,
):
    manager, parent_id, client, directory = installation
    entered, release = asyncio.Event(), asyncio.Event()
    target = get_harness("codex") if full_copy else manager.db
    preparation = "prepare_fork_copy" if full_copy else "create_fork_session"
    prepare = getattr(target, preparation)
    captured = []

    async def hold_original_preparation(*args, **kwargs):
        result = await prepare(*args, **kwargs)
        rows = await manager.db.load_incomplete_forks()
        captured.extend(row["id"] for row in rows)
        entered.set()
        await release.wait()
        return result

    monkeypatch.setattr(target, preparation, hold_original_preparation)
    rewinding = asyncio.create_task(client.post(
        f"/api/sessions/{parent_id}/duplicate" if full_copy else f"/api/sessions/{parent_id}/fork",
        json={} if full_copy else {"rewind_to_msg_seq": 0},
    ))
    try:
        await asyncio.wait_for(entered.wait(), 3)
        assert len(captured) == 1
        child_id = captured[0]
        listed = await client.get("/api/sessions")
        assert listed.status_code == 200
        assert [row["id"] for row in listed.json()] == [parent_id]
        assert (await client.get(f"/api/sessions/{child_id}")).status_code == 404
        assert await manager.retain_result_notice(child_id, "too-early", "Never admitted.") is False
        assert (directory / "original.txt").read_text() == "Source bytes remain unchanged.\n"

        release.set()
        reply = await asyncio.wait_for(rewinding, 3)
        assert reply.status_code == 201, reply.text
        assert reply.json()["id"] == child_id
        assert reply.json()["claude_session_id"] is None
        assert reply.json()["forked_from_session_id"] == parent_id
        assert (await client.get(f"/api/sessions/{child_id}")).status_code == 200
        assert await manager.retain_result_notice(child_id, "ready-material", "Confirmed after installation.")
        detail = await client.get(f"/api/sessions/{child_id}")
        expected = (["Original request.", "Confirmed after installation."]
                    if full_copy else ["Confirmed after installation."])
        assert [row["content"] for row in detail.json()["messages"]] == expected
        archived = await client.get(f"/api/sessions/{parent_id}")
        assert archived.status_code == 200 and archived.json()["archived"] is not full_copy
        assert [row["content"] for row in archived.json()["messages"]] == ["Original request."]
    finally:
        release.set()
        await asyncio.gather(rewinding, return_exceptions=True)


async def test_failed_candidate_retains_recovery_evidence_without_becoming_a_live_session(
    installation, monkeypatch,
):
    manager, parent_id, client, _directory = installation
    harness = get_harness("codex")
    prepare, cleanup = harness.prepare_fork_copy, harness.cleanup_incomplete_fork_artifacts

    async def reject_preparation(*args, **kwargs):
        await prepare(*args, **kwargs)
        raise RuntimeError("Original artifact preparation failed.")

    async def reject_cleanup(*args, **kwargs):
        await cleanup(*args, **kwargs)
        raise OSError("Artifact cleanup remains unavailable.")

    monkeypatch.setattr(harness, "prepare_fork_copy", reject_preparation)
    monkeypatch.setattr(harness, "cleanup_incomplete_fork_artifacts", reject_cleanup)
    with pytest.raises(OSError, match="cleanup remains unavailable"):
        await manager.duplicate_session(parent_id)
    retained = await manager.db.load_incomplete_forks()
    assert len(retained) == 1
    child_id = retained[0]["id"]
    assert (await client.get(f"/api/sessions/{child_id}")).status_code == 404

    await manager.initialize(manager.db)
    assert [row["id"] for row in await manager.db.load_incomplete_forks()] == [child_id]
    assert [row["id"] for row in (await client.get("/api/sessions")).json()] == [parent_id]
    assert (await client.get(f"/api/sessions/{child_id}")).status_code == 404
    assert await manager.retain_result_notice(child_id, "not-installed", "Never accepted.") is False
    monkeypatch.setattr(harness, "cleanup_incomplete_fork_artifacts", cleanup)
    await manager.initialize(manager.db)
    assert await manager.db.load_incomplete_forks() == []
    detail = await client.get(f"/api/sessions/{parent_id}")
    assert [row["content"] for row in detail.json()["messages"]] == ["Original request."]


async def test_rewind_closes_the_original_question_and_wakes_its_captured_poll(installation):
    manager, parent_id, client, _directory = installation
    question_id = await manager.create_pending_question(parent_id, [{"question": "Original form?"}])
    assert question_id is not None
    polling = asyncio.create_task(manager.wait_for_question_answer(parent_id, question_id, timeout=3))
    try:
        await asyncio.sleep(0)
        response = await client.post(f"/api/sessions/{parent_id}/fork", json={"rewind_to_msg_seq": 0})
        assert response.status_code == 201, response.text
        with pytest.raises(QuestionClosedError):
            await asyncio.wait_for(polling, 1)
        detail = await client.get(f"/api/sessions/{parent_id}")
        assert detail.status_code == 200 and detail.json()["archived"] is True
        assert [row["type"] for row in detail.json()["messages"]] == [
            "text", "question_request", "question_closed",
        ]
    finally:
        await asyncio.gather(polling, return_exceptions=True)


async def test_answer_subscriber_can_rewind_without_waiting_on_its_own_publication(installation):
    manager, parent_id, client, _directory = installation
    question_id = await manager.create_pending_question(parent_id, [{"question": "Original form?"}])
    assert question_id is not None
    rewound, delivered = asyncio.Event(), asyncio.Event()
    results, received, failures = [], [], []

    async def rewind_original_answer(event):
        if event.get("type") == "question_answer" and event.get("session_id") == parent_id:
            try:
                async with asyncio.timeout(2):
                    response = await client.post(f"/api/sessions/{parent_id}/fork", json={"rewind_to_msg_seq": 0})
                results.append(response)
            except TimeoutError:
                failures.append("Rewind waited on the answer publication that called it.")
            finally:
                rewound.set()

    async def receive_original_answer(event):
        if event.get("type") == "question_answer" and event.get("session_id") == parent_id:
            received.append(event["content"])
            delivered.set()

    manager.on_broadcast("rewind-from-original-answer", rewind_original_answer)
    manager.on_broadcast("receive-original-answer", receive_original_answer)
    answering = asyncio.create_task(manager.answer_question(parent_id, question_id, [{"text": "Keep the evidence."}]))
    try:
        await asyncio.wait_for(rewound.wait(), 3)
        assert not failures, failures
        assert len(results) == 1 and results[0].status_code == 201
        assert await asyncio.wait_for(answering, 2) is True
        await asyncio.wait_for(delivered.wait(), 2)
        assert received == ["Q: Original form?\nA: Keep the evidence."]
        parent = await client.get(f"/api/sessions/{parent_id}")
        assert parent.status_code == 200 and parent.json()["archived"] is True
        assert [row["content"] for row in parent.json()["messages"]] == [
            "Original request.", None, "Q: Original form?\nA: Keep the evidence.",
        ]
        child = await client.get(f"/api/sessions/{results[0].json()['id']}")
        assert child.status_code == 200
    finally:
        # The subscriber's own deadline releases a broken publication cycle;
        # its original lifecycle command remains owned and can finish.
        await asyncio.gather(answering, return_exceptions=True)
