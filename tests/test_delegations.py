"""Tests for agent-to-agent delegation (agent-collaboration.md Phase 1).

Covers:
  * Schema migration adds `parent_session_id` (FK SET NULL on parent
    delete) and `delegation_request`; `origin='delegation'` round-trips.
  * `DelegationManager` agent-name resolution: case-insensitive,
    missing → 404 (None), ambiguous → 409, self-delegation → 409.
  * Cycle + depth guards on the parent chain.
  * Reply / error / cancellation injection paths produce the right
    structured prompt back into the parent session via
    `SessionManager.start_message`.
  * Multiple concurrent delegations under the same target agent.
  * Bridge broadcast scoping skips delegation children automatically
    (no chat ever maps to a delegation session id).
  * The REST routes (POST/GET/cancel under
    `/api/sessions/{sid}/delegations`).

We use real DelegationManager instances bound to per-test in-memory
SessionManagers. The parent session's `start_message` is patched in
the consumer tests so no model process runs. Their admitted receipts remain
pending until the test model records the original request's text and terminal;
the completion then drives the captured parent prompt.
"""

from __future__ import annotations

import json
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient

from server.agent_manager import AgentManager
from server.database import Database
from server.delegations import (
    DEPTH_CAP,
    DelegationError,
    DelegationManager,
)
from server.delegations import (
    delegation_manager as singleton_delegation_manager,
)
from server.main import app
from server.routers import agents as agents_mod
from server.routers import schedules as schedules_mod
from server.scheduler import ScheduleRunner
from server.session_manager import session_manager
from server.sessions.runtime_types import _Settlement
from server.users import UserManager
from tests.accounts import create_account
from tests.completion_models import CompletionModels

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def models():
    return CompletionModels()


@pytest.fixture
async def db(tmp_path):
    d = Database(":memory:")
    await d.initialize()
    await create_account(d, extra_roots=[tmp_path])
    yield d
    await d.close()


@pytest.fixture
async def mgr(db):
    """Per-test SessionManager bound to a per-test DB."""
    from server.session_manager import SessionManager

    m = SessionManager()
    await m.initialize(db)
    yield m


@pytest.fixture
async def dm(mgr, db):
    """Per-test DelegationManager bound to the test session manager.
    A fresh instance every test so registries don't leak across cases."""
    dm = DelegationManager()
    dm.bind(session_mgr=mgr, db=db)
    yield dm
    dm.shutdown()


async def _make_agent(db, name: str) -> dict:
    am = AgentManager(db)
    owner = (await db.get_system_agent())["user_id"]
    return await am.create_agent(name=name, user_id=owner)


async def _make_session(
    mgr, agent_id: str, *, name: str = "S", origin: str = "user",
    parent_session_id: str | None = None,
):
    return await mgr.create_session(
        agent_id=agent_id,
        name=name,
        working_dir=None,
        origin=origin,
        parent_session_id=parent_session_id,
    )


@asynccontextmanager
async def _model_scope(dm, rec):
    """An explicit execution identity where these tests replace the harness.

    The separate knowledge delegation tests exercise real dispatch. These
    lifecycle tests retain their fake start boundary without posing a human
    API action as a nested model call.
    """
    from server.knowledge import encode
    from server.mcp_identity import mint, register_run, revoke_run, verify

    manager, database = dm.session_mgr, dm.db
    session = manager.get_session(rec.delegation_id)
    assert rec.owned_context is not None
    context = rec.owned_context.execution
    request_id, lease = uuid.uuid4().hex, uuid.uuid4().hex
    messages = await database.load_messages(session.id)
    await database.record_request(
        request_id=request_id, session_id=session.id, seq=len(messages), role="user", type="text", content=rec.request,
        tool_input={"request_context": {"request_id": request_id, "work_id": rec.work_id,
                                        "source": "delegation", "knowledge": json.loads(encode(context))}},
    )
    previous = session._run.knowledge_context, session._run.settlement, session._run.backend
    session._run.knowledge_context = context
    session._run.settlement = _Settlement(request_id=request_id, work_id=rec.work_id)
    session._run.backend = SimpleNamespace(run_lease_id=lease)
    register_run(lease, session.id, session.user_id, rec.work_id)
    try:
        yield verify(mint(session.id, user_id=session.user_id, wiki_task_id=session._run.wiki_task_id,
                          run_lease_id=lease, work_id=rec.work_id))
    finally:
        revoke_run(lease)
        session._run.knowledge_context, session._run.settlement, session._run.backend = previous


# ---------------------------------------------------------------------------
# Persisted delegation identity
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delegation_identity_round_trips_through_storage(db):
    agent = await db.get_system_agent()
    owner = agent["user_id"]
    await db.save_session("identity-parent", "Parent", "/tmp", "time", agent_id=agent["id"], user_id=owner)
    await db.save_session("identity-child", "Child", "/tmp", "time", agent_id=agent["id"], user_id=owner,
                          parent_session_id="identity-parent", origin="delegation", delegation_request="Original request")
    child = next(row for row in await db.load_sessions(user_id=owner) if row["id"] == "identity-child")
    assert child["parent_session_id"] == "identity-parent"
    assert child["delegation_request"] == "Original request"
    assert child["user_id"] == owner


@pytest.mark.asyncio
async def test_origin_delegation_round_trips(mgr, db):
    parent_agent = await db.get_system_agent()
    child_agent = await _make_agent(db, "Vera")
    parent = await _make_session(mgr, parent_agent["id"], name="parent")
    child = await mgr.create_session(
        agent_id=child_agent["id"],
        name="vera-child",
        working_dir=None,
        origin="delegation",
        parent_session_id=parent.id,
        delegation_request="please review",
    )
    assert child.origin == "delegation"
    assert child.parent_session_id == parent.id
    assert child.delegation_request == "please review"

    # Persists across a reload from the DB.
    rows = await db.load_sessions()
    raw = next(r for r in rows if r["id"] == child.id)
    assert raw["origin"] == "delegation"
    assert raw["parent_session_id"] == parent.id
    assert raw["delegation_request"] == "please review"


@pytest.mark.asyncio
async def test_parent_delete_sets_null_on_child(mgr, db):
    parent_agent = await db.get_system_agent()
    child_agent = await _make_agent(db, "Vera")
    parent = await _make_session(mgr, parent_agent["id"])
    child = await mgr.create_session(
        agent_id=child_agent["id"],
        name="c",
        working_dir=None,
        origin="delegation",
        parent_session_id=parent.id,
        delegation_request="r",
    )
    await db.delete_session(parent.id)
    rows = await db.load_sessions()
    raw = next(r for r in rows if r["id"] == child.id)
    assert raw["parent_session_id"] is None
    # The child itself survives — orphaning beats mass-delete.
    assert raw["id"] == child.id


# ---------------------------------------------------------------------------
# Agent resolution
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_target_agent_case_insensitive(dm, db):
    await _make_agent(db, "Vera")
    assert (await dm._resolve_target_agent("vera"))["name"] == "Vera"
    assert (await dm._resolve_target_agent("VERA"))["name"] == "Vera"
    assert (await dm._resolve_target_agent("  Vera  "))["name"] == "Vera"


@pytest.mark.asyncio
async def test_resolve_target_agent_missing_returns_none(dm, db):
    assert await dm._resolve_target_agent("nobody") is None


# ---------------------------------------------------------------------------
# start_delegation: end-to-end happy path + guards
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_files_resolved_against_working_dir(
    models, dm, mgr, db, monkeypatch, tmp_path
):
    """Plan §5.7: file paths are resolved against the parent's
    working_dir and missing entries are flagged. Verifies the prompt
    composer no longer leaves relative paths verbatim (which made the
    'absolute under <working_dir>' claim a lie)."""
    started: list[tuple[str, str]] = []

    async def fake_start_message(sid, prompt, attachment_ids=None, *, source="human", work_id=None, delivery_id=None, **kwargs):
        started.append((sid, prompt))
        return await models.start_message(mgr, sid, prompt, source=source, work_id=work_id)

    monkeypatch.setattr(mgr, "start_message", fake_start_message)
    # Build a real working dir with one file that exists and one
    # that doesn't, plus an absolute path to verify it passes through.
    wd = tmp_path / "ws"
    wd.mkdir()
    (wd / "real.tsx").write_text("hello")
    abs_real = tmp_path / "abs.txt"
    abs_real.write_text("there")

    octo = await db.get_system_agent()
    await _make_agent(db, "Vera")
    parent = await mgr.create_session(
        agent_id=octo["id"], name="parent", working_dir=str(wd),
    )
    await dm.start_delegation(
        parent_session_id=parent.id,
        agent_name="vera",
        request="review",
        files=["real.tsx", str(abs_real), "missing.tsx"],
    )
    assert started
    _, prompt = started[0]
    # Relative path resolved against working_dir.
    assert str((wd / "real.tsx").resolve()) in prompt
    # Absolute path passes through unchanged (modulo .resolve()).
    assert str(abs_real.resolve()) in prompt
    # Missing path is flagged so the child doesn't trust it.
    assert "(not found)" in prompt
    # Prompt text now reflects the resolution semantics.
    assert "paths resolved against" in prompt


@pytest.mark.asyncio
async def test_delegation_session_remains_open_after_terminal_inject(
    models, dm, mgr, db, monkeypatch
):
    """A completed round returns once and leaves its conversation open."""
    monkeypatch.setattr(mgr, "start_message", models.start_for(mgr))
    octo = await db.get_system_agent()
    await _make_agent(db, "Vera")
    parent = await _make_session(mgr, octo["id"])
    rec = await dm.start_delegation(
        parent_session_id=parent.id, agent_name="vera", request="r",
    )
    child = mgr.get_session(rec.delegation_id)
    assert child is not None
    assert not mgr.session_is_executing(child)
    # The terminal completes the round without archiving the conversation.
    await models.emit(dm, {
        "type": "result", "session_id": rec.delegation_id, "is_error": False,
    })
    assert rec.state == "completed"
    assert mgr.get_session(rec.delegation_id) is child


@pytest.mark.asyncio
async def test_delegation_child_can_be_explicitly_archived(
    models, dm, mgr, db, monkeypatch
):
    """Retaining a delegation does not remove the user's Archive action."""
    monkeypatch.setattr(mgr, "start_message", models.start_for(mgr))
    octo = await db.get_system_agent()
    await _make_agent(db, "Vera")
    parent = await _make_session(mgr, octo["id"])
    rec = await dm.start_delegation(
        parent_session_id=parent.id, agent_name="vera", request="r",
    )
    child = mgr.get_session(rec.delegation_id)
    assert child is not None
    assert not mgr.session_is_executing(child)
    await mgr.archive_session(rec.delegation_id, replace=False)
    assert mgr.get_session(rec.delegation_id) is None


