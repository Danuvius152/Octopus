"""Context observations through the real Owner, SQLite and native process seam."""

import asyncio
import sqlite3
from contextlib import closing

import pytest

from server.agent_manager import AgentManager
from server.database import Database
from server.models import AgentUpdate
from server.routers import agents as agent_routes
from server.routers.sessions import get_session
from server.sessions import SessionManager
from server.sessions.presentation import session_info
from server.sessions.runtime_types import SessionOwnerUnavailable
from tests import native_app_server as native_tests
from tests.native_app_server import ROOT, inject, item, start_output, terminal, until
from tests.session_runtime import wait_dispatch_idle

native_runtime = native_tests.native_runtime


async def context_when(manager, session, predicate):
    async with asyncio.timeout(3):
        while True:
            state = (await get_session(manager, session.id, user_id=session.user_id)).context_state
            if state is not None and predicate(state):
                return state
            await asyncio.sleep(0)


def usage(used=250, capacity=1000, *, thread=ROOT, turn="turn-1"):
    return {"method": "thread/tokenUsage/updated", "params": {
        "threadId": thread, "turnId": turn, "tokenUsage": {
            "total": {"totalTokens": 90000, "inputTokens": 89000, "outputTokens": 1000,
                "cachedInputTokens": 0, "cacheWriteInputTokens": 0, "reasoningOutputTokens": 0},
            "last": {"totalTokens": used, "inputTokens": used, "outputTokens": 0,
                "cachedInputTokens": 0, "cacheWriteInputTokens": 0, "reasoningOutputTokens": 0},
            "modelContextWindow": capacity,
        },
    }}


def compact_item(identifier, *, completed=False):
    return {"method": "item/completed" if completed else "item/started", "params": {
        "threadId": ROOT, "turnId": "turn-1", "item": {"id": identifier, "type": "contextCompaction"},
    }}


async def test_root_current_context_survives_detail_and_restart(native_runtime, tmp_path):
    manager, database, session, _, _, events = native_runtime
    receipt = await start_output(native_runtime)
    inject(tmp_path, usage(), usage(900, thread="child"), usage(800, turn="replayed-turn"),
           item(ROOT, "turn-1", "final", "Done"), terminal())
    await wait_dispatch_idle(manager, session.id)
    completion = await database.read_session_result(session.id, receipt["request_id"])
    assert completion.outcome.status == "success"
    detail = await get_session(manager, session.id, user_id=session.user_id)
    state = detail.context_state
    assert state.validity == "valid"
    assert state.sample.used_tokens == 250 and state.sample.capacity_tokens == 1000
    assert state.sample.source.request_id == receipt["request_id"]
    assert state.sample.native_session_id == ROOT
    assert state.sample.basis == "codex_last_total"
    assert [event["context_state"] for event in events if event["type"] == "context_state"][-1] == state.model_dump()
    assert "context_state" not in session_info(manager.list_sessions()[0]).model_dump()
    restarted = SessionManager()
    try:
        await restarted.initialize(database, previous_writers_retired=True)
        restored = await get_session(restarted, session.id, user_id=session.user_id)
        assert restored.context_state == state
    finally:
        await restarted.close_session_owners()


async def test_default_roundtrip_invalidates_and_late_attempt_cannot_restore(native_runtime, tmp_path):
    manager, database, session, _, _, _ = native_runtime
    await start_output(native_runtime)
    inject(tmp_path, usage())
    before = await context_when(manager, session, lambda state: state.validity == "valid")
    dispatch = manager.dispatch_snapshot(session.id)
    agents = AgentManager(database)
    original = await database.get_agent(session.agent_id)
    await agents.update_agent(session.agent_id, user_id=session.user_id, model="different-model")
    await agents.update_agent(session.agent_id, user_id=session.user_id, model=original["model"])
    detail = await get_session(manager, session.id, user_id=session.user_id)
    assert detail.context_pending or detail.context_state.validity == "stale"
    stale = await context_when(manager, session, lambda state: state.validity == "stale")
    assert stale.validity == "stale" and stale.invalidation.reason == "configuration_changed"
    assert stale.binding_revision > before.binding_revision
    assert stale.binding_target.agent_settings_revision == original["context_settings_revision"] + 2
    assert (await get_session(manager, session.id, user_id=session.user_id)).context_state == stale
    assert manager.dispatch_snapshot(session.id) == dispatch
    inject(tmp_path, usage(300), item(ROOT, "turn-1", "final", "Done"), terminal())
    await wait_dispatch_idle(manager, session.id)
    after = (await get_session(manager, session.id, user_id=session.user_id)).context_state
    assert after == stale


