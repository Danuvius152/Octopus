"""Public ask tool behavior at the HTTP boundary."""

from __future__ import annotations

import httpx
import pytest

from server.mcp_servers import ask


@pytest.fixture
def question_host(monkeypatch):
    monkeypatch.setenv("OCTOPUS_API_BASE", "http://question-host")
    monkeypatch.setenv("OCTOPUS_SESSION_ID", "s1")
    monkeypatch.setenv("OCTOPUS_AUTH_TOKEN", "test-token")
    monkeypatch.setattr(
        httpx, "post",
        lambda *a, **k: httpx.Response(201, json={"question_id": "q1", "deadline": None}),
    )
    monkeypatch.setattr(httpx, "delete", lambda *a, **k: httpx.Response(200))


def test_server_wait_policy_allows_answer_after_thirty_five_minutes(
    question_host, monkeypatch,
):
    now = [0.0]
    monkeypatch.setattr(ask.time, "monotonic", lambda: now[0])
    responses = iter([
        httpx.Response(408), httpx.Response(408),
        httpx.Response(200, json={"answer": "Use the existing framework"}),
    ])

    def poll(*args, **kwargs):
        now[0] += 1300
        return next(responses)

    monkeypatch.setattr(httpx, "get", poll)
    answer = ask.ask_user(questions=[{"question": "How should we proceed?"}])
    assert answer == "Use the existing framework"


@pytest.mark.parametrize("failure", ["transport", "server"])
def test_repeated_communication_failure_reports_error_without_assuming_user_answer(
    question_host, monkeypatch, failure,
):
    calls = []

    def poll(*args, **kwargs):
        calls.append(1)
        if len(calls) <= 3:
            if failure == "transport":
                raise httpx.ConnectError("connection refused")
            return httpx.Response(503)
        return httpx.Response(404)

    monkeypatch.setattr(httpx, "get", poll)
    monkeypatch.setattr(ask.time, "sleep", lambda seconds: None)
    answer = ask.ask_user(questions=[{"question": "Approve the action?"}])
    assert len(calls) == 3
    assert "communication" in answer.lower()
    assert "autonomously" not in answer.lower()


def test_abandoned_wait_closes_host_question_once(question_host, monkeypatch):
    closed = []

    def poll(*args, **kwargs):
        raise httpx.ConnectError("connection refused")

    def close(url, **kwargs):
        closed.append((url, kwargs))
        return httpx.Response(200)

    monkeypatch.setattr(httpx, "get", poll)
    monkeypatch.setattr(httpx, "delete", close)
    monkeypatch.setattr(ask.time, "sleep", lambda seconds: None)
    answer = ask.ask_user(questions=[{"question": "Approve the action?"}])
    assert "Error: communication" in answer
    assert len(closed) == 1
    assert closed[0][0] == "http://question-host/api/sessions/s1/questions/q1"
    assert closed[0][1]["params"] == {"reason": "communication_failure"}
