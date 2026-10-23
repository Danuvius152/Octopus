"""A saved file and an execution outcome are independent settlement evidence."""

from types import SimpleNamespace

from server.mcp_identity import mint
from server.wiki.history import WikiHistory
from server.wiki.tasks import tasks
from tests.test_wiki_grant_v2 import assigned, publish
from tests.test_wiki_jobs import Curator, process
from tests.test_wiki_jobs import client as client
from tests.test_wiki_jobs import jobs as jobs


async def test_applied_output_with_failed_terminal_stays_attention_on_reentry(jobs):
    WikiHistory(jobs.store).write("Library/note.md", b"# Claim\nKeep the condition.\n", None, "human", "create")
    job = jobs.maintain("partial-terminal", "", "Review", ["Library/note.md"], None, [], "human")

    class Partial(Curator):
        async def _events(self, *args, **kwargs):
            async for event in super()._events(*args, **kwargs):
                if event["type"] == "result":
                    yield event | {"is_error": True, "content": "Stopped after saving one effect"}
                else:
                    yield event

    manager = Partial(jobs)
    finished = await process(jobs, job, manager)
    assert finished["status"] == "completed_with_attention"
    item = finished["items"][0]
    assert item["status"] == "attention" and item["index_complete"]
    assert item["worker_request"]["terminal"]["status"] != "success"
    assert finished["receipts"][0]["details"]["execution"]["request_id"] == item["worker_request"]["request_id"]
    output = jobs.store.path(item["output"]).read_bytes()
    again = await process(jobs, jobs.retry(job["id"]), manager)
    assert again["status"] == "completed_with_attention"
    assert manager.turns == 1
    assert jobs.store.path(item["output"]).read_bytes() == output


async def test_creation_cannot_borrow_a_receipt_without_its_original_execution(jobs, client):
    job = jobs.maintain("creation-exact-receipt", "", "Create one requested page", [], None, [], "human",
                        creation={"purpose": "requested_knowledge_page", "paths": ["Library/new.md"]})
    with assigned(job, jobs.registration) as (headers, _):
        item = job["items"][0]
        result = await publish(client, headers, item["path"], item["reservation_id"])
        assert result.status_code == 200, result.text
    item = jobs.get(job["id"])["items"][0]
    item["worker_request"] = {"attempt": 1, "session_id": "different-session", "request_id": "different-request",
                              "phase": "settled", "recording_state": "committed", "retired": True,
                              "terminal": {"request_id": "different-request", "status": "success"}}
    jobs.settle_item(job["id"], item)
    assert item["status"] == "attention"
    assert jobs.store.path("Library/new.md").is_file()


async def test_creation_with_changed_source_stays_attention_on_completion_and_verify(jobs, client):
    WikiHistory(jobs.store).write("Library/source.md", b"# Source\nOriginal condition.\n", None, "human", "create")
    job = jobs.maintain("creation-source-drift", "", "Create a sourced page", [], None, [], "human",
                        creation={"purpose": "requested_knowledge_page", "paths": ["Library/new.md"]})

    class Publisher(Curator):
        async def _events(self, session_id, prompt, **kwargs):
            self.turns += 1
            session = SimpleNamespace(id=session_id, user_id=jobs.user_id, agent_id=jobs.registration["curator_agent_id"])
            token = tasks.open(session, "maintain", request_id=kwargs["request_id"])
            headers = {"Authorization": f"Bearer {mint(session_id, wiki_task_id=token)}"}
            try:
                source = (await client.get("/api/wiki/read", params={"doc": "Library/source.md"}, headers=headers)).json()
                item = jobs.get(job["id"])["items"][0]
                response = await client.post("/api/wiki/publish", headers=headers, json={
                    "path": item["path"], "reservation_id": item["reservation_id"], "expected_revision": None,
                    "kind": "concept", "content": "# Sourced page\nRetain the original condition.\n",
                    "reason": "Create the requested page", "sources": [{"doc": source["id"], "revision": source["revision"], "location": "Source"}],
                })
                assert response.status_code == 200, response.text
                jobs.store.path("Library/source.md").write_bytes(b"# Source\nA later condition.\n")
                jobs.store.refresh()
                assert jobs.store.read("Library/new.md")["stale"]
                yield {"type": "result", "content": "Created", "is_error": False}
            finally:
                tasks.close(token)

    manager = Publisher(jobs)
    finished = await process(jobs, job, manager)
    assert finished["status"] == "completed_with_attention"
    assert finished["items"][0]["status"] == "attention"
    raw = jobs.store.path("Library/new.md").read_bytes()
    verified = await jobs.verify(job["id"], manager)
    assert verified["status"] == "completed_with_attention"
    assert "source" in verified["items"][0]["error"]
    assert jobs.store.path("Library/new.md").read_bytes() == raw
    assert manager.turns == manager.sessions == 1
