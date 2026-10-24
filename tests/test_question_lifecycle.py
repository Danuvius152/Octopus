"""Question ownership and resolution through public manager and REST seams."""

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from server import deps
from server.agent_manager import AgentManager
from server.config import settings
from server.database import Database
from server.deps import get_session_manager
from server.harness import HarnessEvent
from server.routers import questions, sessions
from server.session_manager import SessionManager
from server.sessions import owner_questions
from server.sessions import questions as question_module
from server.users import UserManager
from server.workspace import paths_for
from tests.accounts import create_account
from tests.fake_run import FakeRunBase

QUESTIONS = [{"question": "Choose", "options": [{"label": "A"}, {"label": "B"}]}]


@pytest.fixture
async def question_host(tmp_path):
    db = Database(":memory:")
    await db.initialize()
    _, token = await create_account(db, extra_roots=[tmp_path])
    manager = SessionManager()
    await manager.initialize(db)
    agent = await db.get_system_agent()
    session = await manager.create_session(agent["id"], "Question lifecycle", None)
    events = []

    async def receive(event):
        events.append(event)

    manager.on_broadcast("question-test", receive)
    app = FastAPI()
    app.include_router(questions.router)
    app.include_router(sessions.router)
    app.dependency_overrides[get_session_manager] = lambda: manager
    try:
        async with AsyncClient(transport=ASGITransport(app), base_url="http://test",
                               headers={"Authorization": "Bearer " + token}) as client:
            yield manager, session, client, events
    finally:
        await manager.stop_all_held_processes()
        await db.close()


@pytest.fixture
async def owned_question_host(question_host, monkeypatch):
    manager, _, client, events = question_host
    users = UserManager(manager.db)
    owner = await users.create_user(username="question-owner", password="test-password")
    monkeypatch.setattr(deps, "_user_manager", users)
    agent = await AgentManager(manager.db).create_agent(name="Owned questions", user_id=owner["id"])
    session = await manager.create_session(agent["id"], "Owned question", None)
    client.headers["Authorization"] = "Bearer " + await users.issue_token(owner["id"])
    return manager, session, client, events


async def test_competing_human_answers_resolve_once(question_host):
    manager, session, client, events = question_host
    qid = await manager.create_pending_question(session.id, QUESTIONS)
    accepted = await asyncio.gather(
        manager.answer_question(session.id, qid, [{"selected": ["A"]}]),
        manager.answer_question(session.id, qid, [{"selected": ["B"]}]),
    )
    assert sorted(accepted) == [False, True]
    answer = await manager.wait_for_question_answer(session.id, qid, timeout=0.1)
    assert answer in ("Q: Choose\nA: A", "Q: Choose\nA: B")
    assert len([event for event in events if event["type"] == "question_answer"]) == 1
    detail = await client.get(f"/api/sessions/{session.id}", headers=client.headers)
    assert len([msg for msg in detail.json()["messages"] if msg["type"] == "question_answer"]) == 1


async def test_unknown_question_poll_is_closed_instead_of_waiting(question_host):
    _, session, client, _ = question_host
    response = await client.get(
        f"/api/sessions/{session.id}/questions/unknown/answer", headers=client.headers
    )
    assert response.status_code == 404
    assert response.json() == {"detail": "Question not found or closed"}


async def test_cancelling_one_long_poll_keeps_the_question_answerable(question_host):
    manager, session, client, _ = question_host
    qid = await manager.create_pending_question(session.id, QUESTIONS)
    polling = asyncio.create_task(client.get(
        f"/api/sessions/{session.id}/questions/{qid}/answer?timeout=60", headers=client.headers
    ))
    await asyncio.sleep(0)
    polling.cancel()
    with pytest.raises(asyncio.CancelledError):
        await polling
    assert [q.question_id for q in manager.get_pending_questions(session.id)] == [qid]
    answered = await client.post(
        f"/api/sessions/{session.id}/questions/{qid}/answer",
        headers=client.headers, json={"answers": [{"selected": ["A"]}]},
    )
    assert answered.status_code == 200
    late = await client.get(
        f"/api/sessions/{session.id}/questions/{qid}/answer", headers=client.headers
    )
    assert late.status_code == 200
    assert late.json() == {"answer": "Q: Choose\nA: A"}
    assert manager.get_pending_questions(session.id) == ()


