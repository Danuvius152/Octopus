"""Progress preserves job evidence without rewriting unchanged membership."""

import json
from contextlib import contextmanager
from time import perf_counter

import pytest

from tests.test_wiki_maintenance_inputs import prepared as prepared
from tests.test_wiki_maintenance_projection import observe


def progress_measurements(prepared, monkeypatch, count):
    jobs, _ = prepared
    for number in range(count - 2):
        jobs.store.path(f"Library/member-{number:04}.md").write_text(f"# Member {number}\n")
    jobs.store.refresh()
    docs = jobs.store.documents(limit=100)["items"]
    offset = 100
    while len(docs) < count + 1:
        docs.extend(jobs.store.documents(offset=offset, limit=100)["items"])
        offset += 100
    assert len(docs) == count + 1
    items = [{"doc": doc["id"], "path": doc["path"], "revision": doc["revision"], "status": "pending"}
             for doc in docs[:count]]
    job = jobs.enqueue("progress-cost", {"kind": "curation"}, items)
    job = jobs.update(job["id"], status="running")
    assert observe(jobs, limit=50)["ready"]
    with jobs.store.connect() as db:
        db.execute("CREATE TABLE member_write_audit(inserted INTEGER,deleted INTEGER)")
        db.execute("INSERT INTO member_write_audit VALUES(0,0)")
        for action, column in (("INSERT", "inserted"), ("DELETE", "deleted")):
            db.execute(f"CREATE TRIGGER audit_member_{column} AFTER {action} ON maintenance_job_members BEGIN UPDATE member_write_audit SET {column}={column}+1; END")
    connect, steps = jobs.store.connect, [0]

    def progress():
        steps[0] += 100
        return 0

    @contextmanager
    def counted_connect():
        with connect() as db:
            db.set_progress_handler(progress, 100)
            try:
                yield db
            finally:
                db.set_progress_handler(None, 0)

    monkeypatch.setattr(jobs.store, "connect", counted_connect)
    measured = []
    for phase in ("progress", "membership"):
        steps[0] = 0
        before = job["state_version"]
        started = perf_counter()
        if phase == "progress":
            for index in range(3):
                job["items"][index]["status"] = "done"
                job = jobs.update(job["id"], items=job["items"])
        else:
            job["items"][-1] = {"doc": docs[-1]["id"], "path": docs[-1]["path"],
                                "revision": docs[-1]["revision"], "status": "pending"}
            job = jobs.update(job["id"], items=job["items"])
        elapsed = perf_counter() - started
        vm_steps = steps[0]
        with jobs.store.connect() as db:
            writes = dict(db.execute("SELECT * FROM member_write_audit").fetchone())
            events = [json.loads(row[0]) for row in db.execute(
                "SELECT body FROM wiki_job_outbox WHERE job_id=? AND state_version>? ORDER BY state_version",
                (job["id"], before))]
            members = {row[0] for row in db.execute(
                "SELECT doc_id FROM maintenance_job_members WHERE job_id=?", (job["id"],))}
            db.execute("UPDATE member_write_audit SET inserted=0,deleted=0")
        assert members == {item["doc"] for item in job["items"]}
        assert job["attempt"] == 1
        assert len(events) == (3 if phase == "progress" else 1)
        assert [event["state_version"] for event in events] == list(range(before + 1, job["state_version"] + 1))
        assert events[-1]["progress"] == job["progress"] == {"total": count, "completed": 3, "attention": 0, "failed": 0}
        projection = observe(jobs, limit=50)
        assert projection["ready"] and projection["total"] == count + 1
        measured.append({"members": count, "phase": phase, "seconds": elapsed,
                         "vm_steps": vm_steps, "snapshots": len(events), **writes})
    assert [(row["inserted"], row["deleted"]) for row in measured] == [(0, 0), (1, 1)], measured
    return measured


def test_progress_writes_only_real_membership_changes(prepared, monkeypatch):
    progress_measurements(prepared, monkeypatch, 4)


@pytest.mark.wiki_perf
@pytest.mark.parametrize("count", [50, 200, 800])
def test_progress_membership_cost(prepared, monkeypatch, count):
    # Explicit ticket41 measurement, excluded from ordinary correctness gates.
    measurements = progress_measurements(prepared, monkeypatch, count)
    print(json.dumps({
        "timing_scope": "single instrumented sample per phase; updates include SQLite progress callbacks and membership audit triggers; verification and projection reads excluded",
        "progress_membership_cost": measurements,
    }), flush=True)


def test_repeated_job_state_does_not_invalidate_current_maintenance(prepared):
    jobs, docs = prepared
    doc = docs["a"]
    job = jobs.enqueue("unchanged", {"kind": "curation"}, [
        {"doc": doc["id"], "path": doc["path"], "revision": doc["revision"], "status": "pending"}])
    before = observe(jobs)
    with jobs.store.connect() as db:
        generation = db.execute("SELECT generation FROM maintenance_input_state").fetchone()[0]
    unchanged = jobs.update(job["id"], status=job["status"], items=job["items"], error=None)
    assert unchanged["state_version"] == job["state_version"]
    assert [item["review"] for item in observe(jobs)["items"]] == [item["review"] for item in before["items"]]
    with jobs.store.connect() as db:
        assert db.execute("SELECT generation FROM maintenance_input_state").fetchone()[0] == generation
