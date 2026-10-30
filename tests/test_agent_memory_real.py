"""Real-CLI memory + resume safety (docs/implemented/memory.md, Verification).

Against the actual `claude` / `codex` binaries:
1. Each harness READS its per-agent memory through the real wiring (Claude via
   CLAUDE_COWORK_MEMORY_PATH_OVERRIDE, Codex via its blurb).
2. **Resume regression guard**: pointing Claude's memory at the per-agent dir
   must NOT relocate CLAUDE_CONFIG_DIR, so `--resume` still finds the session
   transcript in ~/.claude. (This is the bug that earlier killed sessions.)

Costs real API calls; auto-skipped when the binary isn't on PATH.
"""

from __future__ import annotations

import asyncio
import glob
import os

import pytest

from server import agent_memory
from server.config import settings
from server.harness import HarnessEvent, RunConfig, get_harness

pytestmark = pytest.mark.real

for _d in [
    os.path.expanduser("~/.local/bin"),
    "/usr/local/bin",
    *sorted(glob.glob(os.path.expanduser("~/.nvm/versions/node/*/bin"))),
]:
    if _d and _d not in os.environ.get("PATH", "").split(os.pathsep):
        os.environ["PATH"] = _d + os.pathsep + os.environ.get("PATH", "")

CODENAME = "BLUEOCTOPUS7723"


def _seed(agent_id: str) -> None:
    mem = agent_memory.agent_memory_dir(agent_id)
    mem.mkdir(parents=True, exist_ok=True)
    (mem / "MEMORY.md").write_text(
        "# Memory Index\n\n- [User Profile](user-profile.md) — secret project codename\n"
    )
    (mem / "user-profile.md").write_text(
        "---\nname: user-profile\ndescription: secret project codename\n"
        "metadata:\n  type: user\n---\n\n"
        f"The user's secret project codename is {CODENAME}.\n"
    )


async def _drain(run, timeout: float = 150.0) -> list[HarnessEvent]:
    events: list[HarnessEvent] = []

    async def collect() -> None:
        async for ev in run.stream():
            events.append(ev)

    try:
        await asyncio.wait_for(collect(), timeout=timeout)
    except TimeoutError:
        raise AssertionError(f"stream didn't end in {timeout}s: {[e.type for e in events]}")
    return events


def _text(events) -> str:
    return "\n".join(e.content for e in events if e.type == "text" and e.content)


async def _recall(backend: str, cfg: dict, wd: str) -> str:
    run = get_harness(backend).create_run(RunConfig(**cfg))
    await run.start(
        "Consult your long-term memory about me (read your MEMORY.md and any file "
        "it points to), then tell me my secret project codename. Answer with the "
        "codename, or UNKNOWN if your memory genuinely lacks it.",
        wd,
        None,
        None,
    )
    try:
        events = await _drain(run)
    finally:
        await run.stop()
    return _text(events)


@pytest.mark.real_claude
@pytest.mark.asyncio
async def test_claude_reads_per_agent_memory(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "agents_dir", str(tmp_path / "agents"))
    aid = "memtest-claude"
    agent_memory.ensure_agent_dirs(aid)
    _seed(aid)
    wd = str(tmp_path / "ws")
    os.makedirs(wd)
    cfg = dict(model="haiku", memory_dir=str(agent_memory.agent_memory_dir(aid)))
    out = await _recall("claude-code", cfg, wd)
    assert CODENAME in out.upper()