async def test_stop_while_idle_closes_an_outstanding_form(question_host):
    manager, session, client, events = question_host
    qid = await manager.create_pending_question(session.id, QUESTIONS)
    assert await manager.interrupt(session.id) is False
    assert not manager.has_pending_question(session.id, qid)
    response = await client.get(f"/api/sessions/{session.id}/questions/{qid}/answer", headers=client.headers)
    assert response.status_code == 404
    assert len([event for event in events if event["type"] == "question_closed"]) == 1


@pytest.mark.parametrize("operation", ["interrupt", "reset"])
async def test_idle_control_preserves_a_question_created_while_old_closure_publishes(
    owned_question_host, operation,
):
    manager, session, client, events = owned_question_host
    created = await client.post(f"/api/sessions/{session.id}/questions", json={"questions": QUESTIONS})
    assert created.status_code == 201
    old_id = created.json()["question_id"]
    closing, release = asyncio.Event(), asyncio.Event()

    async def hold_old_closure(event):
        if event["type"] == "question_closed" and event["question_id"] == old_id:
            closing.set()
            await release.wait()

    manager.on_broadcast("hold-old-question-closure", hold_old_closure)
    controlling = asyncio.create_task(
        manager.interrupt(session.id) if operation == "interrupt"
        else client.post(f"/api/sessions/{session.id}/reset")
    )
    polling = None
    try:
        await asyncio.wait_for(closing.wait(), 1)
        created = await client.post(f"/api/sessions/{session.id}/questions", json={"questions": QUESTIONS})
        assert created.status_code == 201
        new_id = created.json()["question_id"]
        polling = asyncio.create_task(client.get(
            f"/api/sessions/{session.id}/questions/{new_id}/answer?timeout=60"
        ))
        await asyncio.sleep(0)
        release.set()
        result = await asyncio.wait_for(controlling, 1)
        if operation == "interrupt":
            assert result is False
        else:
            assert result.status_code == 200
        snapshot = await client.get(f"/api/sessions/{session.id}")
        assert [question["question_id"] for question in snapshot.json()["pending_questions"]] == [new_id]
        answered = await client.post(
            f"/api/sessions/{session.id}/questions/{new_id}/answer",
            json={"answers": [{"selected": ["B"]}]},
        )
        assert answered.status_code == 200
        polled = await asyncio.wait_for(polling, 1)
        assert polled.status_code == 200
        assert polled.json() == {"answer": "Q: Choose\nA: B"}
        assert [event["question_id"] for event in events if event["type"] == "question_closed"] == [old_id]
    finally:
        release.set()
        await asyncio.gather(controlling, return_exceptions=True)
        if polling is not None:
            polling.cancel()
            await asyncio.gather(polling, return_exceptions=True)


@pytest.mark.parametrize("operation", ["archive", "delete", "shutdown"])
async def test_closed_admission_rejects_questions_while_old_closure_publishes(
    owned_question_host, monkeypatch, operation,
):
    manager, session, client, events = owned_question_host
    created = await client.post(f"/api/sessions/{session.id}/questions", json={"questions": QUESTIONS})
    assert created.status_code == 201
    old_id = created.json()["question_id"]
    closing, release = asyncio.Event(), asyncio.Event()
    late_answers = []

    async def observe(event):
        if event["type"] == "question_closed" and event["question_id"] == old_id:
            closing.set()
            await release.wait()
        elif event["type"] == "question_answer" and manager.get_session(session.id) is None:
            late_answers.append({"question_id": event["question_id"], "auto": event.get("auto", False)})

    manager.on_broadcast("closed-question-admission", observe)
    if operation == "archive":
        control = client.post(f"/api/sessions/{session.id}/archive?replace=false")
    elif operation == "delete":
        control = client.delete(f"/api/sessions/{session.id}")
    else:
        control = manager.drain_turns(timeout=1)
    retiring = asyncio.create_task(control)
    try:
        await asyncio.wait_for(closing.wait(), 1)
        # Visibility/resource retirement is independent of this subscriber.
        # The original closure remains ordered and finishes after release.
        result = await asyncio.wait_for(retiring, 1)
        monkeypatch.setattr(settings, "ask_user_question_timeout_seconds", 0.05)
        rejected = await client.post(f"/api/sessions/{session.id}/questions", json={"questions": QUESTIONS})
        release.set()
        if operation == "shutdown":
            assert result == 0
        else:
            assert result.status_code == 204
            assert manager.get_session(session.id) is None
        # A refused question must never create a timer that publishes after
        # the owning Session has been retired.
        await asyncio.sleep(0.1)
        if operation == "shutdown":
            assert rejected.status_code == 400
            assert rejected.json() == {"detail": "Session admission is closed"}
        else:
            assert rejected.status_code == 404
            assert rejected.json() == {"detail": "Session not found"}
        assert [event["question_id"] for event in events if event["type"] == "question_request"] == [old_id]
        assert not [event for event in events if event["type"] == "question_answer"]
        assert late_answers == []
    finally:
        release.set()
        await asyncio.gather(retiring, return_exceptions=True)


