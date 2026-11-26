"""Verify requested Codex live targets after pytest's native deselection."""

import json
from pathlib import Path

import pytest


def pytest_addoption(parser):
    group = parser.getgroup("Codex live acceptance")
    group.addoption("--codex-selection-output", type=Path)
    group.addoption("--codex-expect-target", action="append", default=[])


@pytest.hookimpl(wrapper=True)
def pytest_collection(session):
    result = yield
    output = session.config.getoption("codex_selection_output")
    if output is None or session.testsfailed:
        return result
    selected = [item.nodeid for item in session.items]
    unmatched = [
        target for target in session.config.getoption("codex_expect_target")
        if not any(node == target or node.startswith((target + "::", target + "[")) for node in selected)
    ]
    output.write_text(json.dumps({"selected": selected, "unmatched_targets": unmatched}, indent=2) + "\n")
    if unmatched:
        raise pytest.UsageError(f"Codex live targets matched no eligible cases: {', '.join(unmatched)}")
    return result
