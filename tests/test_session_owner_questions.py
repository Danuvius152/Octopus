"""Question commands retain their original state across detached waiters."""

from __future__ import annotations

import asyncio
import sys
from dataclasses import replace

import pytest

from server.config import settings
from server.harness import HarnessEvent, get_harness
from server.harness.profile import StdinMode
from server.sessions.owner import SessionMailboxFull
from tests import test_question_lifecycle as question_tests

HEADERS, QUESTIONS = question_tests.HEADERS, question_tests.QUESTIONS
question_host = question_tests.question_host


async def test_cancelled_creation_waiter_keeps_the_original_question_command(question_host, monkeypatch):
    manager, session, client, events = question_host
    entered, release, delivered = asyncio.Event(), asyncio.Event(), asyncio.Event()
    execute = manager.db.conn.execute

    async def hold_actual_insert(sql, parameters=()):
        if sql.lstrip().startswith("INSERT INTO messages"):
            entered.set()
            await release.wait()
        return await execute(sql, parameters)

    async def observe(event):
        if event.get("type") == "question_request":
            delivered.set()

    monkeypatch.setattr(manager.db.conn, "execute", hold_actual_insert)
    manager.on_broadcast("owned-create", observe)
    creating = asyncio.create_task(manager.create_pending_question(session.id, QUESTIONS))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        creating.cancel()
        done, _ = await asyncio.wait({creating}, timeout=.15)
        assert creating in done, "The cancelled HTTP waiter still owned the question's SQL command"
        with pytest.raises(asyncio.CancelledError):
            await creating
        release.set()
        await asyncio.wait_for(delivered.wait(), 1)
        question_id = next(event["question_id"] for event in events if event["type"] == "question_request")
        snapshot = await client.get(f"/api/sessions/{session.id}", headers=HEADERS)
        assert [question["question_id"] for question in snapshot.json()["pending_questions"]] == [question_id]
        answered = await client.post(
            f"/api/sessions/{session.id}/questions/{question_id}/answer", headers=HEADERS,
            json={"answers": [{"selected": ["B"]}]},
        )
        assert answered.status_code == 200
        poll = await client.get(f"/api/sessions/{session.id}/questions/{question_id}/answer", headers=HEADERS)
        assert poll.json() == {"answer": "Q: Choose\nA: B"}
        snapshot = await client.get(f"/api/sessions/{session.id}", headers=HEADERS)
        assert [row["type"] for row in snapshot.json()["messages"]] == ["question_request", "question_answer"]
    finally:
        release.set()
        await asyncio.gather(creating, return_exceptions=True)


async def test_question_subscriber_can_answer_and_stop_its_original_session(question_host):
    manager, session, client, events = question_host
    answered, stopped = asyncio.Event(), asyncio.Event()
    answers = []

    async def reenter(event):
        if event.get("type") == "question_request":
            response = await client.post(
                f"/api/sessions/{session.id}/questions/{event['question_id']}/answer", headers=HEADERS,
                json={"answers": [{"selected": ["A"]}]},
            )
            answers.append(response.status_code)
            answered.set()
        if event.get("type") == "question_answer":
            assert await manager.interrupt(session.id) is False
            stopped.set()

    manager.on_broadcast("answer-stop-reentry", reenter)
    created = await asyncio.wait_for(client.post(
        f"/api/sessions/{session.id}/questions", headers=HEADERS, json={"questions": QUESTIONS},
    ), 1)
    assert created.status_code == 201
    await asyncio.wait_for(answered.wait(), 1)
    await asyncio.wait_for(stopped.wait(), 1)
    assert answers == [200]
    assert [event["type"] for event in events if event["type"].startswith("question_")] == [
        "question_request", "question_answer",
    ]
    detail = await client.get(f"/api/sessions/{session.id}", headers=HEADERS)
    assert [(row["type"], row.get("content")) for row in detail.json()["messages"]] == [
        ("question_request", None), ("question_answer", "Q: Choose\nA: A"),
    ]