async def test_closing_question_wakes_poll_and_removes_live_form(question_host):
    manager, session, client, events = question_host
    qid = await manager.create_pending_question(session.id, QUESTIONS)
    polling = asyncio.create_task(client.get(
        f"/api/sessions/{session.id}/questions/{qid}/answer?timeout=60", headers=client.headers
    ))
    await asyncio.sleep(0)
    closed = await client.delete(
        f"/api/sessions/{session.id}/questions/{qid}", headers=client.headers
    )
    assert closed.status_code == 200
    polled = await asyncio.wait_for(polling, timeout=1)
    assert polled.status_code == 404
    assert not manager.has_pending_question(session.id, qid)
    assert len([event for event in events if event["type"] == "question_closed"]) == 1
    late = await client.post(
        f"/api/sessions/{session.id}/questions/{qid}/answer",
        headers=client.headers, json={"answers": [{"selected": ["A"]}]},
    )
    assert late.status_code == 404
    detail = await client.get(f"/api/sessions/{session.id}", headers=client.headers)
    assert detail.json()["pending_questions"] == []
    assert len([msg for msg in detail.json()["messages"] if msg["type"] == "question_closed"]) == 1


@pytest.mark.parametrize("timeout", [60, 0])
async def test_question_deadline_is_owned_by_host_and_survives_snapshot(
    question_host, monkeypatch, timeout
):
    _, session, client, _ = question_host
    monkeypatch.setattr(settings, "ask_user_question_timeout_seconds", timeout)
    before = datetime.now(UTC).timestamp()
    created = await client.post(
        f"/api/sessions/{session.id}/questions", headers=client.headers, json={"questions": QUESTIONS}
    )
    assert created.status_code == 201
    assert "deadline" in created.json()
    deadline = created.json()["deadline"]
    if timeout:
        assert before + 59 < datetime.fromisoformat(deadline).timestamp() < before + 61
    else:
        assert deadline is None
    detail = await client.get(f"/api/sessions/{session.id}", headers=client.headers)
    assert detail.json()["pending_questions"][0]["deadline"] == deadline


class WaitingRun(FakeRunBase):
    """External model run that calls the host question API and waits."""

    def __init__(self, manager, session, *, count=1, after_answer_delay=0):
        self.manager = manager
        self.session = session
        self.count = count
        self.after_answer_delay = after_answer_delay
        self.question_ids = []
        self.created = asyncio.Event()
        self.stopped = asyncio.Event()
        self.stderr_text = ""

    async def start(self, *args, **kwargs):
        pass

    async def stop(self):
        self.stopped.set()

    async def stream(self):
        yield HarnessEvent(type="text", content="I need your decision.")
        for _ in range(self.count):
            self.question_ids.append(await self.manager.create_pending_question(self.session.id, QUESTIONS))
        self.created.set()
        answers = asyncio.gather(*(
            self.manager.wait_for_question_answer(self.session.id, qid, timeout=60)
            for qid in self.question_ids
        ))
        stopped = asyncio.create_task(self.stopped.wait())
        try:
            completed, _ = await asyncio.wait([answers, stopped], return_when=asyncio.FIRST_COMPLETED)
            if stopped in completed:
                return
            await answers
            await asyncio.sleep(self.after_answer_delay)
            if not self.stopped.is_set():
                yield HarnessEvent(type="result", session_id="answered-thread")
        finally:
            answers.cancel()
            stopped.cancel()
            await asyncio.gather(answers, stopped, return_exceptions=True)


async def start_waiting_turn(manager, session, run, monkeypatch):
    final = asyncio.get_running_loop().create_future()

    async def collect(event):
        if event["type"] == "result" and event["session_id"] == session.id and not final.done():
            final.set_result(event)

    manager.on_broadcast("final-observer", collect)
    monkeypatch.setattr(manager, "_make_run", lambda *args, **kwargs: run)
    await manager.start_message(session.id, "Please ask before choosing")
    await asyncio.wait_for(run.created.wait(), timeout=1)
    return final