@pytest.mark.asyncio
async def test_auto_archive_skips_user_origin_sessions(mgr, db, monkeypatch):
    """Sanity check: the auto-archive helper still respects origin —
    user-origin sessions must NOT be auto-archived even when the
    helper is called directly."""
    octo = await db.get_system_agent()
    sess = await _make_session(mgr, octo["id"], origin="user")
    assert not mgr.session_is_executing(sess)
    ok = await mgr.auto_archive_scheduled_session(sess.id)
    assert ok is False
    assert mgr.get_session(sess.id) is not None


@pytest.mark.asyncio
async def test_delegation_conversation_remains_open_on_restart(db):
    """Restart changes round recovery, not the conversation retention policy."""
    from server.session_manager import SessionManager

    # Boot 1: a normal parent + a live delegation child (mid-flight when
    # the process dies).
    m1 = SessionManager()
    await m1.initialize(db)
    octo = await db.get_system_agent()
    vera = await _make_agent(db, "Vera")
    parent = await _make_session(m1, octo["id"], name="parent")
    child = await m1.create_session(
        agent_id=vera["id"], name="vera-child", working_dir=None,
        origin="delegation", parent_session_id=parent.id,
        delegation_request="r",
    )
    assert child.id in m1.sessions  # live before the restart

    # Boot 2: a fresh manager over the same DB — the run registry is gone.
    m2 = SessionManager()
    await m2.initialize(db)

    assert m2.get_session(child.id) is not None
    assert parent.id in m2.sessions
    rows = {r["id"]: r for r in await db.load_sessions(include_archived=True)}
    assert not rows[child.id]["archived"]
    assert not rows[parent.id]["archived"]


@pytest.mark.asyncio
async def test_nested_chain_intermediate_stays_alive_while_grandchild_runs(
    models, dm, mgr, db, monkeypatch
):
    """The full Vera scenario: Octo asks Vera, Vera asks Pete and
    ends her turn. Vera is now idle waiting for Pete. With the
    pre-fix behaviour, Vera would be archived on idle and Pete's
    reply would land on a missing session. With the fix, Vera stays
    alive until her OWN terminal event fires.

    Vera caught the round-2 version of this test using
    ``_noop_start_message``, which silently absorbed missing-parent
    injections and therefore couldn't observe the original bug. This
    revision uses a fake that mirrors the real ``start_message``'s
    failure mode: it raises when the target session id is no longer
    in the in-memory map. So if anything regresses to archiving Vera
    while Pete is still running, Pete's `_inject_terminal` will fail
    the assertion via the captured exception list, not silently
    succeed.
    """
    octo = await db.get_system_agent()
    await _make_agent(db, "Vera")
    await _make_agent(db, "Pete")
    octo_sess = await _make_session(mgr, octo["id"], name="octo-user")

    from server import delegations as _delegations_module

    delivered: list[tuple[str, str]] = []
    inject_errors: list[str] = []

    async def faithful_start_message(sid, prompt, attachment_ids=None, *, source="human", work_id=None, delivery_id=None, **kwargs):
        # Mirror SessionManager.start_message: ValueError when the
        # target session id is no longer in the in-memory map. Any
        # archival-too-early bug surfaces here as an exception that
        # the DelegationManager's logger absorbs — we capture it via
        # monkeypatching logger.exception below so the test fails
        # rather than passing under the bug.
        if sid not in mgr.sessions:
            raise ValueError(f"Session {sid} not found")
        delivered.append((sid, prompt))
        return await models.start_message(mgr, sid, prompt, source=source, work_id=work_id)

    def capture_logger_exception(msg, *args, **kwargs):
        # When DelegationManager._inject_terminal catches a missing-
        # parent error, it logs via logger.exception. We capture that
        # so a regression to the original bug becomes a hard failure.
        try:
            inject_errors.append(msg % args if args else msg)
        except Exception:
            inject_errors.append(str(msg))

    monkeypatch.setattr(mgr, "start_message", faithful_start_message)
    monkeypatch.setattr(
        _delegations_module.logger,
        "exception",
        capture_logger_exception,
    )

    # Octo → Vera.
    vera_rec = await dm.start_delegation(
        parent_session_id=octo_sess.id, agent_name="vera", request="r1",
    )
    vera_sess = mgr.get_session(vera_rec.delegation_id)
    assert vera_sess is not None
    # Vera → Pete.
    async with _model_scope(dm, vera_rec) as scope:
        pete_rec = await dm.start_delegation(
            parent_session_id=vera_sess.id, agent_name="pete", request="r2", scope=scope,
        )
    # Vera is now waiting for Pete; she has no active work of her own.
    assert not mgr.session_is_executing(vera_sess)

    # Drive session_manager's actual idle hook for Vera (mirror of
    # the original owner reporting the session idle). With the round-2
    # fix in place, "delegation" is NOT in _AUTO_ARCHIVE_ORIGINS so
    # this is a no-op; if a future change re-adds it (the original
    # bug), Vera gets archived here and Pete's reply below fails.
    # Vera flagged that the test relied on an adjacent test to catch
    # this directly; this in-place hook makes the nested-chain test
    # self-sufficient at proving the whole bug.
    if vera_sess.origin in mgr._AUTO_ARCHIVE_ORIGINS:
        await mgr.auto_archive_scheduled_session(vera_sess.id)

    # Drive Pete to terminal. If Vera was archived too early,
    # faithful_start_message raises and capture_logger_exception
    # records it — the assertions below fail.
    await models.emit(dm, {
        "type": "result",
        "session_id": pete_rec.delegation_id,
        "is_error": False,
    })
    assert not inject_errors, (
        f"Pete's reply failed to reach Vera — Vera was archived "
        f"too early. Captured errors: {inject_errors!r}"
    )
    # Pete's reply actually landed on Vera's session id.
    assert any(sid == vera_rec.delegation_id for sid, _ in delivered), (
        f"Pete's terminal injection didn't target Vera. "
        f"delivered={delivered!r}"
    )
    # Pete's round is complete and his conversation remains accessible.
    assert mgr.get_session(pete_rec.delegation_id) is not None
    # Vera is STILL alive — she hasn't fired her own terminal yet.
    assert mgr.get_session(vera_rec.delegation_id) is not None

    # Vera's turn 1 ends — the turn that only asked Pete. Even though Pete is
    # already done (he answered above, before she wrapped up), that turn isn't
    # her answer, so it must not finalise or archive her: Pete's reply is
    # queued into her session and she still owes Octo the relay.
    await models.emit(dm, {
        "type": "result",
        "session_id": vera_rec.delegation_id,
        "is_error": False,
    })
    assert mgr.get_session(vera_rec.delegation_id) is not None, (
        "Vera was archived on the turn that merely asked Pete"
    )

    # Turn 2 — the relay Pete's reply woke her for — fires her own terminal.
    await models.emit(dm, {
        "type": "result",
        "session_id": vera_rec.delegation_id,
        "is_error": False,
    })
    assert not inject_errors, (
        f"Vera's reply failed to reach Octo. Captured errors: "
        f"{inject_errors!r}"
    )
    # Both conversations remain accessible after their rounds complete.
    assert mgr.get_session(vera_rec.delegation_id) is not None
    assert mgr.get_session(octo_sess.id) is octo_sess


@pytest.mark.asyncio
async def test_start_delegation_happy_path(models, dm, mgr, db, monkeypatch):
    # Disable real harness spawn — we only want to verify the wiring.
    started: list[tuple[str, str]] = []

    async def fake_start_message(sid, prompt, attachment_ids=None, *, source="human", work_id=None, delivery_id=None, **kwargs):
        started.append((sid, prompt))
        return await models.start_message(mgr, sid, prompt, source=source, work_id=work_id)

    monkeypatch.setattr(mgr, "start_message", fake_start_message)

    parent_agent = await db.get_system_agent()
    await _make_agent(db, "Vera")
    parent = await _make_session(mgr, parent_agent["id"], name="parent")

    rec = await dm.start_delegation(
        parent_session_id=parent.id,
        agent_name="vera",
        request="review the dashboard",
        files=["web/src/Dashboard.tsx"],
    )
    assert rec.state == "running"
    assert rec.delegation_id != parent.id
    assert rec.target_agent_name == "Vera"
    assert rec.parent_session_id == parent.id

    # A child session row exists with origin=delegation, parent set.
    child = mgr.get_session(rec.delegation_id)
    assert child is not None
    assert child.origin == "delegation"
    assert child.parent_session_id == parent.id
    assert child.delegation_request == "review the dashboard"
    assert child.working_dir == parent.working_dir  # inherited

    # The child's first turn was kicked off — the composed prompt
    # mentions the parent agent and the request, and lists files.
    assert started, "expected start_message to be called on the child"
    sid, prompt = started[0]
    assert sid == rec.delegation_id
    assert "Octo" in prompt  # default system agent's name
    assert "review the dashboard" in prompt
    assert "web/src/Dashboard.tsx" in prompt


@pytest.mark.asyncio
async def test_start_delegation_rejects_empty_request(models, dm, mgr, db, monkeypatch):
    monkeypatch.setattr(mgr, "start_message", models.start_for(mgr))
    await _make_agent(db, "Vera")
    parent_agent = await db.get_system_agent()
    parent = await _make_session(mgr, parent_agent["id"])
    with pytest.raises(DelegationError) as excinfo:
        await dm.start_delegation(
            parent_session_id=parent.id,
            agent_name="vera",
            request="   ",
        )
    assert excinfo.value.status_code == 400


@pytest.mark.asyncio
async def test_start_delegation_unknown_parent(dm):
    with pytest.raises(DelegationError) as excinfo:
        await dm.start_delegation(
            parent_session_id="nonexistent",
            agent_name="vera",
            request="hi",
        )
    assert excinfo.value.status_code == 404


@pytest.mark.asyncio
async def test_start_delegation_unknown_target(models, dm, mgr, db, monkeypatch):
    monkeypatch.setattr(mgr, "start_message", models.start_for(mgr))
    parent_agent = await db.get_system_agent()
    parent = await _make_session(mgr, parent_agent["id"])
    with pytest.raises(DelegationError) as excinfo:
        await dm.start_delegation(
            parent_session_id=parent.id,
            agent_name="ghost",
            request="hi",
        )
    assert excinfo.value.status_code == 404


@pytest.mark.asyncio
async def test_start_delegation_rejects_self(models, dm, mgr, db, monkeypatch):
    monkeypatch.setattr(mgr, "start_message", models.start_for(mgr))
    parent_agent = await db.get_system_agent()
    parent = await _make_session(mgr, parent_agent["id"])
    with pytest.raises(DelegationError) as excinfo:
        await dm.start_delegation(
            parent_session_id=parent.id,
            agent_name=parent_agent["name"],
            request="hi",
        )
    assert excinfo.value.status_code == 409
    assert "yourself" in str(excinfo.value)


