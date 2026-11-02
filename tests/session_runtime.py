"""Shared observations of the original session dispatcher."""

import asyncio

import pytest

from server.sessions.runtime_types import SessionOwnerUnavailable


async def fail_destination_owner(manager, session, monkeypatch):
    """Fail the original writer after a real read, retaining its cleanup."""
    read = manager.db.has_delivery_notice
    owner = manager._session_owners.get(id(session)) or manager._session_owner(session)

    async def fail_after_read(*args):
        await read(*args)
        raise RuntimeError("Destination owner failed after a confirmed read")

    with monkeypatch.context() as patch:
        patch.setattr(manager.db, "has_delivery_notice", fail_after_read)
        with pytest.raises(SessionOwnerUnavailable):
            await manager.retain_result_notice(session.id, "fail-original-owner", "Uncommitted notice")
    assert owner.failed and not manager._admission_closed
    return owner


async def wait_dispatch_idle(manager, session_id):
    async with asyncio.timeout(5):
        while (snapshot := manager.dispatch_snapshot(session_id))["active_request_id"] is not None or snapshot["items"]:
            await asyncio.sleep(0.01)
