"""Restored delegation receipts never restore authority or change their owner."""
from __future__ import annotations

import asyncio

import httpx
import pytest
from fastapi import FastAPI

from server import deps
from server.agent_manager import AgentManager
from server.delegations import DelegationError
from server.mcp_identity import scope_is_live, verify
from server.routers import delegations as delegation_router
from server.users import UserManager
from server.workspace import paths_for
from tests.test_delegation_restart import runtime as runtime
from tests.test_delegation_restart import wait_finished


async def wait_live_scope(context):
    token = next(entry.credential for entry in context.mcp_servers if entry.key == "bg")
    scope = verify(token)
    assert scope is not None
    async with asyncio.timeout(5):
        while not scope_is_live(scope):
            await asyncio.sleep(.01)
    return scope


async def completed_before_restart(boot, starts):
    first_boot = await boot()
    first = await first_boot.delegations.start_delegation(
        parent_session_id=first_boot.parent_id, agent_name="Worker", request="review first",
    )
    await asyncio.wait_for(starts.get(), 5)
    await wait_finished(first_boot.manager, first)
    assert first.state == "completed"
    await first_boot.close()
    restored_boot = await boot()
    assert starts.empty()
    return restored_boot, first


async def test_restored_receipt_keeps_old_cancel_on_its_own_round(runtime):
    boot, starts = runtime
    current, original = await completed_before_restart(boot, starts)
    saved = current.delegations.get_delegation(original.delegation_id)
    assert saved is not None and saved.state == "completed"
    assert saved.work_id == original.work_id
    assert [item.work_id for item in current.delegations.list_delegations(current.parent_id)] == [original.work_id]
    following = await current.delegations.follow_up_delegation(
        parent_session_id=current.parent_id, delegation_id=original.delegation_id,
        request="held second review",
    )
    context = await asyncio.wait_for(starts.get(), 5)
    new_scope = await wait_live_scope(context)
    assert following.work_id != original.work_id
    cancelled = await current.delegations.cancel_delegation(
        original.delegation_id, work_id=original.work_id,
    )
    assert cancelled.work_id == original.work_id and cancelled.state == "completed"
    assert current.delegations.get_delegation(original.delegation_id) is following
    assert following.state == "running" and scope_is_live(new_scope)
    assert current.manager.has_pending_or_active_work(following.delegation_id, following.work_id)


async def test_restart_revokes_old_run_bearer_and_followup_uses_new_lease(runtime):
    boot, starts = runtime
    first_boot = await boot()
    original = await first_boot.delegations.start_delegation(
        parent_session_id=first_boot.parent_id, agent_name="Worker", request="held first review",
    )
    old_context = await asyncio.wait_for(starts.get(), 5)
    old_scope = await wait_live_scope(old_context)
    assert old_scope.work_id == original.work_id
    await first_boot.close()
    assert not scope_is_live(old_scope)
    current = await boot()
    assert starts.empty() and not scope_is_live(old_scope)
    restored = current.delegations.get_delegation(original.delegation_id)
    assert restored is not None and restored.state == "interrupted"
    following = await current.delegations.follow_up_delegation(
        parent_session_id=current.parent_id, delegation_id=original.delegation_id,
        request="held resumed review",
    )
    new_context = await asyncio.wait_for(starts.get(), 5)
    new_scope = await wait_live_scope(new_context)
    assert new_scope.session_id == old_scope.session_id == original.delegation_id
    assert new_scope.work_id == following.work_id != old_scope.work_id
    assert new_scope.run_lease_id != old_scope.run_lease_id
    assert not scope_is_live(old_scope)
    with pytest.raises(DelegationError) as rejected:
        await current.delegations.cancel_delegation(
            original.delegation_id, work_id=following.work_id, scope=old_scope,
        )
    assert rejected.value.status_code == 409
    assert following.state == "running" and scope_is_live(new_scope)


