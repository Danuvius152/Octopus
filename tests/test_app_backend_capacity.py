"""Backend admission counts original lifecycles, including startup/retirement."""

import asyncio
import shlex
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException
from starlette.requests import Request

from server import app_backends
from server.app_backends import FAILED, RUNNING, BackendCapacityError, BackendSupervisor
from server.routers import applications as route
from server.wiki import app_bridge


def _app(root: Path, name: str, *, ready: bool = False, hold_stop: bool = False) -> Path:
    app = root / name
    app.mkdir()
    code = (
        "import os,time,signal\nfrom pathlib import Path\n"
        "Path('started').write_text(str(os.getpid()))\n"
        + (
            "def stop(*args):\n"
            " Path('stopping').touch()\n"
            " while not Path('retire').exists(): time.sleep(.01)\n"
            " raise SystemExit(0)\n"
            "signal.signal(signal.SIGTERM,stop)\n"
            if hold_stop else ""
        ) +
        "while not Path('ready').exists(): time.sleep(.01)\n"
        "import http.server\n"
        "http.server.HTTPServer(('127.0.0.1',int(os.environ['PORT'])),"
        "http.server.BaseHTTPRequestHandler).serve_forever()\n"
    )
    script = app / "start.sh"
    script.write_text("#!/bin/sh\nexec " + shlex.join([sys.executable, "-c", code]) + "\n")
    script.chmod(0o700)
    if ready:
        (app / "ready").touch()
    return app


async def _wait(predicate):
    async with asyncio.timeout(3):
        while not predicate():
            await asyncio.sleep(.01)


async def test_fifth_proxy_is_unavailable_while_four_real_backends_are_starting(
    tmp_path, monkeypatch, short_process_shutdown,
):
    supervisor = BackendSupervisor()
    apps = [_app(tmp_path, f"app-{i}") for i in range(5)]
    starts = [asyncio.create_task(supervisor.ensure_running(f"app-{i}", str(apps[i]))) for i in range(4)]
    body_reads = []

    async def authorized(*args):
        return {"id": "app-4", "app_dir": str(apps[4])}

    async def receive():
        body_reads.append(True)
        return {"type": "http.request", "body": b"effect", "more_body": False}

    request = Request({
        "type": "http", "method": "POST", "path": "/apps/app-4/api/save",
        "headers": [], "query_string": b"",
    }, receive)
    monkeypatch.setattr(route, "backend_supervisor", supervisor)
    monkeypatch.setattr(route, "_application_for_request", authorized)
    try:
        await _wait(lambda: all((app / "started").exists() for app in apps[:4]))
        with pytest.raises(HTTPException) as refused:
            await asyncio.wait_for(route.proxy_application_api(request, "app-4", "save"), .5)
        assert refused.value.status_code == 503
        assert not (apps[4] / "started").exists()
        assert not body_reads
        state = supervisor.get("app-4")
        assert state is not None and state.failed_at is None and state.error is None
    finally:
        await supervisor.shutdown()
        await asyncio.gather(*starts, return_exceptions=True)


@pytest.mark.parametrize("failure", ["spawn", "exit", "timeout"])
async def test_failed_start_releases_its_slot(tmp_path, monkeypatch, short_process_shutdown, failure):
    monkeypatch.setattr(app_backends, "MAX_RUNNING", 1)
    monkeypatch.setattr(app_backends, "READY_TIMEOUT_S", .1)
    failed = _app(tmp_path, "failed")
    script = failed / "start.sh"
    if failure == "spawn":
        script.write_text("#!/nonexistent/interpreter\n")
    elif failure == "exit":
        script.write_text("#!/bin/sh\nexit 7\n")
    replacement = _app(tmp_path, "replacement", ready=True)
    supervisor = BackendSupervisor()
    try:
        state = await supervisor.ensure_running("failed", str(failed))
        assert state.state == FAILED and state.failed_at is not None
        # The replacement gets time for real port readiness.
        monkeypatch.setattr(app_backends, "READY_TIMEOUT_S", 3)
        started = await supervisor.ensure_running("replacement", str(replacement))
        assert started.state == RUNNING
        assert state.process is None
    finally:
        await supervisor.shutdown()