async def test_native_question_prefix_precedes_the_original_success_receipt(question_host, monkeypatch):
    manager, session, client, events = question_host
    run = question_tests.EarlyFinishRun()
    entered, release = asyncio.Event(), asyncio.Event()

    async def hold_native_prefix(event):
        if event.get("type") == "question_request":
            entered.set()
            await release.wait()

    manager.on_broadcast("hold-native-prefix", hold_native_prefix)
    monkeypatch.setattr(manager, "_make_run", lambda *_args, **_kwargs: run)
    receipt = await manager.start_message(session.id, "Keep the native question in its original prefix")
    waiting = asyncio.create_task(receipt.wait())
    try:
        await asyncio.wait_for(entered.wait(), 1)
        done, _ = await asyncio.wait({waiting}, timeout=.1)
        assert not done, "Success overtook the original native question notification"
        assert not [event for event in events if event["type"] == "result"]
        detail = await client.get(f"/api/sessions/{session.id}", headers=HEADERS)
        native = next(row for row in detail.json()["messages"] if row["type"] == "question_request")
        assert native["tool_use_id"] == "native-question"
        assert native["tool_input"]["request_id"] == receipt.request_id
        assert native["tool_input"]["attempt_id"]
        release.set()
        completed = await asyncio.wait_for(waiting, 1)
        assert completed.state == "completed" and completed.outcome.status == "success"
        assert len([event for event in events if event["type"] == "question_request"]) == 1
        assert len([event for event in events if event["type"] == "result"]) == 1
    finally:
        release.set()
        await asyncio.gather(waiting, return_exceptions=True)


@pytest.mark.parametrize("entry", ["answer", "native"])
async def test_question_control_has_entry_when_ordinary_command_capacity_is_full(question_host, monkeypatch, entry):
    manager, session, client, _events = question_host
    question = None
    native_ready, allow_native, native_delivered = (asyncio.Event() for _ in range(3))
    receipt = None

    class HeldNativeRun(question_tests.EarlyFinishRun):
        async def stream(self):
            yield HarnessEvent(type="text", content="The original reader is ready.")
            native_ready.set()
            await allow_native.wait()
            yield HarnessEvent(type="question_request", tool_use_id="original-native", tool_input={"questions": QUESTIONS})
            yield HarnessEvent(type="result", session_id="original-native-thread")

    async def observe_native(event):
        if event.get("type") == "question_request" and event.get("question_id") == "original-native":
            native_delivered.set()

    if entry == "answer":
        question = await manager.create_pending_question(session.id, QUESTIONS)
    else:
        run = HeldNativeRun()
        manager.on_broadcast("native-capacity-observer", observe_native)
        monkeypatch.setattr(manager, "_make_run", lambda *_args, **_kwargs: run)
        receipt = await manager.start_message(session.id, "Retain the original native observation")
        await asyncio.wait_for(native_ready.wait(), 1)
    entered, release, queued_entered, queued_release = (asyncio.Event() for _ in range(4))
    has_notice = manager.db.has_delivery_notice

    async def hold_real_reads(session_id, delivery_id):
        result = await has_notice(session_id, delivery_id)
        if delivery_id == "block-ordinary":
            entered.set()
            await release.wait()
        elif delivery_id == "ordinary-0":
            queued_entered.set()
            await queued_release.wait()
        return result

    monkeypatch.setattr(manager.db, "has_delivery_notice", hold_real_reads)
    first = asyncio.create_task(manager.retain_result_notice(session.id, "block-ordinary", "First material"))
    queued = []
    answering = None
    try:
        await asyncio.wait_for(entered.wait(), 1)
        queued = [asyncio.create_task(manager.retain_result_notice(
            session.id, f"ordinary-{index}", f"Original ordinary material {index}",
        )) for index in range(257)]
        done, _ = await asyncio.wait(queued, timeout=.1)
        assert len(done) == 1
        with pytest.raises(SessionMailboxFull):
            await next(iter(done))
        if entry == "answer":
            answering = asyncio.create_task(client.post(
                f"/api/sessions/{session.id}/questions/{question}/answer", headers=HEADERS,
                json={"answers": [{"selected": ["B"]}]},
            ))
        else:
            allow_native.set()
            await asyncio.sleep(0)
        release.set()
        await asyncio.wait_for(queued_entered.wait(), 1)
        if entry == "answer":
            response = await asyncio.wait_for(answering, .2)
            assert response.status_code == 200
            poll = await client.get(f"/api/sessions/{session.id}/questions/{question}/answer", headers=HEADERS)
            assert poll.json() == {"answer": "Q: Choose\nA: B"}
        else:
            await asyncio.wait_for(native_delivered.wait(), .2)
            detail = await client.get(f"/api/sessions/{session.id}", headers=HEADERS)
            native = [row for row in detail.json()["messages"] if row["type"] == "question_request"]
            assert len(native) == 1 and native[0]["tool_use_id"] == "original-native"
    finally:
        allow_native.set()
        release.set()
        queued_release.set()
        await asyncio.gather(first, *queued, *([answering] if answering is not None else []), return_exceptions=True)
        if receipt is not None:
            completed = await asyncio.wait_for(receipt.wait(), 1)
            assert completed.state == "completed" and completed.outcome.status == "success"


