"""Real ordinary Codex resume applies each turn's effort and service tier.

Native fork/independent-history proof uses the original product saga in
test_codex_app_server_real.py, including externally created exec history.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest

from server.harness import HarnessEvent, RunConfig, get_harness
from tests.codex_speed_evidence import (
    capture_rollout,
    record_phase,
    successful_thread,
    verify_resume,
)
from tests.harness_profiles import native_submission

pytestmark = [pytest.mark.real, pytest.mark.real_codex]


def _codex_run(**cfg):
    """A codex HarnessRun for the given RunConfig. The engine resolves the
    `codex` binary via the same nvm-aware fallback, so this spawns even when
    codex lives only under ~/.nvm/.../bin (not on the service PATH)."""
    run = get_harness("codex").create_run(RunConfig(**cfg))
    native_submission(run, cfg["session_id"], "direct-harness-attempt")
    return run


async def _drain(backend, timeout: float = 150.0) -> list[HarnessEvent]:
    events: list[HarnessEvent] = []

    async def collect() -> None:
        async for ev in backend.stream():
            events.append(ev)

    try:
        await asyncio.wait_for(collect(), timeout=timeout)
    except TimeoutError:
        raise AssertionError(
            f"stream() didn't terminate within {timeout}s. "
            f"Collected: {[e.type for e in events]}"
        )
    return events


@pytest.mark.asyncio
async def test_real_resume_speed_changes(tmp_path):
    """Three verified rollout intervals prove settings without guessing TPS."""
    b1 = _codex_run(session_id="r1", mcp_servers=[], reasoning_effort="low", service_tier="default")
    await b1.start(
        "Remember this exact word for later: MARIGOLD. Reply only with OK. Do not use tools.",
        str(tmp_path),
    )
    try:
        e1 = await _drain(b1)
    finally:
        await b1.stop()
    types = [e.type for e in e1]
    assert types[0] == "session_started"
    assert "text" in types and "result" in types
    assert "tool_use" not in types
    text = "".join(e.content or "" for e in e1 if e.type == "text")
    assert "OK" in text
    result = next(e for e in e1 if e.type == "result")
    # thread_id is the resume handle; Codex reports tokens, not USD.
    assert result.session_id is not None
    assert result.cost is None
    assert result.is_error is False
    assert b1.retirement_state == "proven"
    sid = successful_thread(e1)
    home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    evidence = tmp_path / "speed-evidence"
    initial = capture_rollout(home / "sessions", sid, tmp_path)
    record_phase(evidence, "initial", initial, e1)

    b2 = _codex_run(session_id="r2", mcp_servers=[], reasoning_effort="low", service_tier="fast")
    await b2.start(
        "What exact word did I ask you to remember? Reply only with that word.",
        str(tmp_path),
        resume_id=sid,
    )
    try:
        e2 = await _drain(b2)
    finally:
        await b2.stop()
    fast = capture_rollout(home / "sessions", sid, tmp_path)
    record_phase(evidence, "fast", fast, e2)
    successful_thread(e2, expected_thread=sid, recalled_word="MARIGOLD")
    fast_interval = verify_resume(
        initial, fast, thread_id=sid, cwd=tmp_path,
        service_tier="priority", effort="low",
    )

    b3 = _codex_run(session_id="r3", mcp_servers=[], reasoning_effort="low", service_tier="default")
    await b3.start(
        "What exact word did I ask you to remember? Reply only with that word.",
        str(tmp_path), resume_id=sid,
    )
    try:
        e3 = await _drain(b3)
    finally:
        await b3.stop()
    normal = capture_rollout(home / "sessions", sid, tmp_path)
    record_phase(evidence, "normal", normal, e3)
    successful_thread(e3, expected_thread=sid, recalled_word="MARIGOLD")
    normal_interval = verify_resume(
        fast, normal, thread_id=sid, cwd=tmp_path,
        service_tier="default", effort="low",
    )
    # Each prefix was captured after that process stopped. Initial settings
    # stay in the first prefix; they cannot be mistaken for resume settings.
    (evidence / "verified-intervals.json").write_text(
        json.dumps([fast_interval, normal_interval], indent=2) + "\n"
    )
