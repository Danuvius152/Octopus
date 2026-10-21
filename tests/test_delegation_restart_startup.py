"""Startup restores receipts while owning its complete recovery command."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from server import main
from server.config import settings
from tests.test_delegation_restart import runtime as runtime
from tests.test_delegation_restart_recording import seed_unfinished_round
from tests.test_service_shutdown import service as service


async def test_invalid_future_receipt_fails_startup_without_leaking_its_database_or_request(
    runtime, service, monkeypatch, caplog,
):
    boot, starts = runtime
    seed = await boot(settings.db_path)
    rec = await seed.delegations.start_delegation(
        parent_session_id=seed.parent_id, agent_name="Worker", request="held seed startup receipt",
    )
    await asyncio.wait_for(starts.get(), 5)
    before = await seed.database.load_delegation_state(
        rec.delegation_id, parent_session_id=seed.parent_id, user_id=None,
    )
    broken = json.loads(before)
    request_marker = "private malformed request marker"
    broken["request"] = request_marker
    broken["state"] = False
    assert await seed.database.compare_and_set_delegation_state(
        rec.delegation_id, before, json.dumps(broken),
        target_agent_id=rec.target_agent_id, parent_session_id=seed.parent_id, user_id=None,
    )
    await seed.close()
    removed = []
    remove = service.remove_broadcast

    def record_removal(key):
        removed.append(key)
        remove(key)

    monkeypatch.setattr(service, "remove_broadcast", record_removal)
    with pytest.raises(RuntimeError, match="^Invalid persisted delegation state$"):
        async with main.app.router.lifespan_context(main.app):
            pytest.fail("invalid future receipt cannot finish startup")
    with pytest.raises(asyncio.CancelledError, match="Database is closed"):
        await service.db.load_sessions()
    assert main.delegation_manager.BROADCAST_KEY in removed
    assert starts.empty()
    assert request_marker not in caplog.text


async def test_cancelled_startup_joins_its_existing_writer_and_preserves_cancellation_reason(
    runtime, service, monkeypatch,
):
    _, starts = runtime
    seed = seed_unfinished_round(
        Path(settings.db_path), request="held receipt before cancelled startup",
        agents_dir=Path(settings.agents_dir),
    )
    database_factory = main.Database
    entered, release, closing = asyncio.Event(), asyncio.Event(), asyncio.Event()
    writers = []

    def database_with_blocked_writer(path, **options):
        database = database_factory(path, **options)
        write = database.compare_and_set_delegation_state
        close = database.close

        async def blocked_write(*args, **kwargs):
            if json.loads(args[2])["state"] == "interrupted":
                writers.append(asyncio.current_task())
                entered.set()
                await release.wait()
            return await write(*args, **kwargs)

        async def observed_close():
            closing.set()
            await close()

        monkeypatch.setattr(database, "compare_and_set_delegation_state", blocked_write)
        monkeypatch.setattr(database, "close", observed_close)
        return database

    monkeypatch.setattr(main, "Database", database_with_blocked_writer)

    async def start():
        async with main.app.router.lifespan_context(main.app):
            pytest.fail("cancelled startup cannot begin serving")

    starting = asyncio.create_task(start())
    try:
        await asyncio.wait_for(entered.wait(), 5)
        messages = await service.db.load_messages(seed["child_id"])
        original = next(row for row in messages if row["role"] == "user")
        assert original["content"] == seed["input"] and original["seq"] == seed["input_seq"]
        assert original["tool_input"]["request_context"]["request_id"] == seed["request_id"]
        assert any(row["content"] == seed["body"] for row in messages)
        assert await service.db.get_committed_terminal(seed["child_id"], seed["request_id"]) is None
        starting.cancel("startup stopped")
        await asyncio.wait_for(closing.wait(), 5)
        assert len(writers) == 1 and writers[0] is not None
        assert not writers[0].done() and not writers[0].cancelling()
        release.set()
        with pytest.raises(asyncio.CancelledError) as cancellation:
            await asyncio.wait_for(starting, 5)
        assert cancellation.value.args == ("startup stopped",)
        assert writers[0].done() and not writers[0].cancelled()
        selected = main.delegation_manager.get_delegation(seed["child_id"])
        assert selected is not None and selected.state == "interrupted"
        assert selected.work_id == seed["work_id"] and selected.request == seed["request"]
        assert selected.to_public_dict()["recording_state"] == "committed"
        with pytest.raises(asyncio.CancelledError, match="Database is closed"):
            await service.db.load_sessions()
        assert starts.empty()
    finally:
        release.set()
        if not starting.done():
            starting.cancel()
        await asyncio.gather(starting, return_exceptions=True)
