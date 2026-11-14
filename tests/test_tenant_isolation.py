"""One account cannot see or touch another's (multi-tenancy.md §5).

This is the test that has to exist. Ownership is enforced at ~100 routes, and
the failure mode of "remember to scope this lookup" is that one is forgotten,
nothing goes red, and the bug is found by a user seeing someone else's work.

So the shape here is deliberate: two real accounts, each with their own rows,
and then *every* surface asked the same question — can Vera reach Archer's?
A route added later that forgets its scope fails here rather than in someone's
browser. Covering "can she read it" and "can she change it" both, because a
404 on the read and a 200 on the write is a real and popular way to get this
half right.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from httpx import ASGITransport, AsyncClient

from server import agent_memory, crypto, deps
from server.agent_manager import AgentError, AgentManager
from server.config import settings
from server.database import Database
from server.main import app
from server.routers import agents as agents_router
from server.session_manager import session_manager
from server.users import UserManager
from server.workspace import WorkspaceError, paths_for
from tests.fake_run import FakeRunBase


def held_model_runs(monkeypatch):
    """Hold only the external Harness while real owners admit/retire work."""
    starts = asyncio.Queue()

    class Run(FakeRunBase):
        def __init__(self, session_id):
            self.session_id = session_id
            self.stopped = asyncio.Event()

        def is_alive(self):
            return not self.stopped.is_set()

        async def start(self, prompt, working_dir, resume_id=None, credential=None):
            self.prompt = prompt
            starts.put_nowait(self)

        async def stream(self):
            await self.stopped.wait()
            if False:
                yield

        async def stop(self):
            self.stopped.set()

        async def interrupt(self):
            await self.stop()

    monkeypatch.setattr(session_manager, "_make_run", lambda session, *args, **kwargs: Run(session.id))
    return starts


@pytest.fixture
async def two_accounts(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "master_key", "test-master-key")
    # Each account's files under this test's own root, which is also what makes
    # the working directories below legal: a session may only work inside its
    # owner's workspace (§6).
    monkeypatch.setattr(settings, "users_root", str(tmp_path / "users"))
    crypto._MASTER_CACHE.clear()

    db = Database(":memory:")
    await db.initialize()
    session_manager.sessions.clear()
    session_manager._admission_closed = False
    await session_manager.initialize(db)
    agents_router.set_manager(AgentManager(db))
    users = UserManager(db)
    deps.set_user_manager(users)

    archer = await users.create_user(username="archer", password="password1")
    vera = await users.create_user(username="vera", password="password2")
    archer_token = await users.issue_token(archer["id"])
    vera_token = await users.issue_token(vera["id"])

    # One agent and one session each, owned properly.
    mgr = AgentManager(db)
    archer_agent = await mgr.create_agent(name="Archer's agent", user_id=archer["id"])
    vera_agent = await mgr.create_agent(name="Vera's agent", user_id=vera["id"])
    archer_session = await session_manager.create_session(
        archer_agent["id"],
        "archer's work",
        str(paths_for(archer["id"]).ensure().workspace),
    )
    vera_session = await session_manager.create_session(
        vera_agent["id"],
        "vera's work",
        str(paths_for(vera["id"]).ensure().workspace),
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield {
            "client": c,
            "db": db,
            "archer": {"user": archer, "token": archer_token, "agent": archer_agent,
                       "session": archer_session},
            "vera": {"user": vera, "token": vera_token, "agent": vera_agent,
                     "session": vera_session},
        }
    await db.close()
    session_manager.sessions.clear()
    session_manager._admission_closed = False


def _as(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
async def test_a_session_created_under_an_agent_belongs_to_that_agents_owner(
    two_accounts,
):
    """The derivation everything else rests on: nobody passes an owner when
    creating a session, so if this is wrong every check below passes while
    protecting nothing."""
    ctx = two_accounts
    assert ctx["archer"]["session"].user_id == ctx["archer"]["user"]["id"]
    assert ctx["vera"]["session"].user_id == ctx["vera"]["user"]["id"]


@pytest.mark.asyncio
async def test_listing_shows_only_your_own(two_accounts):
    ctx = two_accounts
    c = ctx["client"]

    sessions = (await c.get("/api/sessions", headers=_as(ctx["vera"]["token"]))).json()
    names = {s["name"] for s in sessions}
    assert names == {"vera's work"}, f"Vera was shown {names}"

    agents = (await c.get("/api/agents", headers=_as(ctx["vera"]["token"]))).json()
    agent_names = {a["name"] for a in agents}
    assert "Archer's agent" not in agent_names, f"Vera was shown {agent_names}"


@pytest.mark.parametrize("entry", ["http", "manager"])
async def test_agent_delete_authorizes_before_database_memory_and_events(
    two_accounts, monkeypatch, entry
):
    ctx = two_accounts
    owner = ctx["archer"]["user"]["id"]
    manager = AgentManager(ctx["db"])
    target = await manager.create_agent(name="Empty owned agent", user_id=owner)
    memory = agent_memory.agent_memory_dir(target["id"], owner) / "MEMORY.md"
    memory.write_text("Keep the owner's memory\n")
    events = []

    async def capture(event, **scope):
        events.append((event, scope))

    monkeypatch.setattr(session_manager, "_broadcast", capture)
    for target_id in (target["id"], "missing-agent"):
        if entry == "http":
            response = await ctx["client"].delete(
                f"/api/agents/{target_id}", headers=_as(ctx["vera"]["token"])
            )
            assert response.status_code == 404
        else:
            for caller in (ctx["vera"]["user"]["id"], None):
                with pytest.raises(AgentError, match="not found"):
                    await manager.delete_agent(target_id, user_id=caller)
        assert await ctx["db"].get_agent(target["id"]) == target
        assert memory.read_text() == "Keep the owner's memory\n"
        assert events == []

    response = await ctx["client"].delete(
        f"/api/agents/{target['id']}", headers=_as(ctx["archer"]["token"])
    )
    assert response.status_code == 204
    assert await ctx["db"].get_agent(target["id"]) is None
    assert not memory.parent.parent.exists()
    assert len(events) == 1
    event, scope = events[0]
    assert event["type"] == "agent_deleted"
    assert event["agent_id"] == target["id"]
    assert scope == {"user_id": owner}


@pytest.mark.parametrize("operation", ["fork", "duplicate"])
async def test_owned_fork_survives_interrupt_refresh_and_restart(
    two_accounts, monkeypatch, tmp_path, operation
):
    ctx = two_accounts
    parent = ctx["archer"]["session"]
    owner = ctx["archer"]["user"]["id"]
    other = ctx["vera"]["user"]["id"]
    headers = _as(ctx["archer"]["token"])
    parent.backend = "codex"
    parent.model = "model-a"
    parent.reasoning_effort = "high"
    await ctx["db"].update_session_field(
        parent.id, backend="codex", model="model-a", reasoning_effort="high",
    )
    for seq, (role, content) in enumerate(
        [("user", "first"), ("assistant", "answer"), ("user", "redo"), ("assistant", "old")]
    ):
        await ctx["db"].append_message(
            session_id=parent.id, seq=seq, role=role, type="text", content=content
        )
    monkeypatch.setattr(
        session_manager, "_fork_copy_dest", lambda src, sid: str(tmp_path / f"copy-{sid}")
    )
    owner_events, other_events = [], []

    async def owned_event(event):
        owner_events.append(event)

    async def foreign_event(event):
        other_events.append(event)

    session_manager.on_broadcast("fork-owner", owned_event, owner)
    session_manager.on_broadcast("fork-other", foreign_event, other)
    try:
        body = {"rewind_to_msg_seq": 2} if operation == "fork" else {}
        response = await ctx["client"].post(
            f"/api/sessions/{parent.id}/{operation}", json=body, headers=headers
        )
        assert response.status_code == 201, response.text
        fork_id = response.json()["id"]
        assert session_manager.get_session(fork_id, owner) is not None
        assert session_manager.get_session(fork_id, other) is None
        fork_event = "session_archived" if operation == "fork" else "session_forked"
        assert any(e["type"] == fork_event for e in owner_events)
        assert not any(e["type"] == fork_event for e in other_events)

        starts = held_model_runs(monkeypatch)
        active = await session_manager.start_message(fork_id, "new request")
        original = await asyncio.wait_for(starts.get(), timeout=1)
        assert original.session_id == fork_id
        assert await session_manager.interrupt(fork_id)
        completed = await asyncio.wait_for(active.wait(), timeout=1)
        assert completed.outcome.status == "interrupted"
        assert completed.outcome.request_id == active.request_id
        await session_manager.await_session_execution(fork_id, timeout=1)
        assert original.stopped.is_set() and starts.empty()

        # Reload from persisted rows as a restart would, then use the browser's
        # account-scoped list/detail paths. Neither path may hide this fork.
        await session_manager.drain_turns()
        await session_manager.initialize(ctx["db"])
        live = (await ctx["client"].get("/api/sessions", headers=headers)).json()
        assert fork_id in {s["id"] for s in live}
        assert (parent.id in {s["id"] for s in live}) == (operation == "duplicate")
        detail = await ctx["client"].get(f"/api/sessions/{fork_id}", headers=headers)
        assert detail.status_code == 200, detail.text
        assert detail.json()["model"] == "model-a"
        assert detail.json()["reasoning_effort"] == "high"
        contents = [m["content"] for m in detail.json()["messages"]]
        assert contents[:2] == ["first", "answer"]
        assert contents[-2] == "new request"
        outcome = detail.json()["messages"][-1]
        assert outcome["type"] == "result" and outcome["is_error"] is True
        assert outcome["content"]["kind"] == "outcome"
        assert outcome["content"]["status"] == "interrupted"
        assert outcome["content"]["code"] == "interrupted"
        assert outcome["content"]["message"] == "(interrupted by user)"
        assert outcome["content"]["request_id"]
        foreign = await ctx["client"].get(
            f"/api/sessions/{fork_id}", headers=_as(ctx["vera"]["token"])
        )
        assert foreign.status_code == 404
        parent_detail = await ctx["client"].get(f"/api/sessions/{parent.id}", headers=headers)
        assert parent_detail.status_code == 200
        assert len(parent_detail.json()["messages"]) == 4
    finally:
        session_manager.remove_broadcast("fork-owner")
        session_manager.remove_broadcast("fork-other")


@pytest.mark.asyncio
async def test_model_catalog_cannot_read_another_accounts_credential(two_accounts):
    ctx = two_accounts
    await ctx["db"].save_credential(
        "private-codex", "codex", "private", "oauth", "", "2026-10-11T00:00:00Z",
        user_id=ctx["archer"]["user"]["id"],
    )
    response = await ctx["client"].get(
        "/api/backends/codex/models?credential_id=private-codex",
        headers=_as(ctx["vera"]["token"]),
    )
    assert response.status_code == 404
    missing = await ctx["client"].get(
        "/api/backends/codex/models?credential_id=missing",
        headers=_as(ctx["vera"]["token"]),
    )
    assert missing.status_code == response.status_code
    assert missing.json() == response.json()


@pytest.mark.asyncio
async def test_reading_someone_elses_row_is_a_404_not_a_403(two_accounts):
    """404, because 403 confirms the id exists — which is a slow way to
    enumerate another account's work."""
    ctx = two_accounts
    c = ctx["client"]
    vera = _as(ctx["vera"]["token"])

    assert (
        await c.get(f"/api/sessions/{ctx['archer']['session'].id}", headers=vera)
    ).status_code == 404
    assert (
        await c.get(f"/api/agents/{ctx['archer']['agent']['id']}", headers=vera)
    ).status_code == 404