async def test_restored_delegation_cannot_be_followed_up_by_another_parent(runtime, tmp_path):
    boot, starts = runtime
    current, original = await completed_before_restart(boot, starts)
    root = await current.database.get_system_agent()
    outsider = await current.manager.create_session(
        root["id"], "Other caller", str(tmp_path), backend="codex",
    )
    with pytest.raises(DelegationError) as rejected:
        await current.delegations.follow_up_delegation(
            parent_session_id=outsider.id, delegation_id=original.delegation_id,
            request="take over this review",
        )
    assert rejected.value.status_code == 404
    assert current.delegations.list_delegations(outsider.id) == []
    assert current.delegations.get_delegation(original.delegation_id).work_id == original.work_id
    assert starts.empty()


async def test_concurrent_followups_of_restored_delegation_accept_one_new_round(runtime):
    boot, starts = runtime
    current, original = await completed_before_restart(boot, starts)
    results = await asyncio.gather(*[
        current.delegations.follow_up_delegation(
            parent_session_id=current.parent_id, delegation_id=original.delegation_id,
            request="held concurrent review",
        ) for _ in range(2)
    ], return_exceptions=True)
    rejected = [item for item in results if isinstance(item, DelegationError)]
    accepted = [item for item in results if not isinstance(item, Exception)]
    assert len(rejected) == len(accepted) == 1
    assert rejected[0].status_code == 409
    context = await asyncio.wait_for(starts.get(), 5)
    scope = await wait_live_scope(context)
    assert scope.work_id == accepted[0].work_id != original.work_id
    assert starts.empty()


async def test_legacy_child_without_round_receipt_is_not_guessed_on_restart(runtime, tmp_path):
    boot, starts = runtime
    first_boot = await boot()
    worker = next(agent for agent in await first_boot.database.load_agents() if agent["name"] == "Worker")
    legacy = await first_boot.manager.create_session(
        worker["id"], "Legacy review", str(tmp_path), origin="delegation", backend="codex",
        parent_session_id=first_boot.parent_id, delegation_request="original legacy question",
    )
    await first_boot.close()
    current = await boot()
    assert current.delegations.get_delegation(legacy.id) is None
    assert current.delegations.list_delegations(current.parent_id) == []
    with pytest.raises(DelegationError) as rejected:
        await current.delegations.follow_up_delegation(
            parent_session_id=current.parent_id, delegation_id=legacy.id,
            request="resume ambiguous legacy work",
        )
    assert rejected.value.status_code == 404
    assert starts.empty()


async def test_deleted_restored_child_is_reported_as_gone_without_starting_a_round(runtime):
    boot, starts = runtime
    current, original = await completed_before_restart(boot, starts)
    assert current.manager.get_session(original.delegation_id) is not None
    assert await current.manager.delete_session(original.delegation_id)
    with pytest.raises(DelegationError) as rejected:
        await current.delegations.follow_up_delegation(
            parent_session_id=current.parent_id, delegation_id=original.delegation_id,
            request="review after child deletion",
        )
    assert rejected.value.status_code == 409
    assert "gone" in str(rejected.value)
    assert starts.empty()
    assert current.delegations.get_delegation(original.delegation_id).work_id == original.work_id


@pytest.mark.parametrize("terminal", ["failed", "cancelled"])
async def test_confirmed_failed_or_cancelled_round_survives_restart_and_accepts_explicit_followup(
    runtime, terminal,
):
    boot, starts = runtime
    first_boot = await boot()
    original = await first_boot.delegations.start_delegation(
        parent_session_id=first_boot.parent_id, agent_name="Worker",
        request="fail authentication" if terminal == "failed" else "held cancelled review",
    )
    context = await asyncio.wait_for(starts.get(), 5)
    if terminal == "cancelled":
        await wait_live_scope(context)
        cancelled = await first_boot.delegations.cancel_delegation(
            original.delegation_id, work_id=original.work_id, reason="caller withdrew the review",
        )
        assert cancelled is original
    await wait_finished(first_boot.manager, original)
    await first_boot.delegations.confirm_recording()
    assert original.state == terminal
    assert first_boot.manager.get_session(original.delegation_id) is not None
    confirmed = original.to_public_dict()
    assert confirmed["recording_state"] == "committed"
    assert confirmed["finished_at"] and confirmed["error"]
    await first_boot.close()
    current = await boot()
    assert current.manager.get_session(original.delegation_id) is not None
    recovered = current.delegations.get_delegation(original.delegation_id)
    assert recovered is not None
    assert recovered.to_public_dict() == confirmed
    assert starts.empty()
    assert current.manager.dispatch_snapshot(current.parent_id)["items"] == []
    following = await current.delegations.follow_up_delegation(
        parent_session_id=current.parent_id, delegation_id=original.delegation_id,
        request="held explicit retry after terminal",
    )
    next_context = await asyncio.wait_for(starts.get(), 5)
    scope = await wait_live_scope(next_context)
    assert following.delegation_id == original.delegation_id
    assert scope.work_id == following.work_id != original.work_id
    assert recovered.to_public_dict() == confirmed