@pytest.mark.asyncio
async def test_cycle_rejected(models, dm, mgr, db, monkeypatch):
    """Octo → Vera → Octo is rejected at the cycle check."""
    monkeypatch.setattr(mgr, "start_message", models.start_for(mgr))
    octo = await db.get_system_agent()
    vera = await _make_agent(db, "Vera")

    # Octo's user session
    octo_sess = await _make_session(mgr, octo["id"], name="octo-user")
    # Vera child under Octo
    vera_sess = await mgr.create_session(
        agent_id=vera["id"],
        name="vera",
        working_dir=None,
        origin="delegation",
        parent_session_id=octo_sess.id,
        delegation_request="r",
    )
    # Now Vera tries to ask Octo — cycle.
    with pytest.raises(DelegationError) as excinfo:
        await dm.start_delegation(
            parent_session_id=vera_sess.id,
            agent_name=octo["name"],
            request="back to you",
        )
    assert excinfo.value.status_code == 409
    assert "Cycle" in str(excinfo.value)


@pytest.mark.asyncio
async def test_chain_walk_rejects_session_id_cycle(models, dm, mgr, db, monkeypatch):
    """If the parent_session_id pointers form a session-id cycle
    (corrupted, not a real agent cycle), fail closed rather than
    silently treating the chain as terminated. The cycle is forced
    in memory after a valid creation, so the SQLite FK on
    parent_session_id (which would catch the obvious construction
    error) doesn't apply."""
    monkeypatch.setattr(mgr, "start_message", models.start_for(mgr))
    octo = await db.get_system_agent()
    await _make_agent(db, "Vera")
    a = await _make_agent(db, "A")
    # Build a valid 2-session chain first so the FK is satisfied.
    sess_a = await _make_session(mgr, octo["id"], name="a")
    sess_b = await mgr.create_session(
        agent_id=a["id"], name="b", working_dir=None,
        origin="delegation", parent_session_id=sess_a.id,
        delegation_request="r",
    )
    # Force the cycle in memory only — corrupting the in-memory
    # pointer chain without touching the DB. _check_chain walks via
    # mgr.get_session which reads the in-memory map.
    sess_a.parent_session_id = sess_b.id
    with pytest.raises(DelegationError) as ex:
        await dm.start_delegation(
            parent_session_id=sess_a.id, agent_name="vera", request="r",
        )
    assert ex.value.status_code == 409
    assert "session-id cycle" in str(ex.value)


@pytest.mark.asyncio
async def test_chain_walk_falls_back_to_db_for_archived_ancestor(
    models, dm, mgr, db, monkeypatch
):
    """Vera's round-3 finding: after the auto-archive-after-terminal
    fix, a delegation child can legitimately have its parent archived
    in the DB. If the user then unarchives the child, ``ask_agent``
    from it must still succeed — the walk consults the DB for any
    ancestor missing from the in-memory map. Without this fallback,
    every fresh delegation from an unarchived child would 409 with
    "no longer exists"."""
    monkeypatch.setattr(mgr, "start_message", models.start_for(mgr))
    octo = await db.get_system_agent()
    vera = await _make_agent(db, "Vera")
    await _make_agent(db, "Pete")
    # Build a valid chain Octo (root) → Vera-child (delegation).
    octo_sess = await _make_session(mgr, octo["id"], name="octo")
    vera_child = await mgr.create_session(
        agent_id=vera["id"], name="vera-child", working_dir=None,
        origin="delegation", parent_session_id=octo_sess.id,
        delegation_request="r",
    )
    # Evict octo_sess from the in-memory map as if it had been
    # archived. The DB row is still there.
    mgr.sessions.pop(octo_sess.id, None)
    # ask_agent from Vera-child to Pete should still work — the DB
    # fallback finds the archived ancestor and the walk completes
    # cleanly. With Vera + Pete + Octo's archived agent in the
    # chain, we still satisfy the depth+cycle checks.
    rec = await dm.start_delegation(
        parent_session_id=vera_child.id, agent_name="pete", request="r",
    )
    assert rec.state == "running"


@pytest.mark.asyncio
async def test_chain_walk_rejects_truly_missing_ancestor(
    models, dm, mgr, db, monkeypatch
):
    """Only fail-closed when the ancestor is gone from BOTH memory
    AND the DB. A stale parent_session_id pointing at a hard-deleted
    row still produces a 409 — that's actual corruption, not a
    legitimate archived state."""
    monkeypatch.setattr(mgr, "start_message", models.start_for(mgr))
    octo = await db.get_system_agent()
    await _make_agent(db, "Vera")
    ancestor = await _make_session(mgr, octo["id"], name="ancestor")
    child = await mgr.create_session(
        agent_id=octo["id"], name="orphan", working_dir=None,
        origin="delegation", parent_session_id=ancestor.id,
        delegation_request="r",
    )
    # Evict from memory AND from the DB so the fallback also misses.
    mgr.sessions.pop(ancestor.id, None)
    await db.delete_session(ancestor.id)
    with pytest.raises(DelegationError) as ex:
        await dm.start_delegation(
            parent_session_id=child.id, agent_name="vera", request="r",
        )
    assert ex.value.status_code == 409
    assert "neither memory nor the database" in str(ex.value)


@pytest.mark.asyncio
async def test_depth_cap_rejected(models, dm, mgr, db, monkeypatch):
    """A 4th delegation hop is rejected (DEPTH_CAP=3)."""
    monkeypatch.setattr(mgr, "start_message", models.start_for(mgr))
    octo = await db.get_system_agent()
    a = await _make_agent(db, "A")
    b = await _make_agent(db, "B")
    c = await _make_agent(db, "C")
    d = await _make_agent(db, "D")

    root = await _make_session(mgr, octo["id"], name="root")
    s_a = await mgr.create_session(
        agent_id=a["id"], name="a", working_dir=None,
        origin="delegation", parent_session_id=root.id, delegation_request="r",
    )
    s_b = await mgr.create_session(
        agent_id=b["id"], name="b", working_dir=None,
        origin="delegation", parent_session_id=s_a.id, delegation_request="r",
    )
    s_c = await mgr.create_session(
        agent_id=c["id"], name="c", working_dir=None,
        origin="delegation", parent_session_id=s_b.id, delegation_request="r",
    )
    # Chain so far: 3 delegation hops (A, B, C). Adding D would make 4.
    with pytest.raises(DelegationError) as excinfo:
        await dm.start_delegation(
            parent_session_id=s_c.id, agent_name=d["name"], request="r",
        )
    assert excinfo.value.status_code == 409
    assert str(DEPTH_CAP) in str(excinfo.value)


# ---------------------------------------------------------------------------
# Request completion → injection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reply_injection_on_result(models, dm, mgr, db, monkeypatch):
    """The original committed success receipt produces one parent reply."""
    injected: list[tuple[str, str]] = []

    async def capture(sid, prompt, attachment_ids=None, *, source="human", work_id=None, delivery_id=None, **kwargs):
        injected.append((sid, prompt))
        return await models.start_message(mgr, sid, prompt, source=source, work_id=work_id)

    monkeypatch.setattr(mgr, "start_message", capture)
    octo = await db.get_system_agent()
    await _make_agent(db, "Vera")
    parent = await _make_session(mgr, octo["id"], name="parent")
    rec = await dm.start_delegation(
        parent_session_id=parent.id, agent_name="vera", request="r",
    )
    # First call was the child's start_message; clear it so we only
    # see the injection.
    injected.clear()

    cid = rec.delegation_id
    await models.emit(dm, {"type": "assistant_text", "session_id": cid, "content": "Reviewed. "})
    await models.emit(dm, {"type": "assistant_text", "session_id": cid, "content": "Looks good."})
    await models.emit(dm, {"type": "result", "session_id": cid, "is_error": False})

    assert rec.state == "completed"
    assert rec.finished_at is not None
    assert len(injected) == 1
    target_sid, prompt = injected[0]
    assert target_sid == parent.id
    assert prompt.startswith(f"[agent-reply:Vera delegation={cid}]")
    assert "Reviewed. Looks good." in prompt


@pytest.mark.asyncio
async def test_parent_delegation_waits_for_its_own_sub_delegation(
    models, dm, mgr, db, monkeypatch
):
    """Octo → Vera → Pete: Vera's turn-1 result must NOT finalise her.

    `ask_agent` is asynchronous, so Vera's first turn ends the instant she has
    asked Pete — "awaiting Pete's reply" is not her answer. Finalising there
    relayed that non-answer to Octo and auto-archived Vera's session while Pete
    was still running, so Pete's reply could never be relayed
    (agent-collaboration.md §5: "Pete replies, Vera relays, Octo summarises").
    Vera stays running until she has no sub-delegation in flight.
    """
    injected: list[tuple[str, str]] = []

    async def capture(sid, prompt, attachment_ids=None, *, source="human", work_id=None, delivery_id=None, **kwargs):
        injected.append((sid, prompt))
        return await models.start_message(mgr, sid, prompt, source=source, work_id=work_id)

    monkeypatch.setattr(mgr, "start_message", capture)
    octo = await db.get_system_agent()
    await _make_agent(db, "Vera")
    await _make_agent(db, "Pete")
    parent = await _make_session(mgr, octo["id"], name="parent")

    vera_rec = await dm.start_delegation(
        parent_session_id=parent.id, agent_name="vera", request="ask pete",
    )
    vera_sid = vera_rec.delegation_id
    async with _model_scope(dm, vera_rec) as scope:
        pete_rec = await dm.start_delegation(
            parent_session_id=vera_sid, agent_name="pete", request="reply HOP-7", scope=scope,
        )
    pete_sid = pete_rec.delegation_id
    injected.clear()

    # Turn 1: Vera says she's asked Pete, then her turn ends.
    await models.emit(dm,
        {"type": "assistant_text", "session_id": vera_sid, "content": "Awaiting Pete."}
    )
    await models.emit(dm, {"type": "result", "session_id": vera_sid, "is_error": False})
    assert vera_rec.state == "running", "Vera finalised before Pete answered"
    assert injected == [], f"Octo was told {injected!r} before Pete replied"

    # Pete answers → his terminal injection wakes Vera (into HER session).
    await models.emit(dm,
        {"type": "assistant_text", "session_id": pete_sid, "content": "HOP-7"}
    )
    await models.emit(dm, {"type": "result", "session_id": pete_sid, "is_error": False})
    assert pete_rec.state == "completed"
    assert [sid for sid, _ in injected] == [vera_sid]
    injected.clear()

    # Turn 2: Vera relays. No sub-delegation in flight now, so she finalises
    # and Octo finally hears the token.
    await models.emit(dm,
        {"type": "assistant_text", "session_id": vera_sid, "content": "Pete says HOP-7"}
    )
    await models.emit(dm, {"type": "result", "session_id": vera_sid, "is_error": False})
    assert vera_rec.state == "completed"
    assert len(injected) == 1
    target_sid, prompt = injected[0]
    assert target_sid == parent.id
    assert prompt.startswith(f"[agent-reply:Vera delegation={vera_sid}]")
    assert "HOP-7" in prompt