@pytest.mark.asyncio
async def test_writing_to_someone_elses_row_is_refused(two_accounts):
    """The half that is easy to leave out: a read that 404s and a write that
    succeeds is worse than neither, because it looks protected."""
    ctx = two_accounts
    c = ctx["client"]
    vera = _as(ctx["vera"]["token"])
    victim_session = ctx["archer"]["session"].id
    victim_agent = ctx["archer"]["agent"]["id"]

    renamed = await c.patch(
        f"/api/sessions/{victim_session}", json={"name": "mine now"}, headers=vera
    )
    assert renamed.status_code == 404

    modelled = await c.patch(
        f"/api/sessions/{victim_session}", json={"model": "opus"}, headers=vera
    )
    assert modelled.status_code == 404

    updated = await c.put(
        f"/api/agents/{victim_agent}", json={"name": "mine now"}, headers=vera
    )
    assert updated.status_code in (404, 405), updated.status_code

    archived = await c.post(f"/api/agents/{victim_agent}/archive", headers=vera)
    assert archived.status_code == 404

    # And none of it happened.
    still = await c.get(
        f"/api/sessions/{victim_session}", headers=_as(ctx["archer"]["token"])
    )
    assert still.status_code == 200 and still.json()["name"] == "archer's work"


@pytest.mark.asyncio
async def test_the_transcript_is_not_readable_through_the_messages_route(
    two_accounts,
):
    """The windowed-scrollback route reads rows rather than the session, which
    is exactly the kind of place a scope gets forgotten."""
    ctx = two_accounts
    c = ctx["client"]
    res = await c.get(
        f"/api/sessions/{ctx['archer']['session'].id}/messages?before_seq=999",
        headers=_as(ctx["vera"]["token"]),
    )
    assert res.status_code == 404


