"""The model submission receipt stays small without changing durable job facts."""

import copy
import json

import pytest

from server.mcp_servers import wiki
from tests import wiki_api as wiki_api

client = wiki_api.client


def job_result(status="queued", *, items=None, error=None):
    return {
        "id": "job-id",
        "request_key": "original-key",
        "status": status,
        "session_id": "worker-session" if status == "running" else None,
        "created_at": "2026-10-17T00:00:00+00:00",
        "updated_at": "2026-10-17T00:00:01+00:00",
        "payload": {"material": "private source", "scope": "full scope"},
        "items": items if items is not None else [{"version": 1, "status": "pending"}],
        "receipts": [{"details": {"content": "full durable evidence"}}],
        "error": error,
    }


@pytest.mark.parametrize("size", [1, 200000])
def test_submission_receipt_does_not_echo_source_or_scope(monkeypatch, size):
    material = "知识材料🙂" * size
    scope = "整理目的" * size
    result = job_result()
    result["payload"] = {"material": material, "scope": scope}
    original = copy.deepcopy(result)
    calls = []

    def call(method, path, **kwargs):
        calls.append((method, path, kwargs))
        return result

    monkeypatch.setattr(wiki, "call", call)
    receipt = wiki.maintain("request", material, scope, target="Library/new.md")
    assert receipt == {
        "id": "job-id", "request_key": "original-key", "status": "queued",
        "session_id": None, "created_at": result["created_at"],
        "updated_at": result["updated_at"],
        "progress": {"total": 1, "completed": 0, "attention": 0, "failed": 0},
    }
    assert len(json.dumps(receipt, ensure_ascii=False).encode()) < 1000
    assert calls == [("POST", "maintain", {"body": {
        "idempotency_key": "request", "material": material, "scope": scope,
        "sources": [], "paths": [], "target": "Library/new.md",
    }})]
    assert result == original


@pytest.mark.parametrize("status,item_status", [
    ("queued", "pending"), ("running", "pending"),
    ("completed", "done"), ("completed_with_attention", "attention"),
    ("failed", "failed"), ("cancelled", "pending"),
])
def test_submission_preserves_actual_status_and_bounded_error(monkeypatch, status, item_status):
    result = job_result(status, items=[{"version": 1, "status": item_status}], error="故障🙂" * 1000)
    result["request_key"] = "长键🙂" * 1000
    monkeypatch.setattr(wiki, "call", lambda *args, **kwargs: result)
    receipt = wiki.maintain("submitted-alias")
    assert receipt["status"] == status
    assert receipt["session_id"] == result["session_id"]
    assert receipt["progress"][{"done": "completed", "attention": "attention", "failed": "failed"}.get(item_status, "total")] == 1
    assert receipt["error"] == result["error"][:1024]
    assert receipt["error_truncated"] is True
    assert receipt["request_key"] == result["request_key"][:1024]
    assert receipt["request_key_truncated"] is True
    assert "reused_items" not in receipt
    assert len(json.dumps(receipt, ensure_ascii=False).encode()) < 16000


@pytest.mark.parametrize("status", [403, 409, 422, 503])
def test_submission_errors_pass_through_unchanged(monkeypatch, status):
    error = {"status": status, "error": {"message": "拒绝", "details": "上下文" * 10000}}
    monkeypatch.setattr(wiki, "call", lambda *args, **kwargs: error)
    assert wiki.maintain("request", "material") is error


def test_http_conflict_keeps_existing_job_in_model_receipt(monkeypatch):
    import httpx

    detail = {"detail": "Different intent", "code": "maintenance_intent_conflict",
              "existing_job": {"id": "original", "status": "running"}}
    monkeypatch.setattr(wiki.httpx, "request", lambda *a, **kw: httpx.Response(409, json=detail))
    result = wiki.maintain("second", "material", "new scope")
    assert result == {"error": "Different intent", "status": 409,
                      "code": detail["code"], "existing_job": detail["existing_job"]}


def test_only_explicit_reused_items_have_bounded_locations(monkeypatch):
    items = [{
        "version": 1, "status": "done", "reused": True, "doc": str(index),
        "output": "Library/" + "路径🙂" * 10000,
        "output_revision": "a" * 64,
        "material": "never echo", "scope": "never echo",
    } for index in range(20)]
    result = job_result("completed", items=items)
    monkeypatch.setattr(wiki, "call", lambda *args, **kwargs: result)
    receipt = wiki.maintain("request")
    assert receipt["reused_count"] == 20
    assert len(receipt["reused_items"]) == 3
    assert receipt["reused_items_truncated"] is True
    assert receipt["reused_items"][0] == {
        "doc": "0", "output": items[0]["output"][:512], "output_revision": "a" * 64,
    }
    assert len(json.dumps(receipt, ensure_ascii=False).encode()) < 16000
    items[:] = [{"version": 1, "status": "done", "reused": "inferred", "output": "Library/not-reused.md"}]
    assert "reused_count" not in wiki.maintain("request")


async def test_real_idempotent_and_pending_alias_receipts_keep_job_facts(client, tmp_path, monkeypatch):
    from server.wiki.jobs import WikiJobs
    from server.wiki.store import WikiError

    vault = tmp_path / "vault"
    (vault / "Library").mkdir(parents=True)
    (vault / "Library/note.md").write_text("# Source\nPreserve this material.\n")
    await wiki_api.configure(client, vault)
    owner = (await wiki_api.session_manager.db.get_system_agent())["user_id"]
    jobs = WikiJobs(owner)

    def call(method, path, *, body):
        assert (method, path) == ("POST", "maintain")
        try:
            return jobs.maintain(
                body["idempotency_key"], body["material"], body["scope"],
                body["paths"], body["target"], body["sources"], "human",
            )
        except WikiError as exc:
            return {"error": str(exc), "status": exc.status}

    monkeypatch.setattr(wiki, "call", call)
    first = wiki.maintain("original", "new material", "purpose", target="Library/new.md")
    assert first["status"] == "queued"
    jobs.update(first["id"], status="running", session_id="current-worker")
    replay = wiki.maintain("original", "new material", "purpose", target="Library/new.md")
    alias = wiki.maintain("alias", "new material", "purpose", target="Library/new.md")
    assert replay == alias
    assert alias["id"] == first["id"] and alias["request_key"] == "original"
    assert alias["status"] == "running" and alias["session_id"] == "current-worker"
    assert "reused_items" not in alias
    assert wiki.maintain("original", "changed material", "purpose", target="Library/new.md")["status"] == 409
    full = jobs.get(first["id"])
    assert full["payload"]["request"]["material"] == "new material"
    assert full["payload"]["request"]["scope"] == "purpose"
    assert len(jobs.list(view="full")["items"]) == 1
    with jobs.store.connect() as db:
        assert db.execute("SELECT job_id FROM job_requests WHERE request_key='alias'").fetchone()[0] == first["id"]
    jobs.update(first["id"], status="completed", items=[{"status": "done"}])
    completed = wiki.maintain("original", "new material", "purpose", target="Library/new.md")
    assert completed["status"] == "completed" and completed["progress"]["completed"] == 1
    assert "reused_items" not in completed

    original = (vault / "Library/note.md").read_text()
    reused = wiki.maintain("existing-material", original, "purpose", target="Library/redundant.md")
    assert reused["status"] == "completed"
    assert reused["reused_count"] == 1
    assert reused["reused_items"][0]["output"] == "Library/note.md"
    assert not (vault / "Library/redundant.md").exists()
