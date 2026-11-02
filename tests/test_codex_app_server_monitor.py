"""Native accounting through the original Owner and public Monitor reports."""

import asyncio
import os
import threading
from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from server import monitor
from server.config import settings
from server.monitor import MetricsStore, query
from server.routers.monitor import router
from server.sessions.client_submissions import submit_client_message
from tests import native_app_server as native_tests
from tests.accounts import create_account
from tests.native_app_server import (
    ROOT,
    child_end,
    child_start,
    inject,
    item,
    start_output,
    terminal,
    until,
)
from tests.session_runtime import wait_dispatch_idle

native_runtime = native_tests.native_runtime


@pytest.fixture
async def metrics(tmp_path, monkeypatch, request):
    from server.monitor import store as store_module

    monkeypatch.setattr(store_module, "_QUEUE_MAX", getattr(request, "param", 4096))
    (tmp_path / "native-metrics-enabled").touch()
    store = MetricsStore(str(tmp_path / "metrics.db"))
    await store.initialize()
    monkeypatch.setattr(monitor, "monitor", store)
    try:
        yield store
    finally:
        await store.stop()


def response(thread, turn, identifier, output):
    return {"__rollout": {"thread": thread, "type": "token_usage_record", "payload": {
        "thread_id": thread, "turn_id": turn, "response_id": identifier,
        "usage": {"input_tokens": 100, "cached_input_tokens": 25,
                  "output_tokens": output, "reasoning_output_tokens": 2,
                  "total_tokens": 100 + output}}}}


async def test_response_identity_counts_children_and_followup_without_counting_snapshots(
    native_runtime, metrics, tmp_path,
):
    manager, database, session, _, _, events = native_runtime
    receipt = await start_output(native_runtime)
    root = response(ROOT, "turn-1", "response-root", 10)
    child = response("child", "first", "response-child", 7)
    snapshot = {"method": "thread/tokenUsage/updated", "params": {
        "threadId": ROOT, "turnId": "old-history-turn", "tokenUsage": {
            "total": {"totalTokens": 1000}, "last": {"totalTokens": 100},
            "modelContextWindow": 100000}}}
    inject(tmp_path, snapshot, child_start("child", "first"), root, root,
           child, child, child_end("child", "first"), child_start("child", "second"),
           response("child", "second", "response-child", 3), child_end("child", "second"),
           item(ROOT, "turn-1", "answer", "Final answer"), terminal())
    await wait_dispatch_idle(manager, session.id)
    await metrics.drain()
    overview = await query.overview(metrics.conn)
    assert sum(row["responses"] for row in overview["codex"]) == 3
    assert sum(row["output_tokens"] for row in overview["codex"]) == 20
    missing = next(row for row in overview["codex"] if row["model"] == "unknown")
    assert missing["weighted_tps"] is None and missing["coverage_pct"] == 0
    assert {row["request_id"] for row in overview["codex_turns"]} == {receipt["request_id"]}
    assert (await database.read_session_result(session.id, receipt["request_id"])).fragments == ("Final answer",)
    assert not any(event.get("type") == "model_measurement" for event in events)


async def test_cold_resume_does_not_recount_completed_responses(native_runtime, metrics, tmp_path):
    manager, _, session, _, _, _ = native_runtime
    await start_output(native_runtime)
    inject(tmp_path, response(ROOT, "turn-1", "first", 10), terminal())
    await wait_dispatch_idle(manager, session.id)
    (tmp_path / "native-injections.jsonl").unlink()
    await submit_client_message(manager, session.id, "Continue", [], "next", wiki_mode=None)
    await until(lambda: session._run.backend is not None and session._run.backend.active_turn is not None)
    inject(tmp_path, response(ROOT, "turn-2", "second", 20), {"method": "turn/completed", "params": {
        "threadId": ROOT, "turn": {"id": "turn-2", "status": "completed"}}})
    await wait_dispatch_idle(manager, session.id)
    await metrics.drain()
    rows = await query.codex(metrics.conn)
    assert sum(row["responses"] for row in rows) == 2
    assert sum(row["output_tokens"] for row in rows) == 30
    runs = await query.codex_runs(metrics.conn)
    assert {row["preparation_mode"] for row in runs} == {"new", "resume"}
    assert all(row["spawn_ms"] >= 0 and row["metrics_collection_complete"] == 1 for row in runs)


