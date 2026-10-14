"""Deleted delegation relationships do not masquerade as storage outages."""
from __future__ import annotations

import asyncio
import json

import pytest

from server.delegations import DelegationError
from tests.test_delegation_restart import runtime as runtime
from tests.test_delegation_restart_security import completed_before_restart


@pytest.mark.parametrize(("deleted", "status"), [("parent", 404), ("child", 409)])
async def test_relationship_deleted_during_followup_setup_is_reported_as_gone(
    runtime, monkeypatch, deleted, status,
):
    boot, starts = runtime
    current, original = await completed_before_restart(boot, starts)
    save = current.database.compare_and_set_delegation_state
    saving = asyncio.Event()
    release = asyncio.Event()

    async def gated_save(*args, **kwargs):
        if json.loads(args[2])["state"] == "running":
            saving.set()
            await release.wait()
        return await save(*args, **kwargs)

    monkeypatch.setattr(current.database, "compare_and_set_delegation_state", gated_save)
    following = asyncio.create_task(current.delegations.follow_up_delegation(
        parent_session_id=current.parent_id, delegation_id=original.delegation_id,
        request="held review after setup",
    ))
    try:
        await asyncio.wait_for(saving.wait(), 5)
        deleted_id = current.parent_id if deleted == "parent" else original.delegation_id
        assert await current.manager.delete_session(deleted_id)
    finally:
        release.set()
        result = (await asyncio.gather(following, return_exceptions=True))[0]
    assert isinstance(result, DelegationError)
    assert result.status_code == status
    assert "gone" in str(result)
    assert starts.empty()
    await current.delegations.confirm_recording()
