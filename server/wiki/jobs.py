"""Persistent, account-scoped curator work with observable item outcomes."""

import asyncio
import hashlib
import json
import uuid
from datetime import UTC, datetime
from functools import partial
from pathlib import Path

from ..aio import complete_cleanup
from ..config import settings
from . import materials
from .curation import WikiCuration
from .history import WikiHistory
from .registry import registry
from .store import WikiError, asset_reason, is_maintainable_text


async def _io(function, *args, **kwargs):
    # Native asyncio cancellation must join a writer before its worker is reaped.
    return await complete_cleanup(asyncio.to_thread(partial(function, *args, **kwargs)))


class WikiJobs:
    def __init__(self, user_id):
        self.user_id = user_id
        self.store = registry.store(user_id)
        with self.store.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS jobs (
              id TEXT PRIMARY KEY,request_key TEXT UNIQUE NOT NULL,payload_hash TEXT NOT NULL,
              payload TEXT NOT NULL,status TEXT NOT NULL,items TEXT NOT NULL,error TEXT,
              session_id TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL
            )""")
            db.execute("""CREATE TABLE IF NOT EXISTS job_requests (
              request_key TEXT PRIMARY KEY,request_hash TEXT NOT NULL,job_id TEXT NOT NULL
            )""")

    @staticmethod
    def request_hash(request):
        return hashlib.sha256(json.dumps(request, ensure_ascii=False, sort_keys=True).encode()).hexdigest()

    def previous_request(self, key, request):
        with self.store.connect() as db:
            alias = db.execute("SELECT * FROM job_requests WHERE request_key=?", (key,)).fetchone()
            previous = db.execute("SELECT * FROM jobs WHERE request_key=?", (key,)).fetchone()
        if alias:
            if alias["request_hash"] != self.request_hash(request):
                raise WikiError("Idempotency key already names a different maintenance request", 409)
            return self.get(alias["job_id"])
        if previous:
            if json.loads(previous["payload"]).get("request") != request:
                raise WikiError("Idempotency key already names a different maintenance request", 409)
            return self.public(previous)
        return None

    def alias(self, key, request, job_id):
        with self.store.connect() as db:
            db.execute("INSERT INTO job_requests VALUES(?,?,?)", (key, self.request_hash(request), job_id))
        return self.get(job_id)

    @staticmethod
    def public(row):
        result = dict(row)
        result["payload"] = json.loads(result["payload"])
        result["items"] = json.loads(result["items"])
        result["progress"] = {
            "total": len(result["items"]),
            "completed": sum(i["status"] == "done" for i in result["items"]),
            "attention": sum(i["status"] == "attention" for i in result["items"]),
            "failed": sum(i["status"] == "failed" for i in result["items"]),
        }
        return result

    def get(self, job_id):
        with self.store.connect() as db:
            row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            receipts = [dict(receipt) for receipt in db.execute("SELECT r.*,coalesce(c.status,'applied') AS status FROM operation_receipts r LEFT JOIN changes c ON c.id=r.change_id WHERE r.job_id=? ORDER BY r.rowid", (job_id,))]
        if row is None:
            raise WikiError("Maintenance job not found", 404)
        for receipt in receipts:
            receipt["details"] = json.loads(receipt["details"])
        return self.public(row) | {"receipts": receipts}

    def list(self, offset=0, limit=30):
        if offset < 0 or not 1 <= limit <= 100:
            raise WikiError("Invalid maintenance pagination", 422)
        with self.store.connect() as db:
            rows = db.execute(
                "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (limit + 1, offset),
            ).fetchall()
        return {
            "items": [self.public(row) for row in rows[:limit]],
            "next_offset": offset + limit if len(rows) > limit else None,
        }

    def enqueue(self, key, payload, items):
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        digest = hashlib.sha256(raw.encode()).hexdigest()
        with self.store.connect() as db:
            alias = db.execute("SELECT * FROM job_requests WHERE request_key=?", (key,)).fetchone()
            if alias:
                if alias["request_hash"] != self.request_hash(payload.get("request", payload)):
                    raise WikiError("Idempotency key already names a different submission", 409)
                return self.get(alias["job_id"])
            previous = db.execute(
                "SELECT * FROM jobs WHERE request_key=?", (key,)
            ).fetchone()
            if previous:
                if previous["payload_hash"] != digest:
                    raise WikiError(
                        "Idempotency key already names a different submission", 409
                    )
                return self.public(previous)
            now = datetime.now(UTC).isoformat()
            job_id = uuid.uuid4().hex
            db.execute(
                "INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    job_id,
                    key,
                    digest,
                    raw,
                    "queued",
                    json.dumps(items, ensure_ascii=False),
                    None,
                    None,
                    now,
                    now,
                ),
            )
            db.execute("INSERT INTO job_requests VALUES(?,?,?)", (key, self.request_hash(payload.get("request", payload)), job_id))
        worker.wake(self.user_id)
        return self.get(job_id)

    def update(self, job_id, *, expected_status=None, **fields):
        permitted = {"status", "items", "error", "session_id"}
        if set(fields) - permitted:
            raise ValueError("Invalid job update")
        fields["updated_at"] = datetime.now(UTC).isoformat()
        if "items" in fields:
            fields["items"] = json.dumps(fields["items"], ensure_ascii=False)
        with self.store.connect() as db:
            db.execute(
                "UPDATE jobs SET "
                + ",".join(f"{key}=?" for key in fields)
                + " WHERE id=?"
                + (" AND status=?" if expected_status else ""),
                [
                    *fields.values(),
                    job_id,
                    *([expected_status] if expected_status else []),
                ],
            )
        return self.get(job_id)

    def submit(self, key, material, scope, sources, actor):
        return self.maintain(key, material, scope, [], None, sources, actor)

    def maintain(
        self, key, material, scope, paths, target, sources, actor, created_changes=None
    ):
        with WikiHistory(self.store).locked():
            return self._maintain(key, material, scope, paths, target, sources, actor, created_changes)

    def _maintain(
        self, key, material, scope, paths, target, sources, actor, created_changes=None
    ):
        created_changes = created_changes or {}
        if not key.strip() or not (material.strip() or paths):
            raise WikiError(
                "Maintenance requires a key and material or explicit paths", 422
            )
        if material and paths:
            raise WikiError("Choose new material or explicitly selected files", 422)
        if not material and not paths:
            raise WikiError(
                "Select files for maintenance; citations are read-only sources", 422
            )
        request = {
            "material": material,
            "scope": scope,
            "paths": paths,
            "target": target,
            "sources": sources,
            "actor": actor,
            "created_changes": created_changes,
        }
        previous = self.previous_request(key, request)
        if previous:
            return previous
        refs = WikiCuration(self.store).sources(sources)
        if target:
            self.store.path(target)
            if (
                not target.startswith("Library/")
                or Path(target).suffix.lower() != ".md"
                or asset_reason(target)
            ):
                raise WikiError(
                    "New notes belong in a Library category or Library/Inbox", 422
                )
        selected = []
        if created_changes and (material or set(created_changes) != set(paths)):
            raise WikiError("The new-file manifest must match the selected files", 422)
        for path in dict.fromkeys(paths):
            self.store.path(path)
            document = self.store.read(path, length=1)
            if not is_maintainable_text(document["path"]):
                raise WikiError(f"This is an excluded asset: {path}", 422)
            if created_changes:
                WikiHistory(self.store)
                with self.store.connect() as db:
                    change = db.execute(
                        "SELECT * FROM changes WHERE id=?", (created_changes[path],)
                    ).fetchone()
                if (
                    not change
                    or change["path"] != path
                    or change["before_hash"] is not None
                    or change["status"] != "applied"
                    or change["after_hash"] != document["revision"]
                ):
                    raise WikiError(
                        "A new-file manifest requires the current creation change for each file",
                        403,
                    )
                with self.store.connect() as db:
                    identity = db.execute("SELECT document_id FROM change_retention WHERE change_id=?", (change["id"],)).fetchone()
                if not identity or identity[0] != document["id"]:
                    raise WikiError("The creation receipt belongs to a different document identity", 403)
            item = {
                    "status": "pending",
                    "path": document["path"],
                    "doc": document["id"],
                    "revision": document["revision"],
                    "quality": document["quality"],
                }
            if Path(path).suffix.lower() not in {".md", ".markdown"}:
                paired_path = path + ".md"
                item["paired_doc"] = None
                if self.store.path(paired_path).is_file():
                    with self.store.connect() as db:
                        owned = db.execute("SELECT metadata FROM managed_notes WHERE path=?", (paired_path,)).fetchone()
                    if paired_path not in paths and (not owned or not any(ref.get("doc") == document["id"] for ref in json.loads(owned[0]).get("sources", []))):
                        raise WikiError("The paired reading page is not owned by this source; select it explicitly after review", 403)
                    item["paired_doc"] = self.store.read(paired_path, length=1)["id"]
            selected.append(item)
        payload = {
            "kind": "submission" if material else "curation",
            "new_only": bool(material or created_changes),
            "material": material,
            "scope": scope,
            "paths": list(dict.fromkeys(paths)),
            "target": target,
            "sources": refs,
            "actor": actor,
            "request": request,
        }
        if material:
            with self.store.connect() as db:
                pending = db.execute("SELECT id,payload FROM jobs WHERE status IN ('queued','running','interrupted') ORDER BY created_at").fetchall()
            for job in pending:
                if json.loads(job["payload"]).get("material") == material:
                    return self.alias(key, request, job["id"])
            reused = materials.find(self.store, material.encode())
            if reused:
                result = self.enqueue(key, payload, [{
                    "doc": reused["id"], "path": reused["path"], "revision": reused["revision"],
                    "quality": reused["quality"], "status": "done", "reused": True,
                    "output": reused["path"], "output_revision": reused["revision"],
                }])
                return self.update(result["id"], status="completed")
            if target and self.store.path(target).exists():
                raise WikiError("New-note target already exists with different material", 409)
        return self.enqueue(key, payload, selected)

    def curate(self, key, scope):
        with self.store.connect() as db:
            existing = db.execute(
                "SELECT id FROM jobs WHERE request_key=?", (key,)
            ).fetchone()
        if existing:
            job = self.get(existing[0])
            if (
                job["payload"]["kind"] != "curation"
                or job["payload"]["scope"] != scope
                or job["payload"].get("paths")
            ):
                raise WikiError("Idempotency key already names a different task", 409)
            return job
        self.store.refresh()
        with self.store.connect() as db:
            documents = db.execute(
                "SELECT * FROM documents WHERE kind='original' ORDER BY path"
            ).fetchall()
        items = [
            {
                "doc": row["id"],
                "path": row["path"],
                "revision": row["revision"],
                "quality": row["quality"],
                "status": "pending",
            }
            for row in documents
        ]
        return self.enqueue(
            key,
            {
                "kind": "curation",
                "scope": scope,
                "new_only": False,
            },
            items,
        )

    def mark_cancelled(self, job_id):
        with self.store.connect() as db:
            db.execute(
                "UPDATE jobs SET status='cancelled',updated_at=? WHERE id=? AND status NOT IN ('completed','completed_with_attention','cancelled')",
                (datetime.now(UTC).isoformat(), job_id),
            )
        return self.get(job_id)

    async def cancel(self, job_id, manager):
        job = await _io(self.mark_cancelled, job_id)
        if job["status"] == "cancelled" and job["session_id"] and manager:
            refs = {item["worker_request"]["request_id"] for item in job["items"]
                    if item.get("worker_request", {}).get("session_id") == job["session_id"]}
            # Capture the just-admitted Wiki input if its durable receipt write
            # is still in flight. Human corrections have another source.
            session = manager.get_session(job["session_id"])
            if session is not None:
                refs.update(request.request_id for request in
                            [*session._pending_queue, *([session._current_request] if session._current_request else [])]
                            if request.source == "wiki")
            for request_id in refs:
                await manager.cancel_request(job["session_id"], request_id)
        return await _io(self.get, job_id)

    def recover_jobs(self):
        with self.store.connect() as db:
            rows = db.execute("SELECT id,items FROM jobs WHERE status IN ('running','interrupted')").fetchall()
            for row in rows:
                items = json.loads(row["items"])
                for item in items:
                    if item.get("recording_failure"):
                        item.update(status="pending", recording_recovery=True)
                db.execute(
                    "UPDATE jobs SET status='queued',error='Recovered after server restart',session_id=NULL,items=? WHERE id=? AND status IN ('running','interrupted')",
                    (json.dumps(items, ensure_ascii=False), row["id"]),
                )

    def next_queued(self):
        with self.store.connect() as db:
            row = db.execute(
                "UPDATE jobs SET status='running',error=NULL WHERE id=(SELECT id FROM jobs WHERE status='queued' ORDER BY created_at LIMIT 1) AND status='queued' RETURNING id"
            ).fetchone()
        return row[0] if row else None

    def ingest(self, job_id):
        history = WikiHistory(self.store)
        with history.locked():
            job = self.get(job_id)
            if job["status"] == "cancelled":
                return None
            payload = job["payload"]
            existing = materials.find(self.store, payload["material"].encode())
            if existing:
                with self.store.connect() as db:
                    own_creation = db.execute("""SELECT 1 FROM changes c JOIN change_retention r ON r.change_id=c.id
                      WHERE c.path=? AND c.reason=? AND c.status='applied' AND c.before_hash IS NULL
                        AND r.document_id=?""", (existing["path"], f"Ingest job {job_id}", existing["id"])).fetchone()
                return existing | {"reused": not bool(own_creation)}
            path = payload.get("target") or f"Library/Inbox/{job_id}.md"
            target = self.store.path(path)
            if target.exists():
                current = self.store.read(path, length=1)
                with self.store.connect() as db:
                    receipt = db.execute("""SELECT 1 FROM changes c JOIN change_retention r ON r.change_id=c.id
                      WHERE c.path=? AND c.reason=? AND c.status='applied' AND c.before_hash IS NULL
                        AND r.document_id=?""", (path, f"Ingest job {job_id}", current["id"])).fetchone()
                if not receipt:
                    raise WikiError(
                        "New-note target already exists; the original was not overwritten",
                        409,
                    )
            else:
                history.write_locked(
                    path,
                    payload["material"].encode(),
                    None,
                    payload["actor"],
                    f"Ingest job {job_id}",
                )
            return self.store.read(path, length=1)

    def retry(self, job_id):
        with WikiHistory(self.store).locked():
            return self._retry(job_id)

    def _retry(self, job_id):
        job = self.get(job_id)
        if job["status"] in {"running", "queued", "completed"}:
            raise WikiError(
                "Only cancelled, failed, interrupted or attention jobs can be retried",
                409,
            )
        for item in job["items"]:
            if item["status"] != "done":
                if item.get("recording_failure"):
                    item.update(status="pending", recording_recovery=True)
                    continue
                if item.get("index_recovery") or self.publication(job_id, item["doc"]):
                    item.update(status="pending", index_recovery=True)
                    item.pop("error", None)
                    continue
                if item.get("retry_count", 0) >= 2:
                    item.update(status="attention", error="The two reread attempts are exhausted; create a new explicit task after review")
                    continue
                try:
                    current = self.store.read(item["doc"], length=1)
                except WikiError:
                    pass
                else:
                    if current["revision"] != item["revision"]:
                        item.setdefault("previous_revisions", []).append(
                            item["revision"]
                        )
                        item["revision"] = current["revision"]
                    item["retry_count"] = item.get("retry_count", 0) + 1
                    item["quality"] = current["quality"]
                item.pop("error", None)
                item["status"] = "pending"
        result = self.update(
            job_id, status="queued", items=job["items"], error=None, session_id=None
        )
        worker.wake(self.user_id)
        return result

    def resolve_conflicts(self, job_id, path):
        history = WikiHistory(self.store)
        with history.locked():
            with self.store.connect() as db:
                ids = [row[0] for row in db.execute(
                    "SELECT change_id FROM change_proposals WHERE json_extract(operation,'$.job_id')=? AND json_extract(operation,'$.source_path')=?",
                    (job_id, path),
                )]
            history.resolve_proposals(ids)

    def has_conflict(self, job_id, path):
        with self.store.connect() as db:
            return bool(db.execute("""SELECT 1 FROM change_proposals p
              JOIN change_retention r ON r.change_id=p.change_id
              WHERE json_extract(p.operation,'$.job_id')=?
                AND json_extract(p.operation,'$.source_path')=?
                AND r.resolved_at IS NULL LIMIT 1""", (job_id, path)).fetchone())

    def publication(self, job_id, document_id):
        with self.store.connect() as db:
            receipt = db.execute("""SELECT r.id AS receipt_id,r.job_id,r.item_doc AS source_doc,
                coalesce(json_extract(r.details,'$.input_source_revision'),p.input_revision,r.input_revision) AS input_revision,r.path AS output_path,r.output_revision,r.change_id,
                coalesce(p.indexed,0) AS indexed,coalesce(c.status,'applied') AS change_status,
                r.doc_id AS output_doc,r.operation FROM operation_receipts r
                LEFT JOIN changes c ON c.id=r.change_id
                LEFT JOIN job_publications p ON p.change_id=r.change_id
                WHERE r.job_id=? AND r.item_doc=? ORDER BY r.rowid DESC LIMIT 1""", (job_id, document_id)).fetchone()
            if receipt:
                return receipt
            return db.execute("SELECT p.*,c.status AS change_status,r.document_id AS output_doc FROM job_publications p JOIN changes c ON c.id=p.change_id JOIN change_retention r ON r.change_id=c.id WHERE p.job_id=? AND p.source_doc=? ORDER BY p.rowid DESC LIMIT 1", (job_id, document_id)).fetchone()

    def recover_publication(self, job_id, item):
        """Recognize our durable publication before treating it as input drift."""
        history = WikiHistory(self.store)
        with history.locked():
            published = self.publication(job_id, item["doc"])
            if not published:
                return False
            item.update(input_revision=published["input_revision"], output=published["output_path"],
                        output_revision=published["output_revision"], output_change_id=published["change_id"],
                        index_complete=False, index_recovery=True)
            operation = published["operation"] if "operation" in published.keys() else "organize"
            item["operation"] = operation
            if "receipt_id" in published.keys():
                item["receipt_id"] = published["receipt_id"]
            if published["change_status"] != "applied":
                # The receipt precedes the atomic write. A crash followed by
                # editing or reverting the file cannot prove whether it landed.
                # Preserve that uncertainty instead of generating another draft.
                item.update(status="attention", error="Publication interrupted before its applied marker; review the retained change and current text without regenerating the note")
                return True
            try:
                output = self.store.read(published["output_path"], length=1, expected_revision=published["output_revision"])
                if published["output_path"] != item["path"]:
                    self.store.read(item["doc"], length=1, expected_revision=published["input_revision"])
                if output["id"] != published["output_doc"]:
                    raise WikiError("Published output identity was replaced", 409)
                if operation in {"save", "organize"} and output["stale"]:
                    raise WikiError("Published output or its cited sources changed; review the existing publication", 409)
                with self.store.connect() as db:
                    self.store.require_indexed(db, published["output_path"])
                    db.execute("UPDATE job_publications SET indexed=1 WHERE change_id=?", (published["change_id"],))
            except WikiError as exc:
                item.update(status="attention", index_complete=False, index_recovery=True,
                            output=published["output_path"], output_revision=published["output_revision"],
                            output_change_id=published["change_id"], error=str(exc))
                return True
            item.update(status="done", input_revision=published["input_revision"], output=published["output_path"], output_revision=published["output_revision"], output_change_id=published["change_id"], index_complete=True)
            item.pop("error", None)
            item.pop("index_recovery", None)
            self.resolve_conflicts(job_id, item["path"])
            return True

    def reread(self, job_id, item, error, *, retryable=True):
        if item.get("recording_failure"):
            item.update(status="attention", error="Execution result recording remains unconfirmed; verify the original request without regenerating content")
            return
        history = WikiHistory(self.store)
        with history.locked():
            fresh = self.get(job_id)
            persisted = next(entry for entry in fresh["items"] if entry["doc"] == item["doc"])
            item["retry_count"] = persisted.get("retry_count", 0)
            retrying = retryable and item["retry_count"] < 2
            if retrying:
                # Reserve the attempt durably before reading or starting a new
                # worker turn. Restarts and manual retry cannot reset it.
                item["retry_count"] += 1
                persisted.update(item)
                self.update(job_id, expected_status="running", items=fresh["items"])
            current = self.store.read(item["doc"], length=1)
            raw = self.store.path(current["path"]).read_bytes()
            candidate = history.record(item["path"], raw, raw, "curator-worker", error, "conflict", proposal={
                "job_id": job_id, "source_path": item["path"], "expected_revision": item["revision"],
                "operation": "reread-and-organize", "reason": error,
            })
            item.setdefault("candidate_changes", []).append(candidate["id"])
            item.setdefault("input_revision", item["revision"])
            if not retrying:
                item.update(status="attention", error=error + "; retained candidate for review")
            else:
                if current["revision"] != item["revision"]:
                    item.setdefault("previous_revisions", []).append(item["revision"])
                item.update(status="pending", revision=current["revision"], quality=current["quality"], retry_reason=error)
                item.pop("error", None)
            persisted.update(item)
            self.update(job_id, expected_status="running", items=fresh["items"])


class WikiWorker:
    def __init__(self):
        self.manager = None
        self.loop = None
        self.tasks: dict[str | None, asyncio.Task] = {}
        self.pending: set[str | None] = set()
        self._stopping = False

    async def start(self, manager):
        if self._stopping and self.tasks:
            raise RuntimeError("Wiki curator teardown remains unconfirmed")
        self._stopping = False
        self.manager = manager
        self.loop = asyncio.get_running_loop()
        states = registry.states()
        for user_id, state in states:
            if (state / "config.json").is_file():
                jobs = await _io(WikiJobs, user_id)
                await _io(jobs.recover_jobs)
                self.wake(user_id)

    def wake(self, user_id):
        if self._stopping:
            return
        if self.loop is not None:
            try:
                current = asyncio.get_running_loop()
            except RuntimeError:
                current = None
            if current is not self.loop:
                self.loop.call_soon_threadsafe(self.wake, user_id)
                return
        if self.manager is not None:
            self.pending.add(user_id)
        if self.manager is not None and (
            user_id not in self.tasks or self.tasks[user_id].done()
        ):
            self.tasks[user_id] = asyncio.get_running_loop().create_task(
                self.run(user_id), name=f"wiki-curator-{user_id}"
            )

    def begin_shutdown(self) -> None:
        """Seal wakes and issue curator cancellation before execution is drained."""
        self._stopping = True
        self.pending.clear()
        for task in self.tasks.values():
            if not task.done() and not task.cancelling():
                task.cancel("shutdown")

    async def stop(self, *, timeout: float = 8.0) -> None:
        """Join captured owners without cancelling their shielded cleanup."""
        self.begin_shutdown()
        tasks = tuple(self.tasks.values())
        if tasks:
            done, pending = await asyncio.wait(tasks, timeout=max(0.0, timeout))
            if pending:
                names = ", ".join(sorted(task.get_name() for task in pending))
                raise RuntimeError(f"Wiki curator teardown remains unconfirmed: {names}")
            failures = [
                error for task in done
                if not task.cancelled() and (error := task.exception()) is not None
            ]
            if failures:
                raise RuntimeError("Wiki curator teardown remains unconfirmed") from failures[0]
        self.tasks.clear()
        self.pending.clear()
        self.manager = None
        self.loop = None

    async def run(self, user_id):
        jobs = await _io(WikiJobs, user_id)
        while not self._stopping:
            self.pending.discard(user_id)
            job_id = await _io(jobs.next_queued)
            if job_id is None:
                if user_id in self.pending:
                    continue
                return
            try:
                await self.process(jobs, job_id)
            except asyncio.CancelledError:
                job = await _io(jobs.get, job_id)
                if job["status"] == "running":
                    await _io(
                        jobs.update,
                        job_id,
                        expected_status="running",
                        status="interrupted",
                        error="Server stopped; retry resumes unfinished items",
                    )
                raise
            except Exception as exc:
                if self._stopping:
                    job = await _io(jobs.get, job_id)
                    if job["status"] == "running":
                        await _io(
                            jobs.update,
                            job_id,
                            expected_status="running",
                            status="interrupted",
                            error=f"Server stopped; curator cleanup remains unconfirmed: {type(exc).__name__}: {exc}",
                        )
                    raise
                await _io(
                    jobs.update,
                    job_id,
                    expected_status="running",
                    status="failed",
                    error=f"{type(exc).__name__}: {exc}",
                )

    async def process(self, jobs, job_id):
        config = registry.config(jobs.user_id)
        agent = await self.manager.db.get_agent(
            config["curator_agent_id"], jobs.user_id
        )
        if agent is None:
            raise WikiError("Configured curator is no longer available", 409)
        job = await _io(jobs.get, job_id)
        payload = job["payload"]
        if job["status"] == "cancelled":
            return
        if payload["kind"] == "submission" and payload["material"]:
            path = payload.get("target") or f"Library/Inbox/{job_id}.md"
            if not job["items"]:
                doc = await _io(jobs.ingest, job_id)
                if (
                    doc is None
                    or (await _io(jobs.get, job_id))["status"] == "cancelled"
                ):
                    return
                job["items"].append(
                    {
                        "doc": doc["id"],
                        "path": doc["path"],
                        "revision": doc["revision"],
                        "quality": doc["quality"],
                        "status": "done" if doc.get("reused") else "pending",
                        **({"reused": True, "output": doc["path"], "output_revision": doc["revision"]} if doc.get("reused") else {}),
                    }
                )
                await _io(
                    jobs.update, job_id, expected_status="running", items=job["items"]
                )
        while True:
            job = await _io(jobs.get, job_id)
            if job["status"] == "cancelled":
                return
            batch = [i for i in job["items"] if i["status"] == "pending"][:3]
            if not batch:
                status = (
                    "completed_with_attention"
                    if any(i["status"] in {"failed", "attention"} for i in job["items"])
                    else "completed"
                )
                await _io(
                    jobs.update,
                    job_id,
                    expected_status="running",
                    status=status,
                    session_id=None,
                )
                return
            briefing = []
            readable = []
            for item in batch:
                if item.get("recording_failure"):
                    await self._recover_recorded_item(jobs, job_id, item)
                    continue
                if await _io(jobs.recover_publication, job_id, item):
                    continue
                if item.get("index_recovery"):
                    item.update(status="attention", error="Publication recovery receipt is unavailable; review without regenerating the note")
                    continue
                try:
                    doc = await _io(jobs.store.read, item["doc"], length=1)
                except WikiError as exc:
                    item.update(status="attention", error=str(exc))
                    continue
                if doc["revision"] != item["revision"]:
                    await _io(jobs.reread, job_id, item, "Source changed since this task was submitted")
                elif doc["quality"] == "error" or doc["characters"] == 0:
                    item.update(
                        status="attention",
                        error=doc["metadata"].get(
                            "error",
                            "No extracted text; original retained, OCR/manual review required",
                        ),
                    )
                else:
                    item["quality"] = doc["quality"]
                    readable.append(item)
                    briefing.append(
                        {
                            key: doc[key]
                            for key in (
                                "id",
                                "path",
                                "revision",
                                "title",
                                "quality",
                                "characters",
                                "pages",
                            )
                        }
                    )
            await _io(
                jobs.update, job_id, expected_status="running", items=job["items"]
            )
            if not readable:
                continue
            if (await _io(jobs.get, job_id))["status"] == "cancelled":
                return
            session = await self.manager.create_session(
                config["curator_agent_id"],
                name=f"Wiki 维护 {job_id[:8]}",
                # The worker owns this session through grant retirement and
                # publication verification. A schedule origin lets the generic
                # dispatcher archive it as soon as its execution receipt ends.
                origin="wiki",
                backend=agent.get("backend") or settings.default_backend,
            )
            await _io(
                jobs.update, job_id, expected_status="running", session_id=session.id
            )
            if (await _io(jobs.get, job_id))["status"] == "cancelled":
                await self.manager.archive_session(session.id, replace=False)
                return
            from .tasks import tasks as grants

            assigned = [item["path"] for item in readable]
            assigned.extend(
                item["path"] + ".md"
                for item in readable
                if Path(item["path"]).suffix.lower() not in {".md", ".markdown"}
            )
            grants.assign(session.id, job_id, assigned)
            prompt = (
                "这是已明确授权的持久维护任务。按本次共享维护与写作指南执行。\n"
                + "用户要求: "
                + payload["scope"]
                + "\n仅可修改这些文件（其他旧文只读）:\n"
                + json.dumps(briefing, ensure_ascii=False)
                + "\n重读原因与已用重读次数:\n"
                + json.dumps([{key: item.get(key) for key in ("path", "retry_reason", "retry_count")} for item in readable], ensure_ascii=False)
                + "\n本队列没有人等待同步答复；以实际发布产物结束。"
            )
            receipt = None
            completion = None
            try:
                receipt = await self.manager.start_message(
                    session.id, prompt, source="wiki", wiki_mode="maintain"
                )
                for item in readable:
                    item["worker_request"] = {"session_id": session.id, "request_id": receipt.request_id}
                await _io(jobs.update, job_id, expected_status="running", items=job["items"])
                completion = await receipt.wait()
            except BaseException as error:
                if receipt is not None and receipt.accepted:
                    cancellation = {"cause": "shutdown"} if (
                        isinstance(error, asyncio.CancelledError) and error.args == ("shutdown",)
                    ) else {}
                    await complete_cleanup(self.manager.cancel_request(
                        session.id, receipt.request_id, **cancellation,
                    ))
                    completion = await receipt.wait()
                    if completion.state == "recording_failed":
                        await self._mark_recording_failure(jobs, job_id, job, readable, session.id, completion)
                    else:
                        await self.manager.archive_session(session.id, replace=False)
                raise
            finally:
                # Stop remains fast; the grants owner waits for captured old
                # process retirement before removing its authority.
                await complete_cleanup(self.manager._await_backend_retirement(session))
                grants.unassign(session.id)
            if completion.state == "recording_failed":
                await self._mark_recording_failure(jobs, job_id, job, readable, session.id, completion)
                continue
            errors = []
            if completion.outcome is None:
                errors.append(completion.reason or "Maintenance request was not executed")
            elif completion.outcome.status != "success":
                errors.append(completion.outcome.message or completion.outcome.code or "Maintenance execution failed")
            await self.manager.archive_session(session.id, replace=False)
            if (await _io(jobs.get, job_id))["status"] == "cancelled":
                return
            for item in readable:
                path = item["path"]
                if Path(path).suffix.lower() not in {".md", ".markdown"}:
                    path += ".md"
                try:
                    if await _io(jobs.recover_publication, job_id, item):
                        continue
                    if errors:
                        raise WikiError("; ".join(errors))
                    output = await _io(jobs.store.read, path, length=1)
                    if (
                        output["metadata"].get("organized_from_revision")
                        != item["revision"]
                        or output["metadata"].get("organized_revision")
                        != output["revision"]
                    ):
                        raise WikiError(
                            "No current, preserved in-place organization was published"
                        )
                    if output["stale"]:
                        raise WikiError(
                            "Organized content is stale; review current sources"
                        )
                    with jobs.store.connect() as db:
                        jobs.store.require_indexed(db, path)
                    item.update(
                        status="done", output=path, output_revision=output["revision"], index_complete=True
                    )
                except WikiError as exc:
                    try:
                        current = await _io(jobs.store.read, item["doc"], length=1)
                    except WikiError:
                        item.update(status="attention", error="; ".join(errors) or str(exc))
                    else:
                        conflict = await _io(jobs.has_conflict, job_id, item["path"])
                        await _io(jobs.reread, job_id, item, "; ".join(errors) or str(exc), retryable=current["revision"] != item["revision"] or conflict)
            await _io(
                jobs.update,
                job_id,
                expected_status="running",
                items=job["items"],
                session_id=None,
            )

    async def _mark_recording_failure(self, jobs, job_id, job, readable, session_id, completion):
        reference = {
            "session_id": session_id, "request_id": completion.request_id,
            "recording_state": completion.recording_state,
        }
        for item in readable:
            item.update(status="attention", recording_failure=reference,
                        error="Execution result recording failed or is unconfirmed; verify the original request without rerunning the model")
        current = await _io(jobs.get, job_id)
        await _io(jobs.update, job_id, expected_status=current["status"], items=job["items"],
                  error="Execution result recording remains unconfirmed", session_id=session_id)

    async def _recover_recorded_item(self, jobs, job_id, item):
        """Storage/publication verification only; never start another model."""
        reference = item["recording_failure"]
        session_id, request_id = reference["session_id"], reference["request_id"]
        session = self.manager.get_session(session_id)
        if session is not None and session._recording_blocked is not None:
            blocked = session._recording_blocked
            if blocked.request_id != request_id or not await self.manager._reconcile_recording(session):
                item.update(status="attention", error="Original execution result recording remains unconfirmed")
                return
        try:
            outcome = await self.manager.db.get_committed_terminal(session_id, request_id)
        except Exception:
            item.update(status="attention", error="Original execution result recording could not be verified")
            return
        if outcome is None:
            item.update(status="attention", error="Original execution result is not committed; content was not regenerated")
            return
        if outcome.get("status") != "success":
            item.update(status="attention", error=outcome.get("message") or "Original maintenance execution was not successful")
            return
        try:
            recovered = await _io(jobs.recover_publication, job_id, item)
            if not recovered:
                path = item["path"]
                if Path(path).suffix.lower() not in {".md", ".markdown"}:
                    path += ".md"
                output = await _io(jobs.store.read, path, length=1)
                if (output["metadata"].get("organized_from_revision") != item["revision"]
                    or output["metadata"].get("organized_revision") != output["revision"]
                    or output["stale"]):
                    raise WikiError("Original execution has no current verified publication")
                with jobs.store.connect() as db:
                    jobs.store.require_indexed(db, path)
                item.update(status="done", output=path, output_revision=output["revision"], index_complete=True)
        except WikiError as exc:
            item.update(status="attention", error=str(exc))
            return
        item.pop("recording_failure", None)
        item.pop("recording_recovery", None)
        item.pop("error", None)
        if session is not None:
            await self.manager.archive_session(session_id, replace=False)


worker = WikiWorker()
