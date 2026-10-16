"""End-to-end CodexBackend tests against the real `codex` binary.

These need `codex` installed AND a logged-in ChatGPT subscription
(`~/.codex/auth.json`). Auto-skipped otherwise so CI without a login still
passes. They sit alongside test_backend_codex.py (fake CLI): the fake tests
are fast regression checks for the normalizer; these prove the wire format is
actually what codex emits (codex-backend.md §12, Phase C — keeps the fake
fixture honest against version drift).

Confirmed against codex 0.132.0 in this session:
  thread.started{thread_id} / turn.started / item.completed{agent_message}
  / item.{started,completed}{command_execution} / item.{started,completed}
  {mcp_tool_call: server,tool,arguments,result} / turn.completed{usage}.
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

pytestmark = [pytest.mark.real, pytest.mark.real_codex]


def _codex_run(**cfg):
    """A codex HarnessRun for the given RunConfig. The engine resolves the
    `codex` binary via the same nvm-aware fallback, so this spawns even when
    codex lives only under ~/.nvm/.../bin (not on the service PATH)."""
    return get_harness("codex").create_run(RunConfig(**cfg))


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
async def test_real_text_then_result(tmp_path):
    backend = _codex_run(session_id="rt", mcp_servers=[])
    await backend.start("Reply with exactly: PONG. Do not use any tools.", str(tmp_path))
    try:
        events = await _drain(backend)
    finally:
        await backend.stop()

    types = [e.type for e in events]
    assert types[0] == "session_started"
    assert "text" in types and "result" in types
    text = "".join(e.content or "" for e in events if e.type == "text")
    assert "PONG" in text
    result = next(e for e in events if e.type == "result")
    # thread_id is the resume handle; Codex reports tokens, not USD.
    assert result.session_id is not None
    assert result.cost is None
    assert result.is_error is False
    assert result.duration_ms is not None and result.duration_ms > 0


@pytest.mark.asyncio
async def test_real_command_execution(tmp_path):
    backend = _codex_run(session_id="rc", mcp_servers=[])
    await backend.start(
        "Use the shell to run exactly: echo PONG_FROM_CODEX. Then reply: done.",
        str(tmp_path),
    )
    try:
        events = await _drain(backend)
    finally:
        await backend.stop()

    tu = next((e for e in events if e.type == "tool_use"), None)
    assert tu is not None, f"no tool_use; saw {[e.type for e in events]}"
    assert tu.tool_name == "Bash"
    assert "command" in (tu.tool_input or {})
    tr = next(
        (e for e in events if e.type == "tool_result" and e.tool_use_id == tu.tool_use_id),
        None,
    )
    assert tr is not None and tr.is_error is False
    assert "PONG_FROM_CODEX" in (tr.content or "")


@pytest.mark.asyncio
async def test_real_resume_across_speed_changes(tmp_path):
    b1 = _codex_run(session_id="r1", mcp_servers=[], reasoning_effort="low", service_tier="default")
    await b1.start(
        "Remember this exact word for later: MARIGOLD. Reply only with OK.",
        str(tmp_path),
    )
    try:
        e1 = await _drain(b1)
    finally:
        await b1.stop()
    sid = successful_thread(e1)
    home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    evidence = tmp_path / "speed-evidence"
    initial = capture_rollout(home / "sessions", sid, tmp_path)
    record_phase(evidence, "initial", initial, e1)

    b2 = _codex_run(session_id="r2", mcp_servers=[], reasoning_effort="high", service_tier="fast")
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
        service_tier="priority", effort="high",
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


@pytest.mark.asyncio
async def test_real_native_activity_identity_and_metadata_format(tmp_path):
    """Installed exec diagnostics and metadata prove the exact current native turn."""
    backend = _codex_run(session_id="native_activity_acceptance", mcp_servers=[])
    ticks: list[float | None] = []
    backend.bind_activity(ticks.append)
    await backend.start(
        "Silently work out 1234567 times 7654321. Use the shell to run exactly: "
        "sleep 2; printf 'NATIVE_ACTIVITY_ACCEPTED\\n'. Then reply with the product. "
        "Do not use any other tools.", str(tmp_path),
    )
    try:
        events = await _drain(backend)
        observer = backend._activity_observer
        assert observer.identity_confirmed, "Installed CLI native identity was not verified"
        assert observer.matched_records > 0, "Installed CLI emitted no accepted native activity metadata"
        assert ticks
        assert any(event.type == "result" and not event.is_error for event in events)
        assert "Sent prompt with event ID:" not in backend.stderr_text
        assert "SessionConfiguredEvent" not in backend.stderr_text
    finally:
        await backend.stop()


@pytest.mark.asyncio
async def test_real_native_helpers_use_verified_call_identity_across_two_rounds(tmp_path):
    """Installed CLI lifecycle metadata reaches the public normalized stream."""
    backend = _codex_run(session_id="native_helpers_acceptance", mcp_servers=[], reasoning_effort="low")
    backend.bind_tool_context("native-helper-request", "native-helper-attempt")
    backend.bind_activity(lambda timestamp: None)
    await backend.start(
        "Create exactly one native helper named native_card_probe. Ask it to reply "
        "with exactly FIRST_NATIVE_ROUND, without tools. Collect that helper's result. "
        "Then send the same helper one followup_task asking it to reply with exactly "
        "SECOND_NATIVE_ROUND, without tools. Collect its second result before ending "
        "your turn. Reply NATIVE_CARD_ACCEPTED after both replies. Use only the "
        "native collaboration tools; do not create other helpers, run shell commands, "
        "read files, or use network tools.", str(tmp_path),
    )
    try:
        events = await _drain(backend, timeout=240)
        calls = [event for event in events if event.type == "tool_use" and event.tool_name in {
            "subagent:spawn_agent", "subagent:followup_task",
        }]
        assert [event.tool_name for event in calls] == ["subagent:spawn_agent", "subagent:followup_task"]
        assert all(event.runtime_metadata["attempt_id"] == "native-helper-attempt" for event in calls)
        finished = [event.subagent for event in events if event.type == "subagent" and event.subagent.status == "completed"]
        assert len(finished) == 2
        assert len({child.task_id for child in finished}) == 1
        assert [child.tool_call_id for child in finished] == [call.runtime_metadata["tool_call_id"] for call in calls]
        assert finished[0].tool_call_id != finished[1].tool_call_id
        assert all(not child.summary and not child.prompt for child in finished)
        assert "NATIVE_CARD_ACCEPTED" in "".join(event.content or "" for event in events if event.type == "text")
        assert events[-1].type == "result" and not events[-1].is_error
        assert "Sent prompt with event ID:" not in backend.stderr_text
    finally:
        await backend.stop()
