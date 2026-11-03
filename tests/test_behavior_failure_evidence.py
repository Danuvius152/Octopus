"""Classification sees complete attributed evidence, excluding process stderr."""

from types import SimpleNamespace

import pytest

from server.harness import Harness
from server.harness.codex import CODEX_WEB_LEAF
from server.sessions.turns import TurnsMixin


@pytest.mark.parametrize("tail,classification", [
    ("Your authentication token has expired", "is_auth_error"),
    ("503 service unavailable", "is_transient_error"),
])
def test_failure_classification_reads_tail(tail, classification):
    attempt = SimpleNamespace(error_text="prefix " * 1300 + tail, backend=SimpleNamespace(stderr_text=""))
    blob = TurnsMixin._failure_blob(attempt)
    assert getattr(Harness(CODEX_WEB_LEAF), classification)(blob)


@pytest.mark.parametrize("tail", ["Your authentication token has expired", "503 service unavailable"])
def test_unattributed_process_stderr_cannot_supply_attempt_failure_evidence(tail):
    attempt = SimpleNamespace(error_text="Unrelated failure", backend=SimpleNamespace(stderr_text=tail))
    assert TurnsMixin._failure_blob(attempt) == "Unrelated failure"
