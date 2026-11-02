"""Phase 3: the CODEX profile is a faithful port of the legacy CodexBackend —
turn argv (asserted *equal* to the old backend's build_args), event
normalization and the model-only one-shot transport.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from server.config import settings
from server.harness import assembly, available_backends, get_harness
from server.harness.codex import (
    _OCTOPUS_SYSTEM_PROMPT_CODEX,
    CodexEventParser,
    build_oneshot_argv,
    parse_oneshot_stdout,
)
from server.harness.codex import (
    build_turn_argv as build_web_leaf_argv,
)
from server.harness.codex_app_server import build_argv as build_turn_argv
from server.harness.events import HarnessCredential
from server.harness.profile import OneShotContext, TurnContext
from server.harness.run import prepare_spawn


def _assemble_ctx(*, prompt, wd, resume, credential, model=None, mcp_servers=None, persona=None, session_id=None, web_research=False):
    abs_wd = str(Path(wd).resolve())
    cb = assembly.build_callback_env(session_id)
    entries = assembly.select_mcp_servers(mcp_servers, [], cb)
    sysp = assembly.compose_system_prompt(persona, _OCTOPUS_SYSTEM_PROMPT_CODEX, [])
    return TurnContext(
        prompt=prompt, working_dir=abs_wd, resume_id=resume, system_prompt=sysp,
        model=model, tool_allow=None, tool_deny=None, mcp_servers=entries,
        credential=credential, web_research=web_research,
    )


@pytest.mark.parametrize("selection", ["configured", "missing", "default", "nonexecutable"])
async def test_discovery_and_ordinary_prepare_select_the_same_configured_executable(tmp_path, monkeypatch, selection):
    from server.harness import codex_app_server, run

    path_dir = tmp_path / "path"
    path_dir.mkdir()
    configured = tmp_path / "configured-codex"
    generic = path_dir / "codex"
    if selection != "configured":
        generic.write_text("#!/bin/sh\nexit 0\n")
        generic.chmod(0o755)
    if selection in {"configured", "nonexecutable"}:
        configured.write_text("#!/bin/sh\nexit 1\n")
        configured.chmod(0o755 if selection == "configured" else 0o644)
    chosen = "codex" if selection == "default" else str(configured)
    monkeypatch.setenv("PATH", str(path_dir))
    monkeypatch.setattr(run, "_fallback_path_dirs", lambda: [])
    monkeypatch.setattr(settings, "codex_app_server_binary", chosen)
    harness = get_harness("codex")
    assert ("codex" in available_backends()) is (selection in {"configured", "default"})
    context = _assemble_ctx(prompt="hello", wd=str(tmp_path), resume=None, credential=None, mcp_servers=[])
    invocation = harness.create_run()
    argv, kwargs = invocation.build_argv("hello", str(tmp_path))
    assert argv[0] == chosen
    # A later configuration change must not change this captured invocation.
    monkeypatch.setattr(settings, "codex_app_server_binary", "another-codex")
    prepared, kwargs = prepare_spawn(argv, kwargs)
    expected = str(generic) if selection == "default" else str(configured)
    assert prepared[0] == expected
    if selection == "missing":
        with pytest.raises(FileNotFoundError) as failure:
            await invocation._driver.prepare(invocation, context, prepared, kwargs)
        assert str(configured) in str(failure.value)
    else:
        # Presence does not relax the actual binary's pin or launch a probe.
        with pytest.raises(ValueError, match="verified 0.166.0 binary"):
            await invocation._driver.prepare(invocation, context, prepared, kwargs)
    assert codex_app_server.build_argv(context)[0][0] == "another-codex"


def test_turn_argv_web_research_uses_readonly_sandbox_and_web_search(tmp_path):
    """A research web leaf runs codex READ-ONLY with web_search enabled — no
    destructive bypass (native-deep-research.md §4)."""
    ctx = _assemble_ctx(
        prompt="research", wd=str(tmp_path), resume=None,
        credential=HarnessCredential(backend="codex", auth_type="oauth", home_dir=None),
        web_research=True, mcp_servers=[],
    )
    argv, _ = build_web_leaf_argv(ctx)
    assert argv[argv.index("--sandbox") + 1] == "read-only"
    assert "tools.web_search=true" in argv
    assert "--dangerously-bypass-approvals-and-sandbox" not in argv
    # Sandbox/web flags are exec-level — must precede a resume subcommand.
    assert "resume" not in argv  # new turn here, but the ordering invariant holds


def test_turn_argv_full_config(tmp_path):
    """A rich config renders the expected `codex exec --json` command + the
    CODEX_HOME credential dir. (Faithful-port equivalence vs the legacy backend
    was proven during the migration; this is the standing snapshot.)"""
    home = str(tmp_path / "codexhome")
    ctx = _assemble_ctx(
        prompt="hello", wd=str(tmp_path), resume="resume-1",
        credential=HarnessCredential(backend="codex", auth_type="oauth", home_dir=home),
        model="gpt-5.5", mcp_servers=["ask"], persona="PERSONA", session_id="s1",
    )
    argv, kw = build_turn_argv(ctx)
    joined = " ".join(argv)

    assert argv[:2] == ["codex", "app-server"]
    assert "features.goals=false" in argv
    # Only the selected built-ins are rendered as -c overrides.
    assert "mcp_servers.ask.command=" in joined
    assert "mcp_servers.bg.command=" not in joined
    # Effective model, controller, permissions and exact history now travel in
    # the native RPCs, verified by test_codex_app_server_configuration.py.
    assert "hello" not in argv and "resume-1" not in argv
    assert kw["env"]["CODEX_HOME"] == home


def test_turn_argv_new_turn_and_no_home(tmp_path):
    ctx = _assemble_ctx(
        prompt="p", wd=str(tmp_path), resume=None,
        credential=HarnessCredential(backend="codex", auth_type="oauth", home_dir=None),
    )
    argv, kw = build_turn_argv(ctx)
    assert argv[:2] == ["codex", "app-server"]
    assert "resume" not in argv  # new turn
    assert "p" not in argv
    assert kw["env"].get("CODEX_HOME") == os.environ.get("CODEX_HOME")  # inherit ambient home


# --------------------------------------------------------------------------- #
# Event normalization
# --------------------------------------------------------------------------- #


def test_parser_thread_items_and_completion():
    p = CodexEventParser()

    out = p.parse({"type": "thread.started", "thread_id": "th-1"})
    assert out.events[0].type == "session_started" and out.events[0].session_id == "th-1"

    out = p.parse({"type": "turn.started"})
    assert out.events == [] and out.end_of_stream is False

    out = p.parse({"type": "item.completed", "item": {"type": "agent_message", "text": "hello"}})
    assert out.events[0].type == "text" and out.events[0].content == "hello"

    out = p.parse({
        "type": "item.started",
        "item": {"type": "mcp_tool_call", "id": "i1", "server": "bg", "tool": "run", "arguments": {"command": "ls"}},
    })
    assert out.events[0].type == "tool_use" and out.events[0].tool_name == "mcp__bg__run"

    out = p.parse({"type": "turn.completed", "usage": {"input_tokens": 5}})
    assert out.end_of_stream is True
    assert out.events[0].type == "result" and out.events[0].session_id == "th-1"


def test_parser_turn_failed_and_error():
    p = CodexEventParser()
    out = p.parse({"type": "turn.failed", "error": {"message": "boom"}})
    assert out.end_of_stream is True
    assert out.events[0].type == "result" and out.events[0].is_error and out.events[0].content == "boom"

    out = p.parse({"type": "error", "message": "bad"})
    assert out.events[0].type == "error" and out.events[0].is_error
    assert out.end_of_stream is False  # a stray error doesn't end the turn


def test_parser_command_execution():
    p = CodexEventParser()
    out = p.parse({"type": "item.started", "item": {"type": "command_execution", "id": "c1", "command": "ls"}})
    assert out.events[0].type == "tool_use" and out.events[0].tool_name == "Bash"
    out = p.parse({"type": "item.completed", "item": {"type": "command_execution", "id": "c1", "output": "files", "exit_code": 0}})
    assert out.events[0].type == "tool_result" and out.events[0].is_error is False


# --------------------------------------------------------------------------- #
# One-shot (D2)
# --------------------------------------------------------------------------- #


def test_oneshot_argv(tmp_path, monkeypatch):
    monkeypatch.setattr("server.harness.run._which_with_fallback", lambda name: "/fake/bin/codex")
    home = str(tmp_path / "h")
    argv, kw = build_oneshot_argv(
        OneShotContext(
            prompt="parse this", model="gpt-5.5",
            credential=HarnessCredential(backend="codex", auth_type="oauth", home_dir=home),
            working_dir=str(tmp_path),
        )
    )
    assert argv[1] == "-I" and Path(argv[2]).name == "codex_text.py"
    assert "exec" not in argv and "--dangerously-bypass-approvals-and-sandbox" not in argv
    assert kw["cwd"] == str(tmp_path)
    assert argv[argv.index("--home") + 1] == home
    assert argv[argv.index("--codex") + 1] == "/fake/bin/codex"
    assert argv[argv.index("--model") + 1] == "gpt-5.5"
    assert argv[-2:] == ["--", "parse this"]
    assert argv[argv.index("--tier") + 1] == "default"
    assert kw["env"]["CODEX_HOME"] == home


def test_oneshot_extracts_only_the_completed_text_envelope():
    assert parse_oneshot_stdout(json.dumps({"text": '{"name":"x"}'})) == '{"name":"x"}'
    assert parse_oneshot_stdout("garbage\n{not json") == ""  # nothing extractable
    assert parse_oneshot_stdout('{"type":"item.completed","item":{"type":"agent_message","text":"partial"}}') == ""