@pytest.mark.asyncio
async def test_parent_delegation_waits_when_its_sub_delegation_finished_first(
    models, dm, mgr, db, monkeypatch
):
    """Same chain, inverted timing: Pete answers BEFORE Vera's turn-1 result.

    `_has_running_children` samples at the instant the turn ends, so a fast
    grandchild — or merely a slow wrap-up on Vera's own turn — leaves nothing
    in flight even though the relay turn hasn't happened yet. Vera was then
    finalised with "Delegation started to Pete … waiting for their reply" as
    her answer, and Pete's real reply, already queued into her session, was
    dropped because her record was no longer running.

    The real-CLI `test_real_three_hop_chain` caught this, but only on runs slow
    enough to invert the two events. What decides the deferral is that Vera
    ASKED someone during the turn, not whether they happen to still be working
    when it ends.
    """
    injected: list[tuple[str, str]] = []

    async def capture(sid, prompt, attachment_ids=None, *, source="human", work_id=None, delivery_id=None, **kwargs):
        injected.append((sid, prompt))
        return await models.start_message(mgr, sid, prompt, source=source, work_id=work_id)

    monkeypatch.setattr(mgr, "start_message", capture)
    octo = await db.get_system_agent()
    await _make_agent(db, "Vera")
    await _make_agent(db, "Pete")
    parent = await _make_session(mgr, octo["id"], name="parent")

    vera_rec = await dm.start_delegation(
        parent_session_id=parent.id, agent_name="vera", request="ask pete",
    )
    vera_sid = vera_rec.delegation_id
    async with _model_scope(dm, vera_rec) as scope:
        pete_rec = await dm.start_delegation(
            parent_session_id=vera_sid, agent_name="pete", request="reply HOP-7", scope=scope,
        )
    pete_sid = pete_rec.delegation_id
    injected.clear()

    # Pete is quick: he answers while Vera is still wrapping up turn 1. His
    # terminal injection lands in Vera's session, queued behind that turn.
    await models.emit(dm,
        {"type": "assistant_text", "session_id": pete_sid, "content": "HOP-7"}
    )
    await models.emit(dm, {"type": "result", "session_id": pete_sid, "is_error": False})
    assert pete_rec.state == "completed"
    assert [sid for sid, _ in injected] == [vera_sid]
    injected.clear()

    # Only now does Vera's turn 1 end. Nothing is running under her — but she
    # still owes Octo the relay, so she must not finalise here.
    await models.emit(dm,
        {
            "type": "assistant_text",
            "session_id": vera_sid,
            "content": "Delegation started to Pete. Waiting for their reply.",
        }
    )
    await models.emit(dm, {"type": "result", "session_id": vera_sid, "is_error": False})
    assert vera_rec.state == "running", "Vera finalised on the turn that only asked Pete"
    assert injected == [], f"Octo was told {injected!r} instead of Pete's answer"

    # Turn 2 — woken by Pete's reply — is the relay, and it finalises her.
    await models.emit(dm,
        {"type": "assistant_text", "session_id": vera_sid, "content": "Pete says HOP-7"}
    )
    await models.emit(dm, {"type": "result", "session_id": vera_sid, "is_error": False})
    assert vera_rec.state == "completed"
    assert len(injected) == 1
    target_sid, prompt = injected[0]
    assert target_sid == parent.id
    assert prompt.startswith(f"[agent-reply:Vera delegation={vera_sid}]")
    assert "HOP-7" in prompt


@pytest.mark.asyncio
async def test_plain_delegation_finalises_on_its_first_result(
    models, dm, mgr, db, monkeypatch
):
    """The deferral must not leak onto ordinary one-hop delegations.

    Only a child that asked someone else waits for a second turn; a child that
    simply answers finalises on its first result, as it always has.
    """
    injected: list[tuple[str, str]] = []

    async def capture(sid, prompt, attachment_ids=None, *, source="human", work_id=None, delivery_id=None, **kwargs):
        injected.append((sid, prompt))
        return await models.start_message(mgr, sid, prompt, source=source, work_id=work_id)

    monkeypatch.setattr(mgr, "start_message", capture)
    octo = await db.get_system_agent()
    await _make_agent(db, "Vera")
    parent = await _make_session(mgr, octo["id"], name="parent")

    rec = await dm.start_delegation(
        parent_session_id=parent.id, agent_name="vera", request="just answer",
    )
    injected.clear()

    await models.emit(dm,
        {"type": "assistant_text", "session_id": rec.delegation_id, "content": "done"}
    )
    await models.emit(dm,
        {"type": "result", "session_id": rec.delegation_id, "is_error": False}
    )
    assert rec.state == "completed"
    assert [sid for sid, _ in injected] == [parent.id]
    assert "done" in injected[0][1]


@pytest.mark.asyncio
async def test_parent_delegation_still_fails_fast_on_error(models, dm, mgr, db, monkeypatch):
    """The wait-for-children rule must not swallow failures: an error result
    finalises immediately even with a sub-delegation in flight, otherwise a
    crashed middle agent would hang its caller forever."""
    injected: list[tuple[str, str]] = []

    async def capture(sid, prompt, attachment_ids=None, *, source="human", work_id=None, delivery_id=None, **kwargs):
        injected.append((sid, prompt))
        return await models.start_message(mgr, sid, prompt, source=source, work_id=work_id)

    monkeypatch.setattr(mgr, "start_message", capture)
    octo = await db.get_system_agent()
    await _make_agent(db, "Vera")
    await _make_agent(db, "Pete")
    parent = await _make_session(mgr, octo["id"], name="parent")

    vera_rec = await dm.start_delegation(
        parent_session_id=parent.id, agent_name="vera", request="ask pete",
    )
    await dm.start_delegation(
        parent_session_id=vera_rec.delegation_id, agent_name="pete", request="r",
    )
    injected.clear()

    await models.emit(dm,
        {"type": "result", "session_id": vera_rec.delegation_id, "is_error": True}
    )
    assert vera_rec.state == "failed"
    assert [sid for sid, _ in injected] == [parent.id]


@pytest.mark.asyncio
async def test_error_injection_on_result_error(models, dm, mgr, db, monkeypatch):
    """result(is_error=True) → [agent-error:...] turn, state=failed."""
    injected: list[tuple[str, str]] = []

    async def capture(sid, prompt, attachment_ids=None, *, source="human", work_id=None, delivery_id=None, **kwargs):
        injected.append((sid, prompt))
        return await models.start_message(mgr, sid, prompt, source=source, work_id=work_id)

    monkeypatch.setattr(mgr, "start_message", capture)
    octo = await db.get_system_agent()
    await _make_agent(db, "Vera")
    parent = await _make_session(mgr, octo["id"])
    rec = await dm.start_delegation(
        parent_session_id=parent.id, agent_name="vera", request="r",
    )
    injected.clear()

    cid = rec.delegation_id
    await models.emit(dm, {"type": "result", "session_id": cid, "is_error": True})

    assert rec.state == "failed"
    assert len(injected) == 1
    _, prompt = injected[0]
    assert prompt.startswith(f"[agent-error:Vera delegation={cid}")


@pytest.mark.asyncio
async def test_error_injection_on_error_event(models, dm, mgr, db, monkeypatch):
    """A committed child error completes its captured request and terminates."""
    injected: list[tuple[str, str]] = []

    async def capture(sid, prompt, attachment_ids=None, *, source="human", work_id=None, delivery_id=None, **kwargs):
        injected.append((sid, prompt))
        return await models.start_message(mgr, sid, prompt, source=source, work_id=work_id)

    monkeypatch.setattr(mgr, "start_message", capture)
    octo = await db.get_system_agent()
    await _make_agent(db, "Vera")
    parent = await _make_session(mgr, octo["id"])
    rec = await dm.start_delegation(
        parent_session_id=parent.id, agent_name="vera", request="r",
    )
    injected.clear()

    cid = rec.delegation_id
    await models.emit(dm, {
        "type": "error", "session_id": cid, "message": "harness died",
    })

    assert rec.state == "failed"
    assert "harness died" in rec.error
    assert len(injected) == 1


@pytest.mark.parametrize("event", [
    {"type": "error", "code": "turn_timeout_retry", "retrying": True},
    {"type": "error", "code": "transient_retry", "retrying": True},
    {"type": "error", "code": "stale_session", "retrying": True},
    {"type": "result", "is_error": True, "retrying": True},
])
async def test_recovery_events_do_not_terminate_a_delegation(models, dm, mgr, db, monkeypatch, event):
    injected = []

    async def capture(sid, prompt, attachment_ids=None, *, source="human", work_id=None, delivery_id=None, **kwargs):
        injected.append((sid, prompt))
        return await models.start_message(mgr, sid, prompt, source=source, work_id=work_id)

    monkeypatch.setattr(mgr, "start_message", capture)
    octo = await db.get_system_agent()
    await _make_agent(db, "Vera")
    parent = await _make_session(mgr, octo["id"])
    rec = await dm.start_delegation(parent_session_id=parent.id, agent_name="Vera", request="r")
    injected.clear()

    await models.emit(dm, {**event, "session_id": rec.delegation_id})
    assert rec.state == "running"
    assert rec.delegation_id in mgr.sessions
    assert injected == []
    await models.emit(dm, {
        "type": "assistant_text", "session_id": rec.delegation_id, "content": "recovered reply"
    })
    await models.emit(dm, {"type": "result", "session_id": rec.delegation_id, "is_error": False})
    assert rec.state == "completed"
    assert len(injected) == 1
    assert "recovered reply" in injected[0][1]


