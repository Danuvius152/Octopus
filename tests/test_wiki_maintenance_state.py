"""Daily maintenance uses trusted selection and settled output facts, not names."""

import asyncio
import base64
import json
from contextlib import asynccontextmanager, contextmanager
from types import SimpleNamespace

import pytest

from server import deps
from server.mcp_identity import mint
from server.wiki import registry
from server.wiki.jobs import WikiJobs
from server.wiki.tasks import command_prompt, tasks
from tests import test_tenant_isolation as tenants
from tests import test_wiki_api as api

client = api.client
two_accounts = tenants.two_accounts


async def prepare(client, tmp_path, files=None):
    vault = tmp_path / "vault"
    for path, text in (files or {"Library/journal.md": "# Journal\n\nKeep the current words.\n"}).items():
        file = vault / path
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(text)
    config = await api.configure(client, vault)
    owner = await deps.get_user_manager().resolve_token(
        client.headers["Authorization"].removeprefix("Bearer "), touch=False,
    )
    assert owner is not None
    return vault, config, WikiJobs(owner["id"])


@asynccontextmanager
async def entry(client, mode="maintain", request="/wiki维护", agent="reader", knowledge=None):
    owner = await deps.get_user_manager().resolve_token(
        client.headers["Authorization"].removeprefix("Bearer "), touch=False,
    )
    assert owner is not None
    session = SimpleNamespace(id="state-" + agent, user_id=owner["id"], agent_id=agent)
    token = tasks.open(session, mode, request, knowledge=knowledge)
    try:
        yield {"Authorization": "Bearer " + mint(session.id, user_id=session.user_id, wiki_task_id=token)}, tasks.active[token]
    finally:
        tasks.close(token)


async def state(client, doc=None, headers=None, **kwargs):
    params = {"doc": doc, **kwargs} if doc else kwargs
    result = await client.get("/api/wiki/maintenance-status", params=params, headers=headers or client.headers)
    assert result.status_code == 200, result.text
    return result.json()


async def current_read(client, doc, headers=None):
    for _ in range(100):
        result = await client.get("/api/wiki/read", params={"doc": doc}, headers=headers or client.headers)
        if result.status_code != 503:
            return result
        await asyncio.sleep(0.01)
    pytest.fail("Known filesystem notifications did not settle")


async def settle(client, jobs, config, path="Library/journal.md", *, job=None, operation="no_change", final_status="completed", settled=True, reason="Current record already fits its purpose"):
    job = job or jobs.maintain("baseline-" + path, "", "Review current words", [path] if operation != "publish" else [], None, [], "human",
                               creation={"purpose": "requested_knowledge_page", "paths": ["Library/summary.md"]} if operation == "publish" else None)
    session = SimpleNamespace(id="settle-worker", user_id=jobs.user_id, agent_id=config["curator_agent_id"])
    jobs.update(job["id"], status="running", session_id=session.id)
    tasks.assign(session.id, job["id"], job["items"])
    token = tasks.open(session, "maintain")
    headers = {"Authorization": "Bearer " + mint(session.id, user_id=session.user_id, wiki_task_id=token)}
    try:
        read = (await client.get("/api/wiki/read", params={"doc": path}, headers=headers)).json()
        if operation == "no_change":
            response = await client.post("/api/wiki/revise", headers=headers, json={"doc": read["id"], "expected_revision": read["revision"], "operation": operation, "reason": reason})
        elif operation == "publish":
            response = await client.post("/api/wiki/publish", headers=headers, json={"path": "Library/summary.md", "expected_revision": None, "content": "# Summary\n\nA supported interpretation.\n", "kind": "concept", "sources": [{"doc": read["id"], "revision": read["revision"], "location": "L1-L2"}], "reason": "Publish a separately authorized interpretation", "item_doc": read["id"]})
        else:
            response = await client.post("/api/wiki/organize", headers=headers, json={"path": path, "expected_revision": read["revision"], "key_note": "A conclusion with its conditions.", "reason": "Add only the missing shared summary"})
        assert response.status_code == 200, response.text
        value = response.json()
    finally:
        tasks.close(token)
        tasks.unassign(session.id)
    item_path = "Library/summary.md" if operation == "publish" else path
    item = next(i for i in job["items"] if i["path"] == item_path)
    if not settled:
        return value, jobs.get(job["id"])
    recover = jobs.recover_creation if operation == "publish" else jobs.recover_publication
    assert recover(job["id"], item)
    assert item["receipt_id"] == value["receipt_id"] and item["status"] == "done"
    jobs.update(job["id"], status=final_status, items=job["items"], session_id=None)
    return value, jobs.get(job["id"])


def submission(item):
    return {"idempotency_key": item["review"]["review_key"], "paths": [item["current"]["path"]], "review": item["review"]}


async def test_save_is_not_review_and_a_pure_move_needs_no_new_worker(client, tmp_path):
    vault, config, jobs = await prepare(client, tmp_path, {"Library/journal.md": "# Journal\n\nKeep the current words.\n", "Library/projects/.keep": ""})
    accepted, _ = await settle(client, jobs, config)
    first = (await state(client, "Library/journal.md"))["items"][0]
    assert first["status"] == "unchanged" and not first["actionable"]
    moved = vault / "Library/projects/renamed.MARKDOWN"
    (vault / "Library/journal.md").rename(moved)
    read = await current_read(client, "Library/projects/renamed.MARKDOWN")
    assert read.status_code == 200, read.text
    current = (await state(client, read.json()["id"]))["items"][0]
    assert current["current"]["revision"] == accepted["revision"]
    assert current["status"] == "unchanged" and current["path_changed"] and not current["actionable"]
    assert current["baseline"]["path"] == "Library/journal.md"
    saved = await client.put("/api/wiki/document", headers=client.headers, json={"path": current["current"]["path"], "expected_revision": accepted["revision"], "content": "# Journal\n\nCurrent human changes.\n", "reason": "human save"})
    assert saved.status_code == 200, saved.text
    changed = (await state(client, read.json()["id"]))["items"][0]
    assert changed["status"] == "changed" and changed["baseline"]["id"] == first["baseline"]["id"]


