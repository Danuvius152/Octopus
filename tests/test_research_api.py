"""REST tests for /api/sessions/{sid}/research (native-deep-research.md §7).

Binds the global singletons to a per-test in-memory DB (like the delegations
route tests) and fakes the pipeline so no CLI runs.
"""

from __future__ import annotations

import asyncio

import pytest
from httpx import ASGITransport, AsyncClient

from server.agent_manager import AgentManager
from server.database import Database
from server.main import app
from server.research import manager as rm_mod
from server.research.manager import research_manager
from server.research.orchestrator import ResearchReport
from server.routers import agents as agents_mod
from server.session_manager import session_manager
from tests.accounts import create_account


@pytest.fixture
async def db():
    d = Database(":memory:")
    await d.initialize()
    yield d
    await d.close()


@pytest.fixture
async def client(db, monkeypatch):
    _, bearer = await create_account(db)
    session_manager.sessions.clear()
    session_manager._admission_closed = False
    await session_manager.initialize(db)
    agents_mod.set_manager(AgentManager(db))
    research_manager.bind(session_mgr=session_manager, db=db)
    monkeypatch.setattr(session_manager, "start_message", lambda *a, **k: asyncio.sleep(0))

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test",
                           headers={"Authorization": f"Bearer {bearer}"}) as c:
        yield c


async def _session(client):
    octo = (await client.get("/api/agents", headers=client.headers)).json()[0]
    r = await client.post(
        "/api/sessions",
        json={"name": "S", "agent_id": octo["id"]},
        headers=client.headers,
    )
    assert r.status_code == 201, r.text
    return r.json()


@pytest.mark.asyncio
async def test_start_get_list_research(client, monkeypatch):
    async def fake_run_research(question, *, on_progress=None, **kw):
        return ResearchReport(
            question=question, report="report", findings=[], sources=[],
            angles=["a"], claims_examined=0, cost=None,
        )

    monkeypatch.setattr(rm_mod, "run_research", fake_run_research)

    session = await _session(client)
    r = await client.post(
        f"/api/sessions/{session['id']}/research",
        json={"question": "what is X?"}, headers=client.headers,
    )
    assert r.status_code == 201, r.text
    rid = r.json()["id"]
    assert r.json()["status"] == "running"
    assert "knowledge_context" not in r.json()

    # GET single + list
    got = await client.get(f"/api/sessions/{session['id']}/research/{rid}", headers=client.headers)
    assert got.status_code == 200 and got.json()["question"] == "what is X?"
    assert "knowledge_context" not in got.json()
    lst = await client.get(f"/api/sessions/{session['id']}/research", headers=client.headers)
    assert any(j["id"] == rid for j in lst.json())
    assert all("knowledge_context" not in job for job in lst.json())


async def test_completed_undelivered_report_remains_downloadable_by_its_account(client, db):
    from server.workspace import paths_for

    session = await _session(client)
    binding = await db.get_session_binding(session["id"])
    path = paths_for(binding["user_id"]).research / "saved.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("# Retained answer\nSaved before restart.")
    await db.create_research_job("saved", session["id"], "q", "now")
    await db.confirm_research_outcome("saved", "completed", completed_at="then", report_path=str(path))
    await research_manager.recover_interrupted(previous_writers_retired=True)
    url = f"/api/sessions/{session['id']}/research/saved/report"
    for archived in (False, True):
        if archived:
            await session_manager.archive_session(session["id"], replace=False)
        response = await client.get(url)
        assert response.status_code == 200 and response.text == "# Retained answer\nSaved before restart."
    _, foreign = await create_account(db, username="other-report-owner")
    assert (await client.get(url, headers={"Authorization": f"Bearer {foreign}"})).status_code == 404
    assert (await client.get(url.replace("saved/report", "absent/report"))).status_code == 404


@pytest.mark.parametrize("retirement", ["delete", "archive"])
async def test_research_waiting_for_configuration_rejects_a_retired_session(client, db, monkeypatch, retirement):
    session = await _session(client)
    entered = asyncio.Event()
    lock = db.configuration_lock
    acquire = lock.acquire

    async def acquiring():
        entered.set()
        return await acquire()

    async def forbidden_pipeline(*args, **kwargs):
        pytest.fail("A retired session cannot admit research")

    monkeypatch.setattr(rm_mod, "run_research", forbidden_pipeline)
    await lock.acquire()
    monkeypatch.setattr(lock, "acquire", acquiring)
    pending = asyncio.create_task(client.post(f"/api/sessions/{session['id']}/research",
        json={"question": "waiting for original configuration"}))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        if retirement == "delete":
            assert await session_manager.delete_session(session["id"])
        else:
            await session_manager.archive_session(session["id"], replace=False)
    finally:
        lock.release()
    response = await asyncio.wait_for(pending, 2)
    assert response.status_code == 404, response.text
    assert not research_manager._tasks
    async with db.transaction(write=False) as tx:
        assert await tx.fetch_one("SELECT id FROM research_jobs") is None


@pytest.mark.asyncio
async def test_research_rejects_client_supplied_knowledge_context(client):
    session = await _session(client)
    response = await client.post(
        f"/api/sessions/{session['id']}/research",
        json={"question": "what is X?", "knowledge_context": '{"version":1}'},
        headers=client.headers,
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_start_research_empty_question_422(client):
    session = await _session(client)
    r = await client.post(
        f"/api/sessions/{session['id']}/research", json={"question": "  "},
        headers=client.headers,
    )
    # pydantic min_length on a stripped-empty string → 422, or our 400.
    assert r.status_code in (400, 422)


@pytest.mark.asyncio
async def test_start_research_unknown_session_404(client):
    r = await client.post(
        "/api/sessions/nope/research", json={"question": "q"}, headers=client.headers
    )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_research_requires_auth(client):
    client.headers.clear()
    r = await client.post("/api/sessions/x/research", json={"question": "q"})
    assert r.status_code in (401, 403)


@pytest.mark.asyncio
async def test_cancel_wrong_session_404_does_not_cancel(client, monkeypatch):
    """A cancel scoped to the WRONG session must 404 WITHOUT cancelling the
    real job (Vera review — verify ownership before mutating)."""
    import asyncio as _asyncio

    from server.research.orchestrator import ResearchReport

    async def slow_run(question, *, on_progress=None, **kw):
        await _asyncio.sleep(30)
        return ResearchReport(question=question, report="r", findings=[],
                              sources=[], angles=[], claims_examined=0, cost=None)

    monkeypatch.setattr(rm_mod, "run_research", slow_run)
    session = await _session(client)
    started = await client.post(
        f"/api/sessions/{session['id']}/research",
        json={"question": "q"}, headers=client.headers,
    )
    rid = started.json()["id"]

    bad = await client.post(
        f"/api/sessions/some-other-session/research/{rid}/cancel", headers=client.headers
    )
    assert bad.status_code == 404
    # The real job must still be running — not cancelled by the bad request.
    got = await client.get(f"/api/sessions/{session['id']}/research/{rid}", headers=client.headers)
    assert got.json()["status"] == "running"
