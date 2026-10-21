"""Local runtime diagnostics retain details while excluding credentials."""

from __future__ import annotations

import asyncio
import errno
import json
import logging
import os
import sys
from dataclasses import replace
from typing import Any

import pytest

from server.config import settings
from server.harness import (
    EventParser,
    Harness,
    HarnessCredential,
    HarnessEvent,
    HarnessOneshotError,
    OneShotContext,
    ParseOutput,
    RunConfig,
    RuntimeProfile,
    StdinMode,
    TurnContext,
    get_harness,
)

PRIVATE_PROMPT = "PRIVATE-PROMPT-46-原始输入\n第二行"
PRIVATE_PERSONA = "PRIVATE-PERSONA-46-私有指令"
PRIVATE_TOKEN = "PRIVATE-TOKEN-46-credential"
PRIVATE_TAIL = "PRIVATE-TAIL-46-原生后续"


class _TranscriptParser(EventParser):
    def parse(self, obj: dict[str, Any]) -> ParseOutput:
        return ParseOutput(
            events=[HarnessEvent(type=obj["type"], raw=obj)],
            end_of_stream=obj["type"] == "result",
        )


def _profile(script: str) -> RuntimeProfile:
    def build_turn_argv(ctx: TurnContext) -> tuple[list[str], dict[str, Any]]:
        return (
            [sys.executable, "-c", script, ctx.prompt, ctx.system_prompt],
            {
                "cwd": ctx.working_dir,
                "env": {**os.environ, "PRIVATE_CREDENTIAL": ctx.credential.secret if ctx.credential else ""},
            },
        )

    return RuntimeProfile(
        backend="synthetic",
        binary=sys.executable,
        tools_prompt="",
        credential_style="env_secret",
        premature_exit_recovery=False,
        stdin_mode=StdinMode.CLOSE_AFTER_SPAWN,
        build_turn_argv=build_turn_argv,
        new_event_parser=_TranscriptParser,
        build_oneshot_argv=lambda ctx: ([sys.executable, "-c", script, ctx.prompt], {}),
        parse_oneshot_stdout=lambda text: text,
    )


async def test_success_diagnostics_preserve_input_stderr_and_native_tail(
    tmp_path, caplog, monkeypatch
):
    caplog.set_level(logging.DEBUG)
    installation_token = "installation-credential-46-success"
    monkeypatch.setattr(settings, "auth_token", installation_token)
    stderr = f"native stderr detail {PRIVATE_TOKEN} {installation_token}\n{'x' * 2500}{PRIVATE_TAIL}"
    script = (
        "import json,os,sys; "
        "print(json.dumps({'type':'receipt','prompt':sys.argv[1],'persona':sys.argv[2],"
        "'credential':os.environ['PRIVATE_CREDENTIAL']}),flush=True); "
        f"print({stderr!r},file=sys.stderr,flush=True); "
        "print('{\"type\":\"result\"}',flush=True); "
        f"print(json.dumps({{'type':'native.tail','content':{PRIVATE_TAIL!r}}}),flush=True)"
    )
    run = Harness(_profile(script)).create_run(
        RunConfig(system_prompt=PRIVATE_PERSONA, mcp_servers=[])
    )
    run.bind_tool_context("request-diagnostics-46", "attempt-diagnostics-46")
    tail = []
    tail_seen = asyncio.Event()

    async def receive_native_tail(event: HarnessEvent) -> None:
        tail.append(event)
        tail_seen.set()

    run.set_idle_handler(receive_native_tail)
    try:
        await run.start(
            PRIVATE_PROMPT,
            str(tmp_path),
            credential=HarnessCredential("synthetic", "api_key", PRIVATE_TOKEN),
        )
        events = await asyncio.wait_for(_collect(run), 3)
        await asyncio.wait_for(tail_seen.wait(), 3)
    finally:
        await run.stop()

    assert [event.type for event in events] == ["receipt", "result"]
    assert events[0].raw["prompt"] == PRIVATE_PROMPT
    assert PRIVATE_PERSONA in events[0].raw["persona"]
    assert events[0].raw["credential"] == PRIVATE_TOKEN
    assert [event.raw for event in tail] == [{"type": "native.tail", "content": PRIVATE_TAIL}]
    assert run.stderr_text == stderr
    assert "PRIVATE-PROMPT-46" in caplog.text and PRIVATE_PERSONA in caplog.text
    assert PRIVATE_TOKEN not in caplog.text and installation_token not in caplog.text
    assert "argv=" in caplog.text and f"cwd={tmp_path}" in caplog.text
    stderr_messages = [record.message for record in caplog.records if "CLI stderr" in record.message]
    assert any("native stderr detail" in message for message in stderr_messages)
    assert any("x" * 2500 + PRIVATE_TAIL in message for message in stderr_messages)
    assert "synthetic" in caplog.text and "request-diagnostics-46" in caplog.text
    assert "exit_code=0" in caplog.text and "elapsed_ms=" in caplog.text


