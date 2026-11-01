"""Safe dispatch reproduction and out-of-process offline guard regressions."""

import json
import os
import subprocess
import sys

import pytest


@pytest.mark.parametrize("backend", ["claude-code", "codex"])
async def test_original_harness_path_would_dispatch_before_attachment_interrupt(monkeypatch, tmp_path, backend):
    """Old pure UI used this real run path; stop at a spy, never launch a CLI."""
    from server.harness import RunConfig, get_harness
    from server.harness import run as module
    from tests.harness_profiles import native_submission

    attempted = []

    async def refuse(*argv, **kwargs):
        attempted.append(argv[0])
        raise RuntimeError("safe dispatch spy")

    # Avoid even binary discovery; keep the original real profile's builder.
    monkeypatch.setattr(module, "prepare_spawn", lambda argv, kwargs: (argv, kwargs))
    monkeypatch.setattr(module.asyncio, "create_subprocess_exec", refuse)
    run = get_harness(backend).create_run(RunConfig(mcp_servers=[]))
    if backend == "codex":
        monkeypatch.setattr("server.harness.codex_app_server.verify_binary", lambda path: None)
        native_submission(run, "dispatch-probe", "dispatch-attempt")
    try:
        with pytest.raises(RuntimeError, match="safe dispatch spy"):
            await run.start("attachment receipt probe", str(tmp_path))
        assert attempted == ["claude" if backend == "claude-code" else "codex"]
    finally:
        await run.stop()


@pytest.mark.parametrize("action,reason", [
    ("subprocess.run(['claude', '--version'])", "process_dispatch"),
    ("subprocess.run(['/never-executed/codex', 'exec'])", "process_dispatch"),
    ("subprocess.run([sys.executable, '-c', 'raise Exception()'])", "process_dispatch"),
    ("os.system('claude --version')", "process_dispatch"),
    ("asyncio.run(asyncio.create_subprocess_exec('claude', '--version'))", "process_dispatch"),
    ("asyncio.run(asyncio.create_subprocess_exec('codex', 'exec'))", "process_dispatch"),
    ("asyncio.run(asyncio.create_subprocess_shell('codex exec'))", "process_dispatch"),
    ("asyncio.run(loop_spawn())", "process_dispatch"),
    ("socket.socket().connect(('127.0.0.1', 8000))", "production_callback"),
    ("socket.socket(socket.AF_INET6).connect_ex(('::1', 8000))", "production_callback"),
    ("httpx.get('http://127.0.0.1:8000/mcp/wiki')", "production_callback"),
    ("httpx.Client(proxy='http://127.0.0.1:9').get('http://localhost:8000/mcp/wiki')", "production_callback"),
    ("asyncio.run(httpx.AsyncClient().get('http://localhost:8000/mcp/wiki'))", "production_callback"),
    ("asyncio.run(asyncio.open_connection('127.0.0.1', 8000))", "production_callback"),
    ("httpx.get('https://example.invalid/provider')", "external_network"),
])
def test_guard_refuses_before_process_or_network_and_records_caught_errors(tmp_path, action, reason):
    audit = tmp_path / "audit.jsonl"
    code = f"""
import asyncio, os, socket, subprocess, sys, httpx
from pathlib import Path
from tests.e2e_boundary import install
record = install(Path({str(audit)!r}))
async def loop_spawn():
    await asyncio.get_running_loop().subprocess_exec(asyncio.SubprocessProtocol, 'claude')
try:
    {action}
except RuntimeError:
    pass  # Simulate a business-layer catch: the parent still has evidence.
else:
    raise AssertionError('boundary did not reject')
record('offline_ready')
"""
    child = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=10)
    assert child.returncode == 0, child.stderr
    events = [json.loads(line) for line in audit.read_text().splitlines()]
    assert events == [{"kind": "guard_installed"}, {"kind": "violation", "reason": reason}, {"kind": "offline_ready"}]


@pytest.mark.parametrize("asynchronous", [False, True])
def test_guard_only_allows_the_known_offline_program(tmp_path, asynchronous):
    audit = tmp_path / "audit.jsonl"
    code = f"""
import asyncio, subprocess
from pathlib import Path
from tests.e2e_boundary import OFFLINE_ARGV, install
install(Path({str(audit)!r}))
async def run():
    child = await asyncio.create_subprocess_exec(*OFFLINE_ARGV, env={{}}, stdout=asyncio.subprocess.PIPE)
    stdout, _ = await child.communicate()
    assert child.returncode == 0
    return stdout.decode()
stdout = asyncio.run(run()) if {asynchronous!r} else subprocess.run(OFFLINE_ARGV, env={{}}, capture_output=True, text=True, check=True).stdout
assert 'Offline UI execution complete.' in stdout
"""
    child = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=10)
    assert child.returncode == 0, child.stderr
    assert [json.loads(line)["kind"] for line in audit.read_text().splitlines()] == ["guard_installed"] + (["offline_dispatch"] if asynchronous else [])


def test_offline_backend_refuses_real_mode_before_installing_any_substitute(tmp_path):
    child = subprocess.run(
        [sys.executable, "-c", "import tests.e2e_backend"],
        env={**os.environ, "OCTOPUS_E2E_MODE": "real"},
        capture_output=True, text=True, timeout=10,
    )
    assert child.returncode != 0
    assert "requires OCTOPUS_E2E_MODE=offline" in child.stderr


@pytest.mark.parametrize("backend", ["claude-code", "codex"])
def test_offline_entrypoint_runs_both_profiles_without_real_cli(tmp_path, backend):
    audit = tmp_path / "audit.jsonl"
    code = f"""
import asyncio
import tests.e2e_backend
from server.harness import RunConfig, get_harness
async def main():
    run = get_harness({backend!r}).create_run(RunConfig(mcp_servers=[]))
    try:
        await run.start('offline', {str(tmp_path)!r})
        events = [event async for event in run.stream()]
        assert any(event.content == 'Offline UI execution complete.' for event in events)
        assert any(event.type == 'result' and not event.is_error for event in events)
    finally:
        await run.stop()
asyncio.run(main())
"""
    child = subprocess.run(
        [sys.executable, "-c", code],
        env={**os.environ, "OCTOPUS_E2E_MODE": "offline", "OCTOPUS_E2E_AUDIT_FILE": str(audit)},
        capture_output=True, text=True, timeout=10,
    )
    assert child.returncode == 0, child.stderr
    assert [json.loads(line)["kind"] for line in audit.read_text().splitlines()] == ["guard_installed", "offline_ready", "offline_dispatch"]


def test_offline_lifespan_refuses_an_unguarded_loop_before_product_startup(tmp_path):
    audit = tmp_path / "audit.jsonl"
    code = """
import asyncio
import tests.e2e_backend as backend
async def main():
    original = asyncio.get_running_loop
    asyncio.get_running_loop = lambda: object()
    try:
        async with backend.offline_lifespan(backend.app):
            raise AssertionError('product startup must not run')
    except RuntimeError as error:
        assert 'guarded stdlib asyncio loop' in str(error)
    finally:
        asyncio.get_running_loop = original
asyncio.run(main())
"""
    child = subprocess.run(
        [sys.executable, "-c", code],
        env={**os.environ, "OCTOPUS_E2E_MODE": "offline", "OCTOPUS_E2E_AUDIT_FILE": str(audit)},
        capture_output=True, text=True, timeout=10,
    )
    assert child.returncode == 0, child.stderr
    assert json.loads(audit.read_text().splitlines()[-1]) == {"kind": "violation", "reason": "unsupported_event_loop"}