@pytest.mark.parametrize("mode", ["busy", "busy_changed", "failed"])
async def test_valid_context_diagnostic_read_does_not_wait_for_its_writer(native_runtime, tmp_path, monkeypatch, mode):
    manager, database, session, _, _, _ = native_runtime
    await start_output(native_runtime)
    inject(tmp_path, usage())
    before = await context_when(manager, session, lambda state: state.validity == "valid")
    entered, release = asyncio.Event(), asyncio.Event()
    record = database.record_terminal

    async def hold_terminal(**kwargs):
        entered.set()
        await release.wait()
        return await record(**kwargs)

    if mode != "failed":
        monkeypatch.setattr(database, "record_terminal", hold_terminal)
    inject(tmp_path, item(ROOT, "turn-1", "final", "Done"), terminal())
    if mode == "failed":
        await wait_dispatch_idle(manager, session.id)
        read = database.has_delivery_notice

        async def fail_after_read(*args):
            await read(*args)
            raise RuntimeError("Original context writer failed")

        monkeypatch.setattr(database, "has_delivery_notice", fail_after_read)
        with pytest.raises(SessionOwnerUnavailable):
            await manager.retain_result_notice(session.id, "failed-context-writer", "Retained diagnostic")
    else:
        await asyncio.wait_for(entered.wait(), 2)
        if mode == "busy_changed":
            await AgentManager(database).update_agent(session.agent_id, session.user_id, model="new-context-model")
    try:
        detail = await asyncio.wait_for(get_session(manager, session.id, user_id=session.user_id), 2)
        assert detail.context_state == before
        assert detail.context_pending == (mode != "busy")
    finally:
        release.set()
        if mode == "failed":
            assert await manager.delete_session(session.id)
        else:
            await wait_dispatch_idle(manager, session.id)
    if mode == "busy_changed":
        aligned = await context_when(manager, session, lambda state: state.validity == "stale")
        assert aligned.revision > before.revision
        assert not (await get_session(manager, session.id, user_id=session.user_id)).context_pending


@pytest.mark.parametrize("recomputed", [False, True])
async def test_automatic_compaction_installs_only_its_recomputed_sample(native_runtime, tmp_path, recomputed):
    manager, database, session, _, _, _ = native_runtime
    await start_output(native_runtime)
    inject(tmp_path, usage())
    before = await context_when(manager, session, lambda state: state.validity == "valid")
    inject(tmp_path, compact_item("compact-1"))
    updating = await context_when(manager, session, lambda state: state.activity == "compacting")
    assert updating.sample == before.sample
    candidate = usage(100)
    if recomputed:
        candidate["params"]["tokenUsage"]["last"].update(inputTokens=0, outputTokens=0,
            cachedInputTokens=0, reasoningOutputTokens=0)
    inject(tmp_path, candidate, compact_item("compact-1", completed=True))
    installed = await context_when(manager, session,
        lambda state: state.activity is None and state.revision > updating.revision)
    # Rate-limit updates resend the current usage without taking a new sample.
    inject(tmp_path, candidate, item(ROOT, "turn-1", "final", "Original answer"), terminal())
    await wait_dispatch_idle(manager, session.id)
    state = (await get_session(manager, session.id, user_id=session.user_id)).context_state
    assert state == installed
    assert state.activity is None
    assert state.validity == ("valid" if recomputed else "stale")
    if recomputed:
        assert state.sample.used_tokens == 100 and state.sample.basis == "codex_recomputed_estimate"
    else:
        assert state.invalidation.reason == "compacted" and state.sample == before.sample
    detail = await get_session(manager, session.id, user_id=session.user_id)
    assert len([message for message in detail.messages if message.type == "result"]) == 1


