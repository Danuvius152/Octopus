"""Wiki job fixtures, controlled curator and publication helpers."""

import asyncio
import json
import uuid
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from server import deps
from server.aio import complete_cleanup
from server.mcp_identity import McpScope, mint, verify
from server.wiki.curation import WikiCuration
from server.wiki.history import atomic
from server.wiki.jobs import JobOwner, WikiJobs, WikiWorker
from server.wiki.store import WikiError
from server.wiki.tasks import WikiTask, tasks
from tests import wiki_api
from tests.submission import receipt_from_events
from tests.wiki_api import client as client


@dataclass(frozen=True)
class WorkerScope:
    headers: dict[str, str]
    task: WikiTask
    scope: McpScope


@asynccontextmanager
async def active_worker(config, paths, *, client):
    owner = await deps.get_user_manager().resolve_token(
        client.headers["Authorization"].removeprefix("Bearer "), touch=False,
    )
    assert owner is not None
    jobs = WikiJobs(owner["id"])
    execution = JobOwner()

    async def retire_job(job_id):
        # The fixture is the live owner and never submits a model request.
        # Keep that proof through cancellation, including recorded API effects.
        execution.retired.set()
        jobs.update(job_id, session_id=None)
        stopped = jobs.mark_cancelled(job_id)
        await WikiWorker.cancel_attempt(jobs, stopped, wiki_api.session_manager, owner=execution)

    async def retire_session(creation):
        session = await creation
        await wiki_api.session_manager.delete_session(session.id)

    cleanup = AsyncExitStack()
    try:
        job = jobs.maintain(f"fixture-{uuid.uuid4().hex}", "", "organize", paths, None, [], "human")
        cleanup.push_async_callback(retire_job, job["id"])
        creation = asyncio.create_task(wiki_api.session_manager.create_session(config["curator_agent_id"], "Fixture maintenance"))
        cleanup.push_async_callback(retire_session, creation)
        session = await asyncio.shield(creation)
        current = jobs.update(job["id"], status="running", session_id=session.id)
        execution.entered_attempt = current["attempt"]
        tasks.assign(session.id, job["id"], job["items"])
        cleanup.callback(tasks.unassign, session.id)
        token = tasks.open(session, "maintain")
        cleanup.callback(tasks.close, token)
        bearer = mint(session.id, user_id=session.user_id, wiki_task_id=token)
        yield WorkerScope({"Authorization": f"Bearer {bearer}"}, tasks.active[token], verify(bearer))
    finally:
        await complete_cleanup(cleanup.aclose())


@asynccontextmanager
async def worker_callback(config, paths, *, client):
    from server.deps import get_session_manager
    from server.main import _wiki_error
    from server.routers.wiki import router
    from tests.callback_api import start_callback_api

    async with active_worker(config, paths, client=client) as worker:
        host = await start_callback_api(
            router, overrides={get_session_manager: lambda: wiki_api.session_manager},
            exception_handlers={WikiError: _wiki_error},
            readiness_token=mint(worker.scope.session_id, user_id=worker.scope.user_id),
        )
        try:
            yield worker, host
        finally:
            await complete_cleanup(host.stop())


@pytest.fixture
async def jobs(client, tmp_path):
    root = tmp_path / "vault"
    root.mkdir()
    await wiki_api.configure(client, root)
    owner = (await wiki_api.session_manager.db.get_system_agent())["user_id"]
    return WikiJobs(owner)



