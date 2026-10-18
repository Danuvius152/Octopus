"""A worker's write authority comes from its admitted job, not its role."""

import json
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from server.mcp_identity import mint
from server.wiki import grants
from server.wiki.history import WikiHistory
from server.wiki.jobs import WikiJobs
from server.wiki.registry import registry
from server.wiki.store import WikiError
from server.wiki.tasks import tasks
from tests import test_wiki_api as wiki_api

client = wiki_api.client


@contextmanager
def assigned(job, config, paths=None):
    session = SimpleNamespace(id="exact-worker-" + job["id"], user_id=None, agent_id=config["curator_agent_id"])
    jobs = WikiJobs(None)
    jobs.update(job["id"], status="running", session_id=session.id)
    batch = [item for item in job["items"] if paths is None or item["path"] in paths]
    tasks.assign(session.id, job["id"], batch, registration=config)
    token = tasks.open(session, "maintain")
    try:
        yield {"Authorization": f"Bearer {mint(session.id, wiki_task_id=token)}"}, tasks.active[token]
    finally:
        tasks.close(token)
        tasks.unassign(session.id)


async def proposed(client, root, *, paths=None, key="new-knowledge", material=""):
    config = await wiki_api.configure(client, root)
    response = await client.post("/api/wiki/maintain", headers=wiki_api.HEADERS, json={
        "idempotency_key": key, "scope": "Create the requested finite knowledge page",
        "creation": {"purpose": "requested_knowledge_page", "paths": paths}, "material": material,
    })
    assert response.status_code == 200, response.text
    return response.json(), config


async def publish(client, headers, path, reservation_id=None):
    return await client.post("/api/wiki/publish", headers=headers, json={
        "path": path, "expected_revision": None, "kind": "concept", "content": "# Requested knowledge\n\nA bounded conclusion.\n",
        "reason": "Create the explicitly requested page", "reservation_id": reservation_id,
    })


async def test_existing_document_worker_cannot_publish_an_unrequested_page(client, tmp_path):
    root = tmp_path / "vault"
    (root / "Library").mkdir(parents=True)
    original = b"# Requested note\n\nKeep the original argument.\n"
    (root / "Library/requested.md").write_bytes(original)
    config = await wiki_api.configure(client, root)
    jobs = WikiJobs(None)
    job = jobs.maintain(
        "grant-existing-only", "", "Correct the selected existing note",
        ["Library/requested.md"], None, [], "human",
    )
    session = SimpleNamespace(id="bounded-worker", user_id=None, agent_id=config["curator_agent_id"])
    jobs.update(job["id"], status="running", session_id=session.id)
    tasks.assign(session.id, job["id"], job["items"], registration=config)
    token = tasks.open(session, "maintain")
    headers = {"Authorization": f"Bearer {mint(session.id, wiki_task_id=token)}"}
    unrequested = "Library/unrequested.md"
    try:
        read = await client.get("/api/wiki/read", params={"doc": "Library/requested.md"}, headers=headers)
        assert read.status_code == 200, read.text
        response = await client.post("/api/wiki/publish", headers=headers, json={
            "path": unrequested, "expected_revision": None, "kind": "source",
            "content": "# An unrequested page\n\nOutside the admitted correction.\n",
            "reason": "Exercise the existing-document grant",
        })
        with jobs.store.connect() as db:
            receipts = db.execute(
                "SELECT id FROM operation_receipts WHERE job_id=? AND path=?",
                (job["id"], unrequested),
            ).fetchall()
        facts = {
            "http_status": response.status_code,
            "unrequested_file_exists": (root / unrequested).exists(),
            "unrequested_operation_receipts": len(receipts),
            "selected_file_unchanged": (root / "Library/requested.md").read_bytes() == original,
        }
        print(json.dumps(facts, sort_keys=True))
        assert response.status_code == 403, facts
        assert not facts["unrequested_file_exists"], facts
        assert facts["unrequested_operation_receipts"] == 0, facts
        assert facts["selected_file_unchanged"], facts
    finally:
        tasks.close(token)
        tasks.unassign(session.id)