@pytest.mark.parametrize("failure", ["idle", "transient"])
async def test_delegation_waits_for_real_turn_recovery(models, dm, mgr, db, monkeypatch, failure):
    import asyncio

    from server.config import settings as cfg
    from server.harness import HarnessEvent
    from tests.fake_run import FakeRunBase

    injected = []
    submit = mgr.start_message
    original_receipts = []
    admitted = asyncio.Event()

    async def capture(sid, prompt, attachment_ids=None, *, source="human", work_id=None, delivery_id=None, **kwargs):
        if sid == parent.id:
            injected.append((sid, prompt))
            return await models.start_message(mgr, sid, prompt, source=source, work_id=work_id)
        receipt = await submit(sid, prompt, attachment_ids, source=source, work_id=work_id,
                               delivery_id=delivery_id, **kwargs)
        original_receipts.append(receipt)
        return receipt

    monkeypatch.setattr(mgr, "start_message", capture)
    monkeypatch.setattr(cfg, "turn_idle_timeout_seconds", 0.1)
    monkeypatch.setattr(cfg, "turn_max_seconds", 0)
    monkeypatch.setattr(type(mgr), "_TRANSIENT_RETRY_BASE_DELAY", 0.0)
    octo = await db.get_system_agent()
    await _make_agent(db, "Vera")
    parent = await _make_session(mgr, octo["id"])

    class RecoveryRun(FakeRunBase):
        def __init__(self, recovering=False):
            self.recovering = recovering
            self.unblocked = asyncio.Event()

        async def start(self, *args, **kwargs):
            await admitted.wait()
            assert rec.state == "running"
            assert child.id in mgr.sessions

        async def stop(self):
            self.unblocked.set()

        async def stream(self):
            await admitted.wait()
            yield HarnessEvent(type="session_started", session_id="saved-thread")
            if self.recovering:
                yield HarnessEvent(type="text", content="final review")
                yield HarnessEvent(type="text", content="second finding")
                yield HarnessEvent(type="result", session_id="saved-thread")
            elif failure == "idle":
                yield HarnessEvent(type="text", content="obsolete partial review")
                yield HarnessEvent(type="tool_use", tool_name="Read", tool_use_id="read-1")
                await self.unblocked.wait()
            else:
                yield HarnessEvent(type="text", content="obsolete partial review")
                yield HarnessEvent(type="result", is_error=True, content="API Error: 529 Overloaded")

    runs = iter([RecoveryRun(), RecoveryRun(recovering=True)])
    monkeypatch.setattr(mgr, "_make_run", lambda *args, **kwargs: next(runs))
    events = []

    async def observe(event):
        if event.get("session_id") == child.id:
            events.append(event)

    mgr.on_broadcast("original-recovery-prefix", observe)
    rec = await dm.start_delegation(parent_session_id=parent.id, agent_name="Vera", request="review")
    child = mgr.sessions[rec.delegation_id]
    admitted.set()
    receipt, = original_receipts
    completion = await asyncio.wait_for(receipt.wait(), 5)
    assert completion.recording_state == "committed" and completion.outcome.status == "success"
    await mgr.await_session_execution(child.id, timeout=5)
    await dm.confirm_recording()
    assert any(e.get("retrying") for e in events)
    assert rec.state == "completed"
    assert len(injected) == 1
    assert injected[0][0] == parent.id
    assert "[agent-reply:Vera" in injected[0][1] and "final review" in injected[0][1]
    assert "second finding" in injected[0][1]
    assert "obsolete partial review" not in injected[0][1]
    text_events = [e for e in events if e["type"] == "assistant_text"]
    assert len({e["attempt_id"] for e in text_events}) == 2


@pytest.mark.asyncio
async def test_post_terminal_events_ignored(models, dm, mgr, db, monkeypatch):
    """Once a delegation is terminal, late events don't reopen it."""
    monkeypatch.setattr(mgr, "start_message", models.start_for(mgr))
    octo = await db.get_system_agent()
    await _make_agent(db, "Vera")
    parent = await _make_session(mgr, octo["id"])
    rec = await dm.start_delegation(
        parent_session_id=parent.id, agent_name="vera", request="r",
    )
    cid = rec.delegation_id
    await models.emit(dm, {"type": "result", "session_id": cid, "is_error": False})
    state_before = rec.state
    await models.emit(dm, {"type": "assistant_text", "session_id": cid, "content": "late"})
    await models.emit(dm, {"type": "result", "session_id": cid, "is_error": True})
    assert rec.state == state_before


@pytest.mark.asyncio
async def test_empty_reply_gets_placeholder(models, dm, mgr, db, monkeypatch):
    """A child that ends successfully without any assistant_text still
    gets a reply injection, with a placeholder body."""
    injected: list[tuple[str, str]] = []

    async def capture(sid, prompt, attachment_ids=None, *, source="human", work_id=None, delivery_id=None, **kwargs):
        injected.append((sid, prompt))
        return await models.start_message(mgr, sid, prompt, source=source, work_id=work_id)

    monkeypatch.setattr(mgr, "start_message", capture)
    octo = await db.get_system_agent()
    await _make_agent(db, "Vera")
    parent = await _make_session(mgr, octo["id"])
    rec = await dm.start_delegation(
        parent_session_id=parent.id, agent_name="vera", request="r",
    )
    injected.clear()
    cid = rec.delegation_id
    await models.emit(dm, {"type": "result", "session_id": cid, "is_error": False})
    _, prompt = injected[0]
    assert "without producing any text" in prompt


# ---------------------------------------------------------------------------
# Cancellation, listing, concurrency
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancel_delegation(models, dm, mgr, db, monkeypatch):
    injected: list[tuple[str, str]] = []

    async def capture(sid, prompt, attachment_ids=None, *, source="human", work_id=None, delivery_id=None, **kwargs):
        injected.append((sid, prompt))
        return await models.start_message(mgr, sid, prompt, source=source, work_id=work_id)

    async def fake_interrupt(sid):
        return True

    monkeypatch.setattr(mgr, "start_message", capture)
    monkeypatch.setattr(mgr, "interrupt", fake_interrupt)
    octo = await db.get_system_agent()
    await _make_agent(db, "Vera")
    parent = await _make_session(mgr, octo["id"])
    rec = await dm.start_delegation(
        parent_session_id=parent.id, agent_name="vera", request="r",
    )
    injected.clear()
    updated = await dm.cancel_delegation(rec.delegation_id, reason="user clicked stop")
    assert updated.state == "cancelled"
    assert len(injected) == 1
    _, prompt = injected[0]
    assert "agent-error:Vera" in prompt
    assert "user clicked stop" in prompt
    # Idempotent.
    again = await dm.cancel_delegation(rec.delegation_id)
    assert again.state == "cancelled"


# ---------------------------------------------------------------------------
# follow_up_delegation: continue a prior delegation in the same child session
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_follow_up_happy_path(models, dm, mgr, db, monkeypatch):
    """Parent follow-up creates a new round in the retained conversation."""
    injected: list[tuple[str, str]] = []

    async def capture(sid, prompt, attachment_ids=None, *, source="human", work_id=None, delivery_id=None, **kwargs):
        injected.append((sid, prompt))
        return await models.start_message(mgr, sid, prompt, source=source, work_id=work_id)

    monkeypatch.setattr(mgr, "start_message", capture)
    octo = await db.get_system_agent()
    await _make_agent(db, "Vera")
    parent = await _make_session(mgr, octo["id"], name="octo")
    rec = await dm.start_delegation(
        parent_session_id=parent.id, agent_name="vera", request="round 1",
    )
    # This original body must never be mixed into the follow-up's reply.
    await models.emit(dm, {
        "type": "assistant_text", "session_id": rec.delegation_id, "content": "First round original reply.",
    })
    # Drive Vera's first round to terminal.
    await models.emit(dm, {
        "type": "result", "session_id": rec.delegation_id, "is_error": False,
    })
    assert rec.state == "completed"
    child = mgr.get_session(rec.delegation_id)
    assert child is not None

    # Follow up.
    injected.clear()
    updated = await dm.follow_up_delegation(
        parent_session_id=parent.id,
        delegation_id=rec.delegation_id,
        request="round 2 — please re-check finding 3",
    )
    # Same identity, fresh round.
    assert updated.delegation_id == rec.delegation_id
    assert updated.state == "running"
    assert updated.request == "round 2 — please re-check finding 3"
    assert updated.error is None
    assert updated.finished_at is None
    assert mgr.get_session(rec.delegation_id) is child
    # The new request was delivered to the child's session.
    assert injected
    sid, prompt = injected[0]
    assert sid == rec.delegation_id
    assert "follow-up" in prompt.lower()
    assert "round 2 — please re-check finding 3" in prompt
    # The full chain still works: Vera's NEW reply lands on Octo.
    injected.clear()
    await models.emit(dm, {
        "type": "assistant_text",
        "session_id": rec.delegation_id,
        "content": "Re-checked. Looks fixed.",
    })
    await models.emit(dm, {
        "type": "result", "session_id": rec.delegation_id, "is_error": False,
    })
    assert updated.state == "completed"
    octo_injects = [p for s, p in injected if s == parent.id]
    assert len(octo_injects) == 1
    assert "Re-checked. Looks fixed." in octo_injects[0]
    assert "First round original reply." not in octo_injects[0]


@pytest.mark.asyncio
async def test_follow_up_rejects_while_running(models, dm, mgr, db, monkeypatch):
    monkeypatch.setattr(mgr, "start_message", models.start_for(mgr))
    octo = await db.get_system_agent()
    await _make_agent(db, "Vera")
    parent = await _make_session(mgr, octo["id"])
    rec = await dm.start_delegation(
        parent_session_id=parent.id, agent_name="vera", request="r",
    )
    assert rec.state == "running"
    with pytest.raises(DelegationError) as ex:
        await dm.follow_up_delegation(
            parent_session_id=parent.id,
            delegation_id=rec.delegation_id,
            request="round 2",
        )
    assert ex.value.status_code == 409
    assert "still running" in str(ex.value)


@pytest.mark.asyncio
async def test_follow_up_rejects_unknown_delegation(models, dm, mgr, db, monkeypatch):
    monkeypatch.setattr(mgr, "start_message", models.start_for(mgr))
    octo = await db.get_system_agent()
    parent = await _make_session(mgr, octo["id"])
    with pytest.raises(DelegationError) as ex:
        await dm.follow_up_delegation(
            parent_session_id=parent.id,
            delegation_id="ghost",
            request="hi",
        )
    assert ex.value.status_code == 404


@pytest.mark.asyncio
async def test_follow_up_rejects_wrong_parent(models, dm, mgr, db, monkeypatch):
    """A delegation can only be followed up by its OWN parent — a
    different session can't continue someone else's conversation."""
    monkeypatch.setattr(mgr, "start_message", models.start_for(mgr))
    octo = await db.get_system_agent()
    await _make_agent(db, "Vera")
    parent_a = await _make_session(mgr, octo["id"], name="A")
    parent_b = await _make_session(mgr, octo["id"], name="B")
    rec = await dm.start_delegation(
        parent_session_id=parent_a.id, agent_name="vera", request="r",
    )
    await models.emit(dm, {
        "type": "result", "session_id": rec.delegation_id, "is_error": False,
    })
    with pytest.raises(DelegationError) as ex:
        await dm.follow_up_delegation(
            parent_session_id=parent_b.id,
            delegation_id=rec.delegation_id,
            request="hi",
        )
    assert ex.value.status_code == 404


