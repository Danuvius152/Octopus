"""Account authority remains enforced through auxiliary model entry points."""

import asyncio
from dataclasses import replace
from functools import partial

import pytest

from server.codex_login import codex_home_for
from server.credential_bindings import CredentialBindingError
from server.harness import Harness, HarnessOneshotError, OneShotContext
from server.research.leaf import run_reason_leaf, run_web_leaf
from server.session_manager import session_manager
from server.users import UserManager
from tests.test_agent_credential_bindings import _create_credential
from tests.test_agent_credential_bindings import bindings as bindings
from tests.test_harness_core import _oneshot_profile


async def test_session_patch_rejects_foreign_binding_without_partial_update(bindings):
    client, headers, manager, _ = bindings
    users = UserManager(manager.db)
    other = await users.create_user(username="other", password="password2")
    other_token = await users.issue_token(other["id"])
    foreign = await _create_credential(client, {"Authorization": f"Bearer {other_token}"})
    agent = await client.post(
        "/api/agents", headers=headers, json={"name": "Owned", "backend": "codex"},
    )
    assert agent.status_code == 201
    created = await client.post(
        "/api/sessions", headers=headers,
        json={"agent_id": agent.json()["id"], "name": "Unchanged"},
    )
    assert created.status_code == 201, created.text
    url = f"/api/sessions/{created.json()['id']}"
    for credential_id in ("missing", foreign):
        response = await client.patch(
            url, headers=headers,
            json={"name": "Must not change", "credential_id": credential_id},
        )
        assert response.status_code == 404, response.text
        assert response.json() == {"detail": "Credential not found"}
        current = await client.get(url, headers=headers)
        assert current.json()["name"] == "Unchanged"
        assert current.json()["credential_id"] is None


@pytest.mark.parametrize("backend", ["codex", "claude-code"])
async def test_explicit_binding_never_falls_back_to_host_identity(bindings, backend):
    client, headers, manager, owner = bindings
    cid = await _create_credential(client, headers, backend=backend)
    for credential_id, user_id, selected_backend in (
        ("missing", owner, backend),
        (cid, None, backend),
        (cid, owner, "claude-code" if backend == "codex" else "codex"),
    ):
        with pytest.raises(CredentialBindingError):
            await session_manager.resolve_credential_by_id(
                credential_id, backend=selected_backend, user_id=user_id,
            )
    await manager.db.update_credential(cid, needs_reconnect=True)
    with pytest.raises(ValueError, match="unavailable"):
        await session_manager.resolve_credential_by_id(cid, backend=backend, user_id=owner)
    # A revoked authentication does not prevent an owned historical artifact cleanup.
    artifact = await session_manager.resolve_credential_by_id(
        cid, backend=backend, user_id=owner, require_auth=False,
    )
    if backend == "codex":
        assert artifact is not None and artifact.home_dir == codex_home_for(cid)
    else:
        assert artifact is None
    assert await session_manager.resolve_credential_by_id(
        None, backend=backend, user_id=owner,
    ) is None


async def test_oneshot_checks_current_authority_at_spawn_and_cancellation_is_clean(monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()
    launched = []
    spawned = []
    original_spawn = asyncio.create_subprocess_exec

    async def observe_spawn(*args, **kwargs):
        spawned.append(args)
        return await original_spawn(*args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", observe_spawn)
    profile = _oneshot_profile('{"result":"unused"}')
    original = profile.build_oneshot_argv

    def render(ctx):
        launched.append("rendered")
        return original(ctx)

    async def authorize():
        entered.set()
        await release.wait()
        raise ValueError("Credential is unavailable")

    harness = Harness(replace(profile, build_oneshot_argv=render))
    for cancelled in (False, True):
        entered.clear()
        release.clear()
        call = asyncio.create_task(harness.run_oneshot(
            OneShotContext(prompt="test", authorize_spawn=authorize),
        ))
        await asyncio.wait_for(entered.wait(), 2)
        if cancelled:
            call.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError if cancelled else HarnessOneshotError):
            await call
    assert launched == ["rendered", "rendered"]
    assert spawned == []
    assert await harness.run_oneshot(OneShotContext(prompt="normal")) == "unused"
    assert len(spawned) == 1


@pytest.mark.parametrize("leaf", [run_web_leaf, run_reason_leaf])
async def test_research_leaf_rechecks_revoked_authority_before_spawn(bindings, tmp_path, monkeypatch, leaf):
    _, _, manager, owner = bindings
    calls = []

    async def forbidden_spawn(*args, **kwargs):
        calls.append(args)
        raise AssertionError("A revoked research leaf must never spawn")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden_spawn)
    await manager.db.save_credential("revoked", "claude-code", "owned", "api_key", "", "now", user_id=owner)
    authority = partial(manager.db.require_run_authority, owner, "revoked", "claude-code")
    await authority()  # Resolution was allowed before the credential was revoked.
    await manager.db.update_credential("revoked", needs_reconnect=True)
    result = await leaf(
        Harness(_oneshot_profile('{"result":"unused"}')), prompt="test",
        working_dir=str(tmp_path), credential=None, model=None, authorize_spawn=authority,
    )
    assert result.error
    assert not calls


async def test_oneshot_cancelled_authorization_has_no_process_to_retire(monkeypatch):
    async def cancelled_authorization():
        raise asyncio.CancelledError

    async def forbidden_spawn(*args, **kwargs):
        pytest.fail("Cancelled authorization must not launch a process")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden_spawn)
    harness = Harness(_oneshot_profile('{"result":"unused"}'))
    with pytest.raises(asyncio.CancelledError):
        await harness.run_oneshot(OneShotContext(prompt="test", authorize_spawn=cancelled_authorization))
