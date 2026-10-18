"""Single-note work may add index links without acquiring another body grant."""

import json

import pytest

from server.wiki.jobs import WikiJobs
from tests import test_wiki_api as wiki_api
from tests.test_wiki_grant_v2 import assigned

client = wiki_api.client


async def setup_notes(client, tmp_path, *, index="Library/index.md", topic=True):
    root = tmp_path / "vault"
    (root / "Library").mkdir(parents=True)
    (root / index).parent.mkdir(parents=True, exist_ok=True)
    (root / "Library/requested.md").write_text("# Requested\n\nA useful idea with explicit conditions.\n")
    (root / "Library/other.md").write_text("# Other\n\nKeep this unrelated argument.\n")
    raw = (("---\r\ntype: Topic\r\nprivate_field: keep-this-value\r\n---\r\n" if topic else "")
           + "# Navigation\r\n\r\nKeep these manual entries and explanations.\r\n").encode()
    (root / index).write_bytes(raw)
    config = await wiki_api.configure(client, root)
    jobs = WikiJobs(None)
    job = jobs.maintain("selected-note", "", "Ingest only the selected note, including necessary index links",
                        ["Library/requested.md"], None, [], "human")
    return root, jobs, job, config, raw


async def read_pair(client, headers, index):
    source = await client.get("/api/wiki/read", headers=headers, params={"doc": "Library/requested.md"})
    navigation = await client.get("/api/wiki/read", headers=headers, params={"doc": index})
    assert source.status_code == navigation.status_code == 200
    return source.json(), navigation.json()


def addition(source, navigation):
    return {"path": navigation["path"], "expected_revision": navigation["revision"], "reason": "Add the direct index entry for this ingestion",
            "relations": [{"doc": source["id"], "revision": source["revision"], "location": "L1-L3",
                           "explanation": "This selected note belongs in the existing navigation topic."}]}


@pytest.mark.parametrize("index,topic", [("Library/index.md", False), ("Wiki/topics/topic.md", True)])
async def test_selected_note_can_add_an_index_link_without_reviewing_or_rewriting_the_index(client, tmp_path, index, topic):
    root, jobs, job, config, original = await setup_notes(client, tmp_path, index=index, topic=topic)
    with assigned(job, config) as (headers, task):
        source, navigation = await read_pair(client, headers, index)
        added = await client.post("/api/wiki/organize", headers=headers, json=addition(source, navigation))
        assert added.status_code == 200, added.text
        updated = (root / index).read_bytes()
        assert updated.startswith(original) and b"[[Library/requested.md]]" in updated[len(original):]
        assert (root / source["path"]).read_text() == "# Requested\n\nA useful idea with explicit conditions.\n"
        assert not jobs.recover_publication(job["id"], dict(job["items"][0]))
        from server.wiki.tasks import tasks

        granted = tasks.current_grant(task)
        assert {row["doc_id"] for row in granted["selected_documents"]} == {source["id"]}
        assert granted["allowed_new_paths"] == []
        source, navigation = await read_pair(client, headers, index)
        repeated = await client.post("/api/wiki/organize", headers=headers, json=addition(source, navigation))
        assert repeated.status_code == 200, repeated.text
        assert repeated.json()["changed"] is False
        assert (root / index).read_bytes() == updated
        forbidden = await client.post("/api/wiki/organize", headers=headers, json=addition(source, navigation) | {"key_note": "Replace the index's explanation"})
        assert forbidden.status_code == 403
        assert (root / index).read_bytes() == updated
        with jobs.store.connect() as db:
            assert db.execute("SELECT count(*) FROM job_publications WHERE job_id=?", (job["id"],)).fetchone()[0] == 0
            assert db.execute("SELECT 1 FROM wiki_edges WHERE source_doc=? AND target_doc=? AND status='resolved'", (navigation["id"], source["id"])).fetchone()
            assert db.execute("SELECT count(*) FROM wiki_path_reservations WHERE job_id=?", (job["id"],)).fetchone()[0] == 0
        accepted = await client.post("/api/wiki/revise", headers=headers, json={"doc": source["id"], "expected_revision": source["revision"],
                                                                                "operation": "no_change", "reason": "The current selected text already satisfies the task"})
        assert accepted.status_code == 200, accepted.text
        item = dict(job["items"][0])
        assert jobs.recover_publication(job["id"], item) and item["output"] == source["path"]