@pytest.mark.parametrize("command", ["/wiki维护", "/wiki 维护", "/wiki maintain"])
async def test_bare_command_requires_a_checked_current_library_selection(client, tmp_path, command):
    _, _, jobs = await prepare(client, tmp_path, {"Library/log.md": "# Log\nOne current record.\n", "outside.md": "# Outside\n", "Library/source.txt": "original"})
    async with entry(client, request=command) as (headers, task):
        observed = (await state(client, headers=headers))["items"]
        assert [i["current"]["path"] for i in observed] == ["Library/log.md"]
        assert not task.document_ids and not task.reservation_ids and not task.reads
        raw = await client.post("/api/wiki/maintain", headers=headers, json={"idempotency_key": "not-a-selection", "paths": ["Library/log.md"]})
        assert raw.status_code == 403
        queued = await client.post("/api/wiki/maintain", headers=headers, json=submission(observed[0]))
        assert queued.status_code == 200, queued.text
        replay = await client.post("/api/wiki/maintain", headers=headers, json=submission(observed[0]))
        assert replay.status_code == 200 and replay.json()["id"] == queued.json()["id"], replay.text
        outside = (await state(client, "outside.md", headers=headers))["items"][0]
        assert (await client.post("/api/wiki/maintain", headers=headers, json=submission(outside))).status_code == 403
        assert (await state(client, headers=headers))["items"][0]["attempt_count"] == 1
        assert not task.reads and not task.document_ids and not task.reservation_ids
        assert len(jobs.list()["items"]) == 1


async def test_explicit_short_name_is_unique_and_cannot_expand_to_another_delta(client, tmp_path):
    await prepare(client, tmp_path, {"Library/a/brief.md": "# Brief\n", "Library/another.md": "# Another\n"})
    async with entry(client, request="/wiki维护 brief.md") as (headers, _):
        allowed = (await state(client, "Library/a/brief.md", headers=headers))["items"][0]
        other = (await state(client, "Library/another.md", headers=headers))["items"][0]
        assert (await client.post("/api/wiki/maintain", headers=headers, json=submission(other))).status_code == 403
        assert (await client.post("/api/wiki/maintain", headers=headers, json=submission(allowed))).status_code == 200
    (registry.store((await api.session_manager.db.get_system_agent())["user_id"]).root / "Library/b").mkdir()
    (registry.store((await api.session_manager.db.get_system_agent())["user_id"]).root / "Library/b/brief.md").write_text("# Other brief\n")
    registry.store((await api.session_manager.db.get_system_agent())["user_id"]).refresh()
    async with entry(client, request="/wiki维护 brief.md") as (headers, _):
        observed = (await state(client, "Library/a/brief.md", headers=headers))["items"][0]
        assert (await client.post("/api/wiki/maintain", headers=headers, json=submission(observed))).status_code == 403


async def test_auto_checked_review_is_limited_to_current_library_changes(client, tmp_path):
    vault, _, jobs = await prepare(client, tmp_path, {"Library/new.md": "# New\n", "outside.md": "# Outside\n"})
    original = {path: (vault / path).read_bytes() for path in ("Library/new.md", "outside.md")}
    async with entry(client, mode="auto", request="/wiki 维护个人知识库") as (headers, _):
        outside = (await state(client, "outside.md", headers=headers))["items"][0]
        rejected = await client.post("/api/wiki/maintain", headers=headers, json=submission(outside))
        assert rejected.status_code == 403, rejected.text
        observed = (await state(client, "Library/new.md", headers=headers))["items"][0]
        queued = await client.post("/api/wiki/maintain", headers=headers, json=submission(observed))
        assert queued.status_code == 200, queued.text
        replay = await client.post("/api/wiki/maintain", headers=headers, json=submission(observed))
        assert replay.status_code == 200 and replay.json()["id"] == queued.json()["id"]
        denied_write = await client.post("/api/wiki/organize", headers=headers, json={
            "path": "Library/new.md", "expected_revision": observed["current"]["revision"],
            "key_note": "The submitter cannot write this.", "reason": "Not a worker",
        })
        assert denied_write.status_code == 403
    assert len(jobs.list()["items"]) == 1
    assert {path: (vault / path).read_bytes() for path in original} == original


@pytest.mark.parametrize("read_only", [False, True])
async def test_semantic_child_cannot_widen_its_parents_selection_or_read_control(client, tmp_path, read_only):
    from server.knowledge import AdmittedRequest, derive, for_account

    _, _, jobs = await prepare(client, tmp_path, {"Library/chosen.md": "# Chosen\n", "Library/other.md": "# Other\n"})
    parent = for_account(session_id="parent", request_id="original", user_id=jobs.user_id,
                         prompt="/wiki maintain Library/chosen.md", selected="read" if read_only else None)
    child = derive(AdmittedRequest(parent.origin, parent), "/wiki 维护个人知识库")
    async with entry(client, mode=child.execution.mode, request="/wiki 维护个人知识库",
                     knowledge=child.execution) as (headers, _):
        other = (await state(client, "Library/other.md", headers=headers))["items"][0]
        assert (await client.post("/api/wiki/maintain", headers=headers, json=submission(other))).status_code == 403
        chosen = (await state(client, "Library/chosen.md", headers=headers))["items"][0]
        response = await client.post("/api/wiki/maintain", headers=headers, json=submission(chosen))
        assert response.status_code == (403 if read_only else 200), response.text
    assert len(jobs.list()["items"]) == (0 if read_only else 1)


