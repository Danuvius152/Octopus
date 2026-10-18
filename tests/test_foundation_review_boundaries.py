"""HTTP admission and process-retirement boundaries found by review."""

import asyncio
import os
import shlex
import signal
import sys
import time
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from server import bg_tasks
from server.main import app
from server.session_manager import session_manager
from tests import test_tenant_isolation as tenants
from tests import test_wiki_api as wiki_api
from tests.test_runtime_review_races import background as background
from tests.test_runtime_review_races import gate_sql, process_alive, wait_until
from tests.test_runtime_review_races import runtime as runtime
from tests.test_wiki_maintenance_state import (
    current_read,
    entry,
    prepare,
    state,
    submission,
)

two_accounts = tenants.two_accounts
client = wiki_api.client


@pytest.mark.parametrize("phase", ["pending", "failed"])
async def test_foreign_unarchive_does_not_reveal_unconfirmed_retirement(two_accounts, monkeypatch, phase):
    ctx = two_accounts
    owner, other = ctx["archer"], ctx["vera"]
    identifier = owner["agent"]["id"]
    entered, release = asyncio.Event(), asyncio.Event()

    async def held_retirement(agent_id):
        assert agent_id == identifier
        entered.set()
        await release.wait()
        if phase == "failed":
            raise RuntimeError("synthetic retirement remains unconfirmed")

    monkeypatch.setattr(session_manager, "evict_agent_sessions", held_retirement)
    closing = asyncio.create_task(session_manager.archive_agent(identifier))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        if phase == "failed":
            release.set()
            with pytest.raises(RuntimeError, match="unconfirmed"):
                await closing
        headers = tenants._as(other["token"])
        missing = await ctx["client"].post("/api/agents/nonexistent/unarchive", headers=headers)
        foreign = await ctx["client"].post(f"/api/agents/{identifier}/unarchive", headers=headers)
        assert foreign.status_code == missing.status_code == 404
        assert foreign.json() == missing.json()
        assert identifier in session_manager._closed_agents
        assert (await ctx["db"].get_agent(identifier))["archived"] is False
        own = await ctx["client"].post(
            f"/api/agents/{identifier}/unarchive", headers=tenants._as(owner["token"]),
        )
        assert own.status_code == 409
    finally:
        release.set()
        await asyncio.gather(closing, return_exceptions=True)