@pytest.mark.parametrize("kind", ["existing", "creation"])
@pytest.mark.parametrize("revocation", ["run", "work"])
async def test_actual_replace_rechecks_original_worker_lease_even_when_task_and_session_remain(client, tmp_path, monkeypatch, kind, revocation):
    from uuid import uuid4

    from server import mcp_identity
    from server.wiki import history

    root = tmp_path / "vault"
    (root / "Library").mkdir(parents=True)
    path = "Library/target.md"
    before = b"# Target\n\nKeep this original argument.\n" if kind == "existing" else None
    if before is not None:
        (root / path).write_bytes(before)
    config = await wiki_api.configure(client, root)
    jobs = WikiJobs(None)
    job = jobs.maintain("original-worker-scope", "", "Maintain the finite target", [path] if before else [], None, [], "human",
                        creation={"purpose": "requested_knowledge_page", "paths": [path]} if before is None else None)
    monkeypatch.setattr(mcp_identity, "_run_leases", {})
    monkeypatch.setattr(mcp_identity, "_closed_works", set())
    with assigned(job, config) as (diagnostic, task):
        token = mcp_identity.verify(diagnostic["Authorization"].removeprefix("Bearer ")).wiki_task_id
        lease, work = uuid4().hex, uuid4().hex
        mcp_identity.register_run(lease, task.session_id, task.user_id, work)
        headers = {"Authorization": "Bearer " + mcp_identity.mint(task.session_id, wiki_task_id=token, run_lease_id=lease, work_id=work)}
        current = (await client.get("/api/wiki/read", headers=headers, params={"doc": path})).json() if before else None
        atomic = history.atomic
        injected = []

        def at_replace(target, raw, **kwargs):
            if target == root / path:
                checked = kwargs["before_replace"]
                def check():
                    if revocation == "run":
                        mcp_identity.revoke_run(lease)
                        mcp_identity.register_run(uuid4().hex, task.session_id, task.user_id, work)
                    else:
                        mcp_identity.close_work(work)
                    assert tasks.active[token] is task
                    injected.append(True)
                    checked()
                kwargs["before_replace"] = check
            return atomic(target, raw, **kwargs)

        monkeypatch.setattr(history, "atomic", at_replace)
        if current:
            result = await client.post("/api/wiki/organize", headers=headers, json={
                "path": path, "expected_revision": current["revision"], "key_note": "An authorized addition.", "reason": "Original worker context"})
        else:
            result = await publish(client, headers, path, job["items"][0]["reservation_id"])
        assert result.status_code == 403 and injected == [True], result.text
        assert ((root / path).read_bytes() if (root / path).exists() else None) == before
        with jobs.store.connect() as db:
            receipt = db.execute("SELECT change_id FROM operation_receipts WHERE job_id=?", (job["id"],)).fetchone()
            assert receipt and db.execute("SELECT status FROM changes WHERE id=?", (receipt[0],)).fetchone()[0] == "prepared"
            assert db.execute("SELECT count(*) FROM wiki_path_reservations WHERE job_id=? AND state='bound'", (job["id"],)).fetchone()[0] == 0


async def test_requested_page_consumes_reservation_and_can_be_revised_as_the_same_document(client, tmp_path):
    root = tmp_path / "vault"
    (root / "Library").mkdir(parents=True)
    job, config = await proposed(client, root, paths=["Library/knowledge.md"])
    assert job["grant"]["selected_documents"] == []
    item = job["items"][0]
    assert item["kind"] == "creation" and "doc" not in item
    with assigned(job, config) as (headers, task):
        refused = await publish(client, headers, "Library/other.md", item["reservation_id"])
        assert refused.status_code == 403 and not (root / "Library/other.md").exists()
        created = await publish(client, headers, item["path"], item["reservation_id"])
        assert created.status_code == 200, created.text
        current = await client.get("/api/wiki/read", headers=headers, params={"doc": item["path"]})
        assert current.status_code == 200, current.text
        document = current.json()
        grant = tasks.current_grant(task)
        assert not grant["allow_new_pages"] and grant["allowed_new_paths"] == []
        assert [row["doc_id"] for row in grant["selected_documents"]] == [document["id"]]
        repeated = await publish(client, headers, item["path"], item["reservation_id"])
        assert repeated.status_code == 403
        revised = await client.post("/api/wiki/organize", headers=headers, json={
            "path": item["path"], "expected_revision": document["revision"],
            "key_note": "This conclusion has a bounded scope.", "reason": "Complete the same created page",
        })
        assert revised.status_code == 200, revised.text
        assert (await client.get("/api/wiki/read", headers=headers, params={"doc": document["id"]})).json()["id"] == document["id"]