async def test_observation_drift_and_return_to_old_input_are_not_false_success(client, tmp_path):
    vault, config, jobs = await prepare(client, tmp_path)
    original = (vault / "Library/journal.md").read_bytes()
    async with entry(client) as (headers, _):
        first = (await state(client, "Library/journal.md", headers=headers))["items"][0]
        queued = await client.post("/api/wiki/maintain", headers=headers, json=submission(first))
        assert queued.status_code == 200, queued.text
        output, _ = await settle(client, jobs, config, job=queued.json(), operation="organize")
        assert output["revision"] != first["current"]["revision"]
        replay = await client.post("/api/wiki/maintain", headers=headers, json=submission(first))
        assert replay.status_code == 200 and replay.json()["id"] == queued.json()["id"], replay.text
        (vault / "Library/journal.md").write_bytes(original)
        await client.get("/api/wiki/read", params={"doc": "Library/journal.md"}, headers=headers)
        assert (await client.post("/api/wiki/maintain", headers=headers, json=submission(first))).status_code == 409
        fresh = (await state(client, "Library/journal.md", headers=headers))["items"][0]
        assert fresh["review"]["review_key"] != first["review"]["review_key"] and fresh["status"] == "changed"
        (vault / "Library/journal.md").write_text("# Journal\nAnother human change.\n")
        assert (await client.post("/api/wiki/maintain", headers=headers, json=submission(fresh))).status_code == 409


async def test_status_has_no_private_body_error_or_receipt_payload_and_does_not_write(client, tmp_path):
    secret = "PRIVATE_SENTINEL_NOT_NAVIGATION"
    vault, config, jobs = await prepare(client, tmp_path, {"Library/record.md": "---\ndescription: " + secret + "\nunknown: preserved\n---\n# Record\n" + secret + "\n"})
    _, job = await settle(client, jobs, config, path="Library/record.md")
    store = registry.store((await api.session_manager.db.get_system_agent())["user_id"])
    with store.connect() as db:
        db.execute("UPDATE operation_receipts SET reason=?,details=?", (secret, json.dumps({"secret": secret})))
        db.execute("UPDATE jobs SET error=? WHERE id=?", (secret, job["id"]))
        before = {table: db.execute("SELECT count(*) FROM " + table).fetchone()[0] for table in ("changes", "operation_receipts", "jobs")}
    original = (vault / "Library/record.md").read_bytes()
    async with entry(client, request="/wiki维护 record.md") as (headers, task):
        response = await state(client, "Library/record.md", headers=headers)
        assert secret not in json.dumps(response) and not task.reads
        assert not task.document_ids and not task.reservation_ids
        with store.connect() as db:
            db.execute("UPDATE index_state SET error=?", (secret,))
        unavailable = await state(client, headers=headers)
        assert unavailable["ready"] is False and unavailable["total"] is None
        assert secret not in json.dumps(unavailable)
    with store.connect() as db:
        assert before == {table: db.execute("SELECT count(*) FROM " + table).fetchone()[0] for table in before}
    assert (vault / "Library/record.md").read_bytes() == original


@pytest.mark.parametrize("command", ["/wiki维护", "/wiki 维护", "/wiki maintain"])
def test_dispatch_and_intent_use_the_same_command_parser(command):
    assert command_prompt(command, "maintain") == "执行日常 Wiki 增量维护。"
    assert command_prompt(command, "read") == "说明当前 Wiki 查询范围。"
    assert command_prompt(command + ' "Library/a b.md"', "maintain") == '"Library/a b.md"'
    assert command_prompt("Discuss " + command, "auto") == "Discuss " + command


async def test_three_page_snapshot_detects_receipt_only_and_settlement_changes(client, tmp_path):
    files = {f"Library/notes/item-{i}.md": f"# Item {i}\n\nCurrent words.\n" for i in range(5)}
    _, config, jobs = await prepare(client, tmp_path, files)
    first = await state(client, limit=2)
    second = await state(client, limit=2, cursor=first["next_cursor"])
    third = await state(client, limit=2, cursor=second["next_cursor"])
    paths = [i["current"]["path"] for page in (first, second, third) for i in page["items"]]
    assert paths == sorted(files) and third["next_cursor"] is None
    index = first["index_epoch"]
    await settle(client, jobs, config, path=paths[-1])
    assert (await state(client))["index_epoch"] == index
    stale = await client.get("/api/wiki/maintenance-status", params={"limit": 2, "cursor": first["next_cursor"]}, headers=client.headers)
    assert stale.status_code == 409
    _, pending = await settle(client, jobs, config, path=paths[-2], settled=False)
    before_settlement = await state(client, limit=1)
    item = pending["items"][0]
    assert jobs.recover_publication(pending["id"], item)
    jobs.update(pending["id"], status="completed", items=pending["items"], session_id=None)
    stale = await client.get("/api/wiki/maintenance-status", params={"limit": 1, "cursor": before_settlement["next_cursor"]}, headers=client.headers)
    assert stale.status_code == 409


