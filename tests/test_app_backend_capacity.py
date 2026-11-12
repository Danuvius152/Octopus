"""Backend admission counts original lifecycles, including startup/retirement."""

import asyncio
import shlex
import sys
from pathlib import Path

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


@pytest.mark.parametrize("original", ["absent", "running", "starting"])
async def test_delayed_rebuild_cannot_retire_a_replacement_backend(tmp_path, short_process_shutdown, original):
    app = _app(tmp_path, "rebuild", ready=original == "running")
    supervisor = BackendSupervisor()
    starting = replacement = None
    try:
        if original == "running":
            await supervisor.ensure_running("rebuild", str(app))
        elif original == "starting":
            starting = asyncio.create_task(supervisor.ensure_running("rebuild", str(app)))
            await _wait(lambda: (app / "started").exists())
        captured = supervisor.capture_rebuild("rebuild")
        if original != "absent":
            await supervisor.stop("rebuild")
        if starting:
            await asyncio.gather(starting, return_exceptions=True)
        (app / "ready").unlink(missing_ok=True)
        (app / "started").unlink(missing_ok=True)
        replacement = asyncio.create_task(supervisor.ensure_running("rebuild", str(app)))
        await _wait(lambda: (app / "started").exists())
        state = supervisor.get("rebuild")
        process = state.process
        await supervisor.on_rebuild("rebuild", str(app), target=captured)
        assert process.returncode is None and state.process is process and not replacement.done()
        (app / "ready").touch()
        assert (await asyncio.wait_for(replacement, 3)).state == RUNNING
    finally:
        await supervisor.shutdown()
        await asyncio.gather(*(task for task in (starting, replacement) if task is not None), return_exceptions=True)


async def test_captured_rebuild_cancels_original_blocked_install_without_waiting_for_its_lock(
    tmp_path, short_process_shutdown,
):
    app = _app(tmp_path, "install-rebuild", ready=True)
    install = app / "install.sh"
    install.write_text("#!/bin/sh\ntouch installing\nwhile :; do sleep 1; done\n")
    install.chmod(0o700)
    supervisor = BackendSupervisor()
    starting = asyncio.create_task(supervisor.ensure_running("install-rebuild", str(app)))
    try:
        await _wait(lambda: (app / "installing").exists())
        target = supervisor.capture_rebuild("install-rebuild")
        process = supervisor.get("install-rebuild").process
        await asyncio.wait_for(supervisor.on_rebuild("install-rebuild", str(app), target=target), 2)
        assert starting.cancelled() and process.returncode is not None
        assert not supervisor.get("install-rebuild").installed and not supervisor.drain_blockers()
    finally:
        await supervisor.shutdown()
        await asyncio.gather(starting, return_exceptions=True)


async def test_captured_startup_keeps_its_identity_after_becoming_a_running_process(tmp_path, short_process_shutdown):
    app = _app(tmp_path, "same-startup")
    supervisor = BackendSupervisor()
    starting = asyncio.create_task(supervisor.ensure_running("same-startup", str(app)))
    try:
        await _wait(lambda: (app / "started").exists())
        target = supervisor.capture_rebuild("same-startup")
        (app / "ready").touch()
        state = await asyncio.wait_for(starting, 3)
        process = state.process
        # A quick ensure reuses the same resource rather than changing identity.
        assert await supervisor.ensure_running("same-startup", str(app)) is state
        await supervisor.on_rebuild("same-startup", str(app), target=target)
        assert process.returncode is not None and state.process is None and not state.installed
    finally:
        await supervisor.shutdown()
        await asyncio.gather(starting, return_exceptions=True)


@pytest.mark.parametrize("removed", [True, False])
@pytest.mark.parametrize("active_use", [True, False])
async def test_invalid_start_script_retires_original_process_readers_and_use(
    tmp_path, monkeypatch, short_process_shutdown, removed, active_use,
):
    monkeypatch.setattr(app_backends, "MAX_RUNNING", 1)
    app = _app(tmp_path, "invalidated", ready=True)
    other = _app(tmp_path, "other", ready=True)
    supervisor = BackendSupervisor()
    state = await supervisor.ensure_running("invalidated", str(app))
    process, port, readers = state.process, state.port, state._readers[:]
    use_started = asyncio.Event()

    async def consumer():
        use = await supervisor.acquire("invalidated", str(app))
        use.add_cleanup(use.abort)  # Wiki cleanup also waits for original process exit.
        use_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            await use.finish()

    request = asyncio.create_task(consumer()) if active_use else None
    script = app / "start.sh"
    original = script.read_bytes()
    try:
        if request:
            await use_started.wait()
        if removed:
            script.unlink()
        else:
            script.chmod(0o600)
        result = await asyncio.wait_for(supervisor.ensure_running("invalidated", str(app)), 3)
        assert result.state == app_backends.ABSENT
        assert process.returncode is not None and all(reader.done() for reader in readers)
        assert not app_backends._port_accepts(port)
        assert state.process is None and not state._uses and not state.installed
        assert (await supervisor.ensure_running("other", str(other))).state == RUNNING
        await supervisor.stop("other")
        script.write_bytes(original)
        script.chmod(0o700)
        restarted = await supervisor.ensure_running("invalidated", str(app))
        assert restarted.state == RUNNING and restarted.process is not process and restarted.installed
    finally:
        await supervisor.shutdown()
        if request:
            await asyncio.gather(request, return_exceptions=True)


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