@pytest.mark.real_codex
@pytest.mark.asyncio
async def test_codex_memory_read_emits_native_activity_and_metadata(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "agents_dir", str(tmp_path / "agents"))
    aid = "memtest-codex"
    agent_memory.ensure_agent_dirs(aid)
    _seed(aid)
    wd = str(tmp_path / "ws")
    os.makedirs(wd)
    # This contract uses native file reads. Starting unrelated stdio MCP
    # servers adds processes and handshakes without exercising those tools.
    run = get_harness("codex").create_run(RunConfig(
        session_id="native_activity_acceptance",
        memory_dir=str(agent_memory.agent_memory_dir(aid)), mcp_servers=[],
    ))
    run.bind_tool_context("native-metrics-request", "native-metrics-attempt")
    ticks: list[float | None] = []
    run.bind_activity(ticks.append)
    await run.start(
        "Consult your long-term memory about me: read your MEMORY.md and the file "
        "it points to, then tell me my secret project codename. Also use the shell "
        "to run exactly: sleep 2; printf 'NATIVE_ACTIVITY_ACCEPTED\\n'. "
        "Do not change any files. Finish with the codename, or UNKNOWN if your "
        "memory lacks it.", wd,
    )
    try:
        events = await _drain(run)
        assert CODENAME in _text(events).upper()
        # Memory reads already exercise the real shell. Reuse this run for
        # native identity, tool normalization and per-response measurements.
        # Match the sentinel call explicitly: memory reads may precede it.
        tu = next((e for e in events if e.type == "tool_use" and e.tool_name == "Bash"
                   and "NATIVE_ACTIVITY_ACCEPTED" in (e.tool_input or {}).get("command", "")), None)
        assert tu is not None, f"no sentinel shell call; saw {[e.type for e in events]}"
        tr = next((e for e in events if e.type == "tool_result" and e.tool_use_id == tu.tool_use_id), None)
        assert tr is not None and tr.is_error is False
        assert "NATIVE_ACTIVITY_ACCEPTED" in (tr.content or "")
        assert run._activity_observer.identity_confirmed, "Installed CLI native identity was not verified"
        # Reasoning emission is optional; its acceptance has replay coverage.
        assert ticks
        responses = [e for e in events if e.type == "model_measurement"
                     and e.raw.get("kind") == "codex_response"]
        assert len(responses) >= 2, "Shell and final-answer responses need separate usage windows"
        assert all(e.duration_ms and e.duration_ms > 0 for e in responses)
        assert all(e.raw["model"] != "unknown" and e.raw["output_tokens"] > 0 for e in responses)
        assert all(e.raw["reasoning_tokens"] <= e.raw["output_tokens"] for e in responses)
        assert sum(e.duration_ms for e in responses) < next(e.duration_ms for e in events if e.type == "result")
        assert any(e.type == "result" and not e.is_error for e in events)
        assert "Sent prompt with event ID:" not in run.stderr_text
        assert "SessionConfiguredEvent" not in run.stderr_text
    finally:
        await run.stop()


@pytest.mark.real_claude
@pytest.mark.asyncio
async def test_claude_resume_survives_memory_override(tmp_path, monkeypatch):
    """With memory pointed at the per-agent dir, --resume must still find the
    session transcript (it lives under the untouched host CLAUDE_CONFIG_DIR).
    Turn 1 states a fact; a fresh process RESUMES and recalls it."""
    monkeypatch.setattr(settings, "agents_dir", str(tmp_path / "agents"))
    aid = "memtest-resume"
    agent_memory.ensure_agent_dirs(aid)
    cfg = dict(model="haiku", memory_dir=str(agent_memory.agent_memory_dir(aid)))
    wd = str(tmp_path / "ws")
    os.makedirs(wd)

    # The probe is a build label, NOT a "passphrase": asked to repeat a
    # secret after a resume the model refuses on principle, and the refusal
    # reads as a lost transcript when it is the opposite — it understood the
    # question perfectly. The subject here is `--resume`, so the fact being
    # recalled must be one nothing objects to repeating.
    run1 = get_harness("claude-code").create_run(RunConfig(**cfg))
    await run1.start(
        "Remember for this conversation: the build label is ZEPHYR-7. "
        "Reply with just OK.",
        wd, None, None,
    )
    try:
        ev1 = await _drain(run1)
    finally:
        await run1.stop()
    sid = next((e.session_id for e in ev1 if e.session_id), None)
    assert sid, "no claude session id captured from turn 1"

    run2 = get_harness("claude-code").create_run(RunConfig(**cfg))
    await run2.start(
        "What build label did I give you a moment ago? Reply with only the "
        "label.",
        wd, sid, None,
    )
    try:
        ev2 = await _drain(run2)
    finally:
        await run2.stop()
    assert "ZEPHYR-7" in _text(ev2).upper(), "resume lost the conversation transcript"