@pytest.mark.asyncio
async def test_a_session_cannot_be_pointed_outside_its_owners_workspace(
    two_accounts,
):
    """Confinement has to be *enforced where sessions are made*, not merely
    available in a helper. The helper is unit-tested in test_workspace.py; this
    is the wiring, which is the half that gets forgotten."""
    ctx = two_accounts
    archer = ctx["archer"]

    with pytest.raises(WorkspaceError):
        await session_manager.create_session(
            archer["agent"]["id"], "escape", "/etc"
        )

    # Another account's workspace is outside too — that is the whole point.
    with pytest.raises(WorkspaceError):
        await session_manager.create_session(
            archer["agent"]["id"],
            "reach across",
            str(paths_for(ctx["vera"]["user"]["id"]).workspace),
        )


@pytest.mark.asyncio
async def test_extra_roots_let_the_first_user_keep_their_repository(two_accounts):
    """The single-box affordance from §6, tested where it is read rather than
    only where it is written — an `extra_roots` that nothing consults would
    look configured and do nothing."""
    ctx = two_accounts
    archer = ctx["archer"]

    outside = paths_for("somewhere").workspace.parent.parent / "a-repo"
    outside.mkdir(parents=True, exist_ok=True)

    with pytest.raises(WorkspaceError):
        await session_manager.create_session(
            archer["agent"]["id"], "outside", str(outside)
        )

    await ctx["db"].update_user_field(
        archer["user"]["id"], extra_roots=json.dumps([str(outside)])
    )
    allowed = await session_manager.create_session(
        archer["agent"]["id"], "outside, now permitted", str(outside)
    )
    assert allowed.working_dir == str(outside)