async def test_response_before_delayed_start_ack_uses_the_eventually_accepted_root(
    native_runtime, metrics, tmp_path, monkeypatch,
):
    from server.harness import codex_app_server
    from tests.native_app_server import wire_frames

    monkeypatch.setattr(codex_app_server, "_RPC_TIMEOUT", 3)
    manager, database, session, mode, _, _ = native_runtime
    mode[0] = "control_delayed_ack"
    receipt = await submit_client_message(manager, session.id, "Delayed proof", [], "delayed-proof", wiki_mode=None)
    await until(lambda: (tmp_path / "native-business.jsonl").exists())
    request, = [frame for frame in wire_frames(tmp_path) if frame.get("method") == "turn/start"]
    progress = item(ROOT, "turn-1", "progress-before-ack", "Generated before acceptance proof")
    progress["params"]["item"]["phase"] = "commentary"
    inject(tmp_path, progress, response(ROOT, "turn-1", "completed-before-ack", 13),
           {"__rollout": {"thread": ROOT, "type": "response_item", "payload": {"type": "function_call_output"}}})
    # The provider has completed; allow an ordinary metadata poll while the
    # original start waiter is still open, without manufacturing a root proof.
    await asyncio.sleep(.35)
    inject(tmp_path, {"id": request["id"], "result": {"turn": {"id": "turn-1"}}},
           item(ROOT, "turn-1", "final", "Accepted root answer"), terminal())
    await wait_dispatch_idle(manager, session.id)
    result = await database.read_session_result(session.id, receipt["request_id"])
    assert result.outcome.status == "success"
    await metrics.drain()
    normal = await query.codex_turns(metrics.conn)
    assert sum(row["output_tokens"] for row in normal) == 13, normal
    assert not await query.codex_anomalies(metrics.conn)


async def test_existing_child_observer_waits_for_followup_turn_association(
    native_runtime, metrics, tmp_path, monkeypatch,
):
    from server.harness.codex_activity import CodexActivityObserver

    observed = asyncio.Event()
    attach = CodexActivityObserver.set_event_handler

    def attach_barrier(observer, handler):
        def receive(events):
            if any(event.raw.get("native_turn_id") == "followup" for event in events):
                observed.set()
            return handler(events)
        attach(observer, receive)

    monkeypatch.setattr(CodexActivityObserver, "set_event_handler", attach_barrier)
    manager, database, session, _, _, _ = native_runtime
    receipt = await start_output(native_runtime)
    inject(tmp_path, child_start("child", "first"), response("child", "first", "first-response", 7),
           item("child", "first", "first-answer", "First child answer"), child_end("child", "first"))
    async with asyncio.timeout(3):
        while not await query.codex_turns(metrics.conn):
            await metrics.drain()
            await asyncio.sleep(.01)
    # The same source is already watching. Only the rollout announces the
    # next round; its native protocol notification is deliberately held back.
    inject(tmp_path,
        {"__rollout": {"thread": "child", "type": "event_msg",
                       "payload": {"type": "task_started", "turn_id": "followup"}}},
        {"__rollout": {"thread": "child", "type": "response_item",
                       "payload": {"type": "message", "role": "assistant"}}},
        response("child", "followup", "second-response", 13),
        {"__rollout": {"thread": "child", "type": "response_item",
                       "payload": {"type": "function_call_output"}}})
    await asyncio.wait_for(observed.wait(), 3)
    inject(tmp_path, child_start("child", "followup"), child_end("child", "followup"),
           item(ROOT, "turn-1", "answer", "Root answer"), terminal())
    await wait_dispatch_idle(manager, session.id)
    assert (await database.read_session_result(session.id, receipt["request_id"])).outcome.status == "success"
    await metrics.drain()
    normal = await query.codex_turns(metrics.conn)
    assert sum(row["output_tokens"] for row in normal) == 20, normal
    assert sum(row["child_responses"] for row in normal) == 2
    assert not await query.codex_anomalies(metrics.conn)