@pytest.mark.parametrize("case", ["ordinary-note", "unassigned-relation", "frontmatter-change", "new-index"])
async def test_supporting_index_addition_cannot_expand_to_other_notes_or_body_changes(client, tmp_path, case):
    root, _, job, config, original = await setup_notes(client, tmp_path)
    with assigned(job, config) as (headers, _):
        source, navigation = await read_pair(client, headers, "Library/index.md")
        body = addition(source, navigation)
        if case == "ordinary-note":
            other = (await client.get("/api/wiki/read", headers=headers, params={"doc": "Library/other.md"})).json()
            body |= {"path": other["path"], "expected_revision": other["revision"]}
        elif case == "unassigned-relation":
            other = (await client.get("/api/wiki/read", headers=headers, params={"doc": "Library/other.md"})).json()
            body["relations"] = [{"doc": other["id"], "revision": other["revision"], "location": "L1-L3", "explanation": "An unassigned target"}]
        elif case == "frontmatter-change":
            body["frontmatter_patch"] = {"type": "Topic", "private_field": "rewritten"}
        else:
            body["path"] = "Library/unrequested-index.md"
        refused = await client.post("/api/wiki/organize", headers=headers, json=body)
        assert refused.status_code == 403, refused.text
    assert (root / "Library/index.md").read_bytes() == original
    assert (root / "Library/other.md").read_text() == "# Other\n\nKeep this unrelated argument.\n"
    assert not (root / "Library/unrequested-index.md").exists()


async def test_mcp_default_fields_allow_navigation_but_actual_metadata_changes_do_not(client, tmp_path, monkeypatch):
    from server.mcp_servers import wiki

    root, _, job, config, original = await setup_notes(client, tmp_path)
    monkeypatch.setattr(wiki, "call", lambda method, path, *, body: body)
    with assigned(job, config) as (headers, _):
        source, navigation = await read_pair(client, headers, "Library/index.md")
        payload = wiki.organize(**addition(source, navigation))
        assert payload["key_note"] is None and payload["frontmatter_patch"] is None
        assert payload["reuse_existing_summary"] is False
        for field, value in [("tags", []), ("aliases", []), ("sources", []), ("reuse_existing_summary", True)]:
            refused = await client.post("/api/wiki/organize", headers=headers, json=payload | {field: value})
            assert refused.status_code == 403, refused.text
            assert (root / navigation["path"]).read_bytes() == original
        added = await client.post("/api/wiki/organize", headers=headers, json=payload)
        assert added.status_code == 200, added.text