# ---------------------------------------------------------------------------
# The live stream (multi-tenancy.md §7)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_one_accounts_frames_never_reach_anothers_socket(two_accounts):
    """The quietest way to get multi-tenancy wrong.

    The bus was a fan-out to every subscriber, so with accounts it would have
    streamed one person's assistant text into another person's browser —
    nothing errors, both tabs look plausible, and the only symptom is someone
    reading work that is not theirs.
    """
    ctx = two_accounts
    archer_seen: list[dict] = []
    vera_seen: list[dict] = []
    internal_seen: list[dict] = []

    async def archer_cb(msg):
        archer_seen.append(msg)

    async def vera_cb(msg):
        vera_seen.append(msg)

    async def internal_cb(msg):
        internal_seen.append(msg)

    session_manager.on_broadcast("archer-tab", archer_cb, ctx["archer"]["user"]["id"])
    session_manager.on_broadcast("vera-tab", vera_cb, ctx["vera"]["user"]["id"])
    # No account: the delegation manager and friends, which must keep seeing
    # everything because they act on the server's behalf.
    session_manager.on_broadcast("internal", internal_cb)
    try:
        await session_manager._broadcast(
            {
                "type": "assistant_text",
                "session_id": ctx["archer"]["session"].id,
                "content": "something private",
            }
        )

        assert len(archer_seen) == 1
        assert vera_seen == [], "another account's assistant text reached this socket"
        assert len(internal_seen) == 1, "an internal subscriber stopped seeing frames"

        # A frame that belongs to the install rather than to a person — no
        # session, no owner — still reaches everyone.
        await session_manager._broadcast({"type": "schedules_changed"})
        assert len(archer_seen) == 2 and len(vera_seen) == 1
    finally:
        for key in ("archer-tab", "vera-tab", "internal"):
            session_manager.remove_broadcast(key)


