"""Real local Wiki jobs and worker grants for business-rule tests."""

from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from server.mcp_identity import mint, verify
from server.wiki.jobs import WikiJobs
from server.wiki.registry import registry
from server.wiki.tasks import tasks


@pytest.fixture
def jobs(tmp_path):
    root = tmp_path / "vault"
    root.mkdir()
    owner = "a" * 12
    registry.configure(owner, root, "material-curator")
    try:
        yield WikiJobs(owner)
    finally:
        registry.close()


@contextmanager
def assigned_worker(jobs, job):
    session = SimpleNamespace(
        id="proof-worker-" + job["id"], user_id=jobs.user_id,
        agent_id=jobs.registration["curator_agent_id"],
    )
    current = jobs.update(job["id"], status="running", session_id=session.id)
    tasks.assign(session.id, job["id"], current["items"], registration=jobs.registration)
    token = tasks.open(session, "maintain")
    try:
        scope = verify(mint(session.id, user_id=session.user_id, wiki_task_id=token))
        yield tasks.call(tasks.active[token], scope)
    finally:
        tasks.close(token)
        tasks.unassign(session.id)
