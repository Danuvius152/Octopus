"""Phase 1 harness-layer tests: the merged run engine, shared assembly, the
registry, and run_oneshot — all driven by a fake RuntimeProfile + the shared
fake CLI, so they don't need a real claude/codex binary.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

import pytest

from server.harness import (
    EventParser,
    Harness,
    HarnessEvent,
    HarnessOneshotError,
    OneShotContext,
    ParseOutput,
    RunConfig,
    RuntimeProfile,
    TurnContext,
    assembly,
    available_backends,
    get_harness,
    register,
)
from server.harness.claude_code import ClaudeDriver
from server.harness.codex import CODEX_WEB_LEAF
from server.harness.driver import ArgvDriver, RunDriver
from server.harness.registry import _REGISTRY

FAKE_CLI = Path(__file__).parent / "_fixtures" / "fake_cli.py"


# --------------------------------------------------------------------------- #
# Fake profile
# --------------------------------------------------------------------------- #


class _RawParser(EventParser):
    """Emits one event per stdout object (type from `type`, raw=obj); ends the
    stream when a `result` object arrives — mirrors the real terminal-event
    contract."""

    def parse(self, obj: dict[str, Any]) -> ParseOutput:
        ev = HarnessEvent(type=obj.get("type", "?"), raw=obj)
        return ParseOutput(events=[ev], end_of_stream=obj.get("type") == "result")


def _stream_profile(
    *lines: str, new_driver: type[RunDriver] = ClaudeDriver
) -> RuntimeProfile:
    def build_turn_argv(ctx: TurnContext) -> tuple[list[str], dict[str, Any]]:
        return ([sys.executable, str(FAKE_CLI), "emit-lines", *lines], {"cwd": ctx.working_dir})

    return RuntimeProfile(
        backend="fake",
        binary=sys.executable,
        tools_prompt="TOOLS",
        credential_style="env_secret",
        premature_exit_recovery=False,
        new_driver=new_driver,
        can_steer=True,
        reusable=True,
        build_turn_argv=build_turn_argv,
        new_event_parser=_RawParser,
        build_oneshot_argv=lambda ctx: ([sys.executable], {}),
        parse_oneshot_stdout=lambda s: s,
    )


def _mode_profile(mode: str, *args: str) -> RuntimeProfile:
    """A streaming profile that runs the fake CLI in an arbitrary mode."""
    def build_turn_argv(ctx: TurnContext) -> tuple[list[str], dict[str, Any]]:
        return ([sys.executable, str(FAKE_CLI), mode, *args], {"cwd": ctx.working_dir})

    return RuntimeProfile(**{**_stream_profile().__dict__, "build_turn_argv": build_turn_argv})


async def _drain(run) -> list[HarnessEvent]:
    return [ev async for ev in run.stream()]


# --------------------------------------------------------------------------- #
# Engine: streaming + lifecycle
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_engine_streams_events_and_ends(tmp_path):
    profile = _stream_profile('{"type":"hello"}', '{"type":"result"}')
    run = Harness(profile).create_run(RunConfig())
    assert run.input_may_have_run is False
    await run.start("p", str(tmp_path))
    assert run.input_may_have_run is True
    events = await asyncio.wait_for(_drain(run), timeout=3.0)
    await run.stop()
    assert [e.type for e in events] == ["hello", "result"]


@pytest.mark.asyncio
async def test_engine_skips_malformed_lines(tmp_path):
    def build_turn_argv(ctx):
        return ([sys.executable, str(FAKE_CLI), "bad-json"], {"cwd": ctx.working_dir})

    profile = _stream_profile()
    profile = RuntimeProfile(**{**profile.__dict__, "build_turn_argv": build_turn_argv})
    run = Harness(profile).create_run()
    await run.start("p", str(tmp_path))
    events = await asyncio.wait_for(_drain(run), timeout=3.0)
    await run.stop()
    assert [e.type for e in events] == ["good"]
    assert run.protocol_issues == {"invalid_json": 1}


async def test_unknown_extension_and_diagnostics_preserve_integrity(tmp_path):
    run = Harness(_stream_profile('WARNING: native diagnostic', '{"type":"future.extension"}', '{"type":"result"}')).create_run()
    await run.start("p", str(tmp_path))
    await asyncio.wait_for(_drain(run), 3)
    await run.stop()
    assert run.protocol_issues == {}


async def test_parser_exception_has_bounded_noncontent_evidence(tmp_path):
    run = Harness(_stream_profile('{"type":"bad"}', '{"type":"result"}')).create_run()
    original = run._driver.parser.parse
    def parse(obj):
        if obj.get("type") == "bad":
            raise ValueError("fixture")
        return original(obj)
    run._driver.parser.parse = parse
    await run.start("p", str(tmp_path))
    await asyncio.wait_for(_drain(run), 3)
    await run.stop()
    assert run.protocol_issues == {"parser_exception": 1}
    assert set(run.protocol_locations[0]) == {"kind", "line", "characters", "sha256"}


async def test_oversized_protocol_line_marks_interrupted_recording(tmp_path, monkeypatch):
    from server.harness import run as run_module
    monkeypatch.setattr(run_module, "_STDOUT_LINE_LIMIT_BYTES", 1024)
    profile = _stream_profile()
    profile = RuntimeProfile(**{**profile.__dict__, "build_turn_argv": lambda ctx: (
        [sys.executable, "-c", "print('x'*2048)"], {"cwd": ctx.working_dir})})
    run = Harness(profile).create_run()
    await run.start("p", str(tmp_path))
    await asyncio.wait_for(_drain(run), 3)
    await run.stop()
    assert run.protocol_issues == {"stdout_interrupted": 1}


@pytest.mark.asyncio
async def test_engine_close_stdin_flag(tmp_path):
    # CLOSE_AFTER_SPAWN must not break a normal run (codex's behaviour).
    profile = _stream_profile(
        '{"type":"result"}', new_driver=ArgvDriver
    )
    run = Harness(profile).create_run()
    assert run.input_may_have_run is False
    await run.start("p", str(tmp_path))
    assert run.input_may_have_run is True
    events = await asyncio.wait_for(_drain(run), timeout=3.0)
    await run.stop()
    assert [e.type for e in events] == ["result"]


@pytest.mark.parametrize("new_driver", [ArgvDriver, ClaudeDriver])
async def test_resume_preparation_refusal_proves_no_input_submission(tmp_path, new_driver):
    from dataclasses import replace

    from server.harness.events import HarnessPreparationError

    async def refuse(*args):
        raise RuntimeError("unavailable before spawn")

    profile = replace(_stream_profile(new_driver=new_driver), prepare_resume=refuse)
    run = Harness(profile).create_run()
    try:
        with pytest.raises(HarnessPreparationError):
            await run.start("original", str(tmp_path), resume_id="thread")
        assert run.input_may_have_run is False
        assert run.process_group_id is None
    finally:
        await run.stop()


@pytest.mark.asyncio
async def test_engine_starting_twice_raises(tmp_path):
    run = Harness(_stream_profile('{"type":"result"}')).create_run()
    await run.start("p", str(tmp_path))
    with pytest.raises(RuntimeError, match="already started"):
        await run.start("p", str(tmp_path))
    await run.stop()


@pytest.mark.asyncio
async def test_engine_missing_binary_raises(tmp_path):
    def build_turn_argv(ctx):
        return (["definitely-not-a-real-binary-12345"], {"cwd": ctx.working_dir})

    profile = RuntimeProfile(**{**_stream_profile().__dict__, "build_turn_argv": build_turn_argv})
    run = Harness(profile).create_run()
    with pytest.raises(FileNotFoundError, match="not found on PATH"):
        await run.start("p", str(tmp_path))


@pytest.mark.asyncio
async def test_engine_stop_idempotent(tmp_path):
    run = Harness(_stream_profile('{"type":"result"}')).create_run()
    await run.start("p", str(tmp_path))
    await asyncio.wait_for(_drain(run), timeout=3.0)
    await run.stop()
    await run.stop()  # second stop is a no-op, not an error


@pytest.mark.asyncio
async def test_engine_captures_stderr(tmp_path):
    run = Harness(_mode_profile("fail-exit")).create_run()
    await run.start("p", str(tmp_path))
    await asyncio.wait_for(_drain(run), timeout=3.0)
    await run.stop()
    assert "boom" in run.stderr_text  # fake CLI writes "boom" to stderr


@pytest.mark.asyncio
async def test_engine_stop_kills_hung_subprocess(tmp_path):
    # sleep-then ignores stdin close; stop() must escalate to SIGKILL and
    # still return within its bounded budget.
    run = Harness(_mode_profile("sleep-then", "30")).create_run()
    await run.start("p", str(tmp_path))
    await asyncio.wait_for(run.stop(), timeout=6.0)


@pytest.mark.asyncio
async def test_engine_resolves_binary_from_fallback_dir(tmp_path, monkeypatch):
    """A bare binary not on PATH but in ~/.local/bin still resolves (the
    systemd case where the service PATH strips per-user dirs)."""
    fake_bin_dir = tmp_path / ".local" / "bin"
    fake_bin_dir.mkdir(parents=True)
    fake_binary = fake_bin_dir / "my-cli-xyzzy"
    # A stream-input CLI must stay alive long enough to receive the first frame.
    fake_binary.write_text("#!/bin/sh\nIFS= read -r frame\nexit 0\n")
    fake_binary.chmod(0o755)
    monkeypatch.setenv("PATH", "")
    monkeypatch.setenv("HOME", str(tmp_path))

    def build_turn_argv(ctx):
        return (["my-cli-xyzzy"], {"cwd": ctx.working_dir})

    profile = RuntimeProfile(**{**_stream_profile().__dict__, "build_turn_argv": build_turn_argv})
    run = Harness(profile).create_run()
    await run.start("p", str(tmp_path))  # resolves + spawns the trivial script
    await run.stop()


# --------------------------------------------------------------------------- #
# Shared assembly
# --------------------------------------------------------------------------- #


def test_callback_env_has_session_id_when_present():
    env = assembly.build_callback_env("sess-123")
    assert env["OCTOPUS_SESSION_ID"] == "sess-123"
    assert env["OCTOPUS_API_BASE"].startswith("http://127.0.0.1:")
    assert "OCTOPUS_AUTH_TOKEN" in env
    assert "OCTOPUS_SESSION_ID" not in assembly.build_callback_env(None)


def test_select_mcp_servers_all_by_default():
    env = assembly.build_callback_env("s")
    entries = assembly.select_mcp_servers(None, [], env)
    assert [e.key for e in entries] == ["bg", "ask", "ask_agent", "research", "schedule"]
    bg = next(e for e in entries if e.key == "bg")
    assert bg.env["OCTOPUS_SESSION_ID"] == "s"
    ask_agent_entry = next(e for e in entries if e.key == "ask_agent")
    assert ask_agent_entry.env["OCTOPUS_SESSION_ID"] == "s"
    assert ask_agent_entry.args[-1] == "server.mcp_servers.ask_agent"


def test_select_mcp_servers_subset():
    env = assembly.build_callback_env("s")
    entries = assembly.select_mcp_servers(["ask"], [], env)
    assert [e.key for e in entries] == ["ask"]


def test_select_mcp_servers_silently_drops_unknown_legacy_names():
    # Existing agents may still carry "viewer" in their stored mcp_servers list
    # from before it became a client-only flow. Assembly should treat unknown
    # names as no-ops rather than failing, so old rows keep working.
    env = assembly.build_callback_env("s")
    entries = assembly.select_mcp_servers(["viewer", "bg"], [], env)
    assert [e.key for e in entries] == ["bg"]


def test_select_mcp_servers_merges_connectors():
    class _FakeConnector:
        def mcp_key(self, inst):
            return f"github_{inst}"

        def mcp_entry(self, inst, callback_env):
            return {"command": "py", "args": ["-m", "x"], "env": {**callback_env, "OCTOPUS_INSTALLATION_ID": inst}}

    env = assembly.build_callback_env("s")
    entries = assembly.select_mcp_servers(["bg"], [(_FakeConnector(), "abc123")], env)
    assert [e.key for e in entries] == ["bg", "github_abc123"]
    assert entries[1].env["OCTOPUS_INSTALLATION_ID"] == "abc123"


def test_compose_system_prompt_orders_persona_then_tools():
    assert assembly.compose_system_prompt(None, "TOOLS", []) == "TOOLS"
    assert assembly.compose_system_prompt("PERSONA", "TOOLS", []) == "PERSONA\n\nTOOLS"


# --------------------------------------------------------------------------- #
# Registry + derived predicates
# --------------------------------------------------------------------------- #


def test_registry_register_get_and_unknown():
    # A profile under a unique backend name so we don't collide with real ones.
    profile = RuntimeProfile(**{**_stream_profile().__dict__, "backend": "fake-test-backend"})
    harness = Harness(profile)
    register(harness)
    try:
        assert get_harness("fake-test-backend") is harness
        # None resolves to the default kind (which may be unregistered in
        # Phase 1) — unknown kinds raise explicitly.
        with pytest.raises(ValueError, match="Unknown backend"):
            get_harness("no-such-backend")
        # is_available()/available_backends reflect a resolvable binary
        # (sys.executable always resolves).
        assert harness.is_available() is True
        assert "fake-test-backend" in available_backends()
    finally:
        _REGISTRY.pop("fake-test-backend", None)


def test_derived_predicates_no_codec():
    h = Harness(_stream_profile())
    assert h.can_export is False
    assert h.can_import is False
    assert h.login is None
    assert h.premature_exit_recovery is False


# --------------------------------------------------------------------------- #
# run_oneshot
# --------------------------------------------------------------------------- #


def _oneshot_profile(result_line: str, *, mode: str = "emit-lines") -> RuntimeProfile:
    import json

    def build_oneshot_argv(ctx: OneShotContext) -> tuple[list[str], dict[str, Any]]:
        return ([sys.executable, str(FAKE_CLI), mode, result_line], {})

    def parse_oneshot_stdout(s: str) -> str:
        return json.loads(s.strip().splitlines()[-1]).get("result", "")

    return RuntimeProfile(
        **{
            **_stream_profile().__dict__,
            "build_oneshot_argv": build_oneshot_argv,
            "parse_oneshot_stdout": parse_oneshot_stdout,
        }
    )


@pytest.mark.asyncio
async def test_run_oneshot_returns_text():
    harness = Harness(_oneshot_profile('{"result":"hello world"}'))
    out = await harness.run_oneshot(OneShotContext(prompt="x"))
    assert out == "hello world"


@pytest.mark.asyncio
async def test_run_oneshot_empty_raises():
    harness = Harness(_oneshot_profile('{"result":""}'))
    with pytest.raises(HarnessOneshotError) as ei:
        await harness.run_oneshot(OneShotContext(prompt="x"))
    assert ei.value.code == "empty"


@pytest.mark.asyncio
async def test_run_oneshot_not_found_raises():
    def build_oneshot_argv(ctx):
        return (["definitely-not-a-real-binary-98765"], {})

    profile = RuntimeProfile(**{**_stream_profile().__dict__, "build_oneshot_argv": build_oneshot_argv})
    with pytest.raises(HarnessOneshotError) as ei:
        await Harness(profile).run_oneshot(OneShotContext(prompt="x"))
    assert ei.value.code == "not_found"


@pytest.mark.asyncio
async def test_run_oneshot_nonzero_exit_raises():
    harness = Harness(_oneshot_profile('{"result":"x"}', mode="fail-exit"))
    with pytest.raises(HarnessOneshotError) as ei:
        await harness.run_oneshot(OneShotContext(prompt="x"))
    assert ei.value.code == "failed"


# --------------------------------------------------------------- auth-error detection


def test_is_auth_error_claude_matches_401_and_phrases():
    """Claude/Anthropic 401 phrasings trip the real claude-code harness
    (harness-credential-reauth.md §3); benign output and empty text don't."""
    h = get_harness("claude-code")
    assert h.is_auth_error(
        "Failed to authenticate. API Error: 401 Invalid authentication credentials"
    )
    assert h.is_auth_error("oauth token has expired, please run /login")
    assert not h.is_auth_error("Tool returned HTTP 200; all good")
    assert not h.is_auth_error("")


