"""Wiki job fixtures, controlled curator and publication helpers."""

import asyncio
import json
import uuid
from types import SimpleNamespace

import pytest

from server.mcp_identity import mint, verify
from server.wiki.curation import WikiCuration
from server.wiki.history import atomic
from server.wiki.jobs import WikiJobs, WikiWorker
from server.wiki.store import WikiError
from tests import wiki_api
from tests.submission import receipt_from_events
from tests.wiki_api import client as client


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