@pytest.mark.asyncio
async def test_follow_up_rejects_empty_request(models, dm, mgr, db, monkeypatch):
    monkeypatch.setattr(mgr, "start_message", models.start_for(mgr))
    octo = await db.get_system_agent()
    await _make_agent(db, "Vera")
    parent = await _make_session(mgr, octo["id"])
    rec = await dm.start_delegation(
        parent_session_id=parent.id, agent_name="vera", request="r",
    )
    await models.emit(dm, {
        "type": "result", "session_id": rec.delegation_id, "is_error": False,
    })
    with pytest.raises(DelegationError) as ex:
        await dm.follow_up_delegation(
            parent_session_id=parent.id,
            delegation_id=rec.delegation_id,
            request="   ",
        )
    assert ex.value.status_code == 400


@pytest.mark.asyncio
async def test_route_follow_up_requires_live_parent(models, client, monkeypatch):
    """Vera-round-5 finding: a follow-up from a parent session that's
    no longer in the live sessions map (archived / deleted between
    rounds) must 404 — otherwise the manager would round-reset the
    record and start the child, then silently drop the terminal
    turn because there's no live parent to inject into."""
    async def noop(sid, prompt, attachment_ids=None, *, source="human", work_id=None, delivery_id=None, **kwargs):
        return await models.start_message(session_manager, sid, prompt, source=source, work_id=work_id)

    monkeypatch.setattr(session_manager, "start_message", noop)

    agents = (await client.get("/api/agents", headers=client.headers)).json()
    octo = next(a for a in agents if a["is_system"])
    await _post_agent(client, "Vera")
    parent = await _post_session(client, octo["id"])
    create = await client.post(
        f"/api/sessions/{parent['id']}/delegations",
        json={"agent_name": "vera", "request": "round 1"},
        headers=client.headers,
    )
    did = create.json()["delegation_id"]
    await models.emit(singleton_delegation_manager, {
        "type": "result", "session_id": did, "is_error": False,
    })
    # Evict the parent session from the live map (simulating archive
    # / hard delete between rounds).
    session_manager.sessions.pop(parent["id"], None)

    r = await client.post(
        f"/api/sessions/{parent['id']}/delegations/{did}/follow-up",
        json={"request": "round 2"},
        headers=client.headers,
    )
    assert r.status_code == 404
    # The record must still be in its terminal state — the route
    # rejected before round-reset, so subsequent attempts (if the
    # user unarchives the parent) still see the original state.
    rec = singleton_delegation_manager.get_delegation(did)
    assert rec is not None
    assert rec.state == "completed"


@pytest.mark.asyncio
async def test_route_follow_up(models, client, monkeypatch):
    """HTTP path mirrors the manager: 200/201 happy path; 404 for
    unknown delegation; 409 while running."""
    async def noop(sid, prompt, attachment_ids=None, *, source="human", work_id=None, delivery_id=None, **kwargs):
        return await models.start_message(session_manager, sid, prompt, source=source, work_id=work_id)

    monkeypatch.setattr(session_manager, "start_message", noop)

    agents = (await client.get("/api/agents", headers=client.headers)).json()
    octo = next(a for a in agents if a["is_system"])
    await _post_agent(client, "Vera")
    parent = await _post_session(client, octo["id"])
    create = await client.post(
        f"/api/sessions/{parent['id']}/delegations",
        json={"agent_name": "vera", "request": "round 1"},
        headers=client.headers,
    )
    did = create.json()["delegation_id"]

    # Drive the delegation to terminal via the broadcast bus so the
    # record can be followed-up.
    await models.emit(singleton_delegation_manager, {
        "type": "result", "session_id": did, "is_error": False,
    })

    r = await client.post(
        f"/api/sessions/{parent['id']}/delegations/{did}/follow-up",
        json={"request": "round 2"},
        headers=client.headers,
    )
    assert r.status_code in (200, 201), r.text
    body = r.json()
    assert body["delegation_id"] == did
    assert body["state"] == "running"
    assert body["request"] == "round 2"


@pytest.mark.asyncio
async def test_cancel_cascades_to_descendants(models, dm, mgr, db, monkeypatch):
    """Vera's round-3 finding: cancelling a delegation must cascade
    to its descendants, otherwise a grandchild keeps burning tokens
    and its eventual reply lands on a cancelled parent.

    Scenario: Octo→Vera→Pete. Cancel Vera. Pete should also be
    cancelled with a reason naming the parent cancel."""
    injected: list[tuple[str, str]] = []

    async def capture(sid, prompt, attachment_ids=None, *, source="human", work_id=None, delivery_id=None, **kwargs):
        injected.append((sid, prompt))
        return await models.start_message(mgr, sid, prompt, source=source, work_id=work_id)

    async def fake_interrupt(sid):
        return True

    monkeypatch.setattr(mgr, "start_message", capture)
    monkeypatch.setattr(mgr, "interrupt", fake_interrupt)
    octo = await db.get_system_agent()
    await _make_agent(db, "Vera")
    await _make_agent(db, "Pete")
    octo_sess = await _make_session(mgr, octo["id"], name="octo")
    vera_rec = await dm.start_delegation(
        parent_session_id=octo_sess.id, agent_name="vera", request="r1",
    )
    async with _model_scope(dm, vera_rec) as scope:
        pete_rec = await dm.start_delegation(
            parent_session_id=vera_rec.delegation_id,
            agent_name="pete",
            request="r2", scope=scope,
        )
    assert pete_rec.state == "running"
    injected.clear()

    await dm.cancel_delegation(
        vera_rec.delegation_id, reason="user stop"
    )

    assert dm.get_delegation(vera_rec.delegation_id).state == "cancelled"
    # Pete cascade-cancelled too.
    assert dm.get_delegation(pete_rec.delegation_id).state == "cancelled"
    assert "parent delegation cancelled" in (
        dm.get_delegation(pete_rec.delegation_id).error or ""
    )
    # The descendant retains its cancellation state without waking the
    # closed branch. Only the still-valid caller receives a terminal turn.
    pete_injects = [p for s, p in injected if s == vera_rec.delegation_id]
    vera_injects = [p for s, p in injected if s == octo_sess.id]
    assert pete_injects == []
    assert len(vera_injects) == 1
    assert "agent-error:Vera" in vera_injects[0]
    assert "user stop" in vera_injects[0]


@pytest.mark.asyncio
async def test_cancel_delegation_single_inject_under_interrupt_broadcast(
    models, dm, mgr, db, monkeypatch
):
    """The bug Vera caught: cancel_delegation() calls interrupt()
    which broadcasts an `error` event before returning; without the
    state-flip-first dance, `_on_broadcast` would catch that error,
    finalize the record as `failed`, and inject `[agent-error
    reason=child error]`. Then cancel_delegation would inject a
    SECOND `[agent-error reason=cancelled]` — two terminal turns
    for one cancellation.

    With the fix, state flips to "cancelled" before interrupt fires,
    so `_on_broadcast` short-circuits on `rec.state != "running"`."""
    injected: list[tuple[str, str]] = []

    async def capture(sid, prompt, attachment_ids=None, *, source="human", work_id=None, delivery_id=None, **kwargs):
        injected.append((sid, prompt))
        return await models.start_message(mgr, sid, prompt, source=source, work_id=work_id)

    monkeypatch.setattr(mgr, "start_message", capture)

    async def fake_interrupt(sid):
        # Mirror the real session_manager.interrupt: broadcast an
        # `error` event on the child session before returning.
        await models.emit(dm, {
            "type": "error",
            "session_id": sid,
            "message": "(interrupted by user)",
        }, wait=False)
        return True

    monkeypatch.setattr(mgr, "interrupt", fake_interrupt)
    octo = await db.get_system_agent()
    await _make_agent(db, "Vera")
    parent = await _make_session(mgr, octo["id"])
    rec = await dm.start_delegation(
        parent_session_id=parent.id, agent_name="vera", request="r",
    )
    injected.clear()
    updated = await dm.cancel_delegation(
        rec.delegation_id, reason="user stop"
    )
    assert updated.state == "cancelled"
    # Single terminal injection, NOT two.
    assert len(injected) == 1, (
        f"expected one terminal turn for one cancellation, "
        f"got {len(injected)}: {injected!r}"
    )
    _, prompt = injected[0]
    # The reason should be the cancel reason, not the "child error"
    # placeholder — confirms the cancel path won, not the error path.
    assert "user stop" in prompt
    assert "child session error" not in prompt


@pytest.mark.asyncio
async def test_terminal_injection_is_idempotent(models, dm, mgr, db, monkeypatch):
    """Belt-and-suspenders: even if two terminal-producing events
    race past the state guard (e.g. a `result` and an `error` from a
    crashing child arrive in quick succession), exactly one
    `[agent-…]` turn lands in the parent."""
    injected: list[tuple[str, str]] = []

    async def capture(sid, prompt, attachment_ids=None, *, source="human", work_id=None, delivery_id=None, **kwargs):
        injected.append((sid, prompt))
        return await models.start_message(mgr, sid, prompt, source=source, work_id=work_id)

    monkeypatch.setattr(mgr, "start_message", capture)
    octo = await db.get_system_agent()
    await _make_agent(db, "Vera")
    parent = await _make_session(mgr, octo["id"])
    rec = await dm.start_delegation(
        parent_session_id=parent.id, agent_name="vera", request="r",
    )
    injected.clear()
    # Complete the real recorded request, then try to inject its terminal
    # twice again. The one-shot relay guard must preserve the first return.
    await models.emit(dm, {"type": "result", "session_id": rec.delegation_id, "is_error": False})
    await dm._inject_terminal(rec)
    await dm._inject_terminal(rec)
    assert len(injected) == 1


@pytest.mark.asyncio
async def test_list_delegations_newest_first(models, dm, mgr, db, monkeypatch):
    monkeypatch.setattr(mgr, "start_message", models.start_for(mgr))
    octo = await db.get_system_agent()
    await _make_agent(db, "Vera")
    await _make_agent(db, "Pete")
    parent = await _make_session(mgr, octo["id"])
    r1 = await dm.start_delegation(
        parent_session_id=parent.id, agent_name="vera", request="r1",
    )
    r2 = await dm.start_delegation(
        parent_session_id=parent.id, agent_name="pete", request="r2",
    )
    listed = dm.list_delegations(parent.id)
    assert [r.delegation_id for r in listed] == [r2.delegation_id, r1.delegation_id]