@pytest.mark.parametrize("backend", ["codex", "claude-code"])
async def test_stream_diagnostics_keep_protocol_context_and_mask_credentials(
    backend, tmp_path, caplog
):
    caplog.set_level(logging.DEBUG)
    credential = "run-credential-46-stream"
    malformed = (
        {"type": "item.completed", "item": {"type": "file_change", "changes": [PRIVATE_TOKEN, credential]}}
        if backend == "codex"
        else {"type": "assistant", "message": {"content": [PRIVATE_TOKEN, credential]}}
    )
    final = {"type": "turn.completed" if backend == "codex" else "result"}
    lines = [
        f"WARNING: {PRIVATE_TOKEN} {credential}",
        f'{{"private":"{PRIVATE_TOKEN}","detail":"{credential}"}} trailing',
        json.dumps([PRIVATE_TOKEN]),
        json.dumps(malformed),
        json.dumps({"type": f"{PRIVATE_PERSONA} {credential} Bearer unknown-bearer-46"}),
    ]
    if backend == "claude-code":
        lines.append(json.dumps({
            "type": "system", "subtype": "init", "session_id": "native-session-46",
            "mcp_servers": [
                {"name": PRIVATE_TOKEN, "status": PRIVATE_PERSONA},
                {"name": "secret=namespace-password-46", "status": "failed"},
                {"name": f"namespace-{credential}", "status": f"needs-auth-{credential}"},
            ],
        }))
    lines.append(json.dumps(final))
    script = f"import sys; sys.stdout.write({chr(10).join(lines) + chr(10)!r})"
    profile = replace(
        get_harness(backend).profile,
        build_turn_argv=_profile(script).build_turn_argv,
        stdin_mode=StdinMode.CLOSE_AFTER_SPAWN,
        activity_observer=None,
    )
    run = Harness(profile).create_run(RunConfig(mcp_servers=[]))
    run.bind_tool_context("request-stream-46", "attempt-stream-46")
    try:
        await run.start(
            PRIVATE_PROMPT,
            str(tmp_path),
            credential=HarnessCredential(backend, "api_key", credential),
        )
        events = await asyncio.wait_for(_collect(run), 3)
    finally:
        await run.stop()

    assert events[-1].type == "result"
    assert run.protocol_issues == {"invalid_json": 2, "parser_exception": 1}
    assert [location["line"] for location in run.protocol_locations] == [2, 3, 4]
    assert all("sha256" in location and "characters" in location for location in run.protocol_locations)
    messages = "\n".join(
        record.message for record in caplog.records if "Harness spawn" not in record.message
    )
    assert PRIVATE_TOKEN in messages and PRIVATE_PERSONA in messages
    assert credential not in caplog.text
    assert "unknown-bearer-46" not in caplog.text
    assert "namespace-password-46" not in caplog.text
    assert backend in caplog.text and "request-stream-46" in caplog.text
    assert "invalid_json" in caplog.text and "parser_exception" in caplog.text
    assert "Traceback (most recent call last):" in messages
    assert "AttributeError: 'str' object has no attribute 'get'" in messages
    assert "in parse" in messages and "trailing" in messages
    assert "Unhandled" in messages
    assert f"WARNING: {PRIVATE_TOKEN} [redacted]" in messages
    if backend == "claude-code":
        assert f"{PRIVATE_TOKEN}={PRIVATE_PERSONA}" in messages
        assert "namespace-[redacted]=needs-auth-[redacted]" in messages