@pytest.mark.parametrize("failure", ["recording", "prepared", "index"])
async def test_unsettled_publications_block_regeneration_without_recovery_side_effects(client, tmp_path, failure):
    _, config, jobs = await prepare(client, tmp_path)
    _, job = await settle(client, jobs, config, operation="organize")
    store = registry.store((await api.session_manager.db.get_system_agent())["user_id"])
    if failure == "recording":
        job["items"][0].update(status="unknown", worker_request={
            "session_id": "not-committed", "request_id": "original", "phase": "unknown",
        })
        jobs.update(job["id"], status="cancelled", items=job["items"])
    else:
        with store.connect() as db:
            if failure == "prepared":
                db.execute("UPDATE changes SET status='prepared' WHERE id=?", (job["items"][0]["output_change_id"],))
            else:
                db.execute("UPDATE job_publications SET indexed=0 WHERE change_id=?", (job["items"][0]["output_change_id"],))
    with store.connect() as db:
        before = [tuple(r) for r in db.execute("SELECT id,status FROM changes")]
    result = (await state(client, "Library/journal.md"))["items"][0]
    assert not result["actionable"] and result["attempt_count"]
    with store.connect() as db:
        assert before == [tuple(r) for r in db.execute("SELECT id,status FROM changes")]


async def test_published_output_does_not_review_the_input_and_sources_can_change_independently(client, tmp_path):
    vault, config, jobs = await prepare(client, tmp_path)
    _, job = await settle(client, jobs, config, operation="publish")
    source = (await state(client, "Library/journal.md"))["items"][0]
    child = (await state(client, "Library/summary.md"))["items"][0]
    assert source["status"] == "unreviewed" and source["baseline"] is None
    assert child["status"] == "unchanged" and child["baseline"]["id"] == job["items"][0]["receipt_id"]
    (vault / "Library/journal.md").write_text("# Journal\n\nA later source version.\n")
    await current_read(client, "Library/journal.md")
    current = (await state(client, "Library/summary.md"))["items"][0]
    assert current["status"] == "unchanged" and current["source_warnings"] == {"changed": 1} and current["actionable"]


@pytest.mark.parametrize("final_status", ["completed_with_attention", "failed", "interrupted"])
async def test_completed_item_remains_valid_when_another_item_needs_attention(client, tmp_path, final_status):
    _, config, jobs = await prepare(client, tmp_path, {"Library/left.md": "# Left\n", "Library/right.md": "# Right\n"})
    job = jobs.maintain("two-items", "", "Review selected records", ["Library/left.md", "Library/right.md"], None, [], "human")
    job["items"][1]["status"] = "attention"
    _, completed = await settle(client, jobs, config, path="Library/left.md", job=job, final_status=final_status)
    left = (await state(client, "Library/left.md"))["items"][0]
    assert left["status"] == "unchanged" and left["baseline"]["id"] == completed["items"][0]["receipt_id"]


async def test_same_bytes_replacement_has_no_inherited_review_identity(client, tmp_path):
    vault, config, jobs = await prepare(client, tmp_path)
    _, _ = await settle(client, jobs, config)
    first = (await state(client, "Library/journal.md"))["items"][0]
    original = (vault / "Library/journal.md").read_bytes()
    (vault / "Library/journal.md").unlink()
    assert (await current_read(client, first["current"]["id"])).status_code == 404
    (vault / "Library/journal.md").write_bytes(original)
    fresh = await current_read(client, "Library/journal.md")
    assert fresh.status_code == 200
    replacement = (await state(client, "Library/journal.md"))["items"][0]
    assert replacement["current"]["id"] != first["current"]["id"] and replacement["status"] == "unreviewed"
    assert replacement["current"]["revision"] == first["current"]["revision"]
    old = await client.get("/api/wiki/maintenance-status", params={"doc": first["current"]["id"]}, headers=client.headers)
    assert old.status_code == 404


async def test_renamed_literal_inlinks_include_outside_library_without_leaking_attached_values(client, tmp_path):
    secret = "PRIVATE_LINK_ATTACHMENT"
    files = {"Library/original.md": "# Original\n\nCurrent source.\n[existing asset](unavailable.json)\n",
             "Library/ref.md": "# Reference\n[[Library/original.md?secret=" + secret + "#anchor|" + secret + "]]\n",
             "map.md": "# Navigation\n[[Library/original|Source]]\n",
             "Library/other/original.md": "# A different document\n",
             "Library/other/ref.md": "# Other reference\n[[original.md]]\n"}
    vault, config, jobs = await prepare(client, tmp_path, files)
    await settle(client, jobs, config, path="Library/original.md")
    (vault / "Library/original.md").rename(vault / "Library/renamed.md")
    assert (await current_read(client, "Library/renamed.md")).status_code == 200
    first = await state(client, "Library/renamed.md", view="links", limit=1)
    second = await state(client, "Library/renamed.md", view="links", limit=1, cursor=first["next_cursor"])
    assert first["total"] == 2 and {r["source_path"] for p in (first, second) for r in p["items"]} == {"Library/ref.md", "map.md"}
    assert secret not in json.dumps([first, second]) and "unavailable.json" not in json.dumps([first, second])
    assert all(r["matched_path"] == "Library/original.md" for p in (first, second) for r in p["items"])
    async with entry(client, request="/wiki维护 renamed.md") as (headers, _):
        other = (await state(client, "Library/ref.md", headers=headers))["items"][0]
        assert (await client.post("/api/wiki/maintain", headers=headers, json=submission(other))).status_code == 403