async def test_default_target_and_explicit_target_replays_reuse_the_admitted_job(client, tmp_path):
    root = tmp_path / "vault"
    root.mkdir()
    job, config = await proposed(client, root)
    assert job["grant"]["allowed_new_paths"] == [f"Library/Inbox/{job['id']}-knowledge.md"]
    replay, _ = await proposed(client, root)
    assert replay["id"] == job["id"] and replay["grant"] == job["grant"]
    other, _ = await proposed(client, root, key="different", paths=["Library/fixed.md"])
    conflict = await client.post("/api/wiki/maintain", headers=wiki_api.HEADERS, json={
        "idempotency_key": "third", "creation": {"purpose": "requested_knowledge_page", "paths": ["Library/fixed.md"]},
    })
    assert conflict.status_code == 409
    with registry.store(None).connect() as db:
        assert db.execute("SELECT count(*) FROM jobs").fetchone()[0] == 2
        assert db.execute("SELECT count(*) FROM wiki_path_reservations").fetchone()[0] == 2
    assert other["id"] != job["id"] and config["vault_id"] == replay["grant"]["vault_id"]


async def test_existing_note_remains_writable_and_publish_is_hidden(client, tmp_path):
    from server.mcp_identity import reset_current_scope, set_current_scope, verify
    from server.mcp_servers.wiki import mcp
    from server.wiki.workflow import instructions

    root = tmp_path / "vault"
    (root / "Library").mkdir(parents=True)
    (root / "Library/note.md").write_text("# Note\n\nKeep this original argument.\n")
    config = await wiki_api.configure(client, root)
    job = WikiJobs(None).maintain("existing", "", "Organize only this note", ["Library/note.md"], None, [], "human")
    with assigned(job, config) as (headers, task):
        scope = verify(headers["Authorization"].removeprefix("Bearer "))
        context = set_current_scope(scope)
        try:
            names = {tool.name for tool in await mcp.list_tools()}
        finally:
            reset_current_scope(context)
        assert "organize" in names and "publish" not in names
        card = json.loads(instructions("maintain", scope.wiki_task_id).splitlines()[0].split("：", 1)[1])
        assert card["writable_files"] == ["Library/note.md"] and card["allowed_new_paths"] == []
        current = await client.get("/api/wiki/read", headers=headers, params={"doc": "Library/note.md"})
        assert current.status_code == 200, current.text
        revised = await client.post("/api/wiki/organize", headers=headers, json={
            "path": "Library/note.md", "expected_revision": current.json()["revision"],
            "key_note": "The original argument is retained.", "reason": "Organize the admitted note",
        })
        assert revised.status_code == 200, revised.text
        assert not tasks.current_grant(task)["allow_new_pages"]


async def test_three_document_grant_lookup_does_not_scan_the_whole_job(client, tmp_path):
    """Measure SQLite work, not just the three hydrated result rows."""
    import uuid

    root = tmp_path / "vault"
    (root / "Library").mkdir(parents=True)
    (root / "Library/note-00000.md").write_text("# Current\n\nA synthetic selected document.\n")
    registration = await wiki_api.configure(client, root)
    jobs = WikiJobs(None)
    counts = []
    with jobs.store.connect() as db:
        template = dict(db.execute("SELECT * FROM documents").fetchone())
        documents = [{"doc": template["id"], "path": template["path"], "revision": template["revision"]}]
        # These synthetic ledger rows isolate the grant query from extraction,
        # HTTP and filesystem setup. Their precise identity/path pairs exist;
        # no claim about indexing, admission or model output is made here.
        for number in range(1, 5000):
            doc, path = uuid.uuid4().hex, f"Library/note-{number:05}.md"
            db.execute("INSERT INTO documents VALUES(:id,:path,:revision,:title,:kind,:text,:pages,:quality,:metadata,:signature)",
                       template | {"id": doc, "path": path})
            db.execute("INSERT INTO document_identities VALUES(?,?,'')", (doc, path))
            documents.append({"doc": doc, "path": path, "revision": template["revision"]})
        document_ids = tuple(doc["doc"] for doc in documents[:3])
        expected = {doc["doc"] for doc in documents[:3]}
        for size in (100, 1000, 5000):
            job_id = uuid.uuid4().hex
            db.execute("INSERT INTO jobs VALUES(?,?,?,'{}','queued','[]',NULL,NULL,?,?)",
                       (job_id, job_id, "synthetic-grant-query-fixture", "2026-10-18", "2026-10-18"))
            grants.issue(db, registration, job_id, documents[:size])
            steps = 0

            def progress():
                nonlocal steps
                steps += 100
                return 0

            db.set_progress_handler(progress, 100)
            try:
                found = grants.view(db, job_id, document_ids=document_ids, reservation_ids=())
            finally:
                db.set_progress_handler(None, 0)
            assert {doc["doc_id"] for doc in found["selected_documents"]} == expected
            counts.append(steps)
    print(json.dumps({"grant_sizes": [100, 1000, 5000], "three_document_lookup_VM_steps": counts}))
    assert max(counts) <= min(counts) * 1.5 + 1000, counts


