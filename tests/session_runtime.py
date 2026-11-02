"""Shared observations of the original session dispatcher."""

import asyncio


async def wait_dispatch_idle(manager, session_id):
    async with asyncio.timeout(5):
        while (snapshot := manager.dispatch_snapshot(session_id))["active_request_id"] is not None or snapshot["items"]:
            await asyncio.sleep(0.01)