async def test_old_path_impact_uses_one_uri_decode_for_literal_percent_filename(client, tmp_path):
    files = {
        "Library/a%23b.md": "# Percent source\nOriginal conditions.\n",
        "Library/a#b.md": "# Hash source\nDifferent evidence.\n",
        "map.md": "# Map\n[percent](Library/a%2523b.md)\n[hash](Library/a%23b.md)\n",
    }
    vault, config, jobs = await prepare(client, tmp_path, files)
    await settle(client, jobs, config, path="Library/a%23b.md")
    (vault / "Library/a%23b.md").rename(vault / "Library/renamed.md")
    assert (await current_read(client, "Library/renamed.md")).status_code == 200
    result = await state(client, "Library/renamed.md", view="links")
    assert result["total"] == 1
    assert result["items"][0]["matched_path"] == "Library/a%23b.md"
    assert result["items"][0]["source_path"] == "map.md"


async def test_checked_no_change_replay_does_not_fail_on_its_own_new_baseline(client, tmp_path):
    vault, config, jobs = await prepare(client, tmp_path, {"Library/record.md": "---\nunknown_field: keep\n---\n# Record\n\nCurrent human additions; removed text stays removed.\n"})
    original = (vault / "Library/record.md").read_bytes()
    async with entry(client, request="/wiki维护 record.md") as (headers, _):
        observed = (await state(client, "Library/record.md", headers=headers))["items"][0]
        queued = await client.post("/api/wiki/maintain", headers=headers, json=submission(observed))
        assert queued.status_code == 200, queued.text
        await settle(client, jobs, config, path="Library/record.md", job=queued.json())
        current = (await state(client, "Library/record.md", headers=headers))["items"][0]
        assert current["status"] == "unchanged" and not current["actionable"]
        assert current["review"]["review_key"] != observed["review"]["review_key"]
        replay = await client.post("/api/wiki/maintain", headers=headers, json=submission(observed))
        assert replay.status_code == 200 and replay.json()["id"] == queued.json()["id"], replay.text
    assert (vault / "Library/record.md").read_bytes() == original


@pytest.mark.parametrize("command", ['/wiki维护 "Library/a b.md"', "/wiki维护 Library/a b.md"])
async def test_exact_path_with_spaces_selects_only_that_document(client, tmp_path, command):
    await prepare(client, tmp_path, {"Library/a b.md": "# A B\n", "Library/a.md": "# A\n"})
    async with entry(client, request=command) as (headers, _):
        chosen = (await state(client, "Library/a b.md", headers=headers))["items"][0]
        other = (await state(client, "Library/a.md", headers=headers))["items"][0]
        assert (await client.post("/api/wiki/maintain", headers=headers, json=submission(chosen))).status_code == 200
        assert (await client.post("/api/wiki/maintain", headers=headers, json=submission(other))).status_code == 403


@pytest.mark.parametrize("drift", ["rename", "replacement"])
async def test_new_submission_rejects_identity_or_path_drift_after_observation(client, tmp_path, drift):
    vault, _, jobs = await prepare(client, tmp_path)
    async with entry(client) as (headers, _):
        observed = (await state(client, "Library/journal.md", headers=headers))["items"][0]
        path = vault / "Library/journal.md"
        if drift == "rename":
            path.rename(vault / "Library/renamed.md")
            assert (await current_read(client, "Library/renamed.md")).status_code == 200
        else:
            original = path.read_bytes()
            path.unlink()
            assert (await current_read(client, observed["current"]["id"])).status_code == 404
            path.write_bytes(original)
            assert (await current_read(client, "Library/journal.md")).status_code == 200
        denied = await client.post("/api/wiki/maintain", headers=headers, json=submission(observed))
        assert denied.status_code == 409, denied.text
        assert jobs.list()["items"] == []


async def test_cursor_binds_actor_registration_and_filters(client, tmp_path):
    from server.wiki.maintenance_state import status
    from server.wiki.store import WikiError

    await prepare(client, tmp_path, {"Library/a.md": "# A\n", "Library/b.md": "# B\n"})
    async with entry(client, agent="first") as (first, _), entry(client, agent="second") as (second, _):
        cursor = (await state(client, headers=first, limit=1))["next_cursor"]
        for params, headers, expected in [({"limit": 1, "cursor": cursor}, second, 409),
                                          ({"limit": 1, "cursor": cursor, "directory": "Library/a"}, first, 422),
                                          ({"limit": 1, "cursor": cursor, "doc": "Library/a.md", "view": "links"}, first, 422)]:
            denied = await client.get("/api/wiki/maintenance-status", headers=headers, params=params)
            assert denied.status_code == expected, denied.text
    store = registry.store((await api.session_manager.db.get_system_agent())["user_id"])
    registration = registry.config((await api.session_manager.db.get_system_agent())["user_id"])
    own = status(store, registration, "one-account", limit=1)
    with pytest.raises(WikiError) as changed_account:
        status(store, registration, "another-account", limit=1, cursor=own["next_cursor"])
    assert changed_account.value.status == 409
    with pytest.raises(WikiError) as changed_vault:
        status(store, registration | {"vault_id": "different"}, "one-account", limit=1, cursor=own["next_cursor"])
    assert changed_vault.value.status == 409


async def test_byte_bounded_paging_has_no_omissions(client, tmp_path):
    from server.wiki.read_response import COMPACT_READ_MAX_BYTES, model_result_bytes

    prefix = "/".join(["folder-" + "x" * 60] * 4)
    files = {f"Library/{prefix}/note-{i:02d}.md": f"# Note {i}\n" for i in range(45)}
    await prepare(client, tmp_path, files)
    paths, cursor, pages = [], None, 0
    while True:
        page = await state(client, limit=50, **({"cursor": cursor} if cursor else {}))
        pages += 1
        assert model_result_bytes(page) <= COMPACT_READ_MAX_BYTES and page["total"] == len(files)
        paths.extend(item["current"]["path"] for item in page["items"])
        cursor = page["next_cursor"]
        if not cursor:
            break
        assert pages < len(files)
    assert paths == sorted(files) and pages >= 3