async def test_restart_invalidates_a_possible_writer_only_once(native_runtime, tmp_path):
    manager, _, session, _, _, _ = native_runtime
    await start_output(native_runtime)
    inject(tmp_path, usage())
    before = await context_when(manager, session, lambda state: state.validity == "valid")
    # A consistent backup while native work is running reproduces the actual
    # crash window without fabricating an execution or input-evidence row.
    copy = tmp_path / "crashed.db"
    with closing(sqlite3.connect(tmp_path / "product.db")) as source, closing(sqlite3.connect(copy)) as target:
        source.backup(target)
    await manager.interrupt(session.id)
    await wait_dispatch_idle(manager, session.id)
    database = Database(str(copy))
    await database.initialize()
    restarted = SessionManager()
    try:
        await restarted.initialize(database, previous_writers_retired=True)
        state = (await get_session(restarted, session.id, user_id=session.user_id)).context_state
        assert state.validity == "stale" and state.invalidation.reason == "operation_unconfirmed"
        assert state.revision > before.revision
        await restarted.initialize(database, previous_writers_retired=True)
        assert (await get_session(restarted, session.id, user_id=session.user_id)).context_state == state
        receipt = await restarted.start_message(session.id, "New observation after recovery", source="human")
        live = restarted.get_session(session.id)
        await until(lambda: live._run.backend is not None and live._run.backend.active_turn is not None)
        finish = terminal()
        finish["params"]["turn"]["id"] = "turn-2"
        inject(tmp_path, usage(150, turn="turn-2"), item(ROOT, "turn-2", "new-final", "Recovered"), finish)
        assert (await asyncio.wait_for(receipt.wait(), 4)).outcome.status == "success"
        await wait_dispatch_idle(restarted, session.id)
        fresh = (await get_session(restarted, session.id, user_id=session.user_id)).context_state
        assert fresh.validity == "valid" and fresh.sample.used_tokens == 150
        await restarted.initialize(database, previous_writers_retired=True)
        assert (await get_session(restarted, session.id, user_id=session.user_id)).context_state == fresh
    finally:
        await database.close()


@pytest.mark.parametrize("change", ["session_roundtrip", "rename", "reroute", "empty_defaults"])
async def test_live_changes_preserve_only_applicable_context(native_runtime, tmp_path, change):
    manager, database, session, _, _, _ = native_runtime
    await start_output(native_runtime)
    inject(tmp_path, usage())
    before = await context_when(manager, session, lambda state: state.validity == "valid")
    if change == "session_roundtrip":
        await manager.update_session_configuration(session.id, {"model": "another-model"}, session.user_id)
        await manager.update_session_configuration(session.id, {"model": None}, session.user_id)
    elif change == "rename":
        await AgentManager(database).update_agent(session.agent_id, session.user_id, name="Renamed agent")
        await manager.update_session_configuration(session.id, {"name": "Renamed session"}, session.user_id)
    elif change == "empty_defaults":
        await AgentManager(database).update_agent(session.agent_id, session.user_id, model="")
    else:
        inject(tmp_path, {"method": "model/rerouted", "params": {"threadId": ROOT, "turnId": "turn-1",
            "fromModel": "fixture", "toModel": "another-model", "reason": "modelAvailability"}})
        await context_when(manager, session, lambda state: state.validity == "stale")
    state = (await get_session(manager, session.id, user_id=session.user_id)).context_state
    unchanged = change in {"rename", "empty_defaults"}
    assert (state == before) == unchanged
    if not unchanged:
        assert state.validity == "stale"
        inject(tmp_path, usage(300))
    inject(tmp_path, item(ROOT, "turn-1", "final", "Done"), terminal())
    await wait_dispatch_idle(manager, session.id)
    assert (await get_session(manager, session.id, user_id=session.user_id)).context_state == state


