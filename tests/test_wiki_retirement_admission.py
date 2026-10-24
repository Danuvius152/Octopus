"""Terminal maintenance rows cannot bypass retained session writer owners."""

import asyncio

import pytest

from server.session_manager import SessionManager
from server.sessions.base import BackendRetirement, QueuedPrompt, Session
from server.wiki.jobs import WikiJobs, worker
from server.wiki.registry import registry
from server.wiki.store import WikiError
from tests import test_wiki_api as wiki_api
from tests.fake_run import FakeRunBase
from tests.test_wiki_grant_v2 import proposed

client = wiki_api.client


@pytest.mark.parametrize("operation", ["retry", "readmit"])
@pytest.mark.parametrize("outcome", ["pending", "failed", "cancelled", "success"])
async def test_terminal_job_requires_confirmed_writer_exit(client, tmp_path, monkeypatch, operation, outcome):
    root = tmp_path / "vault"
    root.mkdir()
    job, _ = await proposed(client, root, paths=["Library/new.md"])
    await client.post(f"/api/wiki/jobs/{job['id']}/cancel", headers=client.headers)
    owner = (await wiki_api.session_manager.db.get_system_agent())["user_id"]
    jobs = WikiJobs(owner)
    await jobs.settle_cancelled(job["id"], wiki_api.session_manager)
    manager = SessionManager()
    session = Session(id="retirement-session", name="retiring", working_dir=str(tmp_path), user_id=owner)
    manager.sessions[session.id] = session
    jobs.update(job["id"], session_id=session.id)
    entered, release = asyncio.Event(), asyncio.Event()

    class Writer(FakeRunBase):
        async def interrupt(self):
            pass

        async def stop(self):
            entered.set()
            await release.wait()
            if outcome == "failed":
                raise RuntimeError("writer exit unconfirmed")

    backend = Writer()
    if outcome in {"failed", "cancelled"}:
        # Exercise the HTTP guard's retained-failure projection in isolation.
        # A live Owner observing this failed task instead enters real fatal
        # cleanup, covered by the subprocess owner-failure tests.
        retirement = asyncio.create_task(backend.stop())
        session._run.retirements[id(backend)] = BackendRetirement(backend, retirement)
    else:
        retirement = await manager._retire_backend(session, backend)
    await entered.wait()
    if outcome == "cancelled":
        retirement.cancel()
    elif outcome in {"failed", "success"}:
        release.set()
    if outcome != "pending":
        await asyncio.gather(retirement, return_exceptions=True)
        await asyncio.sleep(0)  # Observe the actual retirement callback.
    monkeypatch.setattr(worker, "manager", manager)
    monkeypatch.setattr(worker, "wake", lambda *args: None)
    assert session._run.request is None and not session._pending_queue
    if operation == "readmit":
        registry.configure(owner, root, "new-curator")
    response = await client.post(f"/api/wiki/jobs/{job['id']}/{operation}", headers=client.headers,
        json={"creation": {"purpose": "requested_knowledge_page", "paths": ["Library/new.md"]}} if operation == "readmit" else None)
    try:
        if outcome == "success":
            assert response.status_code == 200, response.text
            assert not session._run.retirements
        else:
            assert response.status_code == 409, response.text
            assert "previous maintenance execution has not retired" in response.json()["detail"].lower()
            assert id(backend) in session._run.retirements
    finally:
        release.set()
        await asyncio.gather(retirement, return_exceptions=True)
        await manager.close_session_owners()


@pytest.mark.parametrize("location", ["current", "queued"])
@pytest.mark.parametrize("request_id", ["original-worker", "later-human"])
async def test_retirement_check_preserves_exact_request_scope(client, tmp_path, location, request_id):
    root = tmp_path / "vault"
    root.mkdir()
    job, _ = await proposed(client, root, paths=["Library/new.md"])
    owner = (await wiki_api.session_manager.db.get_system_agent())["user_id"]
    jobs = WikiJobs(owner)
    manager = SessionManager()
    session = Session(id="original-session", name="worker", working_dir=str(tmp_path), user_id=owner)
    manager.sessions[session.id] = session
    job["items"][0]["worker_request"] = {"session_id": session.id, "request_id": "original-worker"}
    job = jobs.update(job["id"], session_id=session.id, items=job["items"])
    request = QueuedPrompt(prompt="Retain this instruction", attachment_ids=[], request_id=request_id)
    if location == "current":
        session._run.request = request
    else:
        session._pending_queue.append(request)
    if request_id == "original-worker":
        with pytest.raises(WikiError, match="previous maintenance execution has not retired"):
            jobs.require_retired(job, manager=manager)
    else:
        jobs.require_retired(job, manager=manager)
    if location == "current":
        assert session._run.request is request
    else:
        assert session._pending_queue == [request]