async def test_unassociated_usage_capacity_loss_does_not_change_business_result(
    native_runtime, metrics, tmp_path, monkeypatch,
):
    from server.harness import codex_app_server
    from server.harness.codex_activity import CodexActivityObserver
    from tests.native_app_server import wire_frames

    monkeypatch.setattr(codex_app_server, "_RPC_TIMEOUT", 3)
    monkeypatch.setattr(codex_app_server, "_METRICS_PENDING_COUNT", 2)
    observed = asyncio.Event()
    attach = CodexActivityObserver.set_event_handler

    def attach_barrier(observer, handler):
        def receive(events):
            if any(event.raw.get("response_id") == "pending-7" for event in events):
                observed.set()
            return handler(events)
        attach(observer, receive)

    monkeypatch.setattr(CodexActivityObserver, "set_event_handler", attach_barrier)
    manager, database, session, mode, _, _ = native_runtime
    mode[0] = "control_delayed_ack"
    receipt = await submit_client_message(manager, session.id, "Delayed bounded usage", [], "bounded-usage", wiki_mode=None)
    await until(lambda: (tmp_path / "native-business.jsonl").exists())
    request, = [frame for frame in wire_frames(tmp_path) if frame.get("method") == "turn/start"]
    for index in range(8):
        inject(tmp_path, response(ROOT, "turn-1", f"pending-{index}", 1),
               {"__rollout": {"thread": ROOT, "type": "response_item", "payload": {"type": "function_call_output"}}})
    await asyncio.wait_for(observed.wait(), 3)
    inject(tmp_path, {"id": request["id"], "result": {"turn": {"id": "turn-1"}}},
           item(ROOT, "turn-1", "answer", "Still completes"), terminal())
    await wait_dispatch_idle(manager, session.id)
    result = await database.read_session_result(session.id, receipt["request_id"])
    assert result.outcome.status == "success" and result.fragments == ("Still completes",)
    await metrics.drain()
    report, = await query.codex_runs(metrics.conn)
    assert report["resource_state"] == "proven" and report["metrics_capacity_drops"] == 6
    assert sum(row["output_tokens"] for row in await query.codex(metrics.conn)) == 2


async def test_completed_response_usage_survives_unfinished_child_and_abnormal_root(
    native_runtime, metrics, tmp_path,
):
    manager, _, session, _, _, _ = native_runtime
    await start_output(native_runtime)
    inject(tmp_path, child_start(), response("child", "child-turn", "partial-child", 7),
           {"method": "turn/started", "params": {"threadId": ROOT,
                "turn": {"id": "unaccepted-root", "status": "inProgress"}}},
           response(ROOT, "unaccepted-root", "abnormal", 10),
           {"method": "turn/completed", "params": {"threadId": ROOT,
                "turn": {"id": "unaccepted-root", "status": "completed"}}})
    await wait_dispatch_idle(manager, session.id)
    await metrics.drain()
    rows = await query.codex(metrics.conn)
    assert sum(row["responses"] for row in rows) == 1
    assert sum(row["output_tokens"] for row in rows) == 7
    anomalous, = await query.codex_anomalies(metrics.conn)
    assert anomalous["native_turn_id"] == "unaccepted-root" and anomalous["output_tokens"] == 10


async def test_native_monitor_separates_provider_counters_from_attempt_phases(native_runtime, metrics, tmp_path, monkeypatch):
    manager, database, session, _, _, _ = native_runtime
    receipt = await start_output(native_runtime)
    snapshot = {"method": "thread/tokenUsage/updated", "params": {
        "threadId": ROOT, "turnId": "turn-1", "tokenUsage": {
            "total": {"inputTokens": 900, "cachedInputTokens": 400, "outputTokens": 90,
                      "reasoningOutputTokens": 60, "totalTokens": 990},
            "last": {"inputTokens": 100, "outputTokens": 10}, "modelContextWindow": 100000}}}
    inject(tmp_path, snapshot, snapshot, child_start(), response("child", "child-turn", "child", 7),
           item(ROOT, "turn-1", "final", "Root answer"), terminal())
    await wait_dispatch_idle(manager, session.id)
    await metrics.drain()
    _, token = await create_account(database, username="monitor-operator")
    monkeypatch.setattr(settings, "metrics_db_path", str(tmp_path / "metrics.db"))
    app = FastAPI()
    app.include_router(router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test",
                           headers={"Authorization": "Bearer " + token}) as client:
        report = await client.get("/api/monitor/overview")
        assert report.status_code == 200
        overview = report.json()
    run, = overview["codex_runs"]
    assert run["request_id"] == receipt["request_id"]
    assert run["root_turns"] == 1 and run["child_turns"] == 1 and run["resource_state"] == "proven"
    assert run["preparation_ms"] >= 0 and run["root_active_ms"] >= 0 and run["retirement_ms"] >= 0
    phases = {row["phase"]: row for row in overview["codex_retirement"]}
    assert phases["protocol_interrupt"]["grace_ms"] == 500
    assert phases["graceful_exit"]["grace_ms"] == 2000
    assert phases["pipe_drain"]["grace_ms"] == 2000
    assert all(row["request_id"] == receipt["request_id"] and row["elapsed_ms"] >= 0
               for row in phases.values())
    counter, = overview["codex_native_usage"]
    assert counter["total_tokens"] == 990 and counter["output_tokens"] == 90
    assert sum(row["output_tokens"] for row in overview["codex"]) == 7


