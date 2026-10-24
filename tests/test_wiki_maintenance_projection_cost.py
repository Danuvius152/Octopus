"""Small correctness boundaries and opt-in measurements at production scale."""

import json
from contextlib import contextmanager

import pytest

from server.wiki import maintenance_projection
from tests.test_wiki_maintenance_inputs import prepared as prepared
from tests.test_wiki_maintenance_projection import observe


def assert_shared_job_items_loaded_once(prepared, monkeypatch, count):
    jobs, _ = prepared
    for number in range(count - 3):
        jobs.store.path(f"Library/wide-{number:04}.md").write_text(f"# Wide {number}\n")
    jobs.store.refresh()
    with jobs.store.connect() as db:
        docs = [dict(row) for row in db.execute("SELECT id,path,revision FROM documents ORDER BY path")]
    assert len(docs) == count > maintenance_projection.BATCH_SIZE
    items = [{"doc": doc["id"], "path": doc["path"], "revision": doc["revision"], "status": "pending"}
             for doc in docs]
    job = jobs.enqueue("shared-wide-job", {"kind": "curation"}, items)
    loaded = []
    batch_type = maintenance_projection.Batch

    class CountedBatch(batch_type):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            loaded.append({"documents": len(self.identifiers), "items": len(self.items)})

    monkeypatch.setattr(maintenance_projection, "Batch", CountedBatch)
    measurements = []
    for phase in ("initial_build", "whole_job_invalidation"):
        loaded.clear()
        if phase != "initial_build":
            jobs.update(job["id"], status="running")
        response = observe(jobs, limit=20)
        assert response["ready"] and response["total"] == count
        assert all(item["attempt_count"] == 1 and not item["actionable"] for item in response["items"])
        measurements.append({"phase": phase, "documents": count, "batches": list(loaded),
                             "loaded_items": sum(batch["items"] for batch in loaded),
                             "calculated_documents": sum(batch["documents"] for batch in loaded)})
    assert all(row["calculated_documents"] == count for row in measurements)
    assert all(row["loaded_items"] == count for row in measurements), measurements
    return measurements


@pytest.mark.parametrize("count", [5, 9])
def test_shared_job_items_are_loaded_once_across_projection_batches(prepared, monkeypatch, count):
    # Exercise two and three batches without indexing hundreds of fixture files.
    monkeypatch.setattr(maintenance_projection, "BATCH_SIZE", 4)
    assert_shared_job_items_loaded_once(prepared, monkeypatch, count)


@pytest.mark.wiki_perf
@pytest.mark.parametrize("count", [513, 1025])
def test_shared_job_projection_cost_at_production_batch_size(prepared, monkeypatch, count):
    measurements = assert_shared_job_items_loaded_once(prepared, monkeypatch, count)
    print(json.dumps({"shared_job_projection_cost": measurements}))


def collection_barrier_measurements(prepared, monkeypatch, counts):
    jobs, _ = prepared
    jobs.store.path("Other").mkdir()
    jobs.store.path("Other/unrelated.md").write_text("# Unrelated\n")
    jobs.store.refresh()
    outside = jobs.store.read("Other/unrelated.md")
    first = observe(jobs)
    assert first["ready"] and first["total"] == 3
    connect = jobs.store.connect
    current = {"steps": 0, "barriers": []}

    def progress():
        current["steps"] += 100
        return 0

    def statement(sql):
        if "maintenance_job_dirty" in sql and "maintenance_job_members" in sql and "documents" in sql:
            current["barriers"].append(sql)

    @contextmanager
    def counted_connect():
        with connect() as db:
            db.set_progress_handler(progress, 100)
            db.set_trace_callback(statement)
            try:
                yield db
            finally:
                db.set_progress_handler(None, 0)
                db.set_trace_callback(None)

    monkeypatch.setattr(jobs.store, "connect", counted_connect)
    measurements = []
    inserted = 0
    for count in counts:
        with connect() as db:
            item = {"version": 1, "doc": outside["id"], "path": outside["path"],
                    "revision": outside["revision"], "status": "pending"}
            db.executemany("INSERT INTO jobs(id,request_key,payload_hash,payload,status,items,error,session_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)", [
                (f"outside-{number}", f"outside-key-{number}", "synthetic-fixture", "{}", "queued",
                 json.dumps([item]), None, None, "now", "now")
                for number in range(inserted, count)])
        inserted = count
        current["steps"] = 0
        current["barriers"].clear()
        response = observe(jobs)
        assert response["ready"] and response["total"] == 3
        assert [item["review"] for item in response["items"]] == [item["review"] for item in first["items"]]
        measurements.append({"unrelated_dirty_jobs": count, "vm_steps": current["steps"]})
    with connect() as db:
        assert db.execute("SELECT count(*) FROM maintenance_job_dirty").fetchone()[0] == counts[-1]
        plans = [row["detail"] for row in db.execute("EXPLAIN QUERY PLAN " + current["barriers"][-1])]
    assert any("SEARCH d " in detail and "path>?" in detail for detail in plans), plans
    assert not any("SCAN pending" in detail for detail in plans), plans
    return measurements, plans


def test_collection_barrier_keeps_unrelated_dirty_jobs_out_of_the_page(prepared, monkeypatch):
    collection_barrier_measurements(prepared, monkeypatch, (0, 1, 2))


@pytest.mark.wiki_perf
def test_collection_barrier_vm_work_ignores_unrelated_dirty_jobs(prepared, monkeypatch):
    measurements, plans = collection_barrier_measurements(prepared, monkeypatch, (0, 1000, 5000))
    print(json.dumps({"collection_barrier_cost": measurements, "query_plan": plans}))
    assert measurements[-1]["vm_steps"] <= measurements[1]["vm_steps"] + 1000, measurements