@pytest.mark.asyncio
async def test_concurrent_delegations_to_same_target(models, dm, mgr, db, monkeypatch):
    """Two delegations to Vera in flight at once: both run, both reply
    independently, parent sees both terminal turns."""
    injected: list[tuple[str, str]] = []

    async def capture(sid, prompt, attachment_ids=None, *, source="human", work_id=None, delivery_id=None, **kwargs):
        injected.append((sid, prompt))
        return await models.start_message(mgr, sid, prompt, source=source, work_id=work_id)

    monkeypatch.setattr(mgr, "start_message", capture)
    octo = await db.get_system_agent()
    await _make_agent(db, "Vera")
    parent = await _make_session(mgr, octo["id"])

    rec1 = await dm.start_delegation(
        parent_session_id=parent.id, agent_name="vera", request="r1",
    )
    rec2 = await dm.start_delegation(
        parent_session_id=parent.id, agent_name="vera", request="r2",
    )
    assert rec1.delegation_id != rec2.delegation_id
    injected.clear()

    # Send each child its own result.
    await models.emit(dm, {"type": "assistant_text", "session_id": rec1.delegation_id, "content": "one"})
    await models.emit(dm, {"type": "result", "session_id": rec1.delegation_id, "is_error": False})
    await models.emit(dm, {"type": "assistant_text", "session_id": rec2.delegation_id, "content": "two"})
    await models.emit(dm, {"type": "result", "session_id": rec2.delegation_id, "is_error": False})

    assert rec1.state == "completed"
    assert rec2.state == "completed"
    # Two terminal injections into the same parent, ordered by completion.
    assert len(injected) == 2
    assert all(t[0] == parent.id for t in injected)
    assert f"delegation={rec1.delegation_id}" in injected[0][1]
    assert f"delegation={rec2.delegation_id}" in injected[1][1]


# ---------------------------------------------------------------------------
# Bridges don't fan out delegation sessions
# ---------------------------------------------------------------------------


@pytest.fixture
async def client(db):
    """HTTP test client with the module-level singletons rebound to the
    per-test in-memory DB. Bridges/scheduler are stood up so app
    lifespan doesn't matter for these tests."""
    session_manager.sessions.clear()
    session_manager._admission_closed = False
    await session_manager.initialize(db)

    agents_mod.set_manager(AgentManager(db))
    runner = ScheduleRunner(session_manager, db)
    await runner.initialize()
    schedules_mod._db = db
    schedules_mod._runner = runner

    singleton_delegation_manager.bind(session_mgr=session_manager, db=db)

    transport = ASGITransport(app=app)
    owner = (await db.get_system_agent())["user_id"]
    token = await UserManager(db).issue_token(owner)
    async with AsyncClient(transport=transport, base_url="http://test",
                           headers={"Authorization": "Bearer " + token}) as c:
        yield c

    singleton_delegation_manager.shutdown()
    # Drain the registry between tests so list_delegations doesn't leak
    # state into the next case.
    singleton_delegation_manager._records.clear()
    await runner.shutdown()


async def _post_agent(client, name, **extra):
    r = await client.post(
        "/api/agents", json={"name": name, **extra}, headers=client.headers
    )
    assert r.status_code == 201, r.text
    return r.json()


async def _post_session(client, agent_id, name="parent"):
    r = await client.post(
        "/api/sessions",
        json={"name": name, "agent_id": agent_id},
        headers=client.headers,
    )
    assert r.status_code == 201, r.text
    return r.json()


@pytest.mark.asyncio
async def test_route_start_delegation(models, client, monkeypatch):
    """POST /sessions/{sid}/delegations succeeds; child session shows
    up via GET /sessions/{child_sid}."""
    monkeypatch.setattr(session_manager, "start_message", models.start_for(session_manager))

    agents = (await client.get("/api/agents", headers=client.headers)).json()
    octo = next(a for a in agents if a["is_system"])
    await _post_agent(client, "Vera")
    parent = await _post_session(client, octo["id"])

    r = await client.post(
        f"/api/sessions/{parent['id']}/delegations",
        json={"agent_name": "vera", "request": "review the dashboard"},
        headers=client.headers,
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["state"] == "running"
    assert body["target_agent_name"] == "Vera"
    assert body["parent_session_id"] == parent["id"]
    assert body["delegation_id"] == body["sub_session_id"]

    # Child session is visible via the normal GET route.
    cid = body["delegation_id"]
    detail = (await client.get(f"/api/sessions/{cid}", headers=client.headers)).json()
    assert detail["origin"] == "delegation"
    assert detail["parent_session_id"] == parent["id"]
    assert detail["delegation_request"] == "review the dashboard"


@pytest.mark.asyncio
async def test_route_list_and_cancel(models, client, monkeypatch):
    monkeypatch.setattr(session_manager, "start_message", models.start_for(session_manager))

    async def fake_interrupt(sid):
        return True

    monkeypatch.setattr(session_manager, "interrupt", fake_interrupt)

    agents = (await client.get("/api/agents", headers=client.headers)).json()
    octo = next(a for a in agents if a["is_system"])
    await _post_agent(client, "Vera")
    parent = await _post_session(client, octo["id"])

    create = await client.post(
        f"/api/sessions/{parent['id']}/delegations",
        json={"agent_name": "vera", "request": "go"},
        headers=client.headers,
    )
    did = create.json()["delegation_id"]

    listing = await client.get(
        f"/api/sessions/{parent['id']}/delegations", headers=client.headers
    )
    assert listing.status_code == 200
    rows = listing.json()
    assert [r["delegation_id"] for r in rows] == [did]

    cancel = await client.post(
        f"/api/sessions/{parent['id']}/delegations/{did}/cancel",
        json={"reason": "test"},
        headers=client.headers,
    )
    assert cancel.status_code == 200, cancel.text
    assert cancel.json()["state"] == "cancelled"


@pytest.mark.asyncio
async def test_route_404s(models, client, monkeypatch):
    monkeypatch.setattr(session_manager, "start_message", models.start_for(session_manager))
    # Unknown parent session.
    r = await client.post(
        "/api/sessions/ghost/delegations",
        json={"agent_name": "vera", "request": "go"},
        headers=client.headers,
    )
    assert r.status_code == 404

    # Known parent, unknown agent.
    agents = (await client.get("/api/agents", headers=client.headers)).json()
    octo = next(a for a in agents if a["is_system"])
    parent = await _post_session(client, octo["id"])
    r = await client.post(
        f"/api/sessions/{parent['id']}/delegations",
        json={"agent_name": "ghost", "request": "go"},
        headers=client.headers,
    )
    assert r.status_code == 404

    # Cancel an unknown delegation.
    r = await client.post(
        f"/api/sessions/{parent['id']}/delegations/nope/cancel",
        json={},
        headers=client.headers,
    )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_route_self_delegation_409(models, client, monkeypatch):
    monkeypatch.setattr(session_manager, "start_message", models.start_for(session_manager))
    agents = (await client.get("/api/agents", headers=client.headers)).json()
    octo = next(a for a in agents if a["is_system"])
    parent = await _post_session(client, octo["id"])
    r = await client.post(
        f"/api/sessions/{parent['id']}/delegations",
        json={"agent_name": octo["name"], "request": "x"},
        headers=client.headers,
    )
    assert r.status_code == 409


# ---------------------------------------------------------------------------
# Caller-aware ask: child question → parent injection (Phase 3)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_terminated_delegations_ignore_questions(
    models, dm, mgr, db, monkeypatch
):
    """A late question event after a child finished is dropped — the
    parent's reply has already been injected."""
    monkeypatch.setattr(mgr, "start_message", models.start_for(mgr))
    octo = await db.get_system_agent()
    await _make_agent(db, "Vera")
    parent = await _make_session(mgr, octo["id"])
    rec = await dm.start_delegation(
        parent_session_id=parent.id, agent_name="vera", request="r",
    )
    # Drive to terminal.
    await models.emit(dm, {
        "type": "result", "session_id": rec.delegation_id, "is_error": False,
    })
    # Now a stray question_request should be a no-op.
    captured = []

    async def capture(sid, prompt, attachment_ids=None, *, source="human", work_id=None, delivery_id=None, **kwargs):
        captured.append((sid, prompt))
        return await models.start_message(mgr, sid, prompt, source=source, work_id=work_id)

    monkeypatch.setattr(mgr, "start_message", capture)
    await models.emit(dm, {
        "type": "question_request",
        "session_id": rec.delegation_id,
        "question_id": "q-stray",
        "questions": [{"question": "huh?", "options": []}],
    })
    assert captured == []


# ---------------------------------------------------------------------------
# answer_pending_question on the manager
# ---------------------------------------------------------------------------


def _seed_pending_question(
    child_session, question_id: str, questions: list[dict]
) -> None:
    """Wire a fake pending question onto a child session without going
    through the full create_pending_question flow (which would need a
    persistence path)."""
    from server.session_manager import PendingQuestion

    child_session._questions[question_id] = PendingQuestion(
        question_id=question_id, questions=questions
    )


@pytest.mark.asyncio
async def test_answer_pending_question_happy(models, dm, mgr, db, monkeypatch):
    monkeypatch.setattr(mgr, "start_message", models.start_for(mgr))
    octo = await db.get_system_agent()
    await _make_agent(db, "Vera")
    parent = await _make_session(mgr, octo["id"])
    rec = await dm.start_delegation(
        parent_session_id=parent.id, agent_name="vera", request="r",
    )
    child = mgr.get_session(rec.delegation_id)
    _seed_pending_question(
        child, "q-1", [{"question": "X?", "options": [{"label": "A"}, {"label": "B"}]}]
    )

    # Capture answer_question call shape.
    called: dict = {}

    async def fake_answer(sid, qid, answers, **kwargs):
        called["sid"] = sid
        called["qid"] = qid
        called["answers"] = answers
        return True

    monkeypatch.setattr(mgr, "answer_question", fake_answer)

    out = await dm.answer_pending_question(rec.delegation_id, "A")
    assert out["ok"] is True
    assert out["question_id"] == "q-1"
    assert called["sid"] == rec.delegation_id
    assert called["qid"] == "q-1"
    assert called["answers"] == [{"selected": ["A"], "text": None}]