@pytest.mark.parametrize("metrics", [4], indirect=True)
async def test_metrics_pressure_does_not_fill_business_output(native_runtime, metrics, tmp_path, monkeypatch):
    from server.harness import run as run_module

    monkeypatch.setattr(run_module, "_EVENT_BUFFER_COUNT", 2)
    manager, database, session, _, _, _ = native_runtime
    receipt = await start_output(native_runtime)
    inject(tmp_path, *(response(ROOT, "turn-1", f"response-{i}", 10) for i in range(32)),
           item(ROOT, "turn-1", "answer", "Still completes"), terminal())
    await wait_dispatch_idle(manager, session.id)
    assert metrics.dropped > 0
    assert (await database.read_session_result(session.id, receipt["request_id"])).outcome.status == "success"


async def test_usage_notifications_and_rollout_polls_cannot_extend_idle(native_runtime, metrics, tmp_path, monkeypatch):
    manager, _, session, mode, starts, _ = native_runtime
    mode[0] = "control_output"
    monkeypatch.setattr(settings, "turn_idle_timeout_seconds", 0)
    receipt = await manager.start_message(session.id, "Keep the original idle clock")
    await until(lambda: session._run.backend is not None and session._run.backend.active_turn is not None)
    monkeypatch.setattr(settings, "turn_idle_timeout_seconds", .15)
    progress = item(ROOT, "turn-1", "progress", "Work started")
    progress["params"]["item"]["phase"] = "commentary"
    inject(tmp_path, progress)
    for index in range(8):
        inject(tmp_path, response(ROOT, "turn-1", f"response-{index}", 10),
               {"method": "thread/tokenUsage/updated", "params": {"threadId": ROOT,
                "turnId": "turn-1", "tokenUsage": {"total": {"totalTokens": 10}, "last": {}}}})
        await asyncio.sleep(.04)
    completion = await asyncio.wait_for(receipt.wait(), 3)
    assert completion.outcome.status == "failed" and completion.outcome.code == "turn_timeout"
    assert len(starts) == 1


async def test_unsolicited_root_before_business_start_keeps_only_diagnostic_usage(native_runtime, metrics, tmp_path):
    manager, database, session, mode, _, _ = native_runtime
    mode[0] = "early_root"
    receipt = await manager.start_message(session.id, "Never sent")
    completion = await asyncio.wait_for(receipt.wait(), 3)
    assert completion.outcome.status == "failed"
    assert not (tmp_path / "native-business.jsonl").exists()
    await metrics.drain()
    diagnostic, = await query.codex_anomalies(metrics.conn)
    assert diagnostic["scope"] == "unbound" and diagnostic["output_tokens"] == 9
    assert await query.codex(metrics.conn) == []


async def test_refused_foreign_round_of_known_child_is_diagnostic(native_runtime, metrics, tmp_path):
    manager, _, session, _, _, _ = native_runtime
    await start_output(native_runtime)
    foreign = child_start("child", "foreign")
    foreign["params"]["turn"]["rootTurnId"] = "another-root"
    inject(tmp_path, child_start("child", "accepted"), response("child", "accepted", "ok", 7),
           child_end("child", "accepted"), foreign, response("child", "foreign", "foreign", 11))
    await wait_dispatch_idle(manager, session.id)
    await metrics.drain()
    assert sum(row["output_tokens"] for row in await query.codex(metrics.conn)) == 7
    diagnostic, = await query.codex_anomalies(metrics.conn)
    assert diagnostic["native_turn_id"] == "foreign" and diagnostic["output_tokens"] == 11


