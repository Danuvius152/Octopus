"""Per-agent native memory → harness wiring (docs/implemented/memory.md §3).

Asserts the *rendered* turn for each harness. Crucially, Claude points its
auto-memory at the per-agent dir via CLAUDE_COWORK_MEMORY_PATH_OVERRIDE and
NEVER sets CLAUDE_CONFIG_DIR (that holds session-resume transcripts — moving it
would orphan them). Codex gets the memory blurb in developer_instructions and
never enables features.memories. Pure argv/env inspection — no subprocess.
"""

from __future__ import annotations

import pytest

from server.harness import RunConfig, get_harness
from server.harness.assembly import compose_system_prompt, render_memory_blurb
from tests.harness_profiles import native_submission
from tests.test_codex_app_server_questions import wire_frames
from tests.test_codex_app_server_submission import native_runtime as native_runtime


def _arg_after(argv, flag):
    return argv[argv.index(flag) + 1]


# --- profile flags ---------------------------------------------------------


def test_profile_memory_flags():
    assert get_harness("codex").profile.injects_memory_prompt is True
    assert get_harness("claude-code").profile.injects_memory_prompt is False


# --- assembly --------------------------------------------------------------


def test_render_memory_blurb_names_dir_and_format():
    blurb = render_memory_blurb("/agents/a1/memory")
    assert "/agents/a1/memory" in blurb
    assert "MEMORY.md" in blurb
    assert "frontmatter" in blurb


def test_compose_system_prompt_memory_gating():
    on = compose_system_prompt(None, "TOOLS", [], memory_dir="/x/mem", inject_memory=True)
    assert "== Long-term memory ==" in on and "/x/mem" in on
    off = compose_system_prompt(None, "TOOLS", [], memory_dir="/x/mem", inject_memory=False)
    assert "== Long-term memory ==" not in off
    no_dir = compose_system_prompt(None, "TOOLS", [], memory_dir=None, inject_memory=True)
    assert "== Long-term memory ==" not in no_dir


# --- Claude: override the memory dir, NEVER the config dir ------------------


def test_claude_sets_memory_override_not_config_dir(monkeypatch):
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    run = get_harness("claude-code").create_run(
        RunConfig(session_id="s1", memory_dir="/ag/a/memory")
    )
    argv, kwargs = run.build_argv("hi", "/tmp", None)
    assert kwargs["env"]["CLAUDE_COWORK_MEMORY_PATH_OVERRIDE"] == "/ag/a/memory"
    # Regression guard: we must NOT relocate the config dir (resume transcripts).
    assert "CLAUDE_CONFIG_DIR" not in kwargs["env"]


def test_claude_no_memory_env_without_agent(monkeypatch):
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    # `build_argv` copies os.environ, so an inherited value would satisfy the
    # assertion below for the wrong reason — and Octopus sets exactly this
    # variable for the agents it runs, so a session running *inside* Octopus
    # inherits it and this test fails while passing in a plain shell.
    monkeypatch.delenv("CLAUDE_COWORK_MEMORY_PATH_OVERRIDE", raising=False)
    run = get_harness("claude-code").create_run(RunConfig(session_id="s1"))
    argv, kwargs = run.build_argv("hi", "/tmp", None)
    assert "CLAUDE_COWORK_MEMORY_PATH_OVERRIDE" not in kwargs["env"]
    assert "CLAUDE_CONFIG_DIR" not in kwargs["env"]


def test_claude_omits_memory_blurb():
    run = get_harness("claude-code").create_run(
        RunConfig(session_id="s1", memory_dir="/ag/a/memory")
    )
    argv, _ = run.build_argv("hi", "/tmp", None)
    assert "== Long-term memory ==" not in _arg_after(argv, "--append-system-prompt")


# --- Codex -----------------------------------------------------------------


@pytest.mark.parametrize("memory_dir", [None, "/ag/a/memory"])
async def test_codex_memory_blurb_is_installed_through_native_instructions(native_runtime, tmp_path, memory_dir):
    run = get_harness("codex").create_run(
        RunConfig(session_id="s1", memory_dir=memory_dir, mcp_servers=[])
    )
    proofs = native_submission(run, "memory-request", "memory-attempt")
    try:
        await run.start("hi", str(tmp_path))
        events = [event async for event in run.stream()]
        assert any(event.type == "result" and not event.is_error for event in events)
        start, = [frame for frame in wire_frames(tmp_path) if frame.get("method") == "thread/start"]
        instructions = start["params"]["developerInstructions"]
        assert ("== Long-term memory ==" in instructions) == (memory_dir is not None)
        if memory_dir:
            assert memory_dir in instructions
        assert "features.memories" not in start["params"]["config"]
        assert any(proof.state == "accepted" for proof in proofs)
    finally:
        await run.stop()