@pytest.mark.parametrize(("failed_prefix", "failed_terminal"), [
    pytest.param(False, False, id="success"),
    pytest.param(True, False, id="question-cancelled"),
    pytest.param(False, True, id="terminal-cancelled"),
])
async def test_http_question_prefix_precedes_the_original_success_receipt(
    question_host, monkeypatch, tmp_path, failed_prefix, failed_terminal,
):
    manager, original, client, events = question_host
    session = await manager.create_session(original.agent_id, "Original question prefix", str(tmp_path), backend="codex")
    entered, release, ready = asyncio.Event(), asyncio.Event(), asyncio.Event()
    allow_result = tmp_path / "allow-original-result"
    model = tmp_path / "question_prefix_model.py"
    model.write_text(
        "import json,sys,time\n"
        "from pathlib import Path\n"
        "sys.stdin.readline()\n"
        "print(json.dumps({'type':'thread.started','thread_id':'question-prefix-thread'}),flush=True)\n"
        "print(json.dumps({'type':'item.completed','item':{'id':'first','type':'agent_message','text':'Waiting for the original answer.'}}),flush=True)\n"
        "while not Path(sys.argv[1]).exists(): time.sleep(.01)\n"
        "print(json.dumps({'type':'item.completed','item':{'id':'after','type':'agent_message','text':'The original answer was received.'}}),flush=True)\n"
        "print(json.dumps({'type':'turn.completed','usage':{'input_tokens':1,'output_tokens':1}}),flush=True)\n"
        "sys.stdin.read()\n"
    )
    contexts = asyncio.Queue()

    def build_argv(context):
        contexts.put_nowait(context)
        return [sys.executable, "-u", str(model), str(allow_result)], {"cwd": context.working_dir}

    harness = get_harness("codex")
    monkeypatch.setattr(harness, "profile", replace(
        harness.profile, binary=sys.executable, activity_observer=None, prepare_resume=None,
        stdin_mode=StdinMode.STREAM_JSON, build_turn_argv=build_argv,
    ))
    monkeypatch.setattr(settings, "ask_user_question_timeout_seconds", 0)

    async def hold_original_prefix(event):
        if event.get("session_id") != session.id:
            return
        if event.get("type") == "assistant_text" and event.get("content") == "Waiting for the original answer.":
            ready.set()
        if event.get("type") == "question_request":
            entered.set()
            await release.wait()
            if failed_prefix:
                raise asyncio.CancelledError("The original question subscriber could not finish.")
        if event.get("type") == "result" and failed_terminal:
            raise asyncio.CancelledError("The original terminal subscriber could not finish.")

    manager.on_broadcast("hold-http-question-prefix", hold_original_prefix)
    receipt = await manager.start_message(session.id, "Ask before continuing", work_id="original-question-work")
    waiting = asyncio.create_task(receipt.wait())
    polling = None
    try:
        context = await asyncio.wait_for(contexts.get(), 2)
        await asyncio.wait_for(ready.wait(), 2)
        token = next(entry.credential for entry in context.mcp_servers if entry.key == "ask")
        scope_headers = {"Authorization": f"Bearer {token}"}
        created = await asyncio.wait_for(client.post(
            f"/api/sessions/{session.id}/questions", headers=scope_headers, json={"questions": QUESTIONS},
        ), 1)
        assert created.status_code == 201
        question = created.json()["question_id"]
        await asyncio.wait_for(entered.wait(), 1)
        polling = asyncio.create_task(client.get(
            f"/api/sessions/{session.id}/questions/{question}/answer", headers=scope_headers,
        ))
        answered = await client.post(
            f"/api/sessions/{session.id}/questions/{question}/answer", headers=HEADERS,
            json={"answers": [{"selected": ["A"]}]},
        )
        assert answered.status_code == 200
        assert (await asyncio.wait_for(polling, 1)).json() == {"answer": "Q: Choose\nA: A"}
        if failed_prefix:
            release.set()
            # Let the original publisher advance, then fence through a public
            # owner command before the attempt retires its answered record.
            delivered = asyncio.Event()

            async def receive_after_failure(event):
                if event.get("type") == "delivery_notice":
                    delivered.set()

            manager.on_broadcast("after-original-question-failure", receive_after_failure)
            assert await manager.retain_result_notice(session.id, "after-question-failure", "Retain the original delivery evidence.")
            await asyncio.wait_for(delivered.wait(), 1)
            assert await manager.close_pending_question(session.id, question, reason="already_answered") is False
        allow_result.write_text("The captured long poll received the original answer.")
        if failed_prefix:
            completion = await asyncio.wait_for(waiting, 2)
            assert completion.state == "completed" and completion.recording_state == "committed"
            assert completion.outcome.status == "success" and completion.notification_state == "failed"
            assert not [event for event in events if event["type"] == "result" and event.get("session_id") == session.id]
            detail = await client.get(f"/api/sessions/{session.id}", headers=HEADERS)
            terminal = [row for row in detail.json()["messages"] if row["type"] == "result"]
            assert len(terminal) == 1 and terminal[0]["content"]["status"] == "success"
            assert any(row.get("content") == "The original answer was received." for row in detail.json()["messages"])
        else:
            done, _ = await asyncio.wait({waiting}, timeout=.4)
            assert not done, "Success overtook the original HTTP question notification prefix"
            assert not [event for event in events if event["type"] == "result" and event.get("session_id") == session.id]
            release.set()
            if failed_terminal:
                completion = await asyncio.wait_for(waiting, 2)
                assert completion.state == "completed" and completion.recording_state == "committed"
                assert completion.outcome.status == "success" and completion.notification_state == "failed"
                detail = await client.get(f"/api/sessions/{session.id}", headers=HEADERS)
                terminal = [row for row in detail.json()["messages"] if row["type"] == "result"]
                assert len(terminal) == 1 and terminal[0]["content"]["status"] == "success"
                assert any(row.get("content") == "The original answer was received." for row in detail.json()["messages"])
                await asyncio.wait_for(manager.drain_turns(timeout=2), 3)
            else:
                completed = await asyncio.wait_for(waiting, 2)
                assert completed.state == "completed" and completed.outcome.status == "success"
    finally:
        release.set()
        allow_result.write_text("Release the original child during cleanup.")
        await manager.interrupt(session.id)
        await asyncio.gather(waiting, *([polling] if polling is not None else []), return_exceptions=True)