async def test_normal_weber_mcp_can_observe_and_enqueue_but_not_write_or_reuse_expired_turn(client, tmp_path, monkeypatch):
    import httpx
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    from server.config import settings
    from server.deps import get_session_manager
    from server.main import _wiki_error
    from server.routers.wiki import router
    from server.wiki.store import WikiError
    from tests.callback_api import start_callback_api

    vault, config, jobs = await prepare(client, tmp_path, {"Library/journal.md": "# Journal\n\nKeep the current words.\n", "Library/other.md": "# Other\n"})
    session = await api.session_manager.create_session(config["curator_agent_id"], "Ordinary maintenance entry", str(tmp_path))
    token = tasks.open(session, "maintain", "/wiki维护 journal.md")
    headers = {"Authorization": "Bearer " + mint(session.id, user_id=session.user_id, wiki_task_id=token)}
    host = await start_callback_api(router, overrides={get_session_manager: lambda: api.session_manager}, exception_handlers={WikiError: _wiki_error}, readiness_token=mint(session.id, user_id=session.user_id))
    monkeypatch.setattr(settings, "port", host.port)
    original = (vault / "Library/journal.md").read_bytes()
    try:
        async with httpx.AsyncClient(headers=headers) as transport:
            async with streamable_http_client(f"http://127.0.0.1:{host.port}/mcp/wiki/mcp", http_client=transport) as (reader, writer, _):
                async with ClientSession(reader, writer) as peer:
                    await peer.initialize()
                    visible = {tool.name for tool in (await peer.list_tools()).tools}
                    assert {"maintenance_status", "maintenance_select", "maintenance_selection", "maintain", "read"} <= visible
                    assert not {"revise", "organize", "publish"} & visible
                    observed = json.loads((await peer.call_tool("maintenance_status", {"doc": "Library/journal.md"})).content[0].text)["items"][0]
                    selected = json.loads((await peer.call_tool("maintenance_select", {"limit": 1})).content[0].text)
                    frozen = json.loads((await peer.call_tool("maintenance_selection", {"selection_id": selected["id"], "limit": 1})).content[0].text)
                    assert frozen["items"] == selected["items"]
                    assert frozen["items"][0]["review"] == observed["review"]
                    assert not tasks.active[token].reads
                    assert not tasks.active[token].document_ids and not tasks.active[token].reservation_ids
                    first_page = json.loads((await peer.call_tool("maintenance_status", {"limit": 1})).content[0].text)
                    for cursor in malformed_cursors(first_page["next_cursor"]):
                        rejected = json.loads((await peer.call_tool("maintenance_status", {"limit": 1, "cursor": cursor})).content[0].text)
                        assert rejected["status"] == 422, rejected
                    await peer.call_tool("read", {"doc": observed["current"]["id"]})
                    queued = json.loads((await peer.call_tool("maintain", submission(observed) | {
                        "selection_id": selected["id"], "selection_ordinal": frozen["items"][0]["ordinal"]})).content[0].text)
                    assert queued["status"] == "queued", queued
                    evidence = json.loads((await peer.call_tool("jobs", {"job_id": queued["id"], "view": "full"})).content[0].text)
                    assert evidence["selection_evidence"][0]["review"] == observed["review"]
                    assert len(jobs.list()["items"]) == 1
                    direct = await transport.put(f"http://127.0.0.1:{host.port}/api/wiki/document", json={"path": observed["current"]["path"], "expected_revision": observed["current"]["revision"], "content": "# Unauthorized replacement\n", "reason": "entry is not worker"})
                    assert direct.status_code == 403
                    tasks.close(token)
                    assert (await peer.list_tools()).tools == []
                    expired = json.loads((await peer.call_tool("maintenance_status", {"doc": observed["current"]["id"]})).content[0].text)
                    assert expired["status"] == 403, expired
                    expired_selection = json.loads((await peer.call_tool("maintenance_selection", {"selection_id": selected["id"]})).content[0].text)
                    assert expired_selection["status"] == 403, expired_selection
    finally:
        tasks.close(token)
        await host.stop()
    assert (vault / "Library/journal.md").read_bytes() == original


async def test_read_only_turn_observation_cannot_authorize_maintenance(client, tmp_path):
    from server.wiki.tasks import explicit_mode

    await prepare(client, tmp_path)
    async with entry(client, mode=explicit_mode("/wiki维护", "read")) as (headers, _):
        observed = (await state(client, "Library/journal.md", headers=headers))["items"][0]
        assert observed["actionable"]
        assert (await client.post("/api/wiki/maintain", headers=headers, json=submission(observed))).status_code == 403


async def test_registration_rebinding_invalidates_old_observation(client, tmp_path):
    vault, _, _ = await prepare(client, tmp_path)
    async with entry(client, request="/wiki维护 journal.md") as (headers, _):
        observed = (await state(client, "Library/journal.md", headers=headers))["items"][0]
        registry.configure((await api.session_manager.db.get_system_agent())["user_id"], vault, "new-curator")
        denied = await client.post("/api/wiki/maintain", headers=headers, json=submission(observed))
        assert denied.status_code == 409, denied.text
        assert WikiJobs((await api.session_manager.db.get_system_agent())["user_id"]).list()["items"] == []