async def test_waiting_questions_suspend_idle_and_last_answer_resets_it(question_host, monkeypatch):
    manager, session, _, _ = question_host
    monkeypatch.setattr(settings, "ask_user_question_timeout_seconds", 0)
    monkeypatch.setattr(settings, "turn_idle_timeout_seconds", 0.15)
    monkeypatch.setattr(settings, "turn_max_seconds", 0)
    run = WaitingRun(manager, session, count=2, after_answer_delay=0.1)
    final = await start_waiting_turn(manager, session, run, monkeypatch)
    await asyncio.sleep(0.2)
    assert not run.stopped.is_set()
    assert await manager.answer_question(session.id, run.question_ids[0], [{"selected": ["A"]}])
    await asyncio.sleep(0.2)
    assert not run.stopped.is_set()
    assert await manager.answer_question(session.id, run.question_ids[1], [{"selected": ["B"]}])
    result = await asyncio.wait_for(final, timeout=1)
    assert result["content"]["status"] == "success"


async def test_attempt_finalization_retires_its_answered_question(question_host, monkeypatch):
    manager, session, client, events = question_host
    run = WaitingRun(manager, session)
    final = await start_waiting_turn(manager, session, run, monkeypatch)
    qid = run.question_ids[0]
    assert await manager.answer_question(session.id, qid, [{"selected": ["A"]}])
    outcome = (await asyncio.wait_for(final, timeout=1))["content"]
    assert outcome["status"] == "success"
    response = await client.get(
        f"/api/sessions/{session.id}/questions/{qid}/answer", headers=client.headers
    )
    assert response.status_code == 404
    assert manager.get_pending_questions(session.id) == ()
    assert not [event for event in events if event["type"] == "question_closed"]


@pytest.mark.parametrize("operation", ["interrupt", "reset", "archive", "delete"])
async def test_ending_request_closes_questions_and_wakes_poll(question_host, monkeypatch, operation):
    manager, session, client, events = question_host
    monkeypatch.setattr(settings, "ask_user_question_timeout_seconds", 60)
    run = WaitingRun(manager, session)
    await start_waiting_turn(manager, session, run, monkeypatch)
    qid = run.question_ids[0]
    polling = asyncio.create_task(client.get(
        f"/api/sessions/{session.id}/questions/{qid}/answer?timeout=60", headers=client.headers
    ))
    await asyncio.sleep(0)
    if operation == "interrupt":
        assert await manager.interrupt(session.id)
    elif operation == "reset":
        await manager.reset_session(session.id)
    elif operation == "archive":
        await manager.archive_session(session.id, replace=False)
    else:
        assert await manager.delete_session(session.id)
    response = await asyncio.wait_for(polling, timeout=1)
    assert response.status_code == 404
    assert not manager.has_pending_question(session.id, qid)
    assert len([event for event in events if event["type"] == "question_closed"]) == 1
    assert not [event for event in events if event["type"] == "question_answer"]


async def test_authoritative_deadline_wins_over_delayed_timer_and_late_human_answer(
    question_host, monkeypatch
):
    manager, session, client, events = question_host
    now = [10.0]
    clock = SimpleNamespace(monotonic=lambda: now[0])
    monkeypatch.setattr(question_module, "time", clock)
    monkeypatch.setattr(owner_questions, "time", clock)
    monkeypatch.setattr(settings, "ask_user_question_timeout_seconds", 60)
    qid = await manager.create_pending_question(session.id, QUESTIONS)
    now[0] = 71.0
    assert not manager.has_pending_question(session.id, qid)
    snapshot = await client.get(f"/api/sessions/{session.id}", headers=client.headers)
    assert snapshot.json()["pending_questions"] == []
    assert not await manager.answer_question(session.id, qid, [{"selected": ["A"]}])
    answer = await manager.wait_for_question_answer(session.id, qid, timeout=0.1)
    assert answer == manager.AUTO_ANSWER_TEXT
    assert len([event for event in events if event.get("auto")]) == 1