@pytest.mark.parametrize("mode,trusted_request,allowed", [
    ("read", "Create a requested page", False),
    ("auto", "Create a requested page", True),
    ("maintain", "/wiki maintain Library/existing.md", False),
    ("maintain", "/wiki maintain Library/new.md", True),
    ("maintain", "/wiki maintain", False),
])
async def test_creation_proposals_follow_trusted_entry_scope(client, tmp_path, mode, trusted_request, allowed):
    root = tmp_path / "vault"
    root.mkdir()
    config = await wiki_api.configure(client, root)
    session = SimpleNamespace(id="entry", user_id=None, agent_id=config["curator_agent_id"])
    token = tasks.open(session, mode, trusted_request)
    headers = {"Authorization": f"Bearer {mint(session.id, wiki_task_id=token)}"}
    try:
        result = await client.post("/api/wiki/maintain", headers=headers, json={
            "idempotency_key": "creation-scope", "creation": {"purpose": "requested_knowledge_page", "paths": ["Library/new.md"]},
        })
        assert result.status_code == (200 if allowed else 403), result.text
    finally:
        tasks.close(token)


async def test_forged_grant_fields_are_not_creation_authority(client, tmp_path):
    root = tmp_path / "vault"
    root.mkdir()
    await wiki_api.configure(client, root)
    result = await client.post("/api/wiki/maintain", headers=wiki_api.HEADERS, json={
        "idempotency_key": "forged", "allow_new_pages": True, "allowed_new_paths": ["Library/forged.md"],
    })
    assert result.status_code == 422
    with registry.store(None).connect() as db:
        assert db.execute("SELECT count(*) FROM wiki_job_grants").fetchone()[0] == 0
        grants.require(db)


async def test_created_output_keeps_its_historical_baseline_after_a_human_edit(client, tmp_path):
    from tests.test_wiki_maintenance_state import state

    root = tmp_path / "vault"
    (root / "Library").mkdir(parents=True)
    job, config = await proposed(client, root, paths=["Library/knowledge.md"])
    jobs = WikiJobs(None)
    item = job["items"][0]
    with assigned(job, config) as (headers, _):
        response = await publish(client, headers, item["path"], item["reservation_id"])
        assert response.status_code == 200, response.text
    assert jobs.recover_creation(job["id"], item) and item["status"] == "done", item
    jobs.update(job["id"], items=[item], status="completed", session_id=None)
    accepted = (await state(client, item["doc"]))["items"][0]
    assert accepted["status"] == "unchanged" and accepted["baseline"]["id"] == item["receipt_id"], accepted
    before = (root / item["path"]).read_bytes()
    edited = await client.put("/api/wiki/document", headers=wiki_api.HEADERS, json={
        "path": item["path"], "content": before.decode() + "\nA later human condition.\n",
        "expected_revision": item["output_revision"], "reason": "Human adds a condition",
    })
    assert edited.status_code == 200, edited.text
    changed = (await state(client, item["doc"]))["items"][0]
    assert changed["status"] == "changed", changed
    assert changed["baseline"] == accepted["baseline"]
    assert changed["current"]["revision"] != changed["baseline"]["output_revision"]


