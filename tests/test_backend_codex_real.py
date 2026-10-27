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
import subprocess
import uuid
from pathlib import Path

import pytest

from server.harness import HarnessEvent, RunConfig, get_harness
from server.harness import codex as cx
from server.harness.events import HarnessCredential
from server.harness.run import _which_with_fallback
from tests.cli_gate import codex_home_with_auth
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
async def test_real_resume_speed_changes_and_native_copy(tmp_path):
    """The same real transcript proves speed changes and full-copy resume."""
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
    assert result.duration_ms is not None and result.duration_ms > 0
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
    # A full-copy fork needs an actual completed rollout, which these three
    # phases already supplied. Keep the raw CLI resume in a different cwd;
    # creating another seed conversation adds no independent coverage.
    codex_home = codex_home_with_auth()
    assert codex_home is not None and Path(codex_home).resolve() == home.resolve()
    credential = HarnessCredential(backend="codex", auth_type="oauth", home_dir=codex_home)
    destination = tmp_path / "copied-workspace"
    destination.mkdir()
    copied_id = str(uuid.uuid4())
    artifact = await cx._fork_copy(
        parent_working_dir=str(tmp_path), parent_resume_id=sid,
        parent_credential=credential, dest_working_dir=str(destination),
        new_resume_id=copied_id,
    )
    try:
        assert artifact.needs_replay is False and artifact.resume_id == copied_id
        result = subprocess.run(
            [_which_with_fallback("codex"), "exec", "--json", "--skip-git-repo-check",
             "-C", str(destination), "--dangerously-bypass-approvals-and-sandbox",
             "-c", 'model_reasoning_effort="low"', "resume", copied_id, "--",
             "What exact word did I ask you to remember? Reply with only that word."],
            capture_output=True, text=True, timeout=180,
            env={**os.environ, "CODEX_HOME": codex_home},
        )
        assert result.returncode == 0, result.stderr
        rows = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
        assert any(row.get("type") == "thread.started" and row.get("thread_id") == copied_id for row in rows)
        reply = "".join(row["item"].get("text", "") for row in rows
                        if row.get("type") == "item.completed" and row.get("item", {}).get("type") == "agent_message")
        assert "MARIGOLD" in reply.upper(), f"copied rollout lost context: {reply!r}"
        assert normal.path.read_bytes() == normal.raw, "copy resume changed its parent rollout"
    finally:
        copied = cx._find_rollout(cx._codex_sessions_dir(credential), copied_id)
        if copied is not None:
            copied.unlink(missing_ok=True)