async def test_live_question_subscriber_can_archive_without_waiting_on_its_prefix(
    question_host, monkeypatch, tmp_path,
):
    manager, original, client, events = question_host
    session = await manager.create_session(original.agent_id, "Archive from question", str(tmp_path), backend="codex")
    model = tmp_path / "archive_question_model.py"
    model.write_text(
        "import json,sys\n"
        "sys.stdin.readline()\n"
        "print(json.dumps({'type':'thread.started','thread_id':'archive-question-thread'}),flush=True)\n"
        "print(json.dumps({'type':'item.completed','item':{'id':'ready','type':'agent_message','text':'The original run is waiting.'}}),flush=True)\n"
        "sys.stdin.read()\n"
    )
    harness = get_harness("codex")
    monkeypatch.setattr(harness, "profile", replace(
        harness.profile, binary=sys.executable, activity_observer=None, prepare_resume=None,
        stdin_mode=StdinMode.STREAM_JSON,
        build_turn_argv=lambda context: ([sys.executable, "-u", str(model)], {"cwd": context.working_dir}),
    ))
    ready, archived = asyncio.Event(), asyncio.Event()

    async def archive_from_original_question(event):
        if event.get("session_id") != session.id:
            return
        if event.get("type") == "assistant_text":
            ready.set()
        if event.get("type") == "question_request":
            await manager.archive_session(session.id, replace=False)
            archived.set()

    manager.on_broadcast("archive-original-question", archive_from_original_question)
    receipt = await manager.start_message(session.id, "Retain this original request through archive.")
    try:
        await asyncio.wait_for(ready.wait(), 2)
        created = await client.post(f"/api/sessions/{session.id}/questions", headers=HEADERS, json={"questions": QUESTIONS})
        assert created.status_code == 201
        await asyncio.wait_for(archived.wait(), 3)
        completion = await asyncio.wait_for(receipt.wait(), 3)
        assert completion.outcome is not None and completion.outcome.status == "interrupted"
        detail = await client.get(f"/api/sessions/{session.id}", headers=HEADERS)
        assert detail.status_code == 200 and detail.json()["archived"] is True
        assert len([row for row in detail.json()["messages"] if row["type"] == "question_closed"]) == 1
        assert len([row for row in detail.json()["messages"] if row["type"] == "result"]) == 1
        assert len([event for event in events if event["type"] == "result" and event.get("session_id") == session.id]) == 1
    finally:
        await manager.interrupt(session.id)