@pytest.mark.parametrize("edited", ["body", "owned-region"])
async def test_navigation_preserves_old_owned_evidence_and_does_not_wash_human_edits(client, tmp_path, edited):
    root, jobs, job, config, _ = await setup_notes(client, tmp_path)
    index = "Library/index.md"
    read = (await client.get("/api/wiki/read", headers=wiki_api.HEADERS, params={"doc": index})).json()
    organized = await client.post("/api/wiki/organize", headers=wiki_api.HEADERS, json={
        "path": index, "expected_revision": read["revision"], "key_note": "Original owned summary.", "reason": "Existing trusted organization"})
    assert organized.status_code == 200, organized.text
    with jobs.store.connect() as db:
        previous = dict(db.execute("SELECT * FROM managed_notes WHERE path=?", (index,)).fetchone())
    raw = (root / index).read_bytes().replace(
        b"manual entries" if edited == "body" else b"Original owned summary.",
        b"human entries" if edited == "body" else b"Human corrected summary.")
    (root / index).write_bytes(raw)
    with assigned(job, config) as (headers, _):
        source, navigation = await read_pair(client, headers, index)
        assert navigation["stale"] and navigation["metadata"]["needs_refresh"]
        added = await client.post("/api/wiki/organize", headers=headers, json=addition(source, navigation))
        assert added.status_code == 200, added.text
        assert (root / index).read_bytes().startswith(raw)
        with jobs.store.connect() as db:
            current = dict(db.execute("SELECT * FROM managed_notes WHERE path=?", (index,)).fetchone())
        assert current["base_hash"] == previous["base_hash"] and current["metadata"] == previous["metadata"]
        assert json.loads(current["regions"])[:-1] == json.loads(previous["regions"])
        source, navigation = await read_pair(client, headers, index)
        assert navigation["stale"] and navigation["metadata"]["needs_refresh"]
        repeated = await client.post("/api/wiki/organize", headers=headers, json=addition(source, navigation))
        assert repeated.status_code == 200 and repeated.json()["changed"] is False
        with jobs.store.connect() as db:
            assert dict(db.execute("SELECT * FROM managed_notes WHERE path=?", (index,)).fetchone()) == current


@pytest.mark.parametrize("frontmatter", [False, True])
async def test_later_explicit_index_organization_keeps_owned_navigation_links(client, tmp_path, frontmatter):
    root, jobs, job, config, _ = await setup_notes(client, tmp_path)
    index = "Library/index.md"
    with assigned(job, config) as (headers, _):
        source, navigation = await read_pair(client, headers, index)
        added = await client.post("/api/wiki/organize", headers=headers, json=addition(source, navigation))
        assert added.status_code == 200, added.text
    with jobs.store.connect() as db:
        nav_region = json.loads(db.execute("SELECT regions FROM managed_notes WHERE path=?", (index,)).fetchone()[0])[0]
    own = jobs.maintain("explicit-index", "", "Organize this index itself", [index], None, [], "human")
    with assigned(own, config) as (headers, _):
        current = (await client.get("/api/wiki/read", headers=headers, params={"doc": index})).json()
        body = {"path": index, "expected_revision": current["revision"], "key_note": "Navigation for the related topic.", "reason": "Explicit index body task"}
        if frontmatter:
            body["frontmatter_patch"] = {"type": "Topic"}
        result = await client.post("/api/wiki/organize", headers=headers, json=body)
        assert result.status_code == 200, result.text
    assert (root / index).read_bytes().count(nav_region["raw"].encode()) == 1
    with jobs.store.connect() as db:
        assert nav_region in json.loads(db.execute("SELECT regions FROM managed_notes WHERE path=?", (index,)).fetchone()[0])


async def test_navigation_deduplicates_resolved_identity_and_never_accepts_an_index_baseline(client, tmp_path):
    from tests.test_wiki_maintenance_state import settle, state

    root, jobs, job, config, original = await setup_notes(client, tmp_path)
    index = "Library/index.md"
    await settle(client, jobs, config, path=index)
    before_b = (await state(client, index))["items"][0]
    before_a = (await state(client, "Library/requested.md"))["items"][0]
    with assigned(job, config) as (headers, _):
        source, navigation = await read_pair(client, headers, index)
        added = await client.post("/api/wiki/organize", headers=headers, json=addition(source, navigation))
        assert added.status_code == 200, added.text
        assert not jobs.publication(job["id"], source["id"])
        with jobs.store.connect() as db:
            receipt = dict(db.execute("SELECT * FROM operation_receipts WHERE id=?", (added.json()["receipt_id"],)).fetchone())
        assert receipt["operation"] == "navigation_link" and receipt["item_doc"] is None
        assert receipt["doc_id"] == navigation["id"]
        assert json.loads(receipt["details"])["supporting_navigation"]["inputs"][0]["doc"] == source["id"]
    after_b = (await state(client, index))["items"][0]
    after_a = (await state(client, source["id"]))["items"][0]
    assert after_b["baseline"] == before_b["baseline"] and after_b["status"] == "changed"
    assert after_b["attempt_count"] == before_b["attempt_count"] == 0
    assert after_a["baseline"] is None and after_a["attempt_count"] == before_a["attempt_count"] == 1
    # A title link with different spelling resolves to the same actual A.
    (root / index).write_bytes(original + b"\r\n- [[Requested]]\r\n")
    with assigned(job, config) as (headers, _):
        source, navigation = await read_pair(client, headers, index)
        before = (root / index).read_bytes()
        repeated = await client.post("/api/wiki/organize", headers=headers, json=addition(source, navigation))
        assert repeated.status_code == 200 and repeated.json()["changed"] is False
        assert (root / index).read_bytes() == before


