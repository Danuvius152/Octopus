"""A partial answer and failed settlement are never successful Wiki evidence."""

from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.submission import accepted_receipt
from tests.test_wiki_query_real import QueryProbe


class ReplayManager:
    def __init__(self, scenario):
        self.scenario = scenario
        self.callbacks = {}

    async def create_session(self, *args, **kwargs):
        return SimpleNamespace(id="synthetic-session")

    def on_broadcast(self, key, callback):
        self.callbacks[key] = callback

    def remove_broadcast(self, key):
        del self.callbacks[key]

    async def start_message(self, session_id, question, *, wiki_mode, request_id):
        assert wiki_mode == "read"
        async for event in self.events(session_id, request_id):
            for callback in tuple(self.callbacks.values()):
                await callback(deepcopy(event))
        return accepted_receipt(request_id)

    async def events(self, session_id, request_id):
        yield {"type": "assistant_text", "session_id": session_id, "content": "Replay segment A."}
        yield {"type": "assistant_text", "session_id": session_id, "content": "Replay segment B."}
        final = {"type": "result", "session_id": session_id, "request_id": request_id,
                 "terminal": True, "is_error": False,
                 "content": {"status": "success", "request_id": request_id}}
        if self.scenario == "missing":
            return
        if self.scenario in {"failed", "interrupted"}:
            final["content"]["status"] = self.scenario
            final["is_error"] = True
        elif self.scenario == "failed-status-only":
            final["content"]["status"] = "failed"
        elif self.scenario == "error-flag-only":
            final["is_error"] = True
        elif self.scenario == "nonterminal":
            final["terminal"] = False
        elif self.scenario == "other-session":
            final["session_id"] = "another-session"
        elif self.scenario == "other-request":
            final["request_id"] = "another-request"
        elif self.scenario == "other-content-request":
            final["content"]["request_id"] = "another-request"
        elif self.scenario == "error-event":
            yield {"type": "error", "session_id": session_id, "content": "error before settlement"}
        elif self.scenario == "recovery":
            yield {"type": "recovery", "session_id": session_id, "content": {"provider_code": "server_overloaded"}}
        yield final
        if self.scenario == "duplicate":
            yield deepcopy(final)


@pytest.mark.parametrize("scenario", ["failed", "interrupted", "failed-status-only",
                                    "error-flag-only", "missing", "duplicate", "nonterminal",
                                    "other-session", "other-request", "other-content-request", "error-event"])
async def test_failed_or_unsettled_turn_cannot_pass_on_partial_answer(scenario):
    probe = QueryProbe(ReplayManager(scenario), "synthetic-agent", Path("/tmp/synthetic-vault"))
    with pytest.raises(AssertionError):
        await probe.ask("Return the replay segments.")
    assert probe.last_events and probe.last_session_id == "synthetic-session"


@pytest.mark.parametrize("scenario", ["success", "recovery"])
async def test_successful_settlement_retains_auditable_recovery_and_request(scenario):
    probe = QueryProbe(ReplayManager(scenario), "synthetic-agent", Path("/tmp/synthetic-vault"))
    assert await probe.ask("Return the replay segments.") == "Replay segment A.\nReplay segment B."
    assert probe.last_events[-1]["content"]["request_id"] == probe.last_request_id
    assert any(event["type"] == "recovery" for event in probe.last_events) is (scenario == "recovery")