@pytest.mark.asyncio
async def test_a_frame_can_name_its_owner_when_there_is_no_session(two_accounts):
    """Agent and application events carry a row rather than a session id, so
    the owner is told rather than derived."""
    ctx = two_accounts
    vera_seen: list[dict] = []

    async def vera_cb(msg):
        vera_seen.append(msg)

    session_manager.on_broadcast("vera-tab", vera_cb, ctx["vera"]["user"]["id"])
    try:
        await session_manager._broadcast(
            {"type": "agent_created", "agent": {"name": "Archer's second"}},
            user_id=ctx["archer"]["user"]["id"],
        )
        assert vera_seen == [], "another account's agent event reached this socket"
    finally:
        session_manager.remove_broadcast("vera-tab")


# ---------------------------------------------------------------------------
# Quotas (multi-tenancy.md §8)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_one_account_cannot_run_the_box_out_of_memory(two_accounts, monkeypatch):
    """A held `claude` is ~250 MB and inline-steering.md §7 records the OOM a
    single user managed. With several accounts the cap is not a refinement.

    Counted per account, not per box: one person filling their own allowance
    must not stop anybody else working, which is the difference between a
    quota and an outage.
    """
    from server.config import settings as cfg
    from server.sessions.turns import QuotaExceeded

    monkeypatch.setattr(cfg, "max_concurrent_turns_per_user", 1)
    ctx = two_accounts
    archer, vera = ctx["archer"], ctx["vera"]

    starts = held_model_runs(monkeypatch)
    archer_request = await session_manager.start_message(archer["session"].id, "Archer's current turn")
    vera_request = None
    vera_second = None
    try:
        original = await asyncio.wait_for(starts.get(), 2)
        assert original.session_id == archer["session"].id
        assert session_manager.running_turns_for(archer["user"]["id"]) == 1
        second = await session_manager.create_session(
            archer["agent"]["id"],
            "another",
            str(paths_for(archer["user"]["id"]).workspace),
        )
        with pytest.raises(QuotaExceeded):
            await session_manager.start_message(second.id, "hello")

        # Vera is unaffected by Archer's allowance.
        assert session_manager.running_turns_for(vera["user"]["id"]) == 0
        vera_second = await session_manager.create_session(
            vera["agent"]["id"],
            "vera's second",
            str(paths_for(vera["user"]["id"]).workspace),
        )
        vera_request = await session_manager.start_message(vera_second.id, "Vera's independent turn")
        assert vera_request.accepted
        independent = await asyncio.wait_for(starts.get(), 2)
        assert independent.session_id == vera_second.id
        assert session_manager.running_turns_for(vera["user"]["id"]) == 1
        assert session_manager.running_turns_for(archer["user"]["id"]) == 1
        assert starts.empty()
    finally:
        await session_manager.interrupt(archer["session"].id)
        assert (await asyncio.wait_for(archer_request.wait(), 2)).outcome.status == "interrupted"
        await session_manager.await_session_execution(archer["session"].id, timeout=2)
        if vera_request is not None:
            await session_manager.interrupt(vera_second.id)
            assert (await asyncio.wait_for(vera_request.wait(), 2)).outcome.status == "interrupted"
            await session_manager.await_session_execution(vera_second.id, timeout=2)