async def test_normal_model_ttft_excludes_anomalous_and_unbound_turns(metrics):
    from server.monitor import Event

    metrics.record(Event(kind="codex_response", duration_ms=1000, detail={
        "model": "shared", "output_tokens": 10, "scope": "root", "response_id": "normal",
        "native_thread_id": ROOT, "native_turn_id": "normal"}))
    for scope, latency in (("root", 20), ("anomalous_root", 9000), ("unbound", 99000)):
        metrics.record(Event(kind="codex_turn_latency", duration_ms=latency, detail={
            "model": "shared", "scope": scope, "native_thread_id": ROOT, "native_turn_id": scope}))
    await metrics.drain()
    normal, = await query.codex(metrics.conn)
    assert normal["avg_turn_ttft_ms"] == 20


@pytest.mark.parametrize("phase", ["active", "preparation"])
async def test_native_retirement_cutoff_includes_a_stalled_supplementary_file_read(native_runtime, metrics, tmp_path, monkeypatch, phase):
    from server.harness import codex_activity, codex_app_server

    monkeypatch.setattr(codex_app_server, "_METRICS_CLOSE_GRACE", .05, raising=False)
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    read = os.read
    source = tmp_path / "codex-home" / "sessions" / f"rollout-{ROOT}.jsonl"

    def hold_source(fd, size):
        if Path(f"/proc/self/fd/{fd}").resolve() == source:
            entered.set()
            try:
                assert release.wait(3), "test did not release its supplementary file read"
            finally:
                finished.set()
        return read(fd, size)

    manager, database, session, _, _, _ = native_runtime
    if phase == "preparation":
        monkeypatch.setattr(codex_activity.os, "read", hold_source)
    receipt = await start_output(native_runtime)
    run = session._run.backend
    monkeypatch.setattr(codex_activity.os, "read", hold_source)
    waiting = asyncio.create_task(manager.await_session_execution(session.id, timeout=4))
    try:
        await until(entered.is_set)
        inject(tmp_path, item(ROOT, "turn-1", "answer", "Saved before collection cutoff"), terminal())
        await asyncio.wait_for(asyncio.shield(waiting), .6)
        assert run.retirement_state == "proven" and not release.is_set()
        result = await database.read_session_result(session.id, receipt["request_id"])
        assert result.outcome.status == "success"
        await metrics.drain()
        report, = await query.codex_runs(metrics.conn)
        assert report["metrics_collection_complete"] == 0
    finally:
        release.set()
        await waiting
        assert await asyncio.to_thread(finished.wait, 1)


@pytest.mark.parametrize("failure", ["close", "finish"])
async def test_supplementary_observer_failure_closes_other_sources_without_changing_exit_proof(
    native_runtime, metrics, tmp_path, monkeypatch, failure,
):
    from server.harness import codex_app_server
    from server.harness.codex_activity import CodexActivityObserver

    closed = []

    class FailingSource(CodexActivityObserver):
        def __init__(self, *args, **kwargs):
            self.declared_thread = kwargs["native_thread_id"]
            super().__init__(*args, **kwargs)

        async def close(self, **kwargs):
            complete = await super().close(**kwargs)
            closed.append(self.declared_thread)
            if self.declared_thread == ROOT and failure == "close":
                raise OSError("source close failed after descriptor retirement")
            return complete

        def finish_metrics(self):
            if self.declared_thread == ROOT and failure == "finish":
                raise ValueError("supplementary response finalization unavailable")
            return super().finish_metrics()

    monkeypatch.setattr(codex_app_server, "CodexActivityObserver", FailingSource)
    manager, database, session, _, _, _ = native_runtime
    receipt = await start_output(native_runtime)
    run = session._run.backend
    inject(tmp_path, child_start(), response("child", "child-turn", "child", 7),
           item(ROOT, "turn-1", "answer", "Actual root result"), terminal())
    await wait_dispatch_idle(manager, session.id)
    assert set(closed) == {ROOT, "child"}
    assert run.retirement_state == "proven"
    assert (await database.read_session_result(session.id, receipt["request_id"])).outcome.status == "success"
    await metrics.drain()
    report, = await query.codex_runs(metrics.conn)
    assert report["resource_state"] == "proven" and report["metrics_collection_complete"] == 0