async def test_unarchive_aligns_defaults_before_returning_a_live_session(native_runtime, tmp_path):
    manager, database, session, _, starts, _ = native_runtime
    await start_output(native_runtime)
    inject(tmp_path, usage(), item(ROOT, "turn-1", "final", "Done"), terminal())
    await wait_dispatch_idle(manager, session.id)
    before = (await get_session(manager, session.id, user_id=session.user_id)).context_state
    await manager.archive_session(session.id)
    archived = await get_session(manager, session.id, user_id=session.user_id)
    assert archived.archived and archived.context_state == before
    await AgentManager(database).update_agent(session.agent_id, session.user_id, model="new-default")
    restored = await manager.unarchive_session(session.id, session.user_id)
    assert restored.context_state.validity == "stale"
    assert restored.context_state.invalidation.reason == "configuration_changed"
    assert len(starts) == 1


async def test_reset_invalidates_the_original_attempt_before_late_context(native_runtime, tmp_path):
    manager, _, session, _, _, _ = native_runtime
    await start_output(native_runtime)
    inject(tmp_path, usage())
    before = await context_when(manager, session, lambda state: state.validity == "valid")
    await manager.reset_session(session.id)
    after = (await get_session(manager, session.id, user_id=session.user_id)).context_state
    assert after.validity == "stale" and after.binding_revision > before.binding_revision


async def test_agent_route_wakes_context_owner_after_committing_defaults(native_runtime, tmp_path, monkeypatch):
    manager, database, session, _, _, events = native_runtime
    monkeypatch.setattr(agent_routes, "_manager", AgentManager(database))
    await start_output(native_runtime)
    inject(tmp_path, usage())
    await context_when(manager, session, lambda state: state.validity == "valid")
    await agent_routes.update_agent(manager, session.agent_id, AgentUpdate(model="updated-default"), session.user_id)
    await until(lambda: any(event["type"] == "context_state" and event["context_state"]["validity"] == "stale" for event in events))
    assert session.context_state.invalidation.reason == "configuration_changed"


async def test_binding_is_captured_before_slow_preparation_and_not_rebound_later(native_runtime, tmp_path, monkeypatch):
    manager, _, session, mode, _, _ = native_runtime
    mode[0] = "control_output"
    captured, release = asyncio.Event(), asyncio.Event()
    resolve = manager._resolve_credential

    async def hold_resolved(*args, **kwargs):
        result = await resolve(*args, **kwargs)
        captured.set()
        await release.wait()
        return result

    monkeypatch.setattr(manager, "_resolve_credential", hold_resolved)
    receipt = await manager.start_message(session.id, "Capture these defaults", source="human")
    try:
        await asyncio.wait_for(captured.wait(), 3)
        await manager.update_session_configuration(session.id, {"model": "next-model"}, session.user_id)
    finally:
        release.set()
    await until(lambda: session._run.backend is not None and session._run.backend.active_turn is not None)
    inject(tmp_path, usage(), item(ROOT, "turn-1", "final", "Original configuration completed"), terminal())
    assert (await asyncio.wait_for(receipt.wait(), 4)).outcome.status == "success"
    assert (await get_session(manager, session.id, user_id=session.user_id)).context_state.validity == "stale"


@pytest.mark.parametrize(("used", "capacity"), [(-1, 1000), (True, 1000), (None, 1000), (10, 0), (10, -1), (10, False)])
async def test_invalid_native_numbers_do_not_create_a_context_sample(native_runtime, tmp_path, used, capacity):
    manager, _, session, _, _, _ = native_runtime
    await start_output(native_runtime)
    inject(tmp_path, usage(used, capacity), item(ROOT, "turn-1", "final", "Done"), terminal())
    await wait_dispatch_idle(manager, session.id)
    assert (await get_session(manager, session.id, user_id=session.user_id)).context_state is None