async def test_overall_deadline_still_stops_a_turn_waiting_for_human(question_host, monkeypatch):
    manager, session, client, _ = question_host
    monkeypatch.setattr(settings, "ask_user_question_timeout_seconds", 0)
    monkeypatch.setattr(settings, "turn_idle_timeout_seconds", 0.05)
    monkeypatch.setattr(settings, "turn_max_seconds", 0.15)
    run = WaitingRun(manager, session)
    final = await start_waiting_turn(manager, session, run, monkeypatch)
    outcome = (await asyncio.wait_for(final, timeout=1))["content"]
    assert outcome["status"] == "failed"
    assert "maximum duration" in outcome["message"]
    response = await client.get(
        f"/api/sessions/{session.id}/questions/{run.question_ids[0]}/answer", headers=client.headers
    )
    assert response.status_code == 404


class SilentRun(FakeRunBase):
    def __init__(self):
        self.created = asyncio.Event()
        self.stopped = asyncio.Event()
        self.stderr_text = ""

    async def start(self, *args, **kwargs):
        pass

    async def stop(self):
        self.stopped.set()

    async def stream(self):
        yield HarnessEvent(type="text", content="Working.")
        self.created.set()
        await self.stopped.wait()


async def test_old_question_cannot_exempt_new_attempt_from_idle_timeout(question_host, monkeypatch):
    manager, session, client, events = question_host
    monkeypatch.setattr(settings, "ask_user_question_timeout_seconds", 0)
    monkeypatch.setattr(settings, "turn_idle_timeout_seconds", 0.1)
    monkeypatch.setattr(settings, "turn_max_seconds", 0)
    old = await manager.create_pending_question(session.id, QUESTIONS)
    run = SilentRun()
    final = await start_waiting_turn(manager, session, run, monkeypatch)
    outcome = (await asyncio.wait_for(final, timeout=1))["content"]
    assert outcome["status"] == "failed"
    assert "without validated activity" in outcome["message"].lower()
    assert not manager.has_pending_question(session.id, old)
    assert len([event for event in events if event["type"] == "question_closed"]) == 1
    response = await client.get(f"/api/sessions/{session.id}/questions/{old}/answer", headers=client.headers)
    assert response.status_code == 404


async def test_cancelled_answer_request_still_publishes_its_claimed_resolution(question_host, monkeypatch):
    manager, session, client, _ = question_host
    qid = await manager.create_pending_question(session.id, QUESTIONS)
    broadcasting = asyncio.Event()
    release = asyncio.Event()
    received = asyncio.Event()
    inserting, allow_insert = asyncio.Event(), asyncio.Event()
    execute = manager.db.conn.execute

    async def hold_actual_answer_insert(sql, parameters=()):
        if sql.lstrip().startswith("INSERT INTO messages") and "question_answer" in parameters:
            inserting.set()
            await allow_insert.wait()
        return await execute(sql, parameters)

    async def slow_subscriber(event):
        if event["type"] == "question_answer":
            broadcasting.set()
            await release.wait()

    async def final_subscriber(event):
        if event["type"] == "question_answer":
            received.set()

    manager.on_broadcast("slow-subscriber", slow_subscriber)
    manager.on_broadcast("final-subscriber", final_subscriber)
    monkeypatch.setattr(manager.db.conn, "execute", hold_actual_answer_insert)
    answering = asyncio.create_task(manager.answer_question(session.id, qid, [{"selected": ["A"]}]))
    try:
        await asyncio.wait_for(inserting.wait(), 1)
        answering.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(answering, .2)
        allow_insert.set()
        await asyncio.wait_for(broadcasting.wait(), timeout=1)
        snapshot = await client.get(f"/api/sessions/{session.id}", headers=client.headers)
        assert snapshot.json()["pending_questions"] == []
        assert manager.get_pending_questions(session.id) == ()
        late = await client.get(
            f"/api/sessions/{session.id}/questions/{qid}/answer", headers=client.headers
        )
        assert late.status_code == 200
        assert late.json() == {"answer": "Q: Choose\nA: A"}
        release.set()
        await asyncio.wait_for(received.wait(), timeout=0.2)
        assert not manager.has_pending_question(session.id, qid)
    finally:
        allow_insert.set()
        release.set()
        await asyncio.gather(answering, return_exceptions=True)