async def test_status_and_checked_submission_are_isolated_between_real_accounts(two_accounts):
    from server.workspace import paths_for

    context = two_accounts
    client = context["client"]
    observations = {}
    for name in ("archer", "vera"):
        account = context[name]
        headers = {"Authorization": "Bearer " + account["token"]}
        root = paths_for(account["user"]["id"]).workspace / "notes"
        (root / "Library").mkdir(parents=True)
        for index in range(2):
            (root / f"Library/record-{index}.md").write_text(f"# {name} record {index}\n")
        configured = await client.put("/api/wiki/config", headers=headers, json={"root": str(root), "curator_agent_id": account["agent"]["id"]})
        assert configured.status_code == 200, configured.text
        observations[name] = await state(client, headers=headers, limit=1)
    first = observations["archer"]
    second = observations["vera"]
    assert first["items"][0]["current"]["id"] != second["items"][0]["current"]["id"]
    headers = {"Authorization": "Bearer " + context["vera"]["token"]}
    foreign_doc = first["items"][0]["current"]["id"]
    assert (await client.get("/api/wiki/maintenance-status", headers=headers, params={"doc": foreign_doc})).status_code == 404
    assert (await client.get("/api/wiki/maintenance-status", headers=headers, params={"limit": 1, "cursor": first["next_cursor"]})).status_code == 409
    denied = await client.post("/api/wiki/maintain", headers=headers, json=submission(first["items"][0]))
    assert denied.status_code == 409, denied.text
    assert WikiJobs(context["vera"]["user"]["id"]).list()["items"] == []


async def test_checked_admission_does_not_hold_registration_lock_while_waiting_for_worker(client, tmp_path, monkeypatch):
    import threading

    from server.wiki.history import WikiHistory

    _, _, jobs = await prepare(client, tmp_path)
    observed = (await state(client, "Library/journal.md"))["items"][0]
    worker_ready, admission_waiting = threading.Event(), threading.Event()
    acquired = []
    original = WikiHistory._locked

    @contextmanager
    def observe_lock(history, *, shared=False):
        if threading.current_thread().name != "holding-worker":
            admission_waiting.set()
        with original(history, shared=shared):
            yield

    monkeypatch.setattr(WikiHistory, "_locked", observe_lock)

    def worker():
        with WikiHistory(jobs.store).locked():
            worker_ready.set()
            if not admission_waiting.wait(3):
                return
            guard = registry._account_lock(jobs.user_id)
            obtained = guard.acquire(blocking=False)
            acquired.append(obtained)
            if obtained:
                guard.release()

    thread = threading.Thread(target=worker, name="holding-worker")
    thread.start()
    try:
        assert await asyncio.to_thread(worker_ready.wait, 3)
        queued = jobs.maintain(observed["review"]["review_key"], "", "ignored canonical scope", [observed["current"]["path"]], None, [], "human", review=observed["review"])
        assert queued["status"] == "queued"
    finally:
        thread.join(4)
    assert acquired == [True] and not thread.is_alive()


async def test_empty_explicit_request_is_a_selection_error_not_a_server_error(client, tmp_path):
    await prepare(client, tmp_path)
    async with entry(client, request="") as (headers, _):
        observed = (await state(client, "Library/journal.md", headers=headers))["items"][0]
        denied = await client.post("/api/wiki/maintain", headers=headers, json=submission(observed))
        assert denied.status_code == 403, denied.text


@pytest.mark.parametrize(("command", "title"), [("/wiki maintain", "maintain"), ("/wiki 维护", "维护"), ("/wiki维护", "维护"), ("", "maintain")])
@pytest.mark.parametrize("checked", [True, False])
async def test_command_tokens_do_not_select_a_third_note(client, tmp_path, command, title, checked):
    files = {"Library/a.md": "# A\n", "Library/b.md": "# B\n", "Library/third.md": f"# {title}\n"}
    vault, _, jobs = await prepare(client, tmp_path, files)
    store = registry.store((await api.session_manager.db.get_system_agent())["user_id"])
    async with entry(client, request=command + ' "Library/a.md" "Library/b.md"') as (headers, _):
        third = (await state(client, "Library/third.md", headers=headers))["items"][0]
        request = submission(third) if checked else {"idempotency_key": "legacy-third", "paths": ["Library/third.md"]}
        rejected = await client.post("/api/wiki/maintain", headers=headers, json=request)
        assert rejected.status_code == 403, rejected.text
        assert jobs.list()["items"] == []
        with store.connect() as db:
            assert db.execute("SELECT count(*) FROM operation_receipts").fetchone()[0] == 0
        for path in ("Library/a.md", "Library/b.md"):
            chosen = (await state(client, path, headers=headers))["items"][0]
            request = submission(chosen) if checked else {"idempotency_key": "legacy-" + path, "paths": [path]}
            assert (await client.post("/api/wiki/maintain", headers=headers, json=request)).status_code == 200
        assert {item["path"] for job in jobs.list()["items"] for item in jobs.get(job["id"])["items"]} == {"Library/a.md", "Library/b.md"}
    assert all((vault / path).read_text() == text for path, text in files.items())
    # The same title is a valid selection when it is actually the argument.
    async with entry(client, request="/wiki维护 " + title) as (headers, _):
        chosen = (await state(client, "Library/third.md", headers=headers))["items"][0]
        assert (await client.post("/api/wiki/maintain", headers=headers, json=submission(chosen))).status_code == 200


