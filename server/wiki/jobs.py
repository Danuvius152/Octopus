"""Persistent, account-scoped curator work with observable item outcomes."""

import asyncio
import hashlib
import json
import uuid
from datetime import UTC, datetime
from functools import partial
from pathlib import Path

from ..config import settings
from .app_bridge import complete_cleanup
from .curation import WikiCuration
from .history import WikiHistory
from .registry import registry
from .store import WikiError, asset_reason


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
        if row is None:
            raise WikiError("Maintenance job not found", 404)
        return self.public(row)

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
        with self.store.connect() as db:
            previous = db.execute(
                "SELECT * FROM jobs WHERE request_key=?", (key,)
            ).fetchone()
        if previous:
            if json.loads(previous["payload"]).get("request") != request:
                raise WikiError(
                    "Idempotency key already names a different maintenance request", 409
                )
            return self.public(previous)
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
            if document["kind"] == "asset":
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
            selected.append(
                {
                    "status": "pending",
                    "path": document["path"],
                    "doc": document["id"],
                    "revision": document["revision"],
                    "quality": document["quality"],
                }
            )
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
        snapshot = WikiHistory(self.store).snapshot(
            "Before full original-vault curation"
        )
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
                "snapshot_id": snapshot["id"],
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
            await manager.interrupt(job["session_id"])
        return await _io(self.get, job_id)

    def recover_jobs(self):
        with self.store.connect() as db:
            db.execute(
                "UPDATE jobs SET status='queued',error='Recovered after server restart',session_id=NULL WHERE status IN ('running','interrupted')"
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
            path = payload.get("target") or f"Library/Inbox/{job_id}.md"
            target = self.store.path(path)
            if target.exists():
                if not any(
                    entry["reason"] == f"Ingest job {job_id}"
                    and entry["status"] == "applied"
                    for entry in history.history(path)["items"]
                ):
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
        job = self.get(job_id)
        if job["status"] in {"running", "queued", "completed"}:
            raise WikiError(
                "Only cancelled, failed, interrupted or attention jobs can be retried",
                409,
            )
        for item in job["items"]:
            if item["status"] != "done":
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
                    item["quality"] = current["quality"]
                item.pop("error", None)
                item["status"] = "pending"
        result = self.update(
            job_id, status="queued", items=job["items"], error=None, session_id=None
        )
        worker.wake(self.user_id)
        return result


class WikiWorker:
    def __init__(self):
        self.manager = None
        self.loop = None
        self.tasks: dict[str | None, asyncio.Task] = {}
        self.pending: set[str | None] = set()

    async def start(self, manager):
        self.manager = manager
        self.loop = asyncio.get_running_loop()
        states = [(None, Path(settings.wiki_dir).expanduser())]
        root = Path(settings.users_root).expanduser()
        if root.exists():
            states.extend((p.parent.name, p) for p in root.glob("*/wiki"))
        for user_id, state in states:
            if (state / "config.json").is_file():
                jobs = await _io(WikiJobs, user_id)
                await _io(jobs.recover_jobs)
                self.wake(user_id)

    def wake(self, user_id):
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

    async def stop(self):
        for task in self.tasks.values():
            task.cancel()
        await asyncio.gather(*self.tasks.values(), return_exceptions=True)
        self.tasks.clear()
        self.pending.clear()
        self.manager = None
        self.loop = None

    async def run(self, user_id):
        jobs = await _io(WikiJobs, user_id)
        while True:
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
            if not any(item["path"] == path for item in job["items"]):
                doc = await _io(jobs.ingest, job_id)
                if (
                    doc is None
                    or (await _io(jobs.get, job_id))["status"] == "cancelled"
                ):
                    return
                job["items"].append(
                    {
                        "doc": doc["id"],
                        "path": path,
                        "revision": doc["revision"],
                        "quality": doc["quality"],
                        "status": "pending",
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
                doc = await _io(jobs.store.read, item["doc"], length=1)
                if doc["revision"] != item["revision"]:
                    item.update(
                        status="attention",
                        error="Source changed since this task was submitted; retry after review",
                    )
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
                origin="schedule",
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
                "完整读取指定正文；用 organize 保留原文并补 Key note、必要标签和关联。"
                "开头已有要点则沿用，不重复总结。新增关联之前读相关旧文，链接只写到指定笔记中。"
                "不逐篇复制来源卡、不更新未指定旧文；必要的新概念/主题入口使用 Library 分类。"
                "工具/tmp/静态/PDF 等资产不套模板。完成意义自检和连续冷读。"
                "用实际工具产物完成任务，不能只在对话中说已完成；本队列没有人等待回答问题。\n"
                + "用户要求: "
                + payload["scope"]
                + "\n仅可修改这些文件（其他旧文只读）:\n"
                + json.dumps(briefing, ensure_ascii=False)
            )
            errors = []
            try:
                async for event in self.manager.send_message(
                    session.id, prompt, wiki_mode="maintain"
                ):
                    if event.get("type") == "error":
                        errors.append(event.get("message", "Agent error"))
            finally:
                grants.unassign(session.id)
            await self.manager.archive_session(session.id, replace=False)
            if (await _io(jobs.get, job_id))["status"] == "cancelled":
                return
            for item in readable:
                path = item["path"]
                if Path(path).suffix.lower() not in {".md", ".markdown"}:
                    path += ".md"
                try:
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
                    item.update(
                        status="done", output=path, output_revision=output["revision"]
                    )
                except WikiError as exc:
                    item.update(status="failed", error="; ".join(errors) or str(exc))
            await _io(
                jobs.update,
                job_id,
                expected_status="running",
                items=job["items"],
                session_id=None,
            )


worker = WikiWorker()
