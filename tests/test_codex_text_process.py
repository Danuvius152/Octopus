"""One-shot worker admission, wire protocol and retirement through Harness."""

import asyncio
import json
import os
import sys
import threading
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from server.harness import HarnessCredential, HarnessOneshotError, OneShotContext
from server.harness.codex import CODEX, build_oneshot_argv
from server.harness.harness import Harness


@pytest.fixture
def text_backend():
    state = SimpleNamespace(status=200, events=[{"type": "response.completed", "response": {
        "status": "completed", "output": [{"type": "message", "role": "assistant", "content": [
            {"type": "output_text", "text": "completed text"}]}]}}], requests=[],
        started=threading.Event(), release=threading.Event(), stall=False)

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            state.requests.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            self.send_response(state.status)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            state.started.set()
            if state.stall:
                state.release.wait(10)
            try:
                self.wfile.write("".join("data: " + json.dumps(event) + "\n\n" for event in state.events).encode())
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield state, f"http://127.0.0.1:{server.server_port}/responses"
    finally:
        state.release.set()
        server.shutdown()
        server.server_close()
        thread.join()


def worker_harness(endpoint):
    # Substitute only the external HTTP endpoint in the child. The production
    # worker, argv isolation, stdout parser and common lifecycle all stay real.
    launcher = (
        "import importlib.util,sys;path=sys.argv.pop(1);"
        "spec=importlib.util.spec_from_file_location('text_worker',path);"
        "worker=importlib.util.module_from_spec(spec);spec.loader.exec_module(worker);"
        f"worker.RESPONSES_URL={endpoint!r};raise SystemExit(worker.main())"
    )

    def argv(context):
        args, kwargs = build_oneshot_argv(context)
        return args[:2] + ["-c", launcher] + args[2:], kwargs

    return Harness(replace(CODEX, build_oneshot_argv=argv))


@pytest.fixture
def text_context(tmp_path, monkeypatch):
    home = tmp_path / "login"
    home.mkdir()
    control = tmp_path / "native-keystore"
    control.write_text(f"#!{sys.executable}\n" +
        "import json,sys\n" +
        "for line in sys.stdin:\n" +
        " request=json.loads(line)\n" +
        " if 'id' in request:\n" +
        "  result={'authMethod':'chatgpt','authToken':'synthetic-token'} if request['method']=='getAuthStatus' else {}\n" +
        "  print(json.dumps({'id':request['id'],'result':result}),flush=True)\n")
    control.chmod(0o700)
    monkeypatch.setattr("server.harness.run._which_with_fallback", lambda name: str(control))
    monkeypatch.setenv("CODEX_HOME", str(home))
    return OneShotContext(prompt="x", model="synthetic-model", reasoning_effort="low", service_tier="fast",
                          working_dir=str(tmp_path),
                          credential=HarnessCredential(backend="codex", auth_type="oauth", home_dir=str(home)))


@pytest.mark.parametrize("bound", [True, False])
async def test_public_oneshot_consumes_completed_text_and_maps_fast_tier(text_backend, text_context, bound):
    state, endpoint = text_backend
    if not bound:
        text_context = replace(text_context, credential=None)
    assert await worker_harness(endpoint).run_oneshot(text_context) == "completed text"
    assert len(state.requests) == 1
    assert state.requests[0]["tools"] == [] and state.requests[0]["tool_choice"] == "none"
    assert state.requests[0]["service_tier"] == "priority"


@pytest.mark.parametrize("kind", ["http", "eof", "incomplete"])
async def test_worker_failure_never_delivers_partial_success(text_backend, text_context, kind):
    state, endpoint = text_backend
    state.events = [{"type": "response.output_text.delta", "delta": "partial"}]
    if kind == "http":
        state.status = 500
    elif kind == "incomplete":
        state.events.append({"type": "response.incomplete"})
    with pytest.raises(HarnessOneshotError) as error:
        await worker_harness(endpoint).run_oneshot(text_context)
    assert error.value.code == "failed"
    assert len(state.requests) == 1


async def test_denied_admission_does_not_start_the_text_request(text_backend, text_context):
    state, endpoint = text_backend

    async def denied():
        raise ValueError("The owning request was revoked")

    with pytest.raises(HarnessOneshotError) as error:
        await worker_harness(endpoint).run_oneshot(replace(text_context, authorize_spawn=denied))
    assert error.value.code == "unauthorized"
    assert state.requests == []


async def test_missing_codex_reports_not_found_before_starting_worker(text_context, monkeypatch):
    monkeypatch.setattr("server.harness.run._which_with_fallback", lambda name: None)

    async def unexpected_spawn(*args, **kwargs):
        pytest.fail("Missing native CLI must be reported before starting a worker")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", unexpected_spawn)
    with pytest.raises(HarnessOneshotError) as error:
        await Harness(CODEX).run_oneshot(text_context)
    assert error.value.code == "not_found"


async def test_http_stall_times_out_under_common_harness_ownership(text_backend, text_context):
    state, endpoint = text_backend
    state.stall = True
    with pytest.raises(HarnessOneshotError) as error:
        await worker_harness(endpoint).run_oneshot(text_context, timeout=2)
    assert state.started.is_set()
    assert error.value.code == "timeout"


async def test_cancelled_worker_retires_native_control_and_its_descendant(text_backend, text_context, tmp_path, monkeypatch):
    _, endpoint = text_backend
    control = tmp_path / "native-control"
    ready = tmp_path / "control-pids.json"
    control.write_text(f"#!{sys.executable}\n" +
        "import json,os,signal,subprocess,sys,time\n" +
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n" +
        "child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'])\n" +
        f"open({str(ready)!r},'w').write(json.dumps([os.getpid(),child.pid]))\n" +
        "time.sleep(30)\n")
    control.chmod(0o700)
    monkeypatch.setattr("server.harness.run._which_with_fallback", lambda name: str(control))
    task = asyncio.create_task(worker_harness(endpoint).run_oneshot(replace(text_context, model=None)))
    try:
        async with asyncio.timeout(3):
            while not ready.exists() or not ready.read_text():
                await asyncio.sleep(.01)
        pids = json.loads(ready.read_text())
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        for pid in pids:
            with pytest.raises(ProcessLookupError):
                os.kill(pid, 0)
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