@pytest.mark.parametrize("phase", ["prepare", "repeat", "replace"])
@pytest.mark.parametrize("revocation", ["run", "work"])
async def test_navigation_checks_the_original_scope_at_each_write_boundary(client, tmp_path, monkeypatch, phase, revocation):
    from uuid import uuid4

    from server import mcp_identity
    from server.wiki import history, operations
    from server.wiki.tasks import tasks

    root, jobs, job, config, original = await setup_notes(client, tmp_path)
    monkeypatch.setattr(mcp_identity, "_run_leases", {})
    monkeypatch.setattr(mcp_identity, "_closed_works", set())
    with assigned(job, config) as (diagnostic, task):
        token = mcp_identity.verify(diagnostic["Authorization"].removeprefix("Bearer ")).wiki_task_id
        lease, work = uuid4().hex, uuid4().hex
        mcp_identity.register_run(lease, task.session_id, task.user_id, work)
        bearer = mcp_identity.mint(task.session_id, wiki_task_id=token, run_lease_id=lease, work_id=work)
        headers = {"Authorization": "Bearer " + bearer}
        source, navigation = await read_pair(client, headers, "Library/index.md")
        if phase == "repeat":
            assert (await client.post("/api/wiki/organize", headers=headers, json=addition(source, navigation))).status_code == 200
            source, navigation = await read_pair(client, headers, "Library/index.md")
            original = (root / navigation["path"]).read_bytes()
        with jobs.store.connect() as db:
            before = db.execute("SELECT count(*) FROM operation_receipts WHERE job_id=?", (job["id"],)).fetchone()[0]
        injected = []

        def revoke():
            if revocation == "run":
                mcp_identity.revoke_run(lease)
                mcp_identity.register_run(uuid4().hex, task.session_id, task.user_id, work)
            else:
                mcp_identity.close_work(work)
            assert tasks.active[token] is task
            injected.append(True)

        record, atomic, receipt = history.WikiHistory._record, history.atomic, operations.record
        if phase == "prepare":
            def before_record(self, *args, **kwargs):
                if kwargs.get("operation") == "navigation_link":
                    revoke()
                return record(self, *args, **kwargs)
            monkeypatch.setattr(history.WikiHistory, "_record", before_record)
        elif phase == "repeat":
            def before_receipt(*args, **kwargs):
                if kwargs.get("operation") == "navigation_link":
                    revoke()
                return receipt(*args, **kwargs)
            monkeypatch.setattr(operations, "record", before_receipt)
        else:
            def before_replace(path, raw, **kwargs):
                if path == root / navigation["path"]:
                    checked = kwargs["before_replace"]
                    def check():
                        revoke()
                        checked()
                    kwargs["before_replace"] = check
                return atomic(path, raw, **kwargs)
            monkeypatch.setattr(history, "atomic", before_replace)
        result = await client.post("/api/wiki/organize", headers=headers, json=addition(source, navigation))
        assert result.status_code == 403 and injected == [True], result.text
        assert (root / navigation["path"]).read_bytes() == original
        with jobs.store.connect() as db:
            assert db.execute("SELECT count(*) FROM operation_receipts WHERE job_id=?", (job["id"],)).fetchone()[0] == before + (phase == "replace")
            assert db.execute("SELECT count(*) FROM job_publications WHERE job_id=?", (job["id"],)).fetchone()[0] == 0
            if phase == "replace":
                assert db.execute("SELECT status FROM changes WHERE id=(SELECT change_id FROM operation_receipts WHERE job_id=?)", (job["id"],)).fetchone()[0] == "prepared"