def test_is_auth_error_codex_web_leaf_matches_and_is_case_insensitive():
    h = Harness(CODEX_WEB_LEAF)
    assert h.is_auth_error("stream error: 401 Unauthorized")
    assert h.is_auth_error("Your authentication token has expired")
    assert not h.is_auth_error("turn completed successfully")
    assert not h.is_auth_error("")


def test_is_stale_session_error_claude():
    """The CLI's wording when `--resume <id>` names a conversation it no longer
    has. Distinct from auth (the credential is fine) and from transient
    (retrying the same id fails forever)."""
    h = get_harness("claude-code")
    assert h.is_stale_session_error(
        "No conversation found with session ID: 7d06c77e-7541-43fe-a7cc-5ed5c98eca52"
    )
    assert not h.is_stale_session_error("API Error: 529 Overloaded")
    assert not h.is_stale_session_error("")
    # Codex declares no pattern, so it never claims this failure mode.
    assert not get_harness("codex").is_stale_session_error(
        "No conversation found with session ID: x"
    )


def test_is_auth_error_codex_web_leaf_matches_a_lapsed_chatgpt_login():
    """The verbatim `turn.failed` message a dead ChatGPT login produces. It
    used to match nothing, so the credential was never flagged and the user got
    a generic error instead of a Re-authorize prompt — it only *looked* handled
    because the CLI's stderr happens to carry a websocket "401 Unauthorized"
    alongside it (harness-credential-reauth.md §3)."""
    h = Harness(CODEX_WEB_LEAF)
    assert h.is_auth_error(
        "Your access token could not be refreshed. Please log out and sign in again."
    )
    # Each half stands on its own — the CLI words this differently by channel.
    assert h.is_auth_error("access token could not be refreshed")
    assert h.is_auth_error("Please log out and sign in again")