@pytest.mark.asyncio
async def test_a_message_queued_behind_a_running_turn_is_not_refused(
    two_accounts, monkeypatch
):
    """A queue is one CLI process however long it gets, so refusing to queue
    would make the cap feel like data loss for no saving."""
    from server.config import settings as cfg

    monkeypatch.setattr(cfg, "max_concurrent_turns_per_user", 1)
    ctx = two_accounts
    session = ctx["archer"]["session"]

    starts = held_model_runs(monkeypatch)
    active = await session_manager.start_message(session.id, "Original running input")
    pending = None
    try:
        original = await asyncio.wait_for(starts.get(), 2)
        assert original.session_id == session.id
        pending = await session_manager.start_message(session.id, "queue me")
        assert pending.accepted
        queued = session_manager.dispatch_snapshot(session.id)["items"]
        assert len(queued) == 1
        assert queued[0]["summary"] == "queue me" and queued[0]["request_id"] == pending.request_id
        assert session_manager.running_turns_for(session.user_id) == 1
        assert starts.empty()
    finally:
        await session_manager.interrupt(session.id)
        assert (await asyncio.wait_for(active.wait(), 2)).outcome.status == "interrupted"
        await session_manager.await_session_execution(session.id, timeout=2)
        if pending is not None:
            assert await session_manager.cancel_request(session.id, pending.request_id)
            assert (await asyncio.wait_for(pending.wait(), 2)).state == "withdrawn"


@pytest.mark.asyncio
async def test_two_accounts_can_both_have_an_application_called_notes(two_accounts, monkeypatch, request):
    """The name index was unique across the box. That is a bug with accounts:
    the second person who wants an app called "Notes" cannot have one, and the
    refusal tells them somebody else already does."""
    from server.applications import application_manager

    ctx = two_accounts
    application_manager.bind(db=ctx["db"], session_mgr=session_manager)
    request.addfinalizer(application_manager.shutdown)
    # This case exercises allocation and ownership, not an agent build.
    from unittest.mock import AsyncMock

    build = AsyncMock()
    monkeypatch.setattr(session_manager, "start_message", build)

    first = await application_manager.create_application(
        name="Notes", description="Archer's", agent_id=ctx["archer"]["agent"]["id"]
    )
    second = await application_manager.create_application(
        name="Notes", description="Vera's", agent_id=ctx["vera"]["agent"]["id"]
    )

    assert first["user_id"] == ctx["archer"]["user"]["id"]
    assert second["user_id"] == ctx["vera"]["user"]["id"]
    # Separate directories, so the files cannot collide either.
    assert first["app_dir"] != second["app_dir"]
    assert str(paths_for(ctx["archer"]["user"]["id"]).applications) in first["app_dir"]
    assert build.await_count == 2
    assert {call.args[0] for call in build.await_args_list} == {
        first["session_id"], second["session_id"],
    }

    # Still taken within one account.
    with pytest.raises(Exception) as e:
        await application_manager.create_application(
            name="notes", description="again", agent_id=ctx["archer"]["agent"]["id"]
        )
    assert "already exists" in str(e.value)


