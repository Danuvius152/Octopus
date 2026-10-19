"""Classification must see evidence beyond the display-summary limit."""

from types import SimpleNamespace

import pytest

from server.harness import get_harness
from server.sessions.turns import TurnsMixin


@pytest.mark.parametrize("tail,classification", [
    ("Your authentication token has expired", "is_auth_error"),
    ("503 service unavailable", "is_transient_error"),
])
def test_failure_classification_reads_tail(tail, classification):
    attempt = SimpleNamespace(error_text="prefix " * 1300, backend=SimpleNamespace(stderr_text=tail))
    blob = TurnsMixin._failure_blob(attempt)
    assert getattr(get_harness("codex"), classification)(blob)