async def test_cancelled_late_spawn_keeps_slot_until_original_process_retires(
    tmp_path, monkeypatch, short_process_shutdown,
):
    monkeypatch.setattr(app_backends, "MAX_RUNNING", 1)
    first = _app(tmp_path, "first")
    second = _app(tmp_path, "second", ready=True)
    spawned, release = asyncio.Event(), asyncio.Event()
    real_spawn = asyncio.create_subprocess_exec
    processes = []

    async def spawn(*args, **kwargs):
        process = await real_spawn(*args, **kwargs)
        processes.append(process)
        if len(processes) == 1:
            spawned.set()
            await release.wait()
        return process

    monkeypatch.setattr(app_backends.asyncio, "create_subprocess_exec", spawn)
    supervisor = BackendSupervisor()
    starting = asyncio.create_task(supervisor.ensure_running("first", str(first)))
    try:
        await asyncio.wait_for(spawned.wait(), 3)
        starting.cancel()
        await asyncio.sleep(0)
        starting.cancel()
        with pytest.raises(BackendCapacityError):
            await supervisor.ensure_running("second", str(second))
        assert not (second / "started").exists()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await starting
        assert processes[0].returncode is not None
        assert (await supervisor.ensure_running("second", str(second))).state == RUNNING
    finally:
        release.set()
        await supervisor.shutdown()
        await asyncio.gather(starting, return_exceptions=True)


async def test_failed_retirement_does_not_release_capacity(tmp_path, monkeypatch, short_process_shutdown):
    monkeypatch.setattr(app_backends, "MAX_RUNNING", 1)
    first = _app(tmp_path, "first", ready=True)
    second = _app(tmp_path, "second", ready=True)
    supervisor = BackendSupervisor()
    real_wait = app_backends.wait_group_exit

    async def unconfirmed(*args, **kwargs):
        raise TimeoutError("group exit unknown")

    try:
        await supervisor.ensure_running("first", str(first))
        monkeypatch.setattr(app_backends, "wait_group_exit", unconfirmed)
        with pytest.raises(RuntimeError, match="exit unconfirmed"):
            await supervisor.stop("first")
        with pytest.raises(BackendCapacityError, match="retirement unconfirmed"):
            await supervisor.ensure_running("second", str(second))
        assert not (second / "started").exists()
        monkeypatch.setattr(app_backends, "wait_group_exit", real_wait)
        await supervisor.stop("first")
        assert (await supervisor.ensure_running("second", str(second))).state == RUNNING
    finally:
        monkeypatch.setattr(app_backends, "wait_group_exit", real_wait)
        await supervisor.shutdown()


async def test_concurrent_lru_admission_never_overlaps_retiring_process_groups(
    tmp_path, monkeypatch, short_process_shutdown,
):
    from server.process_groups import group_has_live_members_after_kill

    monkeypatch.setattr(app_backends, "MAX_RUNNING", 2)
    apps = {name: _app(tmp_path, name, ready=True) for name in ("a", "b", "c", "d")}
    real_spawn = asyncio.create_subprocess_exec
    processes, observed_live = [], []

    async def spawn(*args, **kwargs):
        process = await real_spawn(*args, **kwargs)
        processes.append(process)
        observed_live.append(sum(group_has_live_members_after_kill(proc.pid) for proc in processes))
        return process

    monkeypatch.setattr(app_backends.asyncio, "create_subprocess_exec", spawn)
    supervisor = BackendSupervisor()
    try:
        a = await supervisor.ensure_running("a", str(apps["a"]))
        b = await supervisor.ensure_running("b", str(apps["b"]))
        await supervisor.ensure_running("a", str(apps["a"]))
        c = await supervisor.ensure_running("c", str(apps["c"]))
        assert c.state == RUNNING and a.state == RUNNING
        assert b.process is None, "The least recently used original lifecycle must retire first"
        await supervisor.stop("c")
        await supervisor.ensure_running("b", str(apps["b"]))
        results = await asyncio.wait_for(asyncio.gather(
            supervisor.ensure_running("c", str(apps["c"])),
            supervisor.ensure_running("d", str(apps["d"])),
        ), 5)
        assert all(result.state == RUNNING for result in results)
        assert max(observed_live) == 2
    finally:
        await supervisor.shutdown()
    assert all(not group_has_live_members_after_kill(proc.pid) for proc in processes)