@pytest.mark.asyncio
async def test_vera_cannot_reach_archers_work_through_any_surface(two_accounts):
    """One case per router, because the scoping is per route.

    The structural half — "does every route take a scope at all" — is
    `test_route_scoping.py`. This is the behavioural half: the scope is
    actually *used*, and the answer is 404 rather than somebody else's data.
    """
    ctx = two_accounts
    c = ctx["client"]
    vera = _as(ctx["vera"]["token"])
    sid = ctx["archer"]["session"].id
    aid = ctx["archer"]["agent"]["id"]

    reads = [
        f"/api/sessions/{sid}",
        f"/api/sessions/{sid}/messages?before_seq=99",
        f"/api/sessions/{sid}/transcript-export",
        f"/api/sessions/{sid}/files?path=README.md",
        f"/api/sessions/{sid}/files/meta?path=README.md",
        f"/api/sessions/{sid}/bg-tasks",
        f"/api/sessions/{sid}/delegations",
        f"/api/sessions/{sid}/research",
        f"/api/sessions/{sid}/schedules",
        f"/api/agents/{aid}",
        f"/api/agents/{aid}/sessions",
        f"/api/agents/{aid}/schedules",
        f"/api/agents/{aid}/connectors",
    ]
    for path in reads:
        res = await c.get(path, headers=vera)
        assert res.status_code == 404, f"{path} answered {res.status_code}"

    writes = [
        ("post", f"/api/sessions/{sid}/reset", None),
        ("post", f"/api/sessions/{sid}/archive", None),
        ("post", f"/api/sessions/{sid}/unarchive", {"operation_id": "foreign-restore", "service_generation": "unknown"}),
        ("delete", f"/api/sessions/{sid}", None),
        ("post", f"/api/sessions/{sid}/duplicate", {}),
        ("post", f"/api/sessions/{sid}/fork", {"rewind_to_msg_seq": 0}),
        ("post", f"/api/sessions/{sid}/bg-tasks", {"command": "echo hi"}),
        ("post", f"/api/sessions/{sid}/questions", {"questions": [{"question": "?"}]}),
        ("post", f"/api/sessions/{sid}/research", {"question": "what"}),
        (
            "post",
            f"/api/sessions/{sid}/schedules",
            {"prompt": "do it", "name": "n", "interval_seconds": 3600},
        ),
        (
            "post",
            f"/api/sessions/{sid}/delegations",
            {"agent_name": "Archer's agent", "request": "do it"},
        ),
        ("post", f"/api/agents/{aid}/archive", None),
        (
            "post",
            f"/api/agents/{aid}/sessions",
            {"name": "mine now", "working_dir": "/tmp"},
        ),
        ("put", f"/api/agents/{aid}/connectors", {"installation_ids": []}),
    ]
    for method, path, body in writes:
        call = getattr(c, method)
        res = await (call(path, json=body, headers=vera) if body is not None
                     else call(path, headers=vera))
        assert res.status_code == 404, (
            f"{method.upper()} {path} answered {res.status_code}: {res.text}"
        )

    # Multipart, so the body validates and the ownership check is what answers.
    uploaded = await c.post(
        f"/api/sessions/{sid}/attachments",
        files={"file": ("note.txt", b"hello", "text/plain")},
        headers=vera,
    )
    assert uploaded.status_code == 404, uploaded.text


@pytest.mark.asyncio
async def test_a_session_cannot_be_created_under_someone_elses_agent(two_accounts):
    """The derivation runs the other way too: a session takes its owner from
    its agent, so an agent id is the one input that could plant a row in
    another account."""
    ctx = two_accounts
    res = await ctx["client"].post(
        "/api/sessions",
        json={
            "name": "trojan",
            "agent_id": ctx["archer"]["agent"]["id"],
            "working_dir": "/tmp",
        },
        headers=_as(ctx["vera"]["token"]),
    )
    assert res.status_code == 404, res.text


