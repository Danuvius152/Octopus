"""Behavior regressions for review F10–F12, F16 and F20, using owned test data."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from server import app_agent as app_agent_mod
from server import app_backends
from server.app_agent import AppAgentError
from server.app_backends import RUNNING, STOPPED, BackendState, BackendSupervisor
from server.applications import ApplicationError
from server.config import settings
from server.harness import Harness
from server.research import manager as research_mod
from server.research.leaf import LeafResult
from server.research.manager import ResearchManager
from server.research.orchestrator import run_research
from server.sessions.runtime_types import SessionMailboxFull
from tests import test_app_agent as app_fixtures
from tests.test_applications import application_model
from tests.test_harness_core import _stream_profile

aam = app_fixtures.aam
am = app_fixtures.am
apps_root = app_fixtures.apps_root
db = app_fixtures.db
mgr = app_fixtures.mgr
turns = app_fixtures.turns
_make_app = app_fixtures._make_app


@pytest.mark.parametrize("rejection", ["forking", "mailbox", "closed"])
async def test_f10_build_refusal_restores_status_and_error(aam, am, db, mgr, turns, monkeypatch, rejection):
    row = await _make_app(am, db)
    await db.update_application(row["id"], status="ready", error="retained diagnostic")
    session = mgr.get_session(row["session_id"])
    if rejection == "forking":
        monkeypatch.setattr(mgr, "start_message", type(mgr).start_message.__get__(mgr))
        session._forking = True
    elif rejection == "mailbox":
        async def refuse(*args, **kwargs):
            raise SessionMailboxFull("Session command capacity is full")
        monkeypatch.setattr(mgr, "start_message", refuse)
    else:
        monkeypatch.setattr(mgr, "start_message", type(mgr).start_message.__get__(mgr))
        mgr.close_admission()
    try:
        with pytest.raises(ApplicationError) as error:
            await am.request_build(row["id"], "Change the application")
        assert error.value.status_code == 409
        current = await db.get_application(row["id"])
        assert (current["status"], current["error"]) == ("ready", "retained diagnostic")
        assert not mgr.session_is_executing(session)
        assert mgr.dispatch_snapshot(session.id)["items"] == []
    finally:
        session._forking = False


@pytest.mark.parametrize("cap", ["turns", "conversations"])
async def test_f11_concurrent_new_conversations_obey_app_caps(aam, am, db, mgr, turns, monkeypatch, cap):
    row = await _make_app(am, db)
    turns.emit = False
    monkeypatch.setattr(app_agent_mod, "MAX_ACTIVE_TURNS_PER_APP", 2 if cap == "turns" else 5)
    monkeypatch.setattr(app_agent_mod, "MAX_CONVERSATIONS_PER_APP", 2 if cap == "conversations" else 5)
    entered, release = asyncio.Event(), asyncio.Event()
    original_open = aam._open_conversation

    async def gated_open(*args, **kwargs):
        entered.set()
        await release.wait()
        return await original_open(*args, **kwargs)

    monkeypatch.setattr(aam, "_open_conversation", gated_open)
    tasks = [asyncio.create_task(aam.begin_turn(row, message=f"question {i}")) for i in range(3)]
    try:
        await asyncio.wait_for(entered.wait(), 3)
        await asyncio.sleep(0)
        release.set()
        results = await asyncio.gather(*tasks, return_exceptions=True)
        refused = [r for r in results if isinstance(r, AppAgentError)]
        assert len(refused) == 1 and refused[0].status_code == 429
        assert len(aam._turns) == 2
        assert len(aam.list_conversations(row["id"])) == 2
    finally:
        release.set()
        await asyncio.gather(*tasks, return_exceptions=True)


async def test_f11_cancelled_creation_releases_admission(aam, am, db, turns, monkeypatch):
    row = await _make_app(am, db)
    turns.emit = False
    monkeypatch.setattr(app_agent_mod, "MAX_ACTIVE_TURNS_PER_APP", 1)
    entered, release = asyncio.Event(), asyncio.Event()
    original_open = aam._open_conversation

    async def gated_open(*args, **kwargs):
        entered.set()
        await release.wait()
        return await original_open(*args, **kwargs)

    monkeypatch.setattr(aam, "_open_conversation", gated_open)
    task = asyncio.create_task(aam.begin_turn(row, message="cancel before creation"))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not aam._turns
    release.set()
    turn = await aam.begin_turn(row, message="use the released admission")
    assert aam._turns[turn.session_id] is turn


async def test_f10_concurrent_builds_recreate_only_one_deleted_build_session(am, db, mgr, turns, monkeypatch):
    row = await _make_app(am, db)
    await mgr.delete_session(row["session_id"])
    create = mgr.create_session
    created = []

    async def interleaved_create(*args, **kwargs):
        await asyncio.sleep(0)
        session = await create(*args, **kwargs)
        created.append(session.id)
        return session

    monkeypatch.setattr(mgr, "create_session", interleaved_create)
    turns.prompts.clear()
    await asyncio.gather(am.request_build(row["id"], "First change"), am.request_build(row["id"], "Second change"))
    assert len(created) == 1
    assert len(turns.prompts) == 2
    assert {sid for sid, _ in turns.prompts} == set(created)
    assert (await db.get_application(row["id"]))["session_id"] == created[0]


@pytest.mark.parametrize("consumer", ["timeout", "cancel", "sse_disconnect", "admission_cancel"])
async def test_f20_departed_consumer_keeps_execution_counted_until_completion(
    aam, am, db, mgr, monkeypatch, consumer,
):
    monkeypatch.setattr(settings, "max_concurrent_turns_per_user", 6)
    monkeypatch.setattr(app_agent_mod, "ASK_TIMEOUT_SECONDS", 0.04)
    monkeypatch.setattr(app_agent_mod, "MAX_ACTIVE_TURNS_PER_APP", 1)
    entered, release, starts = application_model(mgr, monkeypatch, entrypoint_exists=False)
    row = await _make_app(am, db)
    await asyncio.wait_for(entered.wait(), 3)
    release.set()
    await mgr.await_session_execution(row["session_id"], timeout=5)
    release.clear()
    entered.clear()
    starts.clear()
    response = None
    ack = asyncio.Event()
    try:
        if consumer == "timeout":
            with pytest.raises(AppAgentError) as error:
                await aam.ask(row, message="Keep working after the HTTP timeout")
            assert error.value.status_code == 504
        elif consumer == "sse_disconnect":
            turn = await aam.begin_turn(row, message="Keep working after SSE closes")
            response = aam.stream_turn(turn)
            assert (await anext(response))["type"] == "conversation"
            await response.aclose()
        else:
            if consumer == "admission_cancel":
                original_start = mgr.start_message
                admitted = asyncio.Event()

                async def delayed_ack(*args, **kwargs):
                    receipt = await original_start(*args, **kwargs)
                    admitted.set()
                    await ack.wait()
                    return receipt

                monkeypatch.setattr(mgr, "start_message", delayed_ack)
            waiting = asyncio.create_task(aam.ask(row, message="Keep working after cancellation"))
            if consumer == "admission_cancel":
                await asyncio.wait_for(admitted.wait(), 3)
            else:
                await asyncio.wait_for(entered.wait(), 3)
            waiting.cancel()
            ack.set()
            with pytest.raises(asyncio.CancelledError):
                await waiting
        await asyncio.wait_for(entered.wait(), 3)
        conversations = aam.list_conversations(row["id"])
        assert len(conversations) == 1 and conversations[0]["running"]
        turn = aam._turns[conversations[0]["id"]]
        assert mgr.session_is_executing(mgr.get_session(turn.session_id))
        assert not turn.consumer_attached and turn.queue.empty()
        assert turn.observer is not None and not turn.observer.done()
        with pytest.raises(AppAgentError) as refused:
            await aam.begin_turn(row, message="Over the real execution cap")
        assert refused.value.status_code == 429
        assert len(starts) == 1
        release.set()
        await mgr.await_session_execution(turn.session_id, timeout=5)
        await asyncio.wait_for(asyncio.shield(turn.observer), 5)
        assert not aam._turns
        assert not aam.list_conversations(row["id"])[0]["running"]
        assert len(starts) == 1
    finally:
        ack.set()
        release.set()
        if response is not None:
            await response.aclose()
        await mgr.drain_turns(timeout=5)


@pytest.mark.parametrize("surface", ["harness", "backend", "oneshot"])
@pytest.mark.parametrize("ignore_term", [False, True])
async def test_f12_stop_retires_redirected_children_after_parent_exit(tmp_path, monkeypatch, surface, ignore_term, short_process_shutdown):
    ready, writes = tmp_path / "child.pid", tmp_path / "writes"
    child = (
        "import os,signal,time\nfrom pathlib import Path\n"
        + ("signal.signal(signal.SIGTERM, signal.SIG_IGN)\n" if ignore_term else "")
        + f"Path({str(ready)!r}).write_text(str(os.getpid()))\n"
        + f"while True:\n with open({str(writes)!r}, 'ab') as f: f.write(b'x')\n time.sleep(0.005)\n"
    )
    parent = tmp_path / "parent.py"
    parent.write_text(
        "import subprocess,sys,time\nfrom pathlib import Path\n"
        f"subprocess.Popen([sys.executable, '-c', {child!r}], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
        f"while not Path({str(ready)!r}).exists(): time.sleep(.001)\n"
        "print('{\"type\":\"result\"}', flush=True)\n"
        + ("sys.stdin.buffer.read()\n" if surface == "harness" else "")
    )
    harness = Harness(replace(
        _stream_profile(),
        build_turn_argv=lambda ctx: ([sys.executable, str(parent)], {"cwd": str(tmp_path)}),
        build_oneshot_argv=lambda ctx: ([sys.executable, str(parent)], {"cwd": str(tmp_path)}),
        parse_oneshot_stdout=lambda text: text,
    ))
    proc = None
    try:
        if surface == "harness":
            run = harness.create_run()
            await run.start("test", str(tmp_path))
            proc = run._process
            async with asyncio.timeout(3):
                async for _ in run.stream():
                    pass
            await run.stop()
            assert run._process is None
        elif surface == "oneshot":
            from server.harness.profile import OneShotContext
            assert "result" in await harness.run_oneshot(OneShotContext(prompt="test"))
        else:
            monkeypatch.setattr(app_backends, "_TERM_TIMEOUT_SECONDS", 0.05)
            proc = await asyncio.create_subprocess_exec(
                sys.executable, str(parent), stdout=asyncio.subprocess.PIPE, start_new_session=True,
            )
            supervisor = BackendSupervisor()
            state = BackendState("review-app", str(tmp_path), state=RUNNING, process=proc, pid=proc.pid)
            supervisor._backends[state.app_id] = state
            await asyncio.wait_for(proc.stdout.readline(), 3)
            await asyncio.wait_for(proc.wait(), 3)
            await supervisor.stop(state.app_id)
            assert state.process is None and state.state == STOPPED
        child_pid = int(ready.read_text())
        stat = Path(f"/proc/{child_pid}/stat")
        assert not stat.exists() or stat.read_text().rsplit(")", 1)[1].split()[0] in {"Z", "X"}
        size = writes.stat().st_size
        await asyncio.sleep(0.04)
        assert writes.stat().st_size == size, "retired child kept writing"
    finally:
        if proc is not None:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await proc.wait()
        if ready.exists():
            try:
                os.kill(int(ready.read_text()), signal.SIGKILL)
            except ProcessLookupError:
                pass


async def test_f20_sequential_timeouts_respect_default_four_execution_limit(aam, am, db, mgr, monkeypatch):
    monkeypatch.setattr(settings, "max_concurrent_turns_per_user", 6)
    monkeypatch.setattr(app_agent_mod, "ASK_TIMEOUT_SECONDS", 0.04)
    entered, release, starts = application_model(mgr, monkeypatch, entrypoint_exists=False)
    row = await _make_app(am, db)
    await asyncio.wait_for(entered.wait(), 3)
    release.set()
    await mgr.await_session_execution(row["session_id"], timeout=5)
    release.clear()
    starts.clear()
    try:
        for index in range(4):
            with pytest.raises(AppAgentError) as error:
                await aam.ask(row, message=f"Timed out original request {index}")
            assert error.value.status_code == 504
        with pytest.raises(AppAgentError) as refused:
            await aam.ask(row, message="The fifth request must not start")
        assert refused.value.status_code == 429
        assert len(starts) == 4 and len(aam._turns) == 4
        assert all(row["running"] for row in aam.list_conversations(row["id"]))
        observers = [turn.observer for turn in aam._turns.values()]
        release.set()
        await mgr.drain_turns(timeout=5)
        await asyncio.gather(*observers)
        assert not aam._turns
        assert len(starts) == 4
    finally:
        release.set()
        await mgr.drain_turns(timeout=5)


@pytest.mark.parametrize("failure", ["auth_expired", "reasoning leaf timed out", "empty", "retirement_failure"])
async def test_f16_failed_synthesis_is_failed_and_retains_gathered_evidence(
    am, db, mgr, monkeypatch, failure,
):
    phases, events, injected = [], [], []

    async def reason(prompt):
        if "Decompose" in prompt:
            return LeafResult(text='["one angle"]')
        if failure == "retirement_failure":
            raise RuntimeError("One-shot process retirement remains unconfirmed")
        return LeafResult(text="   " if failure == "empty" else "", error=None if failure == "empty" else failure)

    async def search(prompt):
        if "Fact-check" in prompt:
            return LeafResult(text='{"refuted":false,"verified":true}')
        return LeafResult(text='[{"claim":"Retained finding","url":"https://example.invalid/source"}]')

    async def pipeline(question, *, on_progress=None, **kwargs):
        async def progress(p):
            phases.append(p.phase)
            if on_progress:
                await on_progress(p)
        return await run_research(question, reason=reason, search=search, on_progress=progress, **kwargs)

    async def no_injection(*args, **kwargs):
        injected.append((args, kwargs))
        raise AssertionError("failed research must not inject a success report")

    monkeypatch.setattr(research_mod, "run_research", pipeline)
    monkeypatch.setattr(mgr, "start_message", no_injection)
    rm = ResearchManager()
    rm.bind(session_mgr=mgr, db=db)
    mgr.on_broadcast("review-research", lambda event: _capture_event(events, event))
    agent = await db.get_system_agent()
    session = await mgr.create_session(agent["id"], "Research failure")
    try:
        row = await rm.start(session.id, "A research question")
        if failure == "retirement_failure":
            with pytest.raises(RuntimeError, match="retirement remains unconfirmed"):
                await rm._tasks[row["id"]]
            assert row["id"] in rm._tasks and row["id"] in rm._contexts
        else:
            await rm._tasks[row["id"]]
        saved = await db.get_research_job(row["id"])
        assert saved["status"] == "failed" and saved["phase"] == "synthesize"
        assert ("retirement remains unconfirmed" if failure == "retirement_failure" else "Report synthesis failed") in saved["error"]
        assert saved["report_path"] is None
        assert phases == ["scope", "search", "verify", "synthesize"]
        assert any(event["type"] == "research_failed" for event in events)
        assert not any(event["type"] == "research_completed" for event in events)
        assert not injected
        if failure != "retirement_failure":
            evidence = Path(research_mod._research_dir(session.user_id)) / row["id"] / "synthesis-failure.json"
            retained = json.loads(evidence.read_text())
            assert retained["findings"][0]["claim"] == "Retained finding"
            assert retained["sources"] == ["https://example.invalid/source"]
    finally:
        if failure == "retirement_failure":
            with pytest.raises(RuntimeError, match="retirement remains unconfirmed"):
                await rm.shutdown()
        else:
            await rm.shutdown()


async def _capture_event(events, event):
    events.append(event)
