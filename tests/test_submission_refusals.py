"""Expected refusal codes are independent of exception prose."""

import pytest

from server.sessions.client_submissions import submission_status, submit_client_message
from server.sessions.runtime_types import QuotaExceeded
from server.submission_models import InputRecoveryUnavailable, SubmissionConflict
from tests.test_submission_recovery import runtime as runtime


async def test_quota_refusal_has_its_own_code_and_does_not_reserve_the_input(runtime, monkeypatch):
    manager, db, session, _, starts, _ = runtime

    async def quota(*args, **kwargs):
        raise QuotaExceeded("Localized quota explanation")

    with monkeypatch.context() as refusal:
        refusal.setattr(manager, "start_message", quota)
        reply = await submit_client_message(manager, session.id, "Retry after quota", [], "quota", wiki_mode=None)
    assert reply["code"] == "quota_exceeded" and not reply["accepted"]
    assert await submission_status(manager, session.id, "quota") is None
    retried = await submit_client_message(manager, session.id, "Retry after quota", [], "quota", wiki_mode=None)
    assert retried["accepted"]
    await manager.await_session_execution(session.id, timeout=5)
    assert starts.qsize() == 1


@pytest.mark.parametrize("error,expected", [
    (SubmissionConflict("Another language and wording"), "submission_conflict"),
    (InputRecoveryUnavailable("Review the original input"), "recovery_unavailable"),
    (ValueError("This prose happens to contain different input"), "admission_refused"),
])
async def test_refusal_classification_does_not_parse_error_prose(runtime, monkeypatch, error, expected):
    manager, db, session, _, starts, _ = runtime

    async def refuse(*args, **kwargs):
        raise error

    monkeypatch.setattr(manager, "start_message", refuse)
    reply = await submit_client_message(manager, session.id, "Retain original", [], "refused", wiki_mode=None)
    assert not reply["accepted"] and reply["code"] == expected
    assert reply["message"] == str(error)
    assert await submission_status(manager, session.id, "refused") is None
    assert starts.empty()