@pytest.mark.asyncio
async def test_an_agent_cannot_delegate_into_another_account(two_accounts):
    """A name was enough to start a turn in somebody else's workspace, on their
    credential, reporting back into yours."""
    from server.delegations import delegation_manager

    ctx = two_accounts
    delegation_manager.bind(session_mgr=session_manager, db=ctx["db"])
    target = await delegation_manager._resolve_target_agent(
        "Archer's agent", ctx["vera"]["user"]["id"]
    )
    assert target is None
    # And it still resolves for the account that owns it.
    own = await delegation_manager._resolve_target_agent(
        "Archer's agent", ctx["archer"]["user"]["id"]
    )
    assert own is not None and own["id"] == ctx["archer"]["agent"]["id"]


@pytest.mark.asyncio
async def test_the_monitor_belongs_to_the_operator(two_accounts):
    """Box-level metrics — resident memory, sidecars, HTTP latency across the
    whole install — are the operator's, and neither account here is an admin."""
    ctx = two_accounts
    for token in (ctx["vera"]["token"], ctx["archer"]["token"]):
        for report in ("overview", "codex", "codex_turns"):
            res = await ctx["client"].get(f"/api/monitor/{report}", headers=_as(token))
            assert res.status_code == 403, res.status_code


@pytest.mark.asyncio
async def test_an_agent_belongs_to_the_account_that_made_it(two_accounts):
    """`create_agent` declared a scope and never passed it, so every agent made
    through the route was unowned: invisible to its own account, and a 404 the
    moment that account opened a session under it. A second account could not
    use the product at all."""
    ctx = two_accounts
    c = ctx["client"]
    vera = _as(ctx["vera"]["token"])

    made = await c.post("/api/agents", json={"name": "Vera's own"}, headers=vera)
    assert made.status_code == 201, made.text
    agent_id = made.json()["id"]

    row = await ctx["db"].get_agent(agent_id)
    assert row["user_id"] == ctx["vera"]["user"]["id"], "the agent has no owner"

    listed = await c.get("/api/agents", headers=vera)
    assert "Vera's own" in {a["name"] for a in listed.json()}

    # And the thing that actually broke: a session under it.
    opened = await c.post(
        f"/api/agents/{agent_id}/sessions",
        json={"name": "works", "working_dir": str(paths_for(ctx["vera"]["user"]["id"]).workspace)},
        headers=vera,
    )
    assert opened.status_code == 201, opened.text


@pytest.mark.asyncio
async def test_nobody_can_rename_pin_or_restore_someone_elses_agent(two_accounts):
    """The other half of the same omission: `update`, `pin`, `unpin` and
    `unarchive` all took an agent id and never checked whose it was."""
    ctx = two_accounts
    c = ctx["client"]
    vera = _as(ctx["vera"]["token"])
    victim = ctx["archer"]["agent"]["id"]

    for method, path, body in (
        ("patch", f"/api/agents/{victim}", {"name": "mine now"}),
        ("post", f"/api/agents/{victim}/pin", None),
        ("post", f"/api/agents/{victim}/unpin", None),
        ("post", f"/api/agents/{victim}/unarchive", None),
    ):
        call = getattr(c, method)
        res = await (
            call(path, json=body, headers=vera) if body is not None
            else call(path, headers=vera)
        )
        assert res.status_code == 404, (
            f"{method.upper()} {path} answered {res.status_code}"
        )

    # Untouched.
    still = await ctx["db"].get_agent(victim)
    assert still["name"] == "Archer's agent"


@pytest.mark.asyncio
async def test_two_accounts_can_both_create_the_same_custom_agent_name(two_accounts):
    """Agent names are unique per account, like application names — the index
    says so, and `get_agent_by_name` has to agree or the second person to want
    a custom agent is told somebody else already has one. Both accounts
    already have their own protected default agent."""
    ctx = two_accounts
    c = ctx["client"]
    name = "Shared assistant"

    for who in ("archer", "vera"):
        res = await c.post(
            "/api/agents", json={"name": name}, headers=_as(ctx[who]["token"])
        )
        assert res.status_code == 201, f"{who}: {res.text}"

    # Still taken within one account.
    again = await c.post(
        "/api/agents", json={"name": name}, headers=_as(ctx["vera"]["token"])
    )
    assert again.status_code >= 400