@pytest.mark.parametrize("reason", ["archived", "closing", "missing_credential", "provider", "foreign_credential", "foreign_agent"])
async def test_import_reports_expected_admission_rejections_without_side_effects(two_accounts, reason):
    ctx = two_accounts
    owner, other = ctx["archer"], ctx["vera"]
    identifier = owner["agent"]["id"]
    payload = {"agent_id": identifier, "backend": "codex", "name": "Rejected import",
               "messages": [{"type": "text", "role": "user", "content": "Do not accept partial input", "seq": 0}]}
    expected = 400
    if reason == "archived":
        await ctx["db"].archive_agent(identifier)
    elif reason == "closing":
        session_manager._closed_agents.add(identifier)
    elif reason in {"provider", "foreign_credential"}:
        await ctx["db"].save_credential(
            "import-credential", "claude-code" if reason == "provider" else "codex", "Synthetic",
            "api_key", "not-a-real-secret", "2026-10-17T00:00:00Z",
            user_id=owner["user"]["id"] if reason == "provider" else other["user"]["id"],
        )
        payload["credential_id"] = "import-credential"
        if reason == "foreign_credential":
            expected = 404
    elif reason == "missing_credential":
        payload["credential_id"] = "missing"
        expected = 404
    else:
        payload["agent_id"] = other["agent"]["id"]
        expected = 404
    before = await ctx["db"].load_sessions(include_archived=True)
    before_live = set(session_manager.sessions)
    async with AsyncClient(transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test") as http:
        response = await http.post("/api/sessions/import", headers=tenants._as(owner["token"]), json=payload)
    assert response.status_code == expected, response.text
    assert await ctx["db"].load_sessions(include_archived=True) == before
    assert set(session_manager.sessions) == before_live


async def test_import_storage_failure_remains_a_server_error(two_accounts, monkeypatch):
    async def unavailable(*args, **kwargs):
        raise OSError("synthetic import storage outage")

    monkeypatch.setattr(session_manager, "import_session", unavailable)
    owner = two_accounts["archer"]
    async with AsyncClient(transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test") as http:
        response = await http.post("/api/sessions/import", headers=tenants._as(owner["token"]),
                                   json={"agent_id": owner["agent"]["id"], "backend": "codex"})
    assert response.status_code == 500


async def test_frontmatter_diagnostic_is_shared_by_reads_and_checked_maintenance(client, tmp_path):
    vault, _config, jobs = await prepare(client, tmp_path, {
        "Library/good.md": "# Healthy\nReadable unrelated evidence.\n",
        "Library/bad.md": "---\ntitle: Known metadata\n---\n# Original\n",
    })
    with entry() as (headers, _task):
        observed = await state(client, headers=headers)
        good = next(item for item in observed["items"] if item["current"]["path"] == "Library/good.md")
        assert observed["ready"] is True
        malformed = "---\nx_wiki: &cycle {self: *cycle}\n---\n# Current bad metadata\n"
        (vault / "Library/bad.md").write_text(malformed)
        bad = await current_read(client, "Library/bad.md")
        assert bad.status_code == 200
        assert bad.json()["metadata"]["frontmatter_error"]
        assert bad.json()["metadata"]["frontmatter_retained_from"]
        good_read = await current_read(client, "Library/good.md")
        found = await client.get("/api/wiki/search", headers=wiki_api.HEADERS, params={"query": "Healthy"})
        info = await client.get("/api/wiki/info", headers=wiki_api.HEADERS)
        assert all(response.json()["index_status"] == "partial" for response in (bad, good_read, found, info))
        assert found.json()["items"] and good_read.json()["content"].startswith("# Healthy")
        observed_bad = await state(client, "Library/bad.md", headers=headers)
        assert observed_bad["index_status"] == "partial" and observed_bad["ready"] is False
        assert observed_bad["items"] == []
        refused = await client.post("/api/wiki/maintain", headers=headers, json=submission(good))
        assert refused.status_code == 503, refused.text
        assert jobs.list()["items"] == []
        assert (vault / "Library/bad.md").read_text() == malformed
        (vault / "Library/bad.md").write_text("---\ntitle: Repaired\n---\n# Repaired\n")
        assert (await current_read(client, "Library/bad.md")).json()["index_status"] == "ready"
        assert (await state(client, headers=headers))["ready"] is True


@pytest.mark.parametrize("cancel_count", [1, 2])
async def test_cancelled_registration_waits_for_a_group_that_closed_its_pipes(background, runtime, monkeypatch, tmp_path, cancel_count):
    manager, session, delivered, broadcasts = background
    _, database, _, _, _, _ = runtime
    entered, _ = gate_sql(monkeypatch, database, lambda sql: sql.startswith("INSERT INTO bg_tasks"))
    monkeypatch.setattr(bg_tasks, "_TERM_GRACE_SECONDS", 0.2)
    child_pid = tmp_path / "closed-pipe-child.pid"
    child = ("import os,signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); "
             f"open({str(child_pid)!r},'w').write(str(os.getpid())); time.sleep(30)")
    parent = ("import subprocess,sys,time; "
              f"subprocess.Popen([sys.executable,'-c',{child!r}],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); "
              "time.sleep(30)")
    creation = asyncio.create_task(manager.start_task(
        session_id=session.id, working_dir=str(tmp_path),
        command="exec " + shlex.join([sys.executable, "-c", parent]), idle_timeout_seconds=0,
    ))
    pid = None
    try:
        await asyncio.wait_for(entered.wait(), 5)
        await wait_until(child_pid.exists)
        pid = int(child_pid.read_text())
        creation.cancel()
        if cancel_count == 2:
            captured = next(iter(manager._running.values()))
            await wait_until(lambda: captured.termination is not None)
            creation.cancel()
            await asyncio.sleep(0)
            assert not creation.done(), "second cancellation abandoned captured cleanup"
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(creation, 8)
        assert not process_alive(pid)
        assert await manager.list_tasks(session.id) == []
        assert delivered == broadcasts == []
    finally:
        creation.cancel()
        await asyncio.gather(creation, return_exceptions=True)
        if pid is not None and process_alive(pid):
            os.kill(pid, signal.SIGKILL)


async def test_unconfirmed_group_shutdown_keeps_owner_and_withholds_terminal(background, monkeypatch, tmp_path):
    manager, session, delivered, broadcasts = background
    monkeypatch.setattr(bg_tasks, "_TERM_GRACE_SECONDS", 0.2)
    monkeypatch.setattr(bg_tasks, "_STARTUP_DRAIN_SECONDS", 0.01)
    monkeypatch.setattr(bg_tasks, "_ORCHESTRATION_DRAIN_SECONDS", 0.01)
    ready = tmp_path / "shutdown.ready"
    code = ("import signal,time; from pathlib import Path; "
            "signal.signal(signal.SIGTERM,signal.SIG_IGN); "
            f"Path({str(ready)!r}).write_text('ready'); time.sleep(30)")
    record = await manager.start_task(
        session_id=session.id, working_dir=str(tmp_path),
        command="exec " + shlex.join([sys.executable, "-c", code]), idle_timeout_seconds=0,
    )
    captured = manager._running[record.id]
    try:
        await wait_until(ready.exists)
        with pytest.raises(RuntimeError, match="shutdown incomplete"):
            await manager.shutdown()
        assert manager._running[record.id] is captured
        assert captured.task is not None and not captured.task.done()
        assert (await manager.get_task(record.id)).status == "running"
        assert delivered == []
        assert not [event for event in broadcasts if event["type"] == "bg_completed"]
    finally:
        assert captured.task is not None
        await asyncio.wait_for(asyncio.shield(captured.task), 3)
        await manager.shutdown()
    assert not process_alive(captured.proc.pid)
    assert len(delivered) == 1 and delivered[0].status == "interrupted"


async def test_fork_after_a_process_snapshot_cannot_cancel_the_kill_fallback(background, runtime, monkeypatch, tmp_path):
    manager, session, delivered, broadcasts = background
    _, database, _, _, _, _ = runtime
    entered, _ = gate_sql(monkeypatch, database, lambda sql: sql.startswith("INSERT INTO bg_tasks"))
    monkeypatch.setattr(bg_tasks, "_TERM_GRACE_SECONDS", 0.2)
    ready, fork_now, child_pid = (tmp_path / name for name in ("parent.ready", "fork.now", "late-child.pid"))
    code = "\n".join([
        "import os,signal,time",
        "from pathlib import Path",
        "def mark(path):",
        "    target=Path(path); temporary=target.with_suffix('.tmp')",
        "    temporary.write_text(str(os.getpid())); temporary.replace(target)",
        "def fork_after_term(*_):",
        f"    while not Path({str(fork_now)!r}).exists(): time.sleep(.001)",
        "    child=os.fork()",
        "    if child == 0:",
        "        signal.signal(signal.SIGTERM,signal.SIG_IGN)",
        "        os.close(1); os.close(2)",
        f"        mark({str(child_pid)!r})",
        "        time.sleep(30)",
        "    os._exit(0)",
        "signal.signal(signal.SIGTERM,fork_after_term)",
        f"mark({str(ready)!r})",
        "time.sleep(30)",
    ])
    creation = asyncio.create_task(manager.start_task(
        session_id=session.id, working_dir=str(tmp_path),
        command="exec " + shlex.join([sys.executable, "-c", code]), idle_timeout_seconds=0,
    ))
    actual_iterdir, actual_killpg = Path.iterdir, os.killpg
    snapshot = None
    snapshot_used = killed = False
    pid = None
    try:
        await asyncio.wait_for(entered.wait(), 5)
        await wait_until(ready.exists)
        parent = int(ready.read_text())
        pgid = os.getpgid(parent)

        def held_group_probe(group, sig):
            nonlocal snapshot, killed
            result = actual_killpg(group, sig)
            if group == pgid and sig == signal.SIGKILL:
                killed = True
            if group == pgid and sig == 0 and snapshot is None:
                # Fix the exact interleaving: enumerate old PIDs, then allow
                # the TERM handler to fork a new member and let its parent exit.
                snapshot = list(actual_iterdir(Path("/proc")))
                fork_now.write_text("fork")
                deadline = time.monotonic() + 2
                while not child_pid.exists() or process_alive(parent):
                    if time.monotonic() > deadline:
                        raise TimeoutError("controlled late fork did not finish")
                    time.sleep(.001)
            return result

        def stale_members(path):
            nonlocal snapshot_used
            if path == Path("/proc") and snapshot is not None and not snapshot_used and not killed:
                snapshot_used = True
                return iter(snapshot)
            return actual_iterdir(path)

        monkeypatch.setattr(bg_tasks.os, "killpg", held_group_probe)
        monkeypatch.setattr(Path, "iterdir", stale_members)
        creation.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(creation, 8)
        assert snapshot is not None and child_pid.exists()
        pid = int(child_pid.read_text())
        assert not process_alive(pid), "late same-group child outlived cancellation"
        assert delivered == broadcasts == []
        assert await manager.list_tasks(session.id) == []
    finally:
        fork_now.touch()
        creation.cancel()
        await asyncio.gather(creation, return_exceptions=True)
        if child_pid.exists():
            pid = int(child_pid.read_text())
        if pid is not None and process_alive(pid):
            os.kill(pid, signal.SIGKILL)


@pytest.mark.skipif(sys.platform != "linux", reason="Linux procfs thread state")
async def test_zombie_leader_with_a_live_thread_is_not_a_retired_group(tmp_path):
    ready = tmp_path / "thread.ready"
    code = "\n".join([
        "import ctypes, threading, time",
        "from pathlib import Path",
        "def worker():",
        f"    Path({str(ready)!r}).write_text('ready')",
        "    time.sleep(30)",
        "threading.Thread(target=worker).start()",
        "ctypes.CDLL(None).pthread_exit(None)",
    ])
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-c", code, start_new_session=True,
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
    )
    leader = Path(f"/proc/{process.pid}/stat")

    def zombie_with_threads():
        fields = leader.read_text().rsplit(")", 1)[1].split()
        return fields[0] == "Z" and int(fields[17]) > 1

    try:
        await wait_until(lambda: ready.exists() and zombie_with_threads())
        # This is a real Linux Z leader with a live worker, sampled before
        # KILL. It proves the state distinction, not a real uninterruptible
        # worker surviving KILL (which cannot safely be induced here).
        assert bg_tasks._group_has_live_members_after_kill(process.pid)
    finally:
        os.killpg(process.pid, signal.SIGKILL)
        await asyncio.wait_for(process.wait(), 3)


@pytest.mark.parametrize("thread_count", ["2", "unreadable"])
async def test_uncertain_thread_exit_keeps_shutdown_owner_and_withholds_terminal(
    background, monkeypatch, tmp_path, thread_count,
):
    manager, session, delivered, broadcasts = background
    monkeypatch.setattr(bg_tasks, "_TERM_GRACE_SECONDS", 0.03)
    monkeypatch.setattr(bg_tasks, "_STARTUP_DRAIN_SECONDS", 0.01)
    monkeypatch.setattr(bg_tasks, "_ORCHESTRATION_DRAIN_SECONDS", 0.01)
    ready = tmp_path / "thread-evidence.ready"
    code = ("import signal,time; from pathlib import Path; "
            "signal.signal(signal.SIGTERM,signal.SIG_IGN); "
            f"Path({str(ready)!r}).write_text('ready'); time.sleep(30)")
    record = await manager.start_task(
        session_id=session.id, working_dir=str(tmp_path),
        command="exec " + shlex.join([sys.executable, "-c", code]), idle_timeout_seconds=0,
    )
    captured = manager._running[record.id]
    await wait_until(ready.exists)
    actual_read, actual_getpgid = Path.read_text, os.getpgid
    actual_exists = bg_tasks._group_exists
    sampled = asyncio.Event()
    release = False

    def held_stat(path, *args, **kwargs):
        if not release and path == Path(f"/proc/{captured.pgid}/stat"):
            fields = ["Z", "1", str(captured.pgid), *(["0"] * 14), thread_count]
            sampled_loop.call_soon_threadsafe(sampled.set)
            return f"{captured.pgid} (synthetic post-KILL evidence) " + " ".join(fields)
        return actual_read(path, *args, **kwargs)

    def held_members(path):
        if not release and path == Path("/proc"):
            return iter([Path(f"/proc/{captured.pgid}")])
        return actual_iterdir(path)

    sampled_loop = asyncio.get_running_loop()
    actual_iterdir = Path.iterdir
    monkeypatch.setattr(Path, "read_text", held_stat)
    monkeypatch.setattr(Path, "iterdir", held_members)
    monkeypatch.setattr(bg_tasks.os, "getpgid", lambda pid: captured.pgid if not release and pid == captured.pgid else actual_getpgid(pid))
    monkeypatch.setattr(bg_tasks, "_group_exists", lambda pgid: True if not release and pgid == captured.pgid else actual_exists(pgid))
    try:
        with pytest.raises(RuntimeError, match="shutdown incomplete"):
            await manager.shutdown()
        await asyncio.wait_for(sampled.wait(), 2)
        # Keep the evidence unresolved across event-loop turns. The real
        # SIGKILL has run; only its completion evidence is fault-injected.
        await asyncio.sleep(0.03)
        assert manager._running[record.id] is captured
        assert captured.termination is not None and not captured.termination.done()
        assert captured.task is not None and not captured.task.done()
        assert (await manager.get_task(record.id)).status == "running"
        assert delivered == []
        assert not [event for event in broadcasts if event["type"] == "bg_completed"]
    finally:
        release = True
        assert captured.task is not None
        await asyncio.wait_for(asyncio.shield(captured.task), 3)
        await manager.shutdown()
    assert len(delivered) == 1 and delivered[0].status == "interrupted"