@pytest.mark.parametrize("phase", ["prepare", "replace"])
@pytest.mark.parametrize("mutation", ["input-edit", "input-replace", "index-replace"])
async def test_navigation_cannot_reuse_stale_or_same_byte_replacement_proofs(client, tmp_path, monkeypatch, phase, mutation):
    from server.wiki import history

    root, jobs, job, config, original = await setup_notes(client, tmp_path)
    with assigned(job, config) as (headers, _):
        source, navigation = await read_pair(client, headers, "Library/index.md")
        injected = []

        def mutate():
            target = root / (navigation["path"] if mutation == "index-replace" else source["path"])
            if mutation == "input-edit":
                target.write_bytes(target.read_bytes() + b"\nNew human condition.\n")
            else:
                replacement = target.with_suffix(".replacement")
                replacement.write_bytes(target.read_bytes())
                replacement.replace(target)
            injected.append(True)

        record, atomic = history.WikiHistory._record, history.atomic
        if phase == "prepare":
            def before_record(self, *args, **kwargs):
                if kwargs.get("operation") == "navigation_link":
                    mutate()
                return record(self, *args, **kwargs)
            monkeypatch.setattr(history.WikiHistory, "_record", before_record)
        else:
            def before_replace(path, raw, **kwargs):
                if path == root / navigation["path"]:
                    checked = kwargs["before_replace"]
                    def check():
                        mutate()
                        checked()
                    kwargs["before_replace"] = check
                return atomic(path, raw, **kwargs)
            monkeypatch.setattr(history, "atomic", before_replace)
        result = await client.post("/api/wiki/organize", headers=headers, json=addition(source, navigation))
        assert result.status_code == 409 and injected == [True], result.text
        assert (root / navigation["path"]).read_bytes() == original
        with jobs.store.connect() as db:
            assert db.execute("SELECT count(*) FROM operation_receipts WHERE job_id=?", (job["id"],)).fetchone()[0] == (phase == "replace")
            assert db.execute("SELECT count(*) FROM job_publications WHERE job_id=?", (job["id"],)).fetchone()[0] == 0


@pytest.mark.parametrize("origin", ["material", "created"])
async def test_navigation_accepts_actual_ingest_and_bound_creation_identities_without_claiming_their_completion(client, tmp_path, origin):
    from tests.test_wiki_grant_v2 import publish

    _, jobs, _, config, _ = await setup_notes(client, tmp_path)
    path = "Library/input.md"
    if origin == "material":
        job = jobs.maintain("material-nav", "# Input\n\nRetain the material's exact words.\n", "Organize this material", [], path, [], "human")
        document = jobs.ingest(job["id"])
        job = jobs.update(job["id"], items=[{"doc": document["id"], "path": path, "revision": document["revision"], "status": "pending"}])
    else:
        job = jobs.maintain("created-nav", "", "Create a requested page", [], None, [], "human",
                            creation={"purpose": "requested_knowledge_page", "paths": [path]})
    with assigned(job, config) as (headers, _):
        created = await publish(client, headers, path, job["items"][0]["reservation_id"]) if origin == "created" else None
        if created:
            assert created.status_code == 200, created.text
        source = (await client.get("/api/wiki/read", headers=headers, params={"doc": path})).json()
        navigation = (await client.get("/api/wiki/read", headers=headers, params={"doc": "Library/index.md"})).json()
        added = await client.post("/api/wiki/organize", headers=headers, json=addition(source, navigation))
        assert added.status_code == 200, added.text
        item = dict(job["items"][0])
        if origin == "material":
            assert not jobs.recover_publication(job["id"], item)
        else:
            assert jobs.recover_creation(job["id"], item) and item["status"] == "done"
            assert item["receipt_id"] == created.json()["receipt_id"] != added.json()["receipt_id"]