async def test_cumulative_reset_accepts_new_root_usage_but_not_replays(native_runtime, tmp_path):
    manager, _, session, _, _, _ = native_runtime
    await start_output(native_runtime)
    inject(tmp_path, usage())
    before = await context_when(manager, session, lambda state: state.validity == "valid")
    fresh, old = usage(300), usage(50, turn="old-turn")
    # Native full-window handling can reset cumulative usage below its old
    # value. It is an aggregate, never an observation sequence number.
    fresh["params"]["tokenUsage"]["total"]["totalTokens"] = 1300
    inject(tmp_path, fresh, old, fresh, item(ROOT, "turn-1", "final", "Done"), terminal())
    await wait_dispatch_idle(manager, session.id)
    after = (await get_session(manager, session.id, user_id=session.user_id)).context_state
    assert after.sample.used_tokens == 300 and after.revision == before.revision + 1


@pytest.mark.parametrize("previous_total", [500, 90000])
@pytest.mark.parametrize("compacting", [False, True])
async def test_synthetic_full_window_is_not_a_context_sample(native_runtime, tmp_path, previous_total, compacting):
    manager, _, session, _, _, _ = native_runtime
    await start_output(native_runtime)
    initial = usage()
    initial["params"]["tokenUsage"]["total"]["totalTokens"] = previous_total
    inject(tmp_path, initial)
    before = await context_when(manager, session, lambda state: state.validity == "valid")
    synthetic = usage(max(1000 - previous_total, 0))
    synthetic["params"]["tokenUsage"].update(
        total={"totalTokens": 1000, "inputTokens": 0, "cachedInputTokens": 0, "cacheWriteInputTokens": 0,
            "outputTokens": 0, "reasoningOutputTokens": 0})
    synthetic["params"]["tokenUsage"]["last"].update(
        inputTokens=0, cachedInputTokens=0, outputTokens=0, reasoningOutputTokens=0)
    # Mirrors TokenUsageInfo::fill_to_context_window, including delta=0 when
    # the old cumulative total exceeds the window. Neither delta is a sample.
    frames = [compact_item("compact-1"), synthetic, compact_item("compact-1", completed=True)] \
        if compacting else [synthetic]
    inject(tmp_path, *frames, synthetic, item(ROOT, "turn-1", "final", "Done"), terminal())
    await wait_dispatch_idle(manager, session.id)
    state = (await get_session(manager, session.id, user_id=session.user_id)).context_state
    assert state.validity == ("stale" if compacting else "valid") and state.sample == before.sample
    if not compacting:
        assert state == before


@pytest.mark.parametrize("cache_write_at", ["total", "last"])
@pytest.mark.parametrize("compacting", [False, True])
async def test_cache_write_usage_is_not_synthetic_or_recomputed(native_runtime, tmp_path, cache_write_at, compacting):
    manager, _, session, _, _, _ = native_runtime
    await start_output(native_runtime)
    inject(tmp_path, usage())
    before = await context_when(manager, session, lambda state: state.validity == "valid")
    candidate = usage(100)
    candidate["params"]["tokenUsage"]["total"].update(totalTokens=1000, inputTokens=0, outputTokens=0)
    candidate["params"]["tokenUsage"]["last"].update(inputTokens=0)
    candidate["params"]["tokenUsage"][cache_write_at]["cacheWriteInputTokens"] = 100
    frames = [compact_item("compact-1"), candidate, compact_item("compact-1", completed=True)] \
        if compacting else [candidate]
    inject(tmp_path, *frames, candidate, item(ROOT, "turn-1", "final", "Done"), terminal())
    await wait_dispatch_idle(manager, session.id)
    state = (await get_session(manager, session.id, user_id=session.user_id)).context_state
    if compacting and cache_write_at == "last":
        assert state.validity == "stale" and state.sample == before.sample
    else:
        assert state.validity == "valid" and state.sample.used_tokens == 100
        assert state.sample.basis == ("codex_last_total" if cache_write_at == "last" else "codex_recomputed_estimate")