@pytest.mark.parametrize("drift", ["edit", "replacement"])
async def test_final_item_read_cannot_rebind_a_checked_observation(client, tmp_path, monkeypatch, drift):
    from server.wiki import maintenance_admission, maintenance_projection

    vault, _, jobs = await prepare(client, tmp_path)
    store = registry.store((await api.session_manager.db.get_system_agent())["user_id"])
    original_prepare = maintenance_admission.prepare
    changed = False
    async with entry(client) as (headers, _):
        observed = (await state(client, "Library/journal.md", headers=headers))["items"][0]

        def edit_after_final_preparation(*args, **kwargs):
            nonlocal changed
            original_prepare(*args, **kwargs)
            changed = True
            path = vault / observed["current"]["path"]
            if drift == "edit":
                path.write_text("# Journal\n\nA human edit after validation.\n")
            else:
                raw = path.read_bytes()
                path.unlink()
                store.refresh()  # Observe deletion before recreating identical bytes.
                path.write_bytes(raw)
            store.refresh()
            assert maintenance_projection.settle(store)

        monkeypatch.setattr(maintenance_admission, "prepare", edit_after_final_preparation)
        rejected = await client.post("/api/wiki/maintain", headers=headers, json=submission(observed))
        assert changed and rejected.status_code == 409, rejected.text
        assert jobs.list()["items"] == []


@pytest.mark.parametrize("job_status", ["interrupted", "failed"])
@pytest.mark.parametrize("item_status", ["pending", "unknown"])
@pytest.mark.parametrize("newer_input", [False, True])
async def test_unreceipted_unfinished_work_blocks_another_actor(client, tmp_path, job_status, item_status, newer_input):
    from server.wiki.store import WikiError

    vault, config, jobs = await prepare(client, tmp_path)
    retained = jobs.maintain("retained-unfinished", "", "Review selected note", ["Library/journal.md"], None, [], "earlier-actor")
    retained["items"][0]["status"] = item_status
    jobs.update(retained["id"], status=job_status, items=retained["items"])
    if newer_input:
        (vault / "Library/journal.md").write_text("# Journal\n\nLater human words.\n")
        assert (await current_read(client, "Library/journal.md")).status_code == 200
    async with entry(client, agent="new-actor") as (headers, _):
        current = (await state(client, "Library/journal.md", headers=headers))["items"][0]
        assert not current["actionable"] and current["attempt_count"] == 1
        assert current["attempts"][0]["job_id"] == retained["id"]
        assert current["attempts"][0]["status"] == job_status
        rejected = await client.post("/api/wiki/maintain", headers=headers, json=submission(current))
        assert rejected.status_code == 409, rejected.text
        assert jobs.get(retained["id"])["status"] == job_status
        assert len(jobs.list()["items"]) == 1
    if job_status == "interrupted":
        jobs.recover_jobs()
    if job_status == "interrupted" or item_status == "unknown":
        unknown = jobs.get(retained["id"])
        assert unknown["status"] == job_status and unknown["items"][0]["status"] == "unknown"
        original = (vault / "Library/journal.md").read_bytes()
        with pytest.raises(WikiError, match="original unknown execution") as denied:
            jobs.retry(retained["id"])
        assert denied.value.status == 409
        assert jobs.get(retained["id"])["items"] == unknown["items"]
        assert jobs.get(retained["id"])["attempt"] == unknown["attempt"]
        assert len(jobs.list()["items"]) == 1
        assert (vault / "Library/journal.md").read_bytes() == original
        return
    # An explicitly failed input can retry; a missing original execution
    # after interruption cannot be treated as proof of non-admission.
    jobs.retry(retained["id"])
    pending = (await state(client, "Library/journal.md"))["items"][0]
    assert not pending["actionable"] and pending["attempts"][0]["status"] == "queued"
    await settle(client, jobs, config, job=jobs.get(retained["id"]))
    completed = (await state(client, "Library/journal.md"))["items"][0]
    assert completed["status"] == "unchanged" and completed["attempt_count"] == 0


async def test_later_qualified_baseline_resolves_an_older_failed_attempt(client, tmp_path):
    vault, config, jobs = await prepare(client, tmp_path)
    old = jobs.maintain("older-failed", "", "Review current note", ["Library/journal.md"], None, [], "earlier-actor")
    jobs.update(old["id"], status="failed")
    assert not (await state(client, "Library/journal.md"))["items"][0]["actionable"]
    _, accepted = await settle(client, jobs, config)
    current = (await state(client, "Library/journal.md"))["items"][0]
    assert current["status"] == "unchanged" and not current["attempt_count"]
    assert current["baseline"]["id"] == accepted["items"][0]["receipt_id"]
    (vault / "Library/journal.md").write_text("# Journal\n\nA newer human record.\n")
    assert (await current_read(client, "Library/journal.md")).status_code == 200
    changed = (await state(client, "Library/journal.md"))["items"][0]
    assert changed["actionable"] and changed["attempt_count"] == 0


def malformed_cursors(cursor):
    decoded = json.loads(base64.urlsafe_b64decode(cursor))
    variants = [{k: v for k, v in decoded.items() if k != "generation"}]
    variants.extend(decoded | {"generation": value} for value in (None, 42, [], "not-a-generation"))
    variants.extend([{k: v for k, v in decoded.items() if k != "offset"}, decoded | {"offset": True}])
    return [base64.urlsafe_b64encode(json.dumps(value).encode()).decode() for value in variants]


async def test_malformed_cursor_is_422_at_normal_http_boundary(client, tmp_path):
    await prepare(client, tmp_path, {"Library/a.md": "# A\n", "Library/b.md": "# B\n"})
    first = await state(client, limit=1)
    for cursor in malformed_cursors(first["next_cursor"]):
        rejected = await client.get("/api/wiki/maintenance-status", headers=client.headers,
                                    params={"limit": 1, "cursor": cursor})
        assert rejected.status_code == 422, (cursor, rejected.text)
    # Rejections must leave the original observation and continuation usable.
    continued = await state(client, limit=1, cursor=first["next_cursor"])
    assert continued["items"][0]["current"]["id"] != first["items"][0]["current"]["id"]
