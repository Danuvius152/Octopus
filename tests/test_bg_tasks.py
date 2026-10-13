"""Tests for the cross-turn background task feature.

Covers:
  * `BgTaskManager` lifecycle — spawn/cancel/timeout/cap/persist
  * `mark_in_flight_bg_tasks_interrupted` on startup
  * `render_delivery_prompt` shape (the synthesized text we inject)
  * REST endpoints (`POST /bg-tasks`, list, get, cancel)
  * `SessionManager.deliver_bg_result` glue — verifies the synthesized
    prompt flows through `start_message` and broadcasts a user_message
  * `ClaudeCodeBackend.build_args` registers the bg MCP server

The MCP tool unit tests (bg_run/cancel/list) live inline below; they
invoke the FastMCP-wrapped function directly (no real stdio loop)
with httpx mocked so we don't need a live FastAPI to verify the tool's
request shape.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from httpx import ASGITransport, AsyncClient

from server.bg_tasks import (
    MAX_STREAM_BYTES,
    BgTaskError,
    BgTaskManager,
    BgTaskRecord,
    bg_task_manager,
    render_delivery_prompt,
)
from server.database import Database
from server.main import app
from server.session_manager import session_manager

TOKEN = "changeme"
HEADERS = {"Authorization": f"Bearer {TOKEN}"}


# ---------------------------------------------------------------------------
# BgTaskManager unit tests
# ---------------------------------------------------------------------------


@pytest.fixture
async def db(tmp_path):
    """Per-test in-memory DB with bg_tasks schema applied."""
    d = Database(":memory:")
    await d.initialize()
    yield d
    await d.close()


@pytest.fixture
async def manager(db):
    """Fresh manager bound to a per-test DB, with capture-only callbacks."""
    mgr = BgTaskManager()
    delivered: list[BgTaskRecord] = []
    broadcasts: list[dict] = []

    async def deliver(rec: BgTaskRecord) -> None:
        delivered.append(rec)

    async def broadcast(msg: dict) -> None:
        broadcasts.append(msg)

    mgr.bind(db=db, deliver_cb=deliver, broadcast_cb=broadcast)
    await mgr.start()
    # Stash the lists on the manager for test access.
    mgr._delivered_ = delivered  # type: ignore[attr-defined]
    mgr._broadcasts_ = broadcasts  # type: ignore[attr-defined]
    yield mgr
    await mgr.shutdown()


async def _wait_for(predicate, *, timeout: float = 5.0, interval: float = 0.02) -> None:
    """Poll predicate() until True or timeout. Fails the test on timeout."""
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        if predicate():
            return
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(f"timed out waiting for: {predicate}")
        await asyncio.sleep(interval)


async def _make_session_row(db, session_id: str, working_dir: str) -> None:
    """Insert a sessions row so the FK on bg_tasks is satisfied."""
    await db.save_session(
        session_id=session_id,
        name="t",
        working_dir=working_dir,
        created_at="2026-05-18T00:00:00Z",
    )


async def test_start_task_returns_immediately_and_completes(manager, db, tmp_path):
    await _make_session_row(db, "s1", str(tmp_path))
    rec = await manager.start_task(
        session_id="s1",
        command="echo hello world",
        working_dir=str(tmp_path),
        description="say hi",
    )
    assert rec.id
    assert rec.status == "running"
    assert (rec.idle_timeout_seconds, rec.timeout_seconds) == (60, 1800)

    await _wait_for(lambda: rec.id in [d.id for d in manager._delivered_])
    delivered = next(d for d in manager._delivered_ if d.id == rec.id)
    assert delivered.status == "completed"
    assert delivered.exit_code == 0
    assert "hello world" in delivered.stdout
    # DB row mirrors the in-memory record.
    row = await db.get_bg_task(rec.id)
    assert row["status"] == "completed"
    assert "hello world" in row["stdout"]


async def test_failing_command_records_failure(manager, db, tmp_path):
    await _make_session_row(db, "s1", str(tmp_path))
    rec = await manager.start_task(
        session_id="s1",
        command="exit 7",
        working_dir=str(tmp_path),
    )
    await _wait_for(lambda: any(d.id == rec.id for d in manager._delivered_))
    delivered = next(d for d in manager._delivered_ if d.id == rec.id)
    assert delivered.status == "failed"
    assert delivered.exit_code == 7


@pytest.mark.parametrize("idle, expected_status", [(0, "completed"), (1, "failed")])
async def test_task_selected_idle_limit_controls_a_normal_quiet_phase(
    manager, db, tmp_path, idle, expected_status
):
    await _make_session_row(db, "s1", str(tmp_path))
    rec = await manager.start_task(
        session_id="s1", command="echo started; sleep 1.6; echo finished",
        working_dir=str(tmp_path), idle_timeout_seconds=idle, timeout_seconds=5,
    )
    await _wait_for(lambda: bool(manager._delivered_))
    result = await manager.get_task(rec.id)
    assert result.status == expected_status
    assert result.idle_timeout_seconds == idle
    if idle:
        assert result.stop_reason == "idle_timeout"
        assert "finished" not in result.stdout
    else:
        assert result.stop_reason is None
        assert "finished" in result.stdout


async def test_total_timeout_still_stops_a_task_with_idle_disabled(manager, db, tmp_path):
    await _make_session_row(db, "s1", str(tmp_path))
    rec = await manager.start_task(
        session_id="s1", command="echo started; sleep 10; echo finished",
        working_dir=str(tmp_path), idle_timeout_seconds=0, timeout_seconds=1,
    )
    await _wait_for(lambda: bool(manager._delivered_))
    result = await manager.get_task(rec.id)
    assert result.status == "failed"
    assert result.stop_reason == "total_timeout"
    assert "started" in result.stdout and "finished" not in result.stdout
    assert "total_timeout" in render_delivery_prompt(result)


@pytest.mark.parametrize("notification_delay", [0.6, 1.5])
async def test_slow_started_notification_does_not_extend_total_runtime(
    manager, db, tmp_path, notification_delay
):
    await _make_session_row(db, "s1", str(tmp_path))
    delivered = []

    async def capture(record):
        delivered.append(record)

    async def slow_notification(event):
        if event["type"] == "bg_started":
            await asyncio.sleep(notification_delay)

    manager.bind(db=db, deliver_cb=capture, broadcast_cb=slow_notification)
    rec = await manager.start_task(
        session_id="s1", command="echo started; sleep 1.2; echo finished",
        working_dir=str(tmp_path), idle_timeout_seconds=0, timeout_seconds=1,
    )
    await _wait_for(lambda: bool(delivered))
    result = await manager.get_task(rec.id)
    assert (result.status, result.stop_reason) == ("failed", "total_timeout")
    assert "finished" not in result.stdout


@pytest.mark.parametrize("cause, expected_status", [
    ("user_cancel", "cancelled"), ("server_shutdown", "interrupted"),
    ("external_signal", "interrupted"),
])
async def test_task_reports_the_actual_interruption_source(
    manager, db, tmp_path, cause, expected_status
):
    import os
    import signal

    await _make_session_row(db, "s1", str(tmp_path))
    pid_file = tmp_path / "process.pid"
    rec = await manager.start_task(
        session_id="s1", command="echo $$ > process.pid; exec sleep 10",
        working_dir=str(tmp_path),
    )
    await _wait_for(pid_file.exists)
    if cause == "user_cancel":
        assert await manager.cancel_task(rec.id)
    elif cause == "server_shutdown":
        await manager.shutdown()
    else:
        os.killpg(int(pid_file.read_text()), signal.SIGTERM)
    await _wait_for(lambda: bool(manager._delivered_))
    result = await manager.get_task(rec.id)
    assert (result.status, result.stop_reason) == (expected_status, cause)
    assert cause in render_delivery_prompt(result)


async def test_cancel_reaps_a_descendant_that_ignores_sigterm(manager, db, tmp_path):
    import os
    import shlex
    import signal
    import sys

    await _make_session_row(db, "s1", str(tmp_path))
    child_code = (
        "import os,signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "open('child.pid','w').write(str(os.getpid())); time.sleep(30)"
    )
    parent_code = (
        f"import subprocess,sys,time; subprocess.Popen([sys.executable,'-c',{child_code!r}]); "
        "time.sleep(30)"
    )
    rec = await manager.start_task(
        session_id="s1", command=f"exec {shlex.quote(sys.executable)} -c {shlex.quote(parent_code)}",
        working_dir=str(tmp_path),
    )
    child_file = tmp_path / "child.pid"
    await _wait_for(child_file.exists)
    child_pid = int(child_file.read_text())

    def child_finished():
        try:
            return (Path(f"/proc/{child_pid}/stat").read_text().rsplit(")", 1)[1].split()[0] == "Z")
        except FileNotFoundError:
            return True

    from pathlib import Path

    try:
        assert await manager.cancel_task(rec.id)
        await _wait_for(child_finished, timeout=7)
        await _wait_for(lambda: bool(manager._delivered_))
        assert (await manager.get_task(rec.id)).stop_reason == "user_cancel"
    finally:
        if not child_finished():
            os.kill(child_pid, signal.SIGKILL)


async def test_an_unsent_signal_does_not_claim_a_user_cancellation(
    manager, db, tmp_path, monkeypatch
):
    import os

    await _make_session_row(db, "s1", str(tmp_path))
    rec = await manager.start_task(
        session_id="s1", command="touch ready; sleep 0.15; echo finished",
        working_dir=str(tmp_path),
    )
    await _wait_for((tmp_path / "ready").exists)

    def denied(*args):
        raise PermissionError("test signal denied")

    monkeypatch.setattr(os, "killpg", denied)
    assert not await manager.cancel_task(rec.id)
    await _wait_for(lambda: bool(manager._delivered_))
    result = await manager.get_task(rec.id)
    assert (result.status, result.stop_reason) == ("completed", None)


async def test_stderr_activity_keeps_a_stdout_quiet_task_running(manager, db, tmp_path):
    await _make_session_row(db, "s1", str(tmp_path))
    rec = await manager.start_task(
        session_id="s1",
        command="echo started; for n in 1 2 3 4; do echo tick >&2; sleep 0.4; done; echo finished",
        working_dir=str(tmp_path), idle_timeout_seconds=1, timeout_seconds=5,
    )
    await _wait_for(lambda: bool(manager._delivered_))
    result = await manager.get_task(rec.id)
    assert (result.status, result.stop_reason) == ("completed", None)
    assert "finished" in result.stdout and result.stderr.count("tick") == 4


async def test_idle_watchdog_terminates_proc_that_goes_silent(
    manager, db, tmp_path, monkeypatch
):
    """A command that produces output and then goes silent while
    still alive (the pytest-atexit hang we hit in the field) must
    be force-terminated by the idle watchdog rather than camp on
    the chip until the 30-min wall-clock timeout fires.

    Tightens IDLE_AFTER_OUTPUT_TIMEOUT_SECS so the test takes
    seconds instead of a minute.
    """
    import sys

    from server import bg_tasks as bgm

    monkeypatch.setattr(bgm, "IDLE_CHECK_INTERVAL_SECS", 1)

    await _make_session_row(db, "s1", str(tmp_path))
    # Print one line, then sleep — simulates the atexit-hang shape
    # (visible work done, process won't return).
    cmd = (
        f"{sys.executable} -c "
        "\"import sys, time; print('hello', flush=True); time.sleep(30)\""
    )
    start = asyncio.get_running_loop().time()
    rec = await manager.start_task(
        session_id="s1",
        command=cmd,
        working_dir=str(tmp_path),
        idle_timeout_seconds=2,
    )
    await _wait_for(
        lambda: any(d.id == rec.id for d in manager._delivered_),
        timeout=20.0,
    )
    elapsed = asyncio.get_running_loop().time() - start
    delivered = next(d for d in manager._delivered_ if d.id == rec.id)
    assert delivered.status == "failed", (
        f"idle watchdog should report a timeout failure, got {delivered.status!r} "
        f"(exit_code={delivered.exit_code})"
    )
    assert delivered.exit_code is not None and delivered.exit_code < 0
    # Should kill well before the 30s sleep completes.
    assert elapsed < 10.0, f"watchdog took too long: {elapsed:.1f}s"
    # The output we printed before going silent must be preserved.
    assert "hello" in delivered.stdout


async def test_idle_watchdog_does_not_fire_on_quiet_short_command(
    manager, db, tmp_path, monkeypatch
):
    """A command that never produces output (e.g. `sleep N`) must
    NOT be considered idle — the idle clock only starts after the
    first byte. Otherwise the watchdog would kill legitimate quiet
    tasks unfairly."""
    from server import bg_tasks as bgm

    monkeypatch.setattr(bgm, "IDLE_CHECK_INTERVAL_SECS", 1)

    await _make_session_row(db, "s1", str(tmp_path))
    # 3-second silent sleep — would be killed at ~1s if the watchdog
    # incorrectly treated "no output yet" as idle.
    rec = await manager.start_task(
        session_id="s1",
        command="sleep 3",
        working_dir=str(tmp_path),
        idle_timeout_seconds=1,
    )
    await _wait_for(
        lambda: any(d.id == rec.id for d in manager._delivered_),
        timeout=10.0,
    )
    delivered = next(d for d in manager._delivered_ if d.id == rec.id)
    assert delivered.status == "completed", (
        f"quiet command should complete naturally, got {delivered.status!r} "
        f"(exit_code={delivered.exit_code})"
    )
    assert delivered.exit_code == 0


async def test_external_sigterm_yields_interrupted_status(manager, db, tmp_path):
    """Externally SIGTERMing the bg process group (i.e. not via
    cancel_task / shutdown / timeout — simulating uvicorn --reload,
    systemd KillMode=control-group, or an outside `pkill`) must be
    surfaced as `interrupted`, NOT `failed`. The latter mis-labels
    "something killed us" as "the command itself failed", which is
    what we hit on pytest runs that completed successfully and then
    got SIGTERMed.
    """
    import os
    import signal as _signal

    await _make_session_row(db, "s1", str(tmp_path))
    rec = await manager.start_task(
        session_id="s1",
        command="sleep 30",
        working_dir=str(tmp_path),
    )
    # Wait until the manager actually has the proc tracked.
    await _wait_for(lambda: rec.id in manager._running)
    rt = manager._running[rec.id]
    pgid = os.getpgid(rt.proc.pid)
    # External signal — does NOT touch cancel_task / shutdown /
    # timeout. _run_task's flags stay False, so the new branch is the
    # only thing that can label this correctly.
    os.killpg(pgid, _signal.SIGTERM)

    await _wait_for(lambda: any(d.id == rec.id for d in manager._delivered_))
    delivered = next(d for d in manager._delivered_ if d.id == rec.id)
    assert delivered.status == "interrupted", (
        f"externally-signaled task should be 'interrupted', got {delivered.status!r} "
        f"(exit_code={delivered.exit_code})"
    )
    assert delivered.exit_code is not None and delivered.exit_code < 0


async def test_cancel_running_task(manager, db, tmp_path):
    await _make_session_row(db, "s1", str(tmp_path))
    rec = await manager.start_task(
        session_id="s1",
        command="sleep 30",
        working_dir=str(tmp_path),
    )
    # Give the proc a tick to be tracked.
    await asyncio.sleep(0.05)
    cancelled = await manager.cancel_task(rec.id)
    assert cancelled is True
    await _wait_for(lambda: any(d.id == rec.id for d in manager._delivered_))
    delivered = next(d for d in manager._delivered_ if d.id == rec.id)
    assert delivered.status == "cancelled"


async def test_cancel_unknown_task_returns_false(manager):
    assert await manager.cancel_task("no-such-id") is False


async def test_output_truncation(manager, db, tmp_path):
    await _make_session_row(db, "s1", str(tmp_path))
    # Generate >MAX_STREAM_BYTES (200 KB) of stdout. `yes` repeats
    # its arg + newline forever; `head -c` clips to the byte target.
    # Both are in coreutils on every Linux distro. (Avoid `python`
    # — this box has only `python3` on PATH — and avoid commands
    # that filter bytes since /dev/urandom + tr produces far less
    # than the raw input length.)
    target = MAX_STREAM_BYTES + 50_000
    rec = await manager.start_task(
        session_id="s1",
        command=f"yes 0123456789 | head -c {target}",
        working_dir=str(tmp_path),
    )
    await _wait_for(
        lambda: any(d.id == rec.id for d in manager._delivered_),
        timeout=10.0,
    )
    delivered = next(d for d in manager._delivered_ if d.id == rec.id)
    assert delivered.truncated is True, (
        f"expected truncated=True; got {delivered!r}"
    )
    # The marker prefix tells the model output was clipped.
    assert "truncated" in delivered.stdout
    assert len(delivered.stdout.encode("utf-8")) <= MAX_STREAM_BYTES


async def test_invalid_working_dir_raises(manager):
    with pytest.raises(BgTaskError):
        await manager.start_task(
            session_id="s1",
            command="echo hi",
            working_dir="/does/not/exist/anywhere",
        )


async def test_list_tasks_returns_most_recent_first(manager, db, tmp_path):
    await _make_session_row(db, "s1", str(tmp_path))
    a = await manager.start_task(
        session_id="s1", command="true", working_dir=str(tmp_path)
    )
    await _wait_for(lambda: any(d.id == a.id for d in manager._delivered_))
    # Small sleep so started_at differs deterministically.
    await asyncio.sleep(0.01)
    b = await manager.start_task(
        session_id="s1", command="true", working_dir=str(tmp_path)
    )
    await _wait_for(lambda: any(d.id == b.id for d in manager._delivered_))
    rows = await manager.list_tasks("s1")
    assert [r.id for r in rows[:2]] == [b.id, a.id]


async def test_broadcasts_started_and_completed(manager, db, tmp_path):
    await _make_session_row(db, "s1", str(tmp_path))
    rec = await manager.start_task(
        session_id="s1", command="true", working_dir=str(tmp_path)
    )
    await _wait_for(lambda: any(d.id == rec.id for d in manager._delivered_))
    types = [b["type"] for b in manager._broadcasts_]
    assert "bg_started" in types
    assert "bg_completed" in types
    started = next(b for b in manager._broadcasts_ if b["type"] == "bg_started")
    assert started["session_id"] == "s1"
    assert started["task_id"] == rec.id


async def test_in_flight_marked_interrupted_on_startup(db):
    """Simulate a row left in 'running' by a prior process; new
    manager.start() should flip it to 'interrupted' so the chip stops
    spinning forever."""
    await _make_session_row(db, "s1", "/tmp")
    await db.create_bg_task(
        task_id="orphan-1",
        session_id="s1",
        command="ls",
        description=None,
        working_dir="/tmp",
        started_at="2026-05-18T00:00:00Z",
    )
    mgr = BgTaskManager()
    mgr.bind(db=db, deliver_cb=_noop_deliver, broadcast_cb=_noop_broadcast)
    await mgr.start()
    row = await db.get_bg_task("orphan-1")
    assert row["status"] == "interrupted"
    assert row["completed_at"] is not None


async def test_restart_marks_only_orphans_and_keeps_old_limits_unknown(db):
    await _make_session_row(db, "s1", "/tmp")
    for task_id in ("legacy-finished", "legacy-running"):
        await db.create_bg_task(
            task_id=task_id, session_id="s1", command="true", description=None,
            working_dir="/tmp", started_at="2026-05-18T00:00:00Z",
        )
    await db.update_bg_task("legacy-finished", status="failed", exit_code=7)
    restarted = BgTaskManager()
    restarted.bind(db=db, deliver_cb=_noop_deliver, broadcast_cb=_noop_broadcast)
    await restarted.start()
    orphan = await restarted.get_task("legacy-running")
    history = await restarted.get_task("legacy-finished")
    assert (orphan.status, orphan.stop_reason) == ("interrupted", "server_restart")
    assert (history.status, history.stop_reason) == ("failed", None)
    assert orphan.idle_timeout_seconds is None and orphan.timeout_seconds is None
    assert history.idle_timeout_seconds is None and history.timeout_seconds is None
    assert "idle timeout: unknown" in render_delivery_prompt(history)


async def _noop_deliver(_rec):
    pass


async def _noop_broadcast(_msg):
    pass


# ---------------------------------------------------------------------------
# render_delivery_prompt
# ---------------------------------------------------------------------------


def test_render_delivery_prompt_includes_marker_and_status():
    rec = BgTaskRecord(
        id="abc123",
        session_id="s1",
        command="pytest tests/",
        description="run tests",
        working_dir="/tmp",
        status="completed",
        exit_code=0,
        stdout="3 passed",
        stderr="",
        truncated=False,
        started_at="2026-01-01T00:00:00Z",
        completed_at="2026-01-01T00:00:05Z",
    )
    text = render_delivery_prompt(rec)
    assert text.startswith("[bg-task-result]")
    assert "abc123" in text
    assert "completed" in text
    assert "3 passed" in text


def test_render_delivery_prompt_handles_truncation_and_stderr():
    rec = BgTaskRecord(
        id="x",
        session_id="s1",
        command="bad",
        description=None,
        working_dir="/tmp",
        status="failed",
        exit_code=1,
        stdout="",
        stderr="boom",
        truncated=True,
        started_at="2026-01-01T00:00:00Z",
        completed_at="2026-01-01T00:00:05Z",
    )
    text = render_delivery_prompt(rec)
    assert "truncated" in text.lower()
    assert "boom" in text


# ---------------------------------------------------------------------------
# REST endpoints
# ---------------------------------------------------------------------------


@pytest.fixture
async def api_client(tmp_path, monkeypatch):
    """In-memory DB + ASGI client + a manager wired in the same shape
    as production's lifespan. Mirrors test_attachments.client."""
    db = Database(":memory:")
    await db.initialize()
    session_manager.sessions.clear()
    session_manager._admission_closed = False
    await session_manager.initialize(db)

    # The subprocess is the agreed system boundary. Delivery still runs the
    # real session path, but no hermetic API check may launch a live model.
    import os

    spawn_process = asyncio.create_subprocess_exec

    async def isolated_process(program, *args, **kwargs):
        if os.path.basename(str(program)) in {"claude", "codex"}:
            raise FileNotFoundError("model CLI excluded from this hermetic test")
        return await spawn_process(program, *args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", isolated_process)

    # Mirror main.py lifespan: bind manager to the singleton used by
    # the router and broadcast through session_manager._broadcast.
    bg_task_manager.__init__()  # reset singleton state between tests
    bg_task_manager.bind(
        db=db,
        deliver_cb=session_manager.deliver_bg_result,
        broadcast_cb=session_manager._broadcast,
    )
    await bg_task_manager.start()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c, tmp_path

    await bg_task_manager.shutdown()
    await db.close()


async def test_rest_start_and_get_bg_task(api_client):
    client, wd = api_client
    sess = await session_manager.create_session(
        (await session_manager.db.get_system_agent())["id"], name="rest", working_dir=str(wd)
    )
    r = await client.post(
        f"/api/sessions/{sess.id}/bg-tasks",
        json={"command": "echo hello", "description": "say hi"},
        headers=HEADERS,
    )
    assert r.status_code == 201
    body = r.json()
    task_id = body["id"]
    assert body["status"] == "running"

    # Wait for the task to finish.
    for _ in range(100):
        g = await client.get(
            f"/api/sessions/{sess.id}/bg-tasks/{task_id}", headers=HEADERS
        )
        assert g.status_code == 200
        if g.json()["status"] != "running":
            break
        await asyncio.sleep(0.05)
    final = g.json()
    assert final["status"] == "completed"
    assert "hello" in final["stdout"]


async def test_rest_task_keeps_its_explicit_limits_in_details_and_results(api_client):
    client, wd = api_client
    # Observe the real background process without starting a model on delivery.
    delivered = []
    events = []

    async def capture(record):
        delivered.append(record)

    async def capture_event(event):
        events.append(event)

    bg_task_manager.bind(
        db=session_manager.db,
        deliver_cb=capture,
        broadcast_cb=capture_event,
    )
    sess = await session_manager.create_session(
        (await session_manager.db.get_system_agent())["id"],
        name="explicit limits", working_dir=str(wd),
    )
    response = await client.post(
        f"/api/sessions/{sess.id}/bg-tasks",
        json={"command": "echo started; sleep 0.05; echo finished",
              "idle_timeout_seconds": 0, "timeout_seconds": 2},
        headers=HEADERS,
    )
    assert response.status_code == 201
    started = response.json()
    assert (started["idle_timeout_seconds"], started["timeout_seconds"]) == (0, 2)
    await _wait_for(lambda: bool(delivered))
    details = await client.get(
        f"/api/sessions/{sess.id}/bg-tasks/{started['id']}", headers=HEADERS,
    )
    body = details.json()
    assert (body["idle_timeout_seconds"], body["timeout_seconds"]) == (0, 2)
    assert body["status"] == "completed"
    assert "finished" in body["stdout"]
    prompt = render_delivery_prompt(delivered[0])
    assert "idle timeout: disabled" in prompt
    assert "total timeout: 2s" in prompt
    assert [e["type"] for e in events] == ["bg_started", "bg_completed"]
    assert all((e["idle_timeout_seconds"], e["timeout_seconds"]) == (0, 2) for e in events)
    listing = await client.get(f"/api/sessions/{sess.id}/bg-tasks", headers=HEADERS)
    assert listing.json()[0] == body


INVALID_LIMITS = [
    ("idle_timeout_seconds", -1), ("idle_timeout_seconds", 0.5),
    ("idle_timeout_seconds", True), ("idle_timeout_seconds", "60"),
    ("idle_timeout_seconds", None), ("timeout_seconds", 0),
    ("timeout_seconds", -1), ("timeout_seconds", 0.5),
    ("timeout_seconds", False), ("timeout_seconds", "1800"),
    ("timeout_seconds", None),
    ("idle_timeout_seconds", 2**63), ("timeout_seconds", 2**63),
]


@pytest.mark.parametrize("field,value", INVALID_LIMITS)
async def test_rest_rejects_invalid_limits_before_starting_a_command(api_client, field, value):
    client, wd = api_client
    sess = await session_manager.create_session(
        (await session_manager.db.get_system_agent())["id"],
        name="invalid limits", working_dir=str(wd),
    )
    response = await client.post(
        f"/api/sessions/{sess.id}/bg-tasks",
        json={"command": "touch spawned", field: value}, headers=HEADERS,
    )
    assert response.status_code == 422
    assert not (wd / "spawned").exists()
    tasks = await client.get(f"/api/sessions/{sess.id}/bg-tasks", headers=HEADERS)
    assert tasks.json() == []


@pytest.mark.parametrize("field,value", INVALID_LIMITS)
async def test_manager_rejects_invalid_limits_before_starting_a_command(
    manager, db, tmp_path, field, value
):
    await _make_session_row(db, "s1", str(tmp_path))
    with pytest.raises(BgTaskError, match="integer"):
        await manager.start_task(
            session_id="s1", command="touch spawned", working_dir=str(tmp_path),
            **{field: value},
        )
    assert not (tmp_path / "spawned").exists()
    assert await manager.list_tasks("s1") == []


async def test_rest_rejects_empty_command(api_client):
    client, wd = api_client
    sess = await session_manager.create_session(
        (await session_manager.db.get_system_agent())["id"], name="rest", working_dir=str(wd)
    )
    r = await client.post(
        f"/api/sessions/{sess.id}/bg-tasks",
        json={"command": "  "},
        headers=HEADERS,
    )
    assert r.status_code == 400


async def test_rest_list(api_client):
    client, wd = api_client
    sess = await session_manager.create_session(
        (await session_manager.db.get_system_agent())["id"], name="rest", working_dir=str(wd)
    )
    await client.post(
        f"/api/sessions/{sess.id}/bg-tasks",
        json={"command": "true"},
        headers=HEADERS,
    )
    r = await client.get(f"/api/sessions/{sess.id}/bg-tasks", headers=HEADERS)
    assert r.status_code == 200
    assert len(r.json()) == 1


async def test_rest_cancel_running(api_client):
    client, wd = api_client
    sess = await session_manager.create_session(
        (await session_manager.db.get_system_agent())["id"], name="rest", working_dir=str(wd)
    )
    start = await client.post(
        f"/api/sessions/{sess.id}/bg-tasks",
        json={"command": "sleep 30"},
        headers=HEADERS,
    )
    task_id = start.json()["id"]
    # Give it a moment to enter `running` state.
    await asyncio.sleep(0.05)
    r = await client.post(
        f"/api/sessions/{sess.id}/bg-tasks/{task_id}/cancel", headers=HEADERS
    )
    assert r.status_code == 200
    assert r.json()["cancelled"] is True


async def test_rest_cancel_already_finished(api_client):
    client, wd = api_client
    sess = await session_manager.create_session(
        (await session_manager.db.get_system_agent())["id"], name="rest", working_dir=str(wd)
    )
    start = await client.post(
        f"/api/sessions/{sess.id}/bg-tasks",
        json={"command": "true"},
        headers=HEADERS,
    )
    task_id = start.json()["id"]
    # Let it complete.
    for _ in range(40):
        g = await client.get(
            f"/api/sessions/{sess.id}/bg-tasks/{task_id}", headers=HEADERS
        )
        if g.json()["status"] != "running":
            break
        await asyncio.sleep(0.05)
    r = await client.post(
        f"/api/sessions/{sess.id}/bg-tasks/{task_id}/cancel", headers=HEADERS
    )
    assert r.status_code == 200
    # Already finished → cancelled=False (no live handle).
    assert r.json()["cancelled"] is False


async def test_rest_session_required(api_client):
    client, _ = api_client
    r = await client.post(
        "/api/sessions/no-such/bg-tasks",
        json={"command": "true"},
        headers=HEADERS,
    )
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Cross-turn delivery integration
# ---------------------------------------------------------------------------


async def test_deliver_bg_result_starts_a_new_session_turn(api_client):
    """End-to-end: a completed bg task's deliver_cb runs, calls
    session_manager.deliver_bg_result → start_message → broadcasts a
    user_message with the [bg-task-result] marker. We don't run a real
    claude (no API key), so the turn errors out at backend.start time,
    but the user_message broadcast happens *before* the backend turn,
    which is what we're checking."""
    client, wd = api_client
    sess = await session_manager.create_session(
        (await session_manager.db.get_system_agent())["id"], name="delivery", working_dir=str(wd)
    )

    captured: list[dict] = []
    async def collect(msg):
        captured.append(msg)
    session_manager.on_broadcast("test", collect)

    try:
        rec = BgTaskRecord(
            id="bg-fake",
            session_id=sess.id,
            command="echo done",
            description="fake",
            working_dir=str(wd),
            status="completed",
            exit_code=0,
            stdout="done",
            stderr="",
            truncated=False,
            started_at="2026-01-01T00:00:00Z",
            completed_at="2026-01-01T00:00:05Z",
        )
        delivered = await session_manager.deliver_bg_result(rec)
        assert delivered is True

        # The user_message broadcast happens synchronously inside
        # send_message — before the backend spawn. Wait a tick for the
        # async task to push it through.
        for _ in range(50):
            if any(
                m.get("type") == "user_message"
                and isinstance(m.get("content"), str)
                and m["content"].startswith("[bg-task-result]")
                for m in captured
            ):
                break
            await asyncio.sleep(0.02)
        markers = [
            m for m in captured
            if m.get("type") == "user_message"
            and isinstance(m.get("content"), str)
            and m["content"].startswith("[bg-task-result]")
        ]
        assert len(markers) == 1
        assert "bg-fake" in markers[0]["content"]
    finally:
        session_manager.remove_broadcast("test")


async def test_deliver_bg_result_returns_false_for_missing_session():
    rec = BgTaskRecord(
        id="x",
        session_id="ghost",
        command="x",
        description=None,
        working_dir="/tmp",
        status="completed",
        exit_code=0,
        stdout="",
        stderr="",
        truncated=False,
        started_at="2026-01-01T00:00:00Z",
        completed_at="2026-01-01T00:00:00Z",
    )
    assert await session_manager.deliver_bg_result(rec) is False


# ---------------------------------------------------------------------------
# MCP tool surface
# ---------------------------------------------------------------------------


def _call(name: str, **kwargs):
    """Invoke a FastMCP-wrapped tool function directly."""
    from server.mcp_servers import bg as bg_mcp

    fn = getattr(bg_mcp, name)
    # FastMCP wraps; the underlying callable lives at .fn on newer
    # versions, while older versions expose the function directly.
    try:
        return fn(**kwargs)
    except TypeError:
        return fn.fn(**kwargs)  # type: ignore[attr-defined]


def test_mcp_bg_run_misconfigured(monkeypatch):
    monkeypatch.delenv("OCTOPUS_API_BASE", raising=False)
    monkeypatch.delenv("OCTOPUS_SESSION_ID", raising=False)
    monkeypatch.delenv("OCTOPUS_AUTH_TOKEN", raising=False)
    out = _call("bg_run", command="echo hi")
    assert "misconfigured" in out.lower()


def test_mcp_bg_run_rejects_empty(monkeypatch):
    monkeypatch.setenv("OCTOPUS_API_BASE", "http://x")
    monkeypatch.setenv("OCTOPUS_SESSION_ID", "s")
    monkeypatch.setenv("OCTOPUS_AUTH_TOKEN", "t")
    out = _call("bg_run", command="   ")
    assert "non-empty" in out


def test_mcp_bg_run_success(monkeypatch):
    monkeypatch.setenv("OCTOPUS_API_BASE", "http://x")
    monkeypatch.setenv("OCTOPUS_SESSION_ID", "s")
    monkeypatch.setenv("OCTOPUS_AUTH_TOKEN", "t")
    posted: dict = {}

    class FakeResp:
        status_code = 201
        text = ""

        def json(self):
            return {"id": "task-9", "description": posted.get("description")}

    def fake_post(url, json=None, headers=None, timeout=None):  # noqa: ARG001
        posted["url"] = url
        posted["description"] = (json or {}).get("description")
        posted["command"] = (json or {}).get("command")
        return FakeResp()

    import httpx

    monkeypatch.setattr(httpx, "post", fake_post)
    out = _call("bg_run", command="echo hi", description="say hi")
    assert "Started bg task `task-9`" in out
    assert posted["command"] == "echo hi"
    assert posted["description"] == "say hi"
    assert posted["url"].endswith("/api/sessions/s/bg-tasks")


def test_mcp_task_receipt_uses_the_host_confirmed_limits(monkeypatch):
    import httpx

    monkeypatch.setenv("OCTOPUS_API_BASE", "http://x")
    monkeypatch.setenv("OCTOPUS_SESSION_ID", "s")
    monkeypatch.setenv("OCTOPUS_AUTH_TOKEN", "t")
    posted = {}

    def post(url, *, json, headers, timeout):
        posted.update(json)
        return httpx.Response(201, json={"id": "quiet-tests", **json})

    monkeypatch.setattr(httpx, "post", post)
    receipt = _call(
        "bg_run", command="echo started; sleep 2", idle_timeout_seconds=0,
        timeout_seconds=10,
    )
    assert posted["idle_timeout_seconds"] == 0
    assert posted["timeout_seconds"] == 10
    assert "idle timeout: disabled" in receipt
    assert "total timeout: 10s" in receipt


@pytest.mark.parametrize("field,value", INVALID_LIMITS)
def test_mcp_rejects_invalid_limits_without_sending_a_request(monkeypatch, field, value):
    import httpx

    monkeypatch.setenv("OCTOPUS_API_BASE", "http://x")
    monkeypatch.setenv("OCTOPUS_SESSION_ID", "s")
    monkeypatch.setenv("OCTOPUS_AUTH_TOKEN", "t")

    def post(*args, **kwargs):
        pytest.fail("an invalid command must not reach the host")

    monkeypatch.setattr(httpx, "post", post)
    assert "integer" in _call("bg_run", command="touch spawned", **{field: value})


def test_mcp_bg_cancel_handles_404(monkeypatch):
    monkeypatch.setenv("OCTOPUS_API_BASE", "http://x")
    monkeypatch.setenv("OCTOPUS_SESSION_ID", "s")
    monkeypatch.setenv("OCTOPUS_AUTH_TOKEN", "t")

    class R:
        status_code = 404
        text = ""

    import httpx

    monkeypatch.setattr(httpx, "post", lambda *a, **k: R())
    out = _call("bg_cancel", task_id="t1")
    assert "No bg task" in out


def test_mcp_bg_list_empty(monkeypatch):
    monkeypatch.setenv("OCTOPUS_API_BASE", "http://x")
    monkeypatch.setenv("OCTOPUS_SESSION_ID", "s")
    monkeypatch.setenv("OCTOPUS_AUTH_TOKEN", "t")

    class R:
        status_code = 200

        def json(self):
            return []

    import httpx

    monkeypatch.setattr(httpx, "get", lambda *a, **k: R())
    out = _call("bg_list")
    assert "No background tasks" in out


def test_mcp_bg_list_summary(monkeypatch):
    monkeypatch.setenv("OCTOPUS_API_BASE", "http://x")
    monkeypatch.setenv("OCTOPUS_SESSION_ID", "s")
    monkeypatch.setenv("OCTOPUS_AUTH_TOKEN", "t")

    class R:
        status_code = 200

        def json(self):
            return [
                {
                    "id": "t1",
                    "status": "running",
                    "description": "one",
                    "exit_code": None,
                },
                {
                    "id": "t2",
                    "status": "completed",
                    "description": None,
                    "exit_code": 0,
                },
            ]

    import httpx

    monkeypatch.setattr(httpx, "get", lambda *a, **k: R())
    out = _call("bg_list")
    assert "t1" in out and "t2" in out
    assert "running" in out and "completed" in out


# ---------------------------------------------------------------------------
# build_args integration
# ---------------------------------------------------------------------------


def test_build_args_registers_bg_mcp_over_http_scoped_to_the_session(tmp_path):
    """The bg namespace reaches the model as an HTTP server this app hosts, not
    a subprocess to spawn (docs/plans/polish-2026-09.md §4 B1).

    Two things this pins. The key is still `bg`, because both CLIs build the
    tool name from it — change the key and `mcp__bg__run` silently becomes
    something else, breaking every prompt that names it. And the entry is scoped
    to *this* session: with one endpoint serving every session, the identity
    that used to arrive as OCTOPUS_SESSION_ID in a spawn environment now has to
    travel with the request.
    """
    from server.harness import RunConfig, get_harness
    from server.mcp_identity import verify

    run = get_harness("claude-code").create_run(RunConfig(session_id="sess-xyz"))
    argv, spawn = run.build_argv(
        prompt="hi", working_dir=str(tmp_path), resume_id=None, credential=None
    )
    cfg_idx = argv.index("--mcp-config") + 1
    cfg = json.loads(argv[cfg_idx])
    assert "bg" in cfg["mcpServers"], "the key is what the tool name is built from"

    entry = cfg["mcpServers"]["bg"]
    assert entry["type"] == "http"
    assert entry["url"].endswith("/mcp/bg/mcp")
    assert "command" not in entry and "args" not in entry, "nothing is spawned"

    bearer = entry["headers"]["Authorization"].removeprefix("Bearer ")
    scope = verify(bearer)
    assert scope is not None and scope.session_id == "sess-xyz"


def test_build_args_omits_session_env_when_none(tmp_path):
    from server.harness import RunConfig, get_harness

    run = get_harness("claude-code").create_run(RunConfig())  # no session_id
    argv, _ = run.build_argv(
        prompt="hi", working_dir=str(tmp_path), resume_id=None, credential=None
    )
    cfg = json.loads(argv[argv.index("--mcp-config") + 1])
    env = cfg["mcpServers"]["bg"]["env"]
    assert "OCTOPUS_SESSION_ID" not in env


def test_build_args_system_prompt_teaches_bg_usage(tmp_path):
    from server.harness import RunConfig, get_harness

    run = get_harness("claude-code").create_run(RunConfig(session_id="s"))
    argv, _ = run.build_argv(
        prompt="hi", working_dir=str(tmp_path), resume_id=None, credential=None
    )
    sp = argv[argv.index("--append-system-prompt") + 1]
    assert "mcp__bg__run" in sp
    # Bright-line rule (replaced the older "≥30s" heuristic): the
    # prompt now lists categories like test suites / builds /
    # installs that must always use bg_run.
    assert "Use bg_run unconditionally" in sp