def test_is_auth_error_codex_ignores_bare_unauthorized_from_tools():
    """A non-auth failure that merely contains "unauthorized" (an MCP/connector
    401, a tool error) must NOT be read as a harness-credential failure — the
    patterns are auth-specific, never a bare "unauthorized" (Vera review)."""
    h = Harness(CODEX_WEB_LEAF)
    assert not h.is_auth_error("MCP server returned Unauthorized")
    assert not h.is_auth_error("tool failed: GitHub Unauthorized")
    assert not h.is_auth_error("your session token has expired, refetch it")


def test_is_transient_error_matches_server_reliability_failures():
    """5xx / overloaded / dropped-connection trip the retry classifier on both
    backends (harness-transient-retry.md §3)."""
    c, x = get_harness("claude-code"), Harness(CODEX_WEB_LEAF)
    assert c.is_transient_error("API Error: 529 Overloaded")
    assert c.is_transient_error("API Error: 503 Service Unavailable")
    assert c.is_transient_error("connection reset by peer")
    assert x.is_transient_error("stream error: 503 service unavailable")
    assert x.is_transient_error("error 500 internal server error")
    assert not c.is_transient_error("")
    assert not x.is_transient_error("turn completed")


def test_is_transient_error_excludes_quota_and_auth():
    """Quota/credit and auth failures must NOT be retried — they match no
    transient pattern (harness-transient-retry.md §2), keeping the three
    dispositions mutually exclusive."""
    c, x = get_harness("claude-code"), Harness(CODEX_WEB_LEAF)
    for blob in (
        "rate limit exceeded",
        "429 too many requests",
        "you have insufficient quota",
        "billing: credit balance is too low",
        "you have reached your usage limit",   # the USER's limit — not retryable
        "API Error: 401 Invalid authentication credentials",
        "invalid x-api-key",
    ):
        assert not c.is_transient_error(blob), blob
        assert not x.is_transient_error(blob), blob