class Curator:
    def __init__(self, jobs, *, conflicts=0, crash=False, sources=None):
        self.jobs = jobs
        self.conflicts = conflicts
        self.crash = crash
        self.sources = sources or []
        self.turns = 0
        self.sessions = 0
        self.db = self
        self.terminals = {}

    async def get_agent(self, *args):
        return {"backend": "codex"}

    async def create_session(self, *args, **kwargs):
        self.sessions += 1
        return SimpleNamespace(id=f"curator-{self.sessions}")

    async def archive_session(self, *args, **kwargs):
        pass

    async def start_message(self, session_id, prompt, **kwargs):
        assert kwargs["source"] == "wiki"
        request_id = kwargs["request_id"]
        try:
            receipt = await receipt_from_events(self._events(session_id, prompt, **kwargs), request_id)
        except BaseException:
            self.terminals[(session_id, request_id)] = {"request_id": request_id, "status": "failed"}
            raise
        self.terminals[(session_id, request_id)] = (await receipt.wait()).outcome.model_dump()
        return receipt

    def get_session(self, session_id):
        return None

    def work_has_resources(self, work_id):
        return False

    def session_has_resources(self, session_id, request_ids=None):
        return False

    async def get_committed_terminal(self, session_id, request_id):
        return self.terminals.get((session_id, request_id))

    async def get_request_missing_decisions(self, session_id, request_id):
        return []  # This controlled curator never creates product questions.

    async def cancel_request(self, *args):
        return True

    async def _await_backend_retirement(self, session):
        pass

    async def close_work(self, work_id):
        from server.sessions.owner_runtime import WorkRetirement

        return WorkRetirement(())

    async def finish_work_cancellation(self, affected):
        pass

    async def _events(self, session_id, prompt, **kwargs):
        from server.wiki.tasks import tasks

        self.turns += 1
        failed = False
        with self.jobs.store.connect() as db:
            job_id = db.execute(
                "SELECT id FROM jobs WHERE session_id=?", (session_id,)
            ).fetchone()[0]
        session = SimpleNamespace(id=session_id, user_id=self.jobs.user_id,
                                  agent_id=self.jobs.registration["curator_agent_id"])
        original = self.jobs.get(job_id)
        reference = next((item["worker_request"] for item in original["items"]
                          if item.get("worker_request", {}).get("session_id") == session_id), {})
        token = tasks.open(session, "maintain", request_id=kwargs.get("request_id", reference.get("request_id", "")))
        scope = verify(mint(session_id, user_id=self.jobs.user_id, wiki_task_id=token))
        task = tasks.call(tasks.active[token], scope)
        try:
            job = original
            for item in job["items"]:
                if item["status"] != "pending" or (
                    item.get("doc") not in task.document_ids
                    and item.get("reservation_id") not in task.reservation_ids
                ):
                    continue
                source = self.jobs.store.read(item["doc"])
                tasks.record_read(task, source)
                following = source.get("next_start")
                while following is not None:
                    page = self.jobs.store.read(item["doc"], start=following, expected_revision=source["revision"])
                    tasks.record_read(task, page)
                    following = page.get("next_start")
                for ref in self.sources:
                    page = self.jobs.store.read(ref["doc"])
                    tasks.record_read(task, page)
                    while page.get("next_start") is not None:
                        page = self.jobs.store.read(ref["doc"], start=page["next_start"], expected_revision=page["revision"])
                        tasks.record_read(task, page)
                if self.turns <= self.conflicts:
                    path = self.jobs.store.path(source["path"])
                    path.write_text(path.read_text() + f"\n人工补充 {self.turns}。\n")
                try:
                    await asyncio.to_thread(
                        WikiCuration(self.jobs.store).organize,
                        item["path"], source["revision"], "原文的观点与适用条件。", [], [], self.sources,
                        "curator", "test organization", task=task,
                    )
                except WikiError as exc:
                    failed = True
                    yield {"type": "error", "message": str(exc)}
                else:
                    if self.crash:
                        raise asyncio.CancelledError()
            yield {"type": "result", "content": "published", "is_error": failed}
        finally:
            tasks.close(token)



async def process(jobs, job, curator):
    worker = WikiWorker()
    worker.manager = curator
    jobs.update(job["id"], status="running")
    await worker.process(jobs, job["id"])
    return jobs.get(job["id"])



def sapphire_delete(history, path):
    with history.locked():
        before = history.capture_external([path])
        journal = history.state / "external-change.json"
        entry = {"version": 2, "source_binding_protocol": 1, "id": uuid.uuid4().hex,
                 "before": before, "paths": [path], "actor": "human", "reason": "delete",
                 "trash_root": str(history.state / "trash")}
        atomic(journal, json.dumps(entry).encode())
        history.store.path(path).unlink()
        history.finish_external(entry)
        history.recover()
        assert not journal.exists()
        history.store.refresh()