@pytest.mark.asyncio
async def test_answer_pending_question_requires_complete_multi_question_batch(
    models, dm, mgr, db, monkeypatch
):
    monkeypatch.setattr(mgr, "start_message", models.start_for(mgr))
    octo = await db.get_system_agent()
    await _make_agent(db, "Vera")
    parent = await _make_session(mgr, octo["id"])
    rec = await dm.start_delegation(
        parent_session_id=parent.id, agent_name="vera", request="r",
    )
    child = mgr.get_session(rec.delegation_id)
    _seed_pending_question(
        child,
        "q-1",
        [
            {"question": "first?", "options": [{"label": "A"}]},
            {"question": "second?", "options": [{"label": "B"}]},
            {"question": "third?", "options": [{"label": "C"}]},
        ],
    )

    seen: dict = {}

    async def fake_answer(sid, qid, answers, **kwargs):
        seen["answers"] = answers
        return True

    monkeypatch.setattr(mgr, "answer_question", fake_answer)

    with pytest.raises(DelegationError) as exc:
        await dm.answer_pending_question(rec.delegation_id, "A")
    assert exc.value.status_code == 400
    assert not seen and any(item.question_id == "q-1" for item in mgr.get_pending_questions(child.id))
    answers = [{"selected": ["A"]}, {"text": "custom second"}, {"selected": ["C"]}]
    await dm.answer_pending_question(rec.delegation_id, answers=answers, question_id="q-1")
    assert seen["answers"] == [{"selected": ["A"], "text": None},
                                {"selected": [], "text": "custom second"},
                                {"selected": ["C"], "text": None}]


async def test_delegated_answers_preserve_multiple_choices_and_stable_question_identity(models, dm, mgr, db, monkeypatch):
    monkeypatch.setattr(mgr, "start_message", models.start_for(mgr))
    octo = await db.get_system_agent()
    await _make_agent(db, "Vera")
    parent = await _make_session(mgr, octo["id"])
    rec = await dm.start_delegation(parent_session_id=parent.id, agent_name="vera", request="r")
    child = mgr.get_session(rec.delegation_id)
    child.restore_message_state(*await db.message_sequence_state(child.id))
    _seed_pending_question(child, "batch", [
        {"id": "colors", "question": "Colors?", "multiSelect": True, "options": [{"label": "A"}, {"label": "B"}]},
        {"id": "note", "question": "Note?", "options": []},
    ])
    seen = []
    answer_question = mgr.answer_question
    async def answer(sid, qid, answers, **kwargs):
        result = await answer_question(sid, qid, answers, **kwargs)
        seen.append(answers)
        return result
    monkeypatch.setattr(mgr, "answer_question", answer)
    with pytest.raises(DelegationError):
        await dm.answer_pending_question(rec.delegation_id, answers=[{"text": "incomplete"}], question_id="batch")
    with pytest.raises(DelegationError):
        await dm.answer_pending_question(rec.delegation_id, answers=[{"text": "x"}, {"text": "y"}], question_id="old-batch")
    await dm.answer_pending_question(rec.delegation_id, answers={"colors": {"selected": ["A", "B"]}, "note": {"text": "details"}}, question_id="batch")
    assert seen == [[{"selected": ["A", "B"], "text": None}, {"selected": [], "text": "details"}]]


@pytest.mark.asyncio
async def test_answer_pending_question_rejects_empty_choice(
    models, dm, mgr, db, monkeypatch
):
    monkeypatch.setattr(mgr, "start_message", models.start_for(mgr))
    octo = await db.get_system_agent()
    await _make_agent(db, "Vera")
    parent = await _make_session(mgr, octo["id"])
    rec = await dm.start_delegation(
        parent_session_id=parent.id, agent_name="vera", request="r",
    )
    child = mgr.get_session(rec.delegation_id)
    _seed_pending_question(child, "q-1", [{"question": "?", "options": []}])
    with pytest.raises(DelegationError) as ex:
        await dm.answer_pending_question(rec.delegation_id, "   ")
    assert ex.value.status_code == 400


@pytest.mark.asyncio
async def test_answer_pending_question_when_no_pending(
    models, dm, mgr, db, monkeypatch
):
    monkeypatch.setattr(mgr, "start_message", models.start_for(mgr))
    octo = await db.get_system_agent()
    await _make_agent(db, "Vera")
    parent = await _make_session(mgr, octo["id"])
    rec = await dm.start_delegation(
        parent_session_id=parent.id, agent_name="vera", request="r",
    )
    with pytest.raises(DelegationError) as ex:
        await dm.answer_pending_question(rec.delegation_id, "A")
    assert ex.value.status_code == 409


@pytest.mark.asyncio
async def test_answer_pending_question_after_terminal(
    models, dm, mgr, db, monkeypatch
):
    monkeypatch.setattr(mgr, "start_message", models.start_for(mgr))
    octo = await db.get_system_agent()
    await _make_agent(db, "Vera")
    parent = await _make_session(mgr, octo["id"])
    rec = await dm.start_delegation(
        parent_session_id=parent.id, agent_name="vera", request="r",
    )
    await models.emit(dm, {
        "type": "result", "session_id": rec.delegation_id, "is_error": False,
    })
    with pytest.raises(DelegationError) as ex:
        await dm.answer_pending_question(rec.delegation_id, "A")
    assert ex.value.status_code == 409


@pytest.mark.asyncio
async def test_answer_pending_question_human_race_409(
    models, dm, mgr, db, monkeypatch
):
    """If session_manager.answer_question returns False (the human UI
    drained the queue first) we surface 409."""
    monkeypatch.setattr(mgr, "start_message", models.start_for(mgr))
    octo = await db.get_system_agent()
    await _make_agent(db, "Vera")
    parent = await _make_session(mgr, octo["id"])
    rec = await dm.start_delegation(
        parent_session_id=parent.id, agent_name="vera", request="r",
    )
    child = mgr.get_session(rec.delegation_id)
    _seed_pending_question(child, "q-1", [{"question": "?", "options": []}])

    async def fake_answer(*a, **k):
        return False

    monkeypatch.setattr(mgr, "answer_question", fake_answer)
    with pytest.raises(DelegationError) as ex:
        await dm.answer_pending_question(rec.delegation_id, "A")
    assert ex.value.status_code == 409


# ---------------------------------------------------------------------------
# HTTP: answer route
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_route_answer_happy(models, client, monkeypatch):
    monkeypatch.setattr(session_manager, "start_message", models.start_for(session_manager))

    agents = (await client.get("/api/agents", headers=client.headers)).json()
    octo = next(a for a in agents if a["is_system"])
    await _post_agent(client, "Vera")
    parent = await _post_session(client, octo["id"])

    create = await client.post(
        f"/api/sessions/{parent['id']}/delegations",
        json={"agent_name": "vera", "request": "go"},
        headers=client.headers,
    )
    did = create.json()["delegation_id"]

    # Seed the child's pending-question queue directly + patch the
    # underlying answer_question to capture.
    child = session_manager.get_session(did)
    _seed_pending_question(
        child, "q-route", [{"question": "?", "options": [{"label": "Yes"}]}]
    )

    async def fake_answer(sid, qid, answers, **kwargs):
        assert sid == did and qid == "q-route"
        assert answers == [{"selected": ["Yes"], "text": None}]
        return True

    monkeypatch.setattr(session_manager, "answer_question", fake_answer)

    r = await client.post(
        f"/api/sessions/{parent['id']}/delegations/{did}/answer",
        json={"choice": "Yes"},
        headers=client.headers,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["question_id"] == "q-route"


@pytest.mark.asyncio
async def test_route_answer_404_for_unknown_delegation(models, client, monkeypatch):
    monkeypatch.setattr(session_manager, "start_message", models.start_for(session_manager))
    agents = (await client.get("/api/agents", headers=client.headers)).json()
    octo = next(a for a in agents if a["is_system"])
    parent = await _post_session(client, octo["id"])
    r = await client.post(
        f"/api/sessions/{parent['id']}/delegations/nope/answer",
        json={"choice": "X"},
        headers=client.headers,
    )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_route_answer_409_when_no_pending(models, client, monkeypatch):
    monkeypatch.setattr(session_manager, "start_message", models.start_for(session_manager))
    agents = (await client.get("/api/agents", headers=client.headers)).json()
    octo = next(a for a in agents if a["is_system"])
    await _post_agent(client, "Vera")
    parent = await _post_session(client, octo["id"])
    create = await client.post(
        f"/api/sessions/{parent['id']}/delegations",
        json={"agent_name": "vera", "request": "go"},
        headers=client.headers,
    )
    did = create.json()["delegation_id"]
    r = await client.post(
        f"/api/sessions/{parent['id']}/delegations/{did}/answer",
        json={"choice": "X"},
        headers=client.headers,
    )
    assert r.status_code == 409


async def _terminal_under_closed_parent(models, dm, mgr, db, monkeypatch, state):
    from server.mcp_identity import close_work

    monkeypatch.setattr(mgr, "start_message", models.start_for(mgr))
    parent = await _make_session(mgr, (await db.get_system_agent())["id"], name="metric parent")
    outer_name, leaf_name = f"Metric outer {state}", f"Metric leaf {state}"
    await _make_agent(db, outer_name)
    await _make_agent(db, leaf_name)
    outer = await dm.start_delegation(
        parent_session_id=parent.id, agent_name=outer_name, request="coordinate review",
    )
    async with _model_scope(dm, outer) as scope:
        rec = await dm.start_delegation(
            parent_session_id=outer.delegation_id, agent_name=leaf_name, request="metric review", scope=scope,
        )
    close_work(outer.work_id)
    if state == "cancelled":
        await dm.cancel_delegation(rec.delegation_id)
    else:
        await models.emit(dm, {
            "session_id": rec.delegation_id, "type": "result", "is_error": state == "failed",
        })
    return rec


@pytest.mark.parametrize("state,expected", [("completed", True), ("failed", False), ("cancelled", False)])
async def test_terminal_metric_is_independent_of_closed_parent(models, dm, mgr, db, monkeypatch, state, expected):
    records = []
    monkeypatch.setattr("server.delegations._mon_record", records.append)
    rec = await _terminal_under_closed_parent(models, dm, mgr, db, monkeypatch, state)
    # Repeating the public cancellation must preserve the existing outcome
    # and avoid a second metric or terminal relay.
    await dm.cancel_delegation(rec.delegation_id)
    assert len(records) == 1 and records[0].ok is expected
    assert records[0].error_code == (None if expected else state)
    assert records[0].detail["state"] == state
    assert rec._terminal_disposition == "closed"


async def test_success_and_failure_delegation_metrics_query_real_storage(models, dm, mgr, db, monkeypatch, tmp_path):
    from server.monitor import MetricsStore, query

    store = MetricsStore(str(tmp_path / "delegation-metrics.db"))
    await store.initialize()
    monkeypatch.setattr("server.delegations._mon_record", store.record)
    try:
        for state in ("completed", "failed"):
            await _terminal_under_closed_parent(models, dm, mgr, db, monkeypatch, state)
        await store.drain()
        rows = await query.errors(store.conn, "24h")
        assert [(row["kind"], row["error_code"], row["n"]) for row in rows] == [("delegation", "failed", 1)]
        stored = await store.conn.execute_fetchall("SELECT ok,error_code FROM events ORDER BY rowid")
        assert [tuple(row) for row in stored] == [(1, None), (0, "failed")]
    finally:
        await store.stop()