async def test_deleted_parent_cannot_start_a_followup_from_a_restored_receipt(runtime):
    boot, starts = runtime
    current, original = await completed_before_restart(boot, starts)
    assert await current.manager.delete_session(current.parent_id)
    with pytest.raises(DelegationError) as rejected:
        await current.delegations.follow_up_delegation(
            parent_session_id=current.parent_id, delegation_id=original.delegation_id,
            request="review after caller deletion",
        )
    assert rejected.value.status_code == 404
    assert starts.empty()
    assert current.delegations.get_delegation(original.delegation_id).work_id == original.work_id


async def test_restored_delegation_routes_keep_account_scope_and_owner_access(runtime, monkeypatch):
    boot, starts = runtime
    first_boot = await boot()
    users = UserManager(first_boot.database)
    owner = await users.create_user(username="owner", password="owner-password")
    outsider = await users.create_user(username="other", password="other-password")
    owner_token = await users.issue_token(owner["id"])
    outsider_token = await users.issue_token(outsider["id"])
    deps.set_user_manager(users)
    agents = AgentManager(first_boot.database)
    caller = await agents.create_agent(name="Owned caller", backend="codex", user_id=owner["id"])
    await agents.create_agent(name="Owned worker", backend="codex", user_id=owner["id"])
    parent = await first_boot.manager.create_session(
        caller["id"], "Owned work", str(paths_for(owner["id"]).ensure().workspace), backend="codex",
    )
    await first_boot.manager.interrupt(parent.id)
    original = await first_boot.delegations.start_delegation(
        parent_session_id=parent.id, agent_name="Owned worker", request="review owned work",
    )
    await asyncio.wait_for(starts.get(), 5)
    await wait_finished(first_boot.manager, original)
    assert original.state == "completed"
    await first_boot.close()
    current = await boot()
    deps.set_user_manager(UserManager(current.database))
    await current.manager.interrupt(parent.id)
    application = FastAPI()
    application.include_router(delegation_router.router)
    application.dependency_overrides[deps.get_session_manager] = lambda: current.manager
    monkeypatch.setattr(delegation_router, "delegation_manager", current.delegations)
    path = f"/api/sessions/{parent.id}/delegations"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application), base_url="http://test",
    ) as client:
        foreign_headers = {"Authorization": f"Bearer {outsider_token}"}
        refused = [
            await client.get(path, headers=foreign_headers),
            await client.post(
                f"{path}/{original.delegation_id}/follow-up", headers=foreign_headers,
                json={"request": "take over the review"},
            ),
            await client.post(
                f"{path}/{original.delegation_id}/cancel", headers=foreign_headers,
                json={"work_id": original.work_id},
            ),
        ]
        assert [response.status_code for response in refused] == [404, 404, 404]
        assert starts.empty()
        owner_headers = {"Authorization": f"Bearer {owner_token}"}
        listing = await client.get(path, headers=owner_headers)
        assert listing.status_code == 200
        assert [item["delegation_id"] for item in listing.json()] == [original.delegation_id]
        resumed = await client.post(
            f"{path}/{original.delegation_id}/follow-up", headers=owner_headers,
            json={"request": "held owner followup"},
        )
        assert resumed.status_code == 200, resumed.text
    context = await asyncio.wait_for(starts.get(), 5)
    scope = await wait_live_scope(context)
    assert scope.user_id == owner["id"]
    assert scope.work_id == resumed.json()["work_id"] != original.work_id
