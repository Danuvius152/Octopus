"""Failed startup restores clean resources or retains an unconfirmed owner."""
from __future__ import annotations

import asyncio
import json

import pytest

from server import main
from server.config import settings
from server.delegations import DelegationError
from tests.test_delegation_restart import runtime as runtime
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
        parent_session_id=seed.parent_id, user_id=None,
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


async def test_unconfirmed_startup_restore_keeps_its_owner_and_database_and_original_error(
    runtime, service, monkeypatch, caplog,
):
    boot, starts = runtime
    seed = await boot(settings.db_path)
    rec = await seed.delegations.start_delegation(
        parent_session_id=seed.parent_id, agent_name="Worker", request="held receipt before storage outage",
    )
    await asyncio.wait_for(starts.get(), 5)
    await seed.close()
    database_factory = main.Database
    fault = True

    def database_with_restore_fault(path):
        database = database_factory(path)
        write = database.compare_and_set_delegation_state

        async def unavailable_write(*args, **kwargs):
            if fault and json.loads(args[2])["state"] == "interrupted":
                raise OSError("synthetic startup restoration storage outage")
            return await write(*args, **kwargs)

        monkeypatch.setattr(database, "compare_and_set_delegation_state", unavailable_write)
        return database

    monkeypatch.setattr(main, "Database", database_with_restore_fault)
    removed = []
    remove = service.remove_broadcast

    def record_removal(key):
        removed.append(key)
        remove(key)

    monkeypatch.setattr(service, "remove_broadcast", record_removal)
    try:
        with pytest.raises(DelegationError) as failure:
            async with main.app.router.lifespan_context(main.app):
                pytest.fail("unconfirmed receipt cannot finish startup")
        assert failure.value.status_code == 503
        assert str(failure.value) == "Delegation state saving failed or is unconfirmed"
        owner = main.delegation_manager
        selected = owner.get_delegation(rec.delegation_id)
        assert selected is not None and selected.state == "interrupted"
        assert selected.work_id == rec.work_id
        assert selected.to_public_dict()["recording_state"] == "unconfirmed"
        assert owner.db is service.db and owner.session_mgr is service
        assert owner.BROADCAST_KEY not in removed
        rows = await service.db.load_sessions(include_archived=True)
        assert any(row["id"] == rec.delegation_id for row in rows)
        rejected = await service.start_message(
            seed.parent_id, "late return after failed startup", source="delegation", delivery_id="late-startup-return",
        )
        assert rejected.disposition == "closed"
        assert starts.empty()
        assert "Service startup cleanup unconfirmed; database retained" in caplog.text
    finally:
        fault = False
        # Confirm the selected old receipt through the existing public API.
        # Admission stays sealed, so this request creates no native process.
        await main.delegation_manager.follow_up_delegation(
            parent_session_id=seed.parent_id, delegation_id=rec.delegation_id,
            request="explicit verification after startup storage recovery",
        )
        await service.db.close()
        main.delegation_manager.shutdown()
    assert selected.state == "interrupted"
    assert selected.to_public_dict()["recording_state"] == "committed"
    assert starts.empty()


async def test_cancelled_startup_joins_its_existing_writer_and_preserves_cancellation_reason(
    runtime, service, monkeypatch,
):
    boot, starts = runtime
    seed = await boot(settings.db_path)
    rec = await seed.delegations.start_delegation(
        parent_session_id=seed.parent_id, agent_name="Worker", request="held receipt before cancelled startup",
    )
    await asyncio.wait_for(starts.get(), 5)
    await seed.close()
    database_factory = main.Database
    entered, release, closing = asyncio.Event(), asyncio.Event(), asyncio.Event()
    writers = []

    def database_with_blocked_writer(path):
        database = database_factory(path)
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
        starting.cancel("startup stopped")
        await asyncio.wait_for(closing.wait(), 5)
        assert len(writers) == 1 and writers[0] is not None
        assert not writers[0].done() and not writers[0].cancelling()
        release.set()
        with pytest.raises(asyncio.CancelledError) as cancellation:
            await asyncio.wait_for(starting, 5)
        assert cancellation.value.args == ("startup stopped",)
        assert writers[0].done() and not writers[0].cancelled()
        selected = main.delegation_manager.get_delegation(rec.delegation_id)
        assert selected is not None and selected.state == "interrupted"
        assert selected.to_public_dict()["recording_state"] == "committed"
        with pytest.raises(asyncio.CancelledError, match="Database is closed"):
            await service.db.load_sessions()
        assert starts.empty()
    finally:
        release.set()
        if not starting.done():
            starting.cancel()
        await asyncio.gather(starting, return_exceptions=True)