async def test_spawn_failure_diagnostics_preserve_exception_and_its_traceback(
    tmp_path, caplog, monkeypatch
):
    caplog.set_level(logging.DEBUG)
    failure = OSError(errno.E2BIG, f"{PRIVATE_PROMPT} {PRIVATE_TOKEN}")

    async def reject_spawn(*args, **kwargs):
        raise failure

    monkeypatch.setattr(asyncio, "create_subprocess_exec", reject_spawn)
    run = Harness(_profile("pass")).create_run(RunConfig(mcp_servers=[]))
    run.bind_tool_context("request-spawn-46", "attempt-spawn-46")
    try:
        with pytest.raises(OSError) as raised:
            await run.start(
                PRIVATE_PROMPT,
                str(tmp_path),
                credential=HarnessCredential("synthetic", "api_key", PRIVATE_TOKEN),
            )
    finally:
        await run.stop()

    assert raised.value is failure and raised.value.errno == errno.E2BIG
    assert PRIVATE_TOKEN in str(raised.value)
    assert PRIVATE_TOKEN not in caplog.text and PRIVATE_PROMPT in caplog.text
    assert "provider=synthetic" in caplog.text and "request-spawn-46" in caplog.text
    assert "error_type=OSError" in caplog.text and "errno=7" in caplog.text
    assert "Traceback (most recent call last):" in caplog.text
    assert "in reject_spawn" in caplog.text
    assert "OSError: [Errno 7]" in caplog.text


async def test_oneshot_failure_diagnostics_keep_complete_stderr_and_exit_facts(
    tmp_path, caplog, monkeypatch
):
    caplog.set_level(logging.DEBUG)
    installation_token = "installation-credential-46-oneshot"
    monkeypatch.setattr(settings, "auth_token", installation_token)
    detail = f"provider stderr detail {PRIVATE_TOKEN} {installation_token}\n{'z' * 2200}oneshot-end-46"
    script = f"import sys; print(sys.argv[1],file=sys.stderr); print({detail!r},file=sys.stderr); sys.exit(9)"
    with pytest.raises(HarnessOneshotError) as raised:
        await Harness(_profile(script)).run_oneshot(
            OneShotContext(
                prompt=PRIVATE_PROMPT,
                working_dir=str(tmp_path),
                credential=HarnessCredential("synthetic", "api_key", PRIVATE_TOKEN),
            )
        )

    assert raised.value.code == "failed"
    assert PRIVATE_TOKEN not in caplog.text and installation_token not in caplog.text
    assert PRIVATE_PROMPT in caplog.text and "provider stderr detail" in caplog.text
    assert "z" * 2200 + "oneshot-end-46" in caplog.text
    assert "provider=synthetic" in caplog.text
    assert "exit_code=9" in caplog.text and "elapsed_ms=" in caplog.text