async def test_creation_reply_and_answer_do_not_wait_for_the_creation_subscriber(question_host):
    manager, session, client, events = question_host
    broadcasting = asyncio.Event()
    release, answered = asyncio.Event(), asyncio.Event()

    async def slow_subscriber(event):
        if event["type"] == "question_request":
            broadcasting.set()
            await release.wait()
        if event["type"] == "question_answer":
            answered.set()

    manager.on_broadcast("slow-create-subscriber", slow_subscriber)
    creating = asyncio.create_task(manager.create_pending_question(session.id, QUESTIONS))
    try:
        await asyncio.wait_for(broadcasting.wait(), timeout=1)
        qid = await asyncio.wait_for(creating, .2)
        assert manager.has_pending_question(session.id, qid)
        response = await client.post(
            f"/api/sessions/{session.id}/questions/{qid}/answer", headers=client.headers,
            json={"answers": [{"selected": ["B"]}]},
        )
        assert response.status_code == 200
        poll = await client.get(f"/api/sessions/{session.id}/questions/{qid}/answer", headers=client.headers)
        assert poll.json() == {"answer": "Q: Choose\nA: B"}
        assert not answered.is_set(), "The resolution overtook its held creation prefix"
        assert await manager.interrupt(session.id) is False
        assert not [event for event in events if event["type"] == "question_closed"]
        release.set()
        await asyncio.wait_for(answered.wait(), 1)
    finally:
        release.set()
        await asyncio.gather(creating, return_exceptions=True)


async def test_host_auto_answer_resolves_form_and_poll_once(question_host, monkeypatch):
    manager, session, client, events = question_host
    monkeypatch.setattr(settings, "ask_user_question_timeout_seconds", 0.03)
    created = await client.post(
        f"/api/sessions/{session.id}/questions", headers=client.headers, json={"questions": QUESTIONS}
    )
    qid = created.json()["question_id"]
    answer = await client.get(f"/api/sessions/{session.id}/questions/{qid}/answer", headers=client.headers)
    assert answer.status_code == 200
    assert answer.json()["answer"] == manager.AUTO_ANSWER_TEXT
    snapshot = await client.get(f"/api/sessions/{session.id}", headers=client.headers)
    assert snapshot.json()["pending_questions"] == []
    # The answer wakes its poll promptly; publication may complete afterwards.
    await asyncio.sleep(0.01)
    assert len([event for event in events if event.get("auto")]) == 1


class EarlyFinishRun(SilentRun):
    async def stream(self):
        yield HarnessEvent(
            type="question_request", tool_use_id="native-question",
            tool_input={"questions": QUESTIONS},
        )
        self.created.set()
        yield HarnessEvent(type="result", session_id="finished-thread")


async def test_natural_attempt_end_closes_its_unanswered_native_question(question_host, monkeypatch):
    manager, session, client, events = question_host
    run = EarlyFinishRun()
    final = await start_waiting_turn(manager, session, run, monkeypatch)
    assert (await asyncio.wait_for(final, timeout=1))["content"]["status"] == "success"
    request = next(event for event in events if event["type"] == "question_request")
    assert request["attempt_id"]
    assert request["request_id"]
    snapshot = await client.get(f"/api/sessions/{session.id}", headers=client.headers)
    assert snapshot.json()["pending_questions"] == []
    history = next(msg for msg in snapshot.json()["messages"] if msg["type"] == "question_request")
    assert history["tool_input"]["attempt_id"] == request["attempt_id"]
    poll = await client.get(f"/api/sessions/{session.id}/questions/native-question/answer", headers=client.headers)
    assert poll.status_code == 404
    assert len([event for event in events if event["type"] == "question_closed"]) == 1


async def test_delete_question_closure_keeps_the_removed_sessions_account_scope(
    question_host, tmp_path, monkeypatch
):
    manager, _, _, _ = question_host
    monkeypatch.setattr(settings, "users_root", str(tmp_path / "users"))
    owner = await manager.db.create_user(
        user_id="question-owner", username="question-owner", password_hash="unused",
        created_at=datetime.now(UTC).isoformat(),
    )
    agent = await AgentManager(manager.db).create_agent(name="Owned questions", user_id=owner["id"])
    session = await manager.create_session(
        agent["id"], "Owned question", str(paths_for(owner["id"]).ensure().workspace)
    )
    owned, foreign = [], []

    async def owned_subscriber(event):
        owned.append(event)

    async def foreign_subscriber(event):
        foreign.append(event)

    manager.on_broadcast("owned-browser", owned_subscriber, user_id=owner["id"])
    manager.on_broadcast("foreign-browser", foreign_subscriber, user_id="someone-else")
    await manager.create_pending_question(session.id, QUESTIONS)
    assert await manager.delete_session(session.id)
    assert len([event for event in owned if event["type"] == "question_closed"]) == 1
    assert foreign == []