async def test_same_application_shares_one_start(tmp_path, monkeypatch, short_process_shutdown):
    app = _app(tmp_path, "shared")
    supervisor = BackendSupervisor()
    real_spawn = asyncio.create_subprocess_exec
    processes = []

    async def spawn(*args, **kwargs):
        process = await real_spawn(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(app_backends.asyncio, "create_subprocess_exec", spawn)
    starts = [asyncio.create_task(supervisor.ensure_running("shared", str(app))) for _ in range(5)]
    try:
        await _wait((app / "started").exists)
        (app / "ready").touch()
        results = await asyncio.wait_for(asyncio.gather(*starts), 3)
        assert len(processes) == 1
        assert all(result.state == RUNNING and result.pid == processes[0].pid for result in results)
    finally:
        await supervisor.shutdown()
        await asyncio.gather(*starts, return_exceptions=True)


async def test_same_application_shares_admission_while_lru_victim_retires(
    tmp_path, monkeypatch, short_process_shutdown,
):
    monkeypatch.setattr(app_backends, "MAX_RUNNING", 2)
    monkeypatch.setattr(app_backends, "_TERM_TIMEOUT_SECONDS", 3)
    a_dir = _app(tmp_path, "a", ready=True, hold_stop=True)
    b_dir = _app(tmp_path, "b", ready=True)
    c_dir = _app(tmp_path, "c", ready=True)
    supervisor = BackendSupervisor()
    starts = []
    second_entered = asyncio.Event()

    async def open_again():
        second_entered.set()
        return await supervisor.ensure_running("c", str(c_dir))

    try:
        await supervisor.ensure_running("a", str(a_dir))
        b = await supervisor.ensure_running("b", str(b_dir))
        original_b = b.process
        starts.append(asyncio.create_task(supervisor.ensure_running("c", str(c_dir))))
        await _wait((a_dir / "stopping").exists)
        starts.append(asyncio.create_task(open_again()))
        await second_entered.wait()
        (a_dir / "retire").touch()
        first, second = await asyncio.wait_for(asyncio.gather(*starts), 5)
        assert first is second and first.state == RUNNING
        assert b.state == RUNNING and b.process is original_b
        assert original_b is not None and original_b.returncode is None
    finally:
        (a_dir / "retire").touch()
        await supervisor.shutdown()
        await asyncio.gather(*starts, return_exceptions=True)


async def test_installation_does_not_take_a_running_backend_slot(tmp_path, monkeypatch, short_process_shutdown):
    monkeypatch.setattr(app_backends, "MAX_RUNNING", 1)
    installing = _app(tmp_path, "installing")
    install = installing / "install.sh"
    install.write_text("#!/bin/sh\necho installing > installing\nexec sleep 60\n")
    install.chmod(0o700)
    ready = _app(tmp_path, "ready", ready=True)
    supervisor = BackendSupervisor()
    starting = asyncio.create_task(supervisor.ensure_running("installing", str(installing)))
    try:
        await _wait((installing / "installing").exists)
        assert (await supervisor.ensure_running("ready", str(ready))).state == RUNNING
        assert not (installing / "started").exists()
    finally:
        await supervisor.shutdown()
        await asyncio.gather(starting, return_exceptions=True)


async def test_mutation_readmission_capacity_refusal_never_forwards_the_body(tmp_path, monkeypatch):
    attempts, forwarded, finished = [], [], []
    real_client = httpx.AsyncClient

    async def authorized(*args):
        return {"id": "app", "app_dir": str(tmp_path)}

    async def running(*args):
        attempts.append(True)
        if len(attempts) == 1:
            return SimpleNamespace(state=RUNNING, port=12345)
        raise BackendCapacityError("Application backend capacity is unavailable")

    class Mutation:
        async def finish(self):
            finished.append(True)

        async def abort(self, app_id):
            pytest.fail("A refused request must not abort an application writer")

    async def begin(*args):
        return Mutation()

    def upstream(request):
        forwarded.append(request)
        return httpx.Response(200)

    def client(**kwargs):
        return real_client(transport=httpx.MockTransport(upstream), **kwargs)

    async def receive():
        return {"type": "http.request", "body": b"effect", "more_body": False}

    request = Request({
        "type": "http", "method": "POST", "path": "/apps/app/api/save",
        "headers": [], "query_string": b"",
    }, receive)
    monkeypatch.setattr(route, "_application_for_request", authorized)
    monkeypatch.setattr(route.backend_supervisor, "ensure_running", running)
    monkeypatch.setattr(app_bridge, "begin", begin)
    monkeypatch.setattr(httpx, "AsyncClient", client)
    with pytest.raises(HTTPException) as refused:
        await route.proxy_application_api(request, "app", "save")
    assert refused.value.status_code == 503
    assert len(attempts) == 2 and finished == [True] and forwarded == []