@pytest.mark.parametrize("chain", ["cause", "context"])
async def test_native_tail_handler_failure_preserves_full_exception_chain(
    chain, tmp_path, caplog
):
    caplog.set_level(logging.DEBUG)
    detail = f"{PRIVATE_PROMPT}\n{'y' * 2200}diagnostic-end-46"
    cause = ValueError(detail)
    failure = RuntimeError(f"{PRIVATE_TOKEN} tail handler detail")
    script = (
        "import json; print('{\"type\":\"result\"}'); "
        "print('{\"type\":\"native.failed\"}'); "
        f"print(json.dumps({{'type':'native.tail','content':{PRIVATE_TAIL!r}}}))"
    )
    run = Harness(_profile(script)).create_run(RunConfig(mcp_servers=[]))
    tail = []
    tail_seen = asyncio.Event()

    async def handle_tail(event: HarnessEvent) -> None:
        if event.type == "native.failed":
            try:
                raise cause
            except ValueError as error:
                if chain == "cause":
                    raise failure from error
                raise failure
        tail.append(event.raw)
        tail_seen.set()

    run.set_idle_handler(handle_tail)
    try:
        await run.start(
            PRIVATE_PROMPT,
            str(tmp_path),
            credential=HarnessCredential("synthetic", "api_key", PRIVATE_TOKEN),
        )
        events = await asyncio.wait_for(_collect(run), 3)
        await asyncio.wait_for(tail_seen.wait(), 3)
    finally:
        await run.stop()

    assert [event.type for event in events] == ["result"]
    assert tail == [{"type": "native.tail", "content": PRIVATE_TAIL}]
    assert run.protocol_issues == {}
    assert PRIVATE_TOKEN not in caplog.text and detail in caplog.text
    assert "idle event handler failed" in caplog.text and "error_type=RuntimeError" in caplog.text
    assert "Traceback (most recent call last):" in caplog.text
    assert "in handle_tail" in caplog.text and "tail handler detail" in caplog.text
    assert ("direct cause" if chain == "cause" else "During handling") in caplog.text
    assert failure.__context__ is cause and str(cause) == detail
    assert str(failure) == f"{PRIVATE_TOKEN} tail handler detail"


@pytest.mark.parametrize("stage", ["spawn", "retirement"])
async def test_cancelled_waiter_keeps_late_failure_diagnostics_with_the_run(
    stage, tmp_path, monkeypatch, caplog,
):
    caplog.set_level(logging.DEBUG)
    credential = "bare-run-value-46-late-error"
    detail = "late native failure after its caller stopped waiting"
    failure = OSError(errno.EIO, f"{detail}: {credential}")
    entered, release = asyncio.Event(), asyncio.Event()
    profile = _profile("print('{\"type\":\"result\"}', flush=True)")

    if stage == "spawn":
        async def refused(*argv, **kwargs):
            entered.set()
            await release.wait()
            raise failure

        monkeypatch.setattr(asyncio, "create_subprocess_exec", refused)
    else:
        class ClosingObserver:
            def observe_stdout(self, obj):
                pass

            def consume_stderr(self, line):
                return False

            async def start(self):
                pass

            def invalidate(self):
                pass

            async def close(self):
                entered.set()
                await release.wait()
                raise failure

        profile = replace(profile, activity_observer=lambda *args, **kwargs: ClosingObserver())

    run = Harness(profile).create_run(RunConfig(mcp_servers=[]))
    run.bind_activity(lambda timestamp: None)
    starting = asyncio.create_task(run.start(
        PRIVATE_PROMPT, str(tmp_path),
        credential=HarnessCredential("synthetic", "api_key", credential),
    ))
    try:
        if stage == "spawn":
            await asyncio.wait_for(entered.wait(), 2)
            starting.cancel()
            await asyncio.gather(starting, return_exceptions=True)
        else:
            await asyncio.wait_for(starting, 2)
            await asyncio.wait_for(_collect(run), 2)
            stopping = asyncio.create_task(run.stop())
            await asyncio.wait_for(entered.wait(), 2)
            stopping.cancel()
            await asyncio.gather(stopping, return_exceptions=True)
        release.set()
        with pytest.raises(OSError) as raised:
            await asyncio.wait_for(run.stop(), 2)
        assert raised.value is failure
        assert credential not in caplog.text
        assert detail in caplog.text and "Traceback (most recent call last):" in caplog.text
        assert credential in str(failure) and failure.errno == errno.EIO
    finally:
        release.set()
        await asyncio.gather(starting, run.stop(), return_exceptions=True)


async def _collect(run) -> list[HarnessEvent]:
    return [event async for event in run.stream()]
