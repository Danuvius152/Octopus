"""A partial answer and failed settlement are never successful Wiki evidence."""

from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.test_wiki_query_real import QueryProbe


class ReplayManager:
    def __init__(self, scenario):
        self.scenario = scenario

    async def create_session(self, *args, **kwargs):
        return SimpleNamespace(id="synthetic-session")

    async def _execute_claimed_request(self, session_id, question, *, wiki_mode, request_id):
        assert wiki_mode == "read"
        yield {"type": "assistant_text", "content": "职业未记载。"}
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
            yield {"type": "error", "content": "error before settlement"}
        elif self.scenario == "recovery":
            yield {"type": "recovery", "content": {"provider_code": "server_overloaded"}}
        yield final
        if self.scenario == "duplicate":
            yield deepcopy(final)


@pytest.mark.parametrize("scenario", ["failed", "interrupted", "failed-status-only",
                                    "error-flag-only", "missing", "duplicate", "nonterminal",
                                    "other-session", "other-request", "other-content-request", "error-event"])
async def test_failed_or_unsettled_turn_cannot_pass_on_partial_answer(scenario):
    probe = QueryProbe(ReplayManager(scenario), "synthetic-agent", Path("/tmp/synthetic-vault"))
    with pytest.raises(AssertionError):
        await probe.ask("我的职业是什么")
    assert probe.last_events and probe.last_session_id == "synthetic-session"


@pytest.mark.parametrize("scenario", ["success", "recovery"])
async def test_successful_settlement_retains_auditable_recovery_and_request(scenario):
    probe = QueryProbe(ReplayManager(scenario), "synthetic-agent", Path("/tmp/synthetic-vault"))
    assert await probe.ask("我的职业是什么") == "职业未记载。"
    assert probe.last_events[-1]["content"]["request_id"] == probe.last_request_id