async def test_only_navigation_output_does_not_complete_primary_or_unstarted_creation_in_the_real_worker(client, tmp_path):
    from types import SimpleNamespace

    from server.mcp_identity import mint
    from server.wiki.tasks import tasks
    from tests.submission import receipt_from_events
    from tests.test_wiki_jobs import Curator, process

    root, jobs, _, config, _ = await setup_notes(client, tmp_path)
    job = jobs.maintain("navigation-only-worker", "", "Maintain A and create the requested finite output",
                        ["Library/requested.md"], None, [], "human",
                        creation={"purpose": "requested_knowledge_page", "paths": ["Library/new.md"]})

    class NavigationCurator(Curator):
        async def start_message(self, session_id, prompt, **kwargs):
            async def events():
                self.turns += 1
                token = tasks.open(SimpleNamespace(id=session_id, user_id=None, agent_id=config["curator_agent_id"]), "maintain",
                                   request_id=kwargs["request_id"])
                try:
                    headers = {"Authorization": "Bearer " + mint(session_id, wiki_task_id=token)}
                    source, navigation = await read_pair(client, headers, "Library/index.md")
                    result = await client.post("/api/wiki/organize", headers=headers, json=addition(source, navigation))
                    assert result.status_code == 200, result.text
                    # Follow the ordinary final-output read contract too.
                    assert (await client.get("/api/wiki/read", headers=headers, params={"doc": navigation["id"]})).status_code == 200
                    yield {"type": "result", "content": "Navigation was updated", "is_error": False}
                finally:
                    tasks.close(token)
            return await receipt_from_events(events(), kwargs["request_id"])

    curator = NavigationCurator(jobs)
    result = await process(jobs, job, curator)
    assert result["status"] == "completed_with_attention" and curator.turns == 1
    assert all(item["status"] == "attention" for item in result["items"])
    assert not (root / "Library/new.md").exists()
    assert len(result["receipts"]) == 1 and result["receipts"][0]["operation"] == "navigation_link"
    assert result["grant"]["reservations"][0]["state"] == "released"


async def test_navigation_recovery_preserves_the_original_change_and_receipt_without_completing_A(client, tmp_path, monkeypatch):
    from server.wiki.history import WikiHistory

    root, jobs, job, config, _ = await setup_notes(client, tmp_path)
    status = WikiHistory.status

    def fail_applied(self, change_id, value):
        if value == "applied":
            raise OSError("injected navigation applied-marker interruption")
        return status(self, change_id, value)

    with assigned(job, config) as (headers, _):
        source, navigation = await read_pair(client, headers, "Library/index.md")
        with monkeypatch.context() as patch:
            patch.setattr(WikiHistory, "status", fail_applied)
            with pytest.raises(OSError, match="applied-marker"):
                await client.post("/api/wiki/organize", headers=headers, json=addition(source, navigation))
        raw = (root / navigation["path"]).read_bytes()
        with jobs.store.connect() as db:
            before = dict(db.execute("SELECT * FROM operation_receipts WHERE job_id=?", (job["id"],)).fetchone())
            assert db.execute("SELECT status FROM changes WHERE id=?", (before["change_id"],)).fetchone()[0] == "prepared"
        source, navigation = await read_pair(client, headers, navigation["path"])
        repeated = await client.post("/api/wiki/organize", headers=headers, json=addition(source, navigation))
        assert repeated.status_code == 200 and repeated.json()["changed"] is False
        assert (root / navigation["path"]).read_bytes() == raw
        assert not jobs.recover_publication(job["id"], dict(job["items"][0]))
        with jobs.store.connect() as db:
            assert dict(db.execute("SELECT * FROM operation_receipts WHERE id=?", (before["id"],)).fetchone()) == before
            assert db.execute("SELECT status FROM changes WHERE id=?", (before["change_id"],)).fetchone()[0] == "applied"
            assert db.execute("SELECT count(*) FROM changes WHERE path=?", (navigation["path"],)).fetchone()[0] == 1