@pytest.mark.parametrize("pressure", ["count", "bytes"])
async def test_question_resolution_keeps_its_original_slot_when_publication_is_full(question_host, pressure):
    manager, session, client, _events = question_host
    question = await manager.create_pending_question(session.id, QUESTIONS)
    entered, release, answered = asyncio.Event(), asyncio.Event(), asyncio.Event()
    delivered = []
    count = 256 if pressure == "count" else 1

    async def hold_original(event):
        if event.get("type") == "delivery_notice" and event["tool_input"]["delivery_id"] == "prefix-0":
            entered.set()
            await release.wait()

    async def observe(event):
        if event.get("type") == "delivery_notice":
            delivered.append(event["tool_input"]["delivery_id"])
        elif event.get("type") == "question_answer":
            delivered.append("original-answer")
            answered.set()

    manager.on_broadcast("full-question-prefix", hold_original)
    manager.on_broadcast("receive-full-question-prefix", observe)
    try:
        content = "One original notice" if pressure == "count" else "L" * (1024 * 1024 + 32)
        assert await manager.retain_result_notice(session.id, "prefix-0", content)
        await asyncio.wait_for(entered.wait(), 1)
        for index in range(1, count):
            assert await manager.retain_result_notice(session.id, f"prefix-{index}", "Original queued material")
        refused = await client.post(
            f"/api/sessions/{session.id}/questions", headers=HEADERS, json={"questions": QUESTIONS},
        )
        assert refused.status_code == 400 and "notification capacity is full" in refused.json()["detail"]
        response = await asyncio.wait_for(client.post(
            f"/api/sessions/{session.id}/questions/{question}/answer", headers=HEADERS,
            json={"answers": [{"selected": ["B"]}]},
        ), 1)
        assert response.status_code == 200
        poll = await client.get(f"/api/sessions/{session.id}/questions/{question}/answer", headers=HEADERS)
        assert poll.json() == {"answer": "Q: Choose\nA: B"}
        assert await manager.interrupt(session.id) is False
        assert not delivered and not answered.is_set()
        release.set()
        await asyncio.wait_for(answered.wait(), 2)
        assert delivered == [*(f"prefix-{index}" for index in range(count)), "original-answer"]
        detail = await client.get(
            f"/api/sessions/{session.id}/messages?before_seq={count + 2}&limit=500", headers=HEADERS,
        )
        assert [row["type"] for row in detail.json()["messages"]] == [
            "question_request", *(["delivery_notice"] * count), "question_answer",
        ]
    finally:
        release.set()