def test_is_transient_error_retries_server_side_throttle():
    """The server-side throttle Anthropic marks "(not your usage limit)" IS a
    transient blip and must retry — even though it says "Rate limited". Keying
    on the specific phrasing distinguishes it from the user's own usage limit."""
    c = get_harness("claude-code")
    msg = (
        "API Error: Server is temporarily limiting requests "
        "(not your usage limit) · Rate limited"
    )
    assert c.is_transient_error(msg)


# --------------------------------------------------------------- process-group reaping
# turn-safety.md §2: turns spawn in their own process group so nested children
# (MCP servers / subagents) are reaped as a unit, not orphaned.


def test_prepare_spawn_sets_session_leader():
    from server.harness.run import prepare_spawn

    _, kwargs = prepare_spawn(["sh", "-c", "true"], {})
    assert kwargs.get("start_new_session") is True


@pytest.mark.asyncio
async def test_terminate_process_group_reaps_children(tmp_path):
    """Killing the group must take down a CHILD the spawned process started —
    the orphan leak the old direct-child kill() left behind."""
    import os
    import signal as _signal

    from server.harness.run import _terminate_process_group, prepare_spawn

    def _alive(pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        return True

    # Parent starts a backgrounded `sleep`, prints its pid, then waits — so the
    # child shares the parent's new process group.
    argv, kwargs = prepare_spawn(
        ["sh", "-c", "sleep 30 & echo $!; wait"], {}
    )
    proc = await asyncio.create_subprocess_exec(
        *argv, stdout=asyncio.subprocess.PIPE, **kwargs
    )
    child_pid = int((await proc.stdout.readline()).strip())
    assert _alive(child_pid)

    sent_group = _terminate_process_group(proc, _signal.SIGKILL)
    await proc.wait()
    for _ in range(50):  # let the kernel reap the child
        if not _alive(child_pid):
            break
        await asyncio.sleep(0.02)
    assert sent_group is True
    assert not _alive(child_pid), "child process was orphaned, not reaped"


@pytest.mark.asyncio
async def test_run_oneshot_reaps_group_on_cancel(monkeypatch):
    """Cancelling a run_oneshot mid-flight must reap its process group, not
    orphan the CLI (Vera review). We spy on the group-kill helper."""
    import signal as _signal

    import server.harness.run as run_mod

    calls: list[int] = []
    real = run_mod._terminate_process_group

    def spy(proc, sig):
        calls.append(sig)
        return real(proc, sig)

    monkeypatch.setattr(run_mod, "_terminate_process_group", spy)
    spawned = asyncio.Event()
    spawn = asyncio.create_subprocess_exec

    async def observed_spawn(*args, **kwargs):
        process = await spawn(*args, **kwargs)
        spawned.set()
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", observed_spawn)

    def build_oneshot_argv(ctx):
        return ([sys.executable, "-c", "import time; time.sleep(30)"], {})

    profile = RuntimeProfile(
        **{**_stream_profile().__dict__, "build_oneshot_argv": build_oneshot_argv}
    )
    task = asyncio.create_task(
        Harness(profile).run_oneshot(OneShotContext(prompt="x"), timeout=30)
    )
    await asyncio.wait_for(spawned.wait(), 3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert _signal.SIGKILL in calls


@pytest.mark.asyncio
async def test_cancelled_oneshot_reports_unconfirmed_process_retirement(tmp_path, monkeypatch, short_process_shutdown):
    import os
    import signal

    import server.harness.run as run_mod

    pid_file = tmp_path / "oneshot.pid"

    def alive(pid):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        return True

    def argv(context):
        script = f"import os,time;open({str(pid_file)!r},'w').write(str(os.getpid()));time.sleep(60)"
        return [sys.executable, "-u", "-c", script], {}

    profile = RuntimeProfile(**{**_stream_profile().__dict__, "build_oneshot_argv": argv})
    monkeypatch.setattr(run_mod, "_terminate_process_group", lambda *args: None)
    task = asyncio.create_task(Harness(profile).run_oneshot(OneShotContext(prompt="synthetic")))
    pid = None
    try:
        async with asyncio.timeout(3):
            while not pid_file.exists() or not pid_file.read_text():
                await asyncio.sleep(.01)
        pid = int(pid_file.read_text())
        task.cancel("shutdown")
        with pytest.raises(RuntimeError, match="retirement.*unconfirmed"):
            await asyncio.wait_for(asyncio.shield(task), 4)
        assert alive(pid), "the fault gate must leave the captured process unretired"
    finally:
        if pid is not None and alive(pid):
            os.killpg(pid, signal.SIGKILL)
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("cleanup_wait", ["confirmed", "cancelled", "timeout"])
async def test_cancelled_oneshot_retires_process_delivered_after_cancellation(
    tmp_path, monkeypatch, cleanup_wait, short_process_shutdown
):
    import os
    import signal

    original_spawn = asyncio.create_subprocess_exec
    spawned = asyncio.get_running_loop().create_future()
    release_handle = asyncio.Event()

    async def delayed_handle(*argv, **kwargs):
        process = await original_spawn(*argv, **kwargs)
        spawned.set_result(process)
        await release_handle.wait()
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", delayed_handle)

    def oneshot_argv(context):
        return [sys.executable, str(FAKE_CLI), "sleep-then", "60"], {"cwd": str(tmp_path)}

    profile = RuntimeProfile(**{
        **_stream_profile().__dict__, "build_oneshot_argv": oneshot_argv,
    })
    invocation = asyncio.create_task(Harness(profile).run_oneshot(OneShotContext(prompt="late handle")))
    process = await asyncio.wait_for(spawned, timeout=3)
    try:
        invocation.cancel("shutdown")
        await asyncio.sleep(0)
        assert not invocation.done(), "cancelled one-shot surrendered its pending process handle"

        if cleanup_wait == "confirmed":
            release_handle.set()
            with pytest.raises(asyncio.CancelledError) as error:
                async with asyncio.timeout(3):
                    await invocation
            assert error.value.args == ("shutdown",)
        else:
            if cleanup_wait == "cancelled":
                invocation.cancel("second shutdown")
            with pytest.raises(RuntimeError, match="retirement.*unconfirmed"):
                async with asyncio.timeout(3):
                    await invocation
            assert process.returncode is None
            # The failed public call retains its local retirement owner: a
            # late handle still gets reaped without calling the harness again.
            release_handle.set()
            async with asyncio.timeout(3):
                await process.wait()
        assert process.returncode is not None
        with pytest.raises(ProcessLookupError):
            os.kill(process.pid, 0)
    finally:
        release_handle.set()
        if process.returncode is None:
            os.killpg(process.pid, signal.SIGKILL)
        await process.wait()
        await asyncio.gather(invocation, return_exceptions=True)


# --------------------------------------------------------------------------- #
# stdin as an input channel (inline-steering.md §6)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_stream_json_writes_the_prompt_to_stdin(tmp_path):
    """Under STREAM_JSON the prompt is a frame on stdin, not an argv tail."""
    profile = _mode_profile("echo-stdin")
    run = Harness(profile).create_run()
    await run.start("hello from stdin", str(tmp_path))
    events = await asyncio.wait_for(_drain(run), timeout=5.0)
    await run.stop()

    seen = [e.raw for e in events if e.raw and e.raw.get("type") == "frame_seen"]
    assert [f["content"] for f in seen] == ["hello from stdin"]
    # And the argv carries no prompt.
    argv, _ = run.build_argv("hello from stdin", str(tmp_path))
    assert "hello from stdin" not in argv


@pytest.mark.asyncio
async def test_every_input_id_is_distinct(tmp_path):
    """The real CLI reports our frame uuid back as `command_uuid` and
    deduplicates on it: two frames sharing a uuid means the second command is
    silently dropped and the turn hangs waiting for a reply that never comes.

    This cost a real debugging session — a uuid derived from
    (session_id, turn_index) repeated on every turn of a session, so the second
    turn of any resumed session hung for the full timeout.
    """
    profile = _mode_profile("echo-stdin")
    run = Harness(profile).create_run()
    await run.start("first", str(tmp_path))
    second = await run.send_input("second")
    third = await run.send_input("third")

    events = await asyncio.wait_for(_drain(run), 3)
    first = next(event.raw["uuid"] for event in events if event.raw.get("type") == "frame_seen")
    assert first is not None
    assert len({first, second, third}) == 3
    await run.stop()


@pytest.mark.asyncio
async def test_send_input_refused_on_argv_backends(tmp_path):
    """A backend whose prompt lives in argv has no input channel; asking it to
    take one must fail loudly rather than write into a closed pipe."""
    profile = _stream_profile(
        '{"type":"result"}', new_driver=ArgvDriver
    )
    run = Harness(profile).create_run()
    await run.start("p", str(tmp_path))
    with pytest.raises(RuntimeError, match="does not take input on stdin"):
        await run.send_input("late")
    await run.stop()


@pytest.mark.asyncio
async def test_open_stdin_does_not_grant_steering_support(tmp_path):
    from dataclasses import replace

    profile = replace(_mode_profile("echo-stdin"), can_steer=False, reusable=True)
    run = Harness(profile).create_run()
    try:
        await run.start("original", str(tmp_path))
        with pytest.raises(RuntimeError, match="does not support steering"):
            await run.steer("must not be written")
        events = await asyncio.wait_for(_drain(run), 3)
        assert [event.raw["content"] for event in events if event.raw.get("type") == "frame_seen"] == ["original"]
    finally:
        await run.stop()


# --------------------------------------------------------------------------- #
# Reuse identity (inline-steering.md §7)
# --------------------------------------------------------------------------- #


def _sig_profile() -> RuntimeProfile:
    from dataclasses import replace

    from server.harness.profile import ToolPolicySupport

    return replace(_stream_profile('{"type":"result"}'), tool_policy=ToolPolicySupport(allow=True, deny=True))


def test_spawn_signature_is_stable_across_turns():
    """The signature decides whether a live process may serve the next turn, so
    it must depend on CONFIG, never on object identity.

    `_load_connectors` builds fresh connector objects every turn. Keying on the
    object meant its default repr — which carries a memory address — landed in
    the signature, so every turn looked like a config change and reuse silently
    never happened. That fails quietly, as a permanent ~1s-per-turn tax.
    """
    from copy import copy

    from server.connectors.base import ConnectorInstallation
    from server.connectors.registry import get_connector

    connector = get_connector("gmail")
    installation = ConnectorInstallation(id="install-1", kind="gmail", label="Mail")

    profile = _sig_profile()
    cfg = dict(system_prompt="P", model="m")
    a = Harness(profile).create_run(RunConfig(connectors=[(copy(connector), copy(installation))], **cfg))
    b = Harness(profile).create_run(RunConfig(connectors=[(copy(connector), copy(installation))], **cfg))
    assert a.spawn_signature("/tmp", None) == b.spawn_signature("/tmp", None)


def test_held_wiki_process_signature_is_independent_of_short_lived_task():
    profile = _sig_profile()
    a = Harness(profile).create_run(RunConfig(session_id="wiki-held", wiki_task_id="first", wiki_mode="read"))
    b = Harness(profile).create_run(RunConfig(session_id="wiki-held", wiki_task_id="second", wiki_mode="read"))
    assert a.spawn_signature("/tmp", None) == b.spawn_signature("/tmp", None)


def test_held_wiki_transport_is_bound_only_to_its_active_authorized_task():
    from types import SimpleNamespace

    from server.mcp_identity import register_run, revoke_run, verify
    from server.wiki.grants import principal
    from server.wiki.store import WikiError
    from server.wiki.tasks import tasks
    session = SimpleNamespace(id="wiki-held", user_id=None, agent_id=None)
    first = tasks.open(session, "auto", request_id="r1")
    second = tasks.open(session, "auto", request_id="r2")
    run = Harness(_sig_profile()).create_run(RunConfig(session_id=session.id, wiki_task_id=first, wiki_mode="auto"))
    register_run(run.run_lease_id, session.id, None, None)
    try:
        run.bind_wiki_task(first)
        context = run._make_context("p", "/tmp", None, None)
        scope = verify(next(entry.credential for entry in context.mcp_servers if entry.key == "wiki"))
        old_task = tasks.require(scope)
        assert old_task.request_id == "r1"
        assert principal(old_task, scope, {}, "session:wiki-held") == "auto"
        tasks.close(first)
        with pytest.raises(WikiError):
            tasks.require(scope)
        run.bind_wiki_task(second)
        assert tasks.require(scope).request_id == "r2"
        assert tasks.resolve(scope) is not old_task
        # An entry call captured before rebinding cannot acquire the next
        # turn's authority at its final admission transaction.
        with pytest.raises(WikiError, match="no longer authorized"):
            principal(old_task, scope, {}, "session:wiki-held")
        assert principal(tasks.require(scope), scope, {}, "session:wiki-held") == "auto"
        revoke_run(run.run_lease_id)
        with pytest.raises(WikiError):
            tasks.require(scope)
    finally:
        revoke_run(run.run_lease_id)
        tasks.close(first)
        tasks.close(second)


def test_spawn_signature_changes_with_anything_baked_in_at_spawn():
    """Persona, model, tool policy, MCP set and working dir are all argv or env
    at spawn and cannot change afterwards — each must force a respawn."""
    profile = _sig_profile()
    base = dict(system_prompt="P", model="m", mcp_servers=["bg"],
                tool_allow=["Read"], tool_deny=["Write"], memory_dir="/m")
    ref = Harness(profile).create_run(RunConfig(**base)).spawn_signature("/tmp", None)

    for field, value in [
        ("system_prompt", "DIFFERENT"),
        ("model", "other-model"),
        ("mcp_servers", ["bg", "ask"]),
        ("tool_allow", ["Read", "Glob"]),
        ("tool_deny", []),
        ("memory_dir", "/other"),
    ]:
        changed = {**base, field: value}
        got = Harness(profile).create_run(RunConfig(**changed)).spawn_signature("/tmp", None)
        assert got != ref, f"{field} must change the signature"

    # …and so must the working dir.
    same_cfg = Harness(profile).create_run(RunConfig(**base))
    assert same_cfg.spawn_signature("/elsewhere", None) != ref


def test_spawn_signature_follows_where_the_mcp_namespaces_are_served():
    """A held process bakes in the URL and bearer of every namespace.

    Since the namespaces moved in-process (polish-2026-09.md §4 B1), an entry's
    URL carries the server's port and its credential is derived from the session
    and the access token. The signature used to hold only the namespace *names*,
    so a process spawned against one port was reused against another: it
    reported every namespace as unreachable and the model lost its tools
    mid-conversation, with nothing in the logs but the CLI's own complaint.
    Found by the real-CLI tier, where each test serves its own ephemeral port.
    """
    from server.config import settings

    profile = _sig_profile()
    cfg = RunConfig(system_prompt="P", model="m", mcp_servers=["bg"], session_id="s1")
    run = Harness(profile).create_run(cfg)

    original_port = settings.port
    original_token = settings.auth_token
    try:
        settings.port = 45001
        ref = run.spawn_signature("/tmp", None)
        settings.port = 45002
        assert run.spawn_signature("/tmp", None) != ref, "the URL moved"
        settings.port = 45001
        assert run.spawn_signature("/tmp", None) == ref, "and back again"
        # The bearer is derived from the access token, so rotating it invalidates
        # every minted scope — a held process must not carry the old one.
        settings.auth_token = original_token + "-rotated"
        assert run.spawn_signature("/tmp", None) != ref, "the bearer changed"
    finally:
        settings.port = original_port
        settings.auth_token = original_token

    # A session-less run renders stdio entries instead — and those carry the
    # same base URL in their spawn environment, so it follows the port too.
    stdio = Harness(profile).create_run(
        RunConfig(system_prompt="P", model="m", mcp_servers=["bg"])
    )
    before = stdio.spawn_signature("/tmp", None)
    settings.port = 45999
    try:
        assert stdio.spawn_signature("/tmp", None) != before
    finally:
        settings.port = original_port


@pytest.mark.asyncio
async def test_retirement_owns_late_spawn_after_start_and_waiter_cancel(tmp_path, monkeypatch):
    """A cancelled start/stop caller cannot leave a writer alive for the next attempt."""
    original_spawn = asyncio.create_subprocess_exec
    entered = asyncio.Event()
    release = asyncio.Event()
    spawned = []

    async def delayed_spawn(*argv, **kwargs):
        entered.set()
        await release.wait()
        process = await original_spawn(*argv, **kwargs)
        spawned.append(process)
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", delayed_spawn)
    run = Harness(_mode_profile("sleep-then", "60")).create_run()
    ticks = []
    run.bind_activity(ticks.append)
    starting = asyncio.create_task(run.start("prompt", str(tmp_path)))
    await entered.wait()
    starting.cancel()
    await asyncio.gather(starting, return_exceptions=True)
    waiting = asyncio.create_task(run.stop())
    await asyncio.sleep(0)
    assert not waiting.done()
    waiting.cancel()
    await asyncio.gather(waiting, return_exceptions=True)
    release.set()
    await asyncio.wait_for(run.stop(), 4)
    assert len(spawned) == 1 and spawned[0].returncode is not None
    assert ticks == []


@pytest.mark.asyncio
async def test_native_reader_drain_does_not_keep_cli_alive(tmp_path, short_process_shutdown):
    from dataclasses import replace

    close_entered = asyncio.Event()
    allow_close = asyncio.Event()

    class WaitingObserver:
        def observe_stdout(self, obj):
            pass

        def consume_stderr(self, line):
            return False

        async def start(self):
            pass

        def invalidate(self):
            pass

        async def close(self):
            close_entered.set()
            await allow_close.wait()

    profile = replace(_mode_profile("sleep-then", "60"),
                      activity_observer=lambda *args, **kwargs: WaitingObserver())
    run = Harness(profile).create_run()
    run.bind_activity(lambda _: None)
    await run.start("prompt", str(tmp_path))
    stopping = asyncio.create_task(run.stop())
    try:
        await asyncio.wait_for(close_entered.wait(), 5)
        assert not run.is_alive()
        assert [event async for event in run.stream()] == []
        assert not stopping.done()
    finally:
        allow_close.set()
        await asyncio.wait_for(stopping, 2)