@pytest.mark.parametrize("failure", ["process", "use"])
async def test_invalid_script_keeps_slot_until_original_retirement_is_confirmed(
    tmp_path, monkeypatch, short_process_shutdown, failure,
):
    monkeypatch.setattr(app_backends, "MAX_RUNNING", 1)
    monkeypatch.setattr(app_backends, "_TASK_DRAIN_SECONDS", .03)
    app, other = _app(tmp_path, "app", ready=True), _app(tmp_path, "other", ready=True)
    supervisor = BackendSupervisor()
    state = await supervisor.ensure_running("app", str(app))
    process = state.process
    real_wait = app_backends.wait_group_exit
    cleanup_entered, release = asyncio.Event(), asyncio.Event()

    async def unconfirmed(*args, **kwargs):
        raise TimeoutError("original group exit unknown")

    async def cleanup():
        cleanup_entered.set()
        await release.wait()

    use = None
    if failure == "process":
        monkeypatch.setattr(app_backends, "wait_group_exit", unconfirmed)
    else:
        use = await supervisor.acquire("app", str(app))
        use.detach()
        use.add_cleanup(cleanup)
    (app / "start.sh").chmod(0o600)
    try:
        with pytest.raises(RuntimeError, match="unconfirmed"):
            await supervisor.ensure_running("app", str(app))
        assert state.process is process and state._retiring and id(state) in supervisor._slots
        with pytest.raises(BackendCapacityError):
            await supervisor.ensure_running("other", str(other))
        if use:
            assert cleanup_entered.is_set() and not use.retired
            release.set()
            await use.finish()
        monkeypatch.setattr(app_backends, "wait_group_exit", real_wait)
        await supervisor.stop("app")
        assert state.process is None and not state._uses and id(state) not in supervisor._slots
    finally:
        release.set()
        monkeypatch.setattr(app_backends, "wait_group_exit", real_wait)
        await supervisor.shutdown()


async def test_cancelled_invalid_script_observer_preserves_retirement_and_restored_script(
    tmp_path, monkeypatch, short_process_shutdown,
):
    monkeypatch.setattr(app_backends, "_TERM_TIMEOUT_SECONDS", 3)
    app = _app(tmp_path, "app", ready=True, hold_stop=True)
    supervisor = BackendSupervisor()
    state = await supervisor.ensure_running("app", str(app))
    process = state.process
    script = app / "start.sh"
    script.chmod(0o600)
    observer = asyncio.create_task(supervisor.ensure_running("app", str(app)))
    try:
        await _wait((app / "stopping").exists)
        retirement = state._retirement
        observer.cancel()
        with pytest.raises(asyncio.CancelledError):
            await observer
        assert retirement is state._retirement and not retirement.done() and state._retiring
        script.chmod(0o700)
        (app / "retire").touch()
        await asyncio.wait_for(asyncio.shield(retirement), 3)
        assert process.returncode is not None and not state.installed
        restarted = await supervisor.ensure_running("app", str(app))
        assert restarted.state == RUNNING and restarted.process is not process
    finally:
        (app / "retire").touch()
        await supervisor.shutdown()
        await asyncio.gather(observer, return_exceptions=True)


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
        with pytest.raises(BackendCapacityError, match="retiring"):
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
    supervisor = BackendSupervisor()
    directory = _app(tmp_path, "app", ready=True)
    ensure = supervisor.ensure_running
    require = supervisor.require_running
    refuse = False

    async def authorized(*args):
        return {"id": "app", "app_dir": str(directory)}

    async def running(*args):
        attempts.append(True)
        return await ensure(*args)

    class Mutation:
        async def finish(self):
            finished.append(True)

        async def abort(self, app_id):
            pytest.fail("A refused request must not abort an application writer")

    async def begin(*args):
        nonlocal refuse
        refuse = True
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
    monkeypatch.setattr(route, "backend_supervisor", supervisor)
    monkeypatch.setattr(supervisor, "ensure_running", running)
    def refuse_readmission(*args, **kwargs):
        if refuse:
            raise BackendCapacityError("Application backend capacity is unavailable")
        return require(*args, **kwargs)
    monkeypatch.setattr(supervisor, "require_running", refuse_readmission)
    monkeypatch.setattr(app_bridge, "begin", begin)
    monkeypatch.setattr(httpx, "AsyncClient", client)
    try:
        with pytest.raises(HTTPException) as refused:
            await route.proxy_application_api(request, "app", "save")
        assert refused.value.status_code == 503
        assert len(attempts) == 1 and finished == [True] and forwarded == []
    finally:
        await supervisor.shutdown()