async def test_landed_creation_recovers_the_same_change_without_a_second_publish(client, tmp_path, monkeypatch):
    root = tmp_path / "vault"
    (root / "Library").mkdir(parents=True)
    job, config = await proposed(client, root, paths=["Library/knowledge.md"])
    jobs = WikiJobs(None)
    status = WikiHistory.status
    injected = []

    def fail_applied(history, change_id, value):
        if value == "applied" and not injected:
            injected.append(change_id)
            raise WikiError("Injected failure after the file landed", 503)
        return status(history, change_id, value)

    monkeypatch.setattr(WikiHistory, "status", fail_applied)
    with assigned(job, config) as (headers, _):
        item = job["items"][0]
        response = await publish(client, headers, item["path"], item["reservation_id"])
        assert response.status_code == 503 and (root / item["path"]).exists(), response.text
    assert jobs.recover_creation(job["id"], item) and item["status"] == "done", item
    with jobs.store.connect() as db:
        reservation = db.execute("SELECT * FROM wiki_path_reservations WHERE id=?", (item["reservation_id"],)).fetchone()
        assert reservation["state"] == "bound" and reservation["change_id"] == injected[0]
        assert reservation["document_id"] == item["doc"]
        assert db.execute("SELECT count(*) FROM changes").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM operation_receipts").fetchone()[0] == 1


async def test_framework_material_is_an_existing_document_grant_not_a_publish_grant(client, tmp_path):
    root = tmp_path / "vault"
    root.mkdir()
    config = await wiki_api.configure(client, root)
    jobs = WikiJobs(None)
    job = jobs.maintain("material", "# Original material\n\nPreserve these exact words.\n", "Organize the material", [], None, [], "human")
    document = jobs.ingest(job["id"])
    item = {"doc": document["id"], "path": document["path"], "revision": document["revision"],
            "quality": document["quality"], "status": "pending"}
    job = jobs.update(job["id"], items=[item])
    assert job["grant"]["allowed_new_paths"] == []
    with assigned(job, config) as (headers, task):
        assert [d["doc_id"] for d in tasks.current_grant(task)["selected_documents"]] == [document["id"]]
        denied = await publish(client, headers, "Library/a-second-original.md")
        assert denied.status_code == 403
        assert (await client.get("/api/wiki/read", headers=headers, params={"doc": document["id"]})).status_code == 200
    assert jobs.ingest(job["id"])["id"] == document["id"]
    assert len(list(root.rglob("*.md"))) == 1


@pytest.mark.parametrize("material", ["", "# A raw source\n\nPreserve all the supplied words.\n"])
async def test_worker_executes_each_finite_output_and_does_not_skip_material_ingest(client, tmp_path, material):
    from server.mcp_identity import verify
    from tests.submission import receipt_from_events
    from tests.test_wiki_jobs import Curator, process

    root = tmp_path / "vault"
    (root / "Library").mkdir(parents=True)
    paths = [f"Library/page-{index}.md" for index in range(5)]
    job, config = await proposed(client, root, paths=paths, material=material)
    jobs = WikiJobs(None)

    class HttpCurator(Curator):
        async def start_message(self, session_id, prompt, **kwargs):
            async def events():
                self.turns += 1
                session = SimpleNamespace(id=session_id, user_id=None, agent_id=config["curator_agent_id"])
                token = tasks.open(session, "maintain", request_id=kwargs["request_id"])
                headers = {"Authorization": "Bearer " + mint(session_id, wiki_task_id=token)}
                task = tasks.active[token]
                assert verify(headers["Authorization"].removeprefix("Bearer ")).wiki_task_id == token
                try:
                    for item in self.jobs.get(job["id"])["items"]:
                        if item["status"] != "pending" or (
                            item.get("doc") not in task.document_ids
                            and item.get("reservation_id") not in task.reservation_ids
                        ):
                            continue
                        if item.get("kind") == "creation":
                            response = await publish(client, headers, item["path"], item["reservation_id"])
                        else:
                            read = await client.get("/api/wiki/read", headers=headers, params={"doc": item["doc"]})
                            assert read.status_code == 200, read.text
                            response = await client.post("/api/wiki/organize", headers=headers, json={
                                "path": read.json()["path"], "expected_revision": read.json()["revision"],
                                "key_note": "Preserve the supplied original argument.", "reason": "Organize the ingested input",
                            })
                        assert response.status_code == 200, response.text
                    yield {"type": "result", "content": "All assigned outputs published", "is_error": False}
                finally:
                    tasks.close(token)
            return await receipt_from_events(events(), kwargs["request_id"])

    curator = HttpCurator(jobs)
    result = await process(jobs, job, curator)
    assert result["status"] == "completed", result
    assert curator.turns == 2
    assert result["progress"]["completed"] == 5 + bool(material)
    assert len(result["grant"]["selected_documents"]) == 5 + bool(material)
    assert all(item.get("doc") and item["index_complete"] for item in result["items"])
    assert len(list(root.rglob("*.md"))) == 5 + bool(material)
