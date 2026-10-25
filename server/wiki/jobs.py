"""Persistent, account-scoped curator work with observable item outcomes."""

import asyncio
import copy
import hashlib
import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import partial
from pathlib import Path

from ..aio import complete_cleanup
from ..config import settings
from ..mcp_identity import McpScope, close_work
from ..sessions.runtime_types import SessionOwnerUnavailable
from . import (
    grants,
    job_controls,
    job_events,
    job_execution,
    job_items,
    maintenance_inputs,
    materials,
)
from .curation import WikiCuration
from .formats import MARKDOWN_SQL, is_markdown_maintenance, require_markdown_maintenance
from .history import WikiHistory
from .maintenance_contract import MAINTENANCE_OPERATIONS
from .registry import registry
from .store import WikiError, asset_reason

logger = logging.getLogger(__name__)


async def _io(function, *args, **kwargs):
    # Native asyncio cancellation must join a writer before its worker is reaped.
    return await complete_cleanup(asyncio.to_thread(partial(function, *args, **kwargs)))


@dataclass
class JobOwner:
    entered_attempt: int | None = None
    retired: asyncio.Event = field(default_factory=asyncio.Event)


class WikiJobs:
    def __init__(self, user_id, *, store=None, registration=None):
        self.user_id = user_id
        self.registration = registration if registration is not None else registry.config(user_id)
        self.store = store if store is not None else registry.store(user_id)
        with self.store.connect() as db:
            maintenance_inputs.require(db)
            job_events.require(db)

    @staticmethod
    def initialize_ledger(db):
        """Create the job ledger before its dependents in the creation transaction."""
        if not db.in_transaction:
            raise ValueError("Job initialization requires an explicit transaction")
        db.execute(WikiJobs.LEDGER_DDL)
        db.execute("""CREATE TABLE job_requests (
          request_key TEXT PRIMARY KEY,request_hash TEXT NOT NULL,job_id TEXT NOT NULL
        )""")
        db.execute("INSERT INTO wiki_protocols VALUES('maintenance_jobs',1)")

    LEDGER_DDL = """CREATE TABLE "jobs" (
          id TEXT UNIQUE NOT NULL,request_key TEXT UNIQUE NOT NULL,payload_hash TEXT NOT NULL,
          payload TEXT NOT NULL,status TEXT NOT NULL,items TEXT NOT NULL,error TEXT,
          session_id TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,
          attempt_errors TEXT NOT NULL DEFAULT '{}',
          admission_sequence INTEGER PRIMARY KEY AUTOINCREMENT
        )"""


    @staticmethod
    def request_hash(request):
        return hashlib.sha256(json.dumps(request, ensure_ascii=False, sort_keys=True).encode()).hexdigest()

    def previous_request(self, key, request, *, db=None):
        if db is None:
            with self.store.connect() as connection:
                return self.previous_request(key, request, db=connection)
        alias = db.execute("SELECT * FROM job_requests WHERE request_key=?", (key,)).fetchone()
        previous = db.execute("SELECT * FROM jobs WHERE request_key=?", (key,)).fetchone()
        if alias:
            if alias["request_hash"] != self.request_hash(request):
                raise WikiError("Idempotency key already names a different maintenance request", 409)
            return self.get(alias["job_id"], db=db)
        if previous:
            if json.loads(previous["payload"]).get("request") != request:
                raise WikiError("Idempotency key already names a different maintenance request", 409)
            return self.get(previous["id"], db=db)
        return None

    def subscribe(self, db, job_id, requester):
        if requester is not None and requester.owner != self.user_id:
            raise WikiError("Maintenance notifications belong to the original account", 403)
        return job_events.subscribe(db, job_id, requester)

    @staticmethod
    def effect_paths(db, job):
        """Include effective destinations even when the submitted paths were empty."""
        grant = job.get("grant") or {}
        return tuple(dict.fromkeys(
            [item["path"] for item in job["items"] if item.get("path")]
            + [row[0] for row in db.execute("SELECT path FROM wiki_path_reservations WHERE job_id=?", (job["id"],))]
            + [item["current_path"] for item in grant.get("selected_documents", ()) if item.get("current_path")]
        ))

    def replay(self, key, request, *, task=None, mcp_scope=None, requester=None, alias_job=None):
        """Even an idempotent return admits its subscriber in the final writer TX."""
        with registry._account_lock(self.user_id), registry.registration_lock(self.user_id), self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if registry.config(self.user_id) != self.registration:
                raise WikiError("Wiki registration changed before admission", 409)
            from .tasks import tasks

            tasks.require_submission(task, request, self.store, db, mcp_scope, self.registration)
            previous = self.previous_request(key, request, db=db)
            new_alias = previous is None and alias_job is not None
            if new_alias:
                previous = self.get(alias_job, db=db)
            if previous is not None:
                tasks.require_submission(task, request, self.store, db, mcp_scope, self.registration,
                                         actual_paths=self.effect_paths(db, previous))
            if new_alias:
                db.execute("INSERT INTO job_requests VALUES(?,?,?)", (key, self.request_hash(request), alias_job))
            if previous is not None:
                self.subscribe(db, previous["id"], requester)
            return previous

    @staticmethod
    def public(row):
        result = dict(row)
        result["payload"] = json.loads(result["payload"])
        result["items"] = job_items.decode(result["items"])
        try:
            result["attempt_errors"] = json.loads(result["attempt_errors"])
        except (TypeError, ValueError) as error:
            raise WikiError("Original attempt diagnostics are malformed; durable evidence retained", 503) from error
        if (not isinstance(result["attempt_errors"], dict)
                or any(not key.isdecimal() or not isinstance(error, str) for key, error in result["attempt_errors"].items())):
            raise WikiError("Original attempt diagnostics are malformed; durable evidence retained", 503)
        result["progress"] = {
            "total": len(result["items"]),
            "completed": sum(i["status"] == "done" for i in result["items"]),
            "attention": sum(i["status"] in {"attention", "unknown", "verifying", "excluded"} for i in result["items"]),
            "failed": sum(i["status"] == "failed" for i in result["items"]),
        }
        return result

    def get(self, job_id, *, db=None):
        from .maintenance_selections import evidence

        if db is None:
            with self.store.connect() as connection:
                return self.get(job_id, db=connection)
        row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        receipts = [dict(receipt) for receipt in db.execute("SELECT r.*,coalesce(c.status,'applied') AS status FROM operation_receipts r LEFT JOIN changes c ON c.id=r.change_id WHERE r.job_id=? ORDER BY r.rowid", (job_id,))]
        if row is None:
            raise WikiError("Maintenance job not found", 404)
        for receipt in receipts:
            receipt["details"] = json.loads(receipt["details"])
        return self.public(row) | job_events.metadata(db, job_id) | {
            "receipts": receipts, "selection_evidence": evidence(db, job_id), "grant": grants.view(db, job_id)}

    @staticmethod
    def summary(row, *, exact_key=False):
        """Navigation omits source material, instructions and individual receipts."""
        result = {key: row[key] for key in ("id", "request_key", "status", "session_id", "created_at", "updated_at")}
        result.update({key: row[key] for key in ("state_version", "attempt", "settlement_phase") if key in row.keys()})
        if not exact_key and len(result["request_key"]) > 1024:
            result["request_key"] = result["request_key"][:1024]
            result["request_key_truncated"] = True
        items = job_items.decode(row["items"])
        result["progress"] = {
            "total": len(items),
            "completed": sum(item["status"] == "done" for item in items),
            "attention": sum(item["status"] in {"attention", "unknown", "verifying", "excluded"} for item in items),
            "failed": sum(item["status"] == "failed" for item in items),
        }
        error = row.get("error") if isinstance(row, dict) else row["error"]
        if error:
            result["error"] = error[:1024]
            result["error_truncated"] = len(error) > 1024
        return result

    def list(self, offset=0, limit=30, *, view="full", idempotency_key=None, model_response=False):
        if offset < 0 or not 1 <= limit <= 100:
            raise WikiError("Invalid maintenance pagination", 422)
        if view not in {"compact", "full"}:
            raise WikiError("Invalid maintenance view", 422)
        columns = "j.*" if view == "full" else "j.id,j.request_key,j.status,j.error,j.session_id,j.created_at,j.updated_at,j.items"
        columns += ",s.state_version,s.attempt,s.settlement_phase"
        condition = ""
        parameters: list[str | int] = []
        if idempotency_key is not None:
            condition = " WHERE j.request_key=? OR j.id=(SELECT job_id FROM job_requests WHERE request_key=?)"
            parameters.extend((idempotency_key, idempotency_key))
        with self.store.connect() as db:
            rows = db.execute(
                f"SELECT {columns} FROM jobs j JOIN wiki_job_state s ON s.job_id=j.id{condition} ORDER BY j.created_at DESC LIMIT ? OFFSET ?",  # noqa: S608
                (*parameters, limit + 1, offset),
            ).fetchall()
        result = {
            "items": [(self.summary(row, exact_key=model_response) if view == "compact" else self.public(row)) for row in rows[:limit]],
            "next_offset": offset + limit if len(rows) > limit else None,
        }
        if model_response and view == "compact":
            from .model_response import page

            return page(result["items"], result, offset, offset + len(rows), lambda following: None)
        return result

    def enqueue(self, key, payload, items, *, task=None, mcp_scope=None, requester=None):
        creation = payload.get("creation")
        grants.creation_policy(task, creation)
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        digest = hashlib.sha256(raw.encode()).hexdigest()
        replayed = self.replay(key, payload.get("request", payload), task=task, mcp_scope=mcp_scope, requester=requester)
        if replayed is not None:
            return replayed
        job_id = uuid.uuid4().hex
        outputs = grants.targets(self.store, creation.get("paths") or [f"Library/Inbox/{job_id}-knowledge.md"]) if creation else []
        material = (grants.targets(self.store, [payload.get("target") or f"Library/Inbox/{job_id}.md"])[0]
                    if payload.get("material") and not items else None)
        with WikiHistory(self.store).locked(), registry._account_lock(self.user_id), registry.registration_lock(self.user_id), self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if registry.config(self.user_id) != self.registration:
                raise WikiError("Wiki registration changed before admission", 409)
            from .tasks import tasks

            effects = [item["path"] for item in items if item.get("path")]
            effects += [item["path"] for item in outputs]
            if material is not None:
                effects.append(material["path"])
            policy = tasks.require_submission(task, payload, self.store, db, mcp_scope, self.registration,
                                              actual_paths=effects)
            for item in items:
                if item.get("doc") and item.get("revision") and item.get("status") != "done":
                    current = db.execute("SELECT path,revision FROM documents WHERE id=?", (item["doc"],)).fetchone()
                    if not current or current["path"] != item["path"] or current["revision"] != item["revision"]:
                        raise WikiError("Selected document identity or revision changed before admission", 409)
            alias = db.execute("SELECT * FROM job_requests WHERE request_key=?", (key,)).fetchone()
            if alias:
                if alias["request_hash"] != self.request_hash(payload.get("request", payload)):
                    raise WikiError("Idempotency key already names a different submission", 409)
                previous_job = self.get(alias["job_id"], db=db)
                tasks.require_submission(task, payload, self.store, db, mcp_scope, self.registration,
                                         actual_paths=self.effect_paths(db, previous_job))
                self.subscribe(db, alias["job_id"], requester)
                return self.get(alias["job_id"], db=db)
            previous = db.execute(
                "SELECT * FROM jobs WHERE request_key=?", (key,)
            ).fetchone()
            if previous:
                if previous["payload_hash"] != digest:
                    raise WikiError(
                        "Idempotency key already names a different submission", 409
                    )
                previous_job = self.get(previous["id"], db=db)
                tasks.require_submission(task, payload, self.store, db, mcp_scope, self.registration,
                                         actual_paths=self.effect_paths(db, previous_job))
                self.subscribe(db, previous["id"], requester)
                return self.get(previous["id"], db=db)
            now = datetime.now(UTC).isoformat()
            db.execute(
                "INSERT INTO jobs(id,request_key,payload_hash,payload,status,items,error,session_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    job_id,
                    key,
                    digest,
                    raw,
                    "queued",
                    job_items.encode(items),
                    None,
                    None,
                    now,
                    now,
                ),
            )
            db.execute("INSERT INTO job_requests VALUES(?,?,?)", (key, self.request_hash(payload.get("request", payload)), job_id))
            created = grants.issue(db, self.registration, job_id, items, policy=policy,
                                   creation=creation, outputs=outputs, material=material)
            if created:
                db.execute("UPDATE jobs SET items=? WHERE id=?", (job_items.encode([*items, *created]), job_id))
            self.subscribe(db, job_id, requester)
        worker.wake(self.user_id)
        return self.get(job_id)

    def update(self, job_id, *, expected_status=None, **fields):
        permitted = {"status", "items", "error", "session_id"}
        if set(fields) - permitted:
            raise ValueError("Invalid job update")
        fields["updated_at"] = datetime.now(UTC).isoformat()
        if "items" in fields:
            fields["items"] = job_items.encode(fields["items"])
        with self.store.connect() as db:
            retained_error = (",attempt_errors=CASE WHEN error IS NULL THEN attempt_errors ELSE json_set(attempt_errors,"
                              "'$.\"'||(SELECT attempt FROM wiki_job_state WHERE job_id=jobs.id)||'\"',error) END"
                              if "error" in fields else "")
            db.execute(
                "UPDATE jobs SET "
                + ",".join(f"{key}=?" for key in fields)
                + retained_error
                + " WHERE id=?"
                + (" AND status=?" if expected_status else ""),
                [
                    *fields.values(),
                    job_id,
                    *([expected_status] if expected_status else []),
                ],
            )
        return self.get(job_id)

    def maintain(
        self, key, material, scope, paths, target, sources, actor, created_changes=None, *, review=None, selection_task=None,
        selection_id=None, selection_ordinal=None, mcp_scope: McpScope | None = None, creation=None, requester=None
    ):
        from .maintenance_selections import validate_pair

        validate_pair(selection_id, selection_ordinal, review)
        if requester is None and selection_task is not None:
            requester = job_events.from_task(selection_task)
        if review is not None:
            if creation is not None:
                raise WikiError("Checked daily maintenance cannot request new pages", 422)
            from .maintenance_admission import admit

            return admit(self, key, material, paths, target, sources, actor, created_changes or {}, review,
                         selection_task, selection_id=selection_id, selection_ordinal=selection_ordinal,
                         mcp_scope=mcp_scope, requester=requester)
        with WikiHistory(self.store).locked():
            return self._maintain(key, material, scope, paths, target, sources, actor, created_changes,
                                  task=selection_task, mcp_scope=mcp_scope, creation=creation, requester=requester)

    def _maintain(
        self, key, material, scope, paths, target, sources, actor, created_changes=None, *, task=None, mcp_scope=None, creation=None, requester=None
    ):
        created_changes = created_changes or {}
        if not key.strip() or not (material.strip() or paths or creation):
            raise WikiError(
                "Maintenance requires a key and material or explicit paths", 422
            )
        if material and paths:
            raise WikiError("Choose new material or explicitly selected files", 422)
        if not material and not paths and not creation:
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
        if creation is not None:
            from .contracts import CreationRequest

            creation = CreationRequest.model_validate(creation).model_dump()
            grants.creation_policy(task, creation)
            request["creation"] = creation
        previous = self.replay(key, request, task=task, mcp_scope=mcp_scope, requester=requester)
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
            require_markdown_maintenance(document["path"])
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
        if creation is not None:
            payload["creation"] = creation
        if material:
            with self.store.connect() as db:
                pending = db.execute("SELECT id,payload,items FROM jobs WHERE status IN ('queued','running','interrupted') ORDER BY created_at").fetchall()
            for job in pending:
                pending_payload = json.loads(job["payload"])
                eligible = (
                    pending_payload.get("kind") == "submission"
                    and (not pending_payload.get("target") or is_markdown_maintenance(pending_payload["target"]))
                    and all(is_markdown_maintenance(item["path"]) for item in job_items.decode(job["items"]))
                )
                if eligible and pending_payload.get("material") == material and pending_payload.get("creation") == creation:
                    return self.replay(key, request, task=task, mcp_scope=mcp_scope,
                                       requester=requester, alias_job=job["id"])
            reused = materials.find(self.store, material.encode(), markdown_only=True)
            if reused and creation is None:
                result = self.enqueue(key, payload, [{
                    "doc": reused["id"], "path": reused["path"], "revision": reused["revision"],
                    "quality": reused["quality"], "status": "done", "reused": True,
                    "output": reused["path"], "output_revision": reused["revision"],
                }], task=task, mcp_scope=mcp_scope, requester=requester)
                return self.update(result["id"], status="completed")
            if target and self.store.path(target).exists():
                raise WikiError("New-note target already exists with different material", 409)
        return self.enqueue(key, payload, selected, task=task, mcp_scope=mcp_scope, requester=requester)

    def curate(self, key, scope, *, requester=None, task=None, mcp_scope=None):
        from .maintenance_contract import actor_for

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
            with registry._account_lock(self.user_id), registry.registration_lock(self.user_id), self.store.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                if registry.config(self.user_id) != self.registration:
                    raise WikiError("Wiki registration changed before admission", 409)
                from .tasks import tasks

                tasks.require_submission(task, {"actor": actor_for(task)}, self.store, db, mcp_scope, self.registration,
                                         actual_paths=self.effect_paths(db, job))
                self.subscribe(db, job["id"], requester)
                return self.get(job["id"], db=db)
        self.store.refresh()
        with self.store.connect() as db:
            documents = db.execute(
                "SELECT d.* FROM documents d WHERE d.kind='original' AND " + MARKDOWN_SQL + " ORDER BY d.path"
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
            if is_markdown_maintenance(row["path"])
        ]
        return self.enqueue(
            key,
            {
                "kind": "curation",
                "scope": scope,
                "new_only": False,
                "actor": actor_for(task),
            },
            items,
            requester=requester,
            task=task,
            mcp_scope=mcp_scope,
        )

    def require_control(self, job, expected_attempt=None, expected_vault_id=None):
        if ((expected_attempt is not None and expected_attempt != job["attempt"])
                or expected_vault_id is not None and expected_vault_id != self.registration["vault_id"]):
            raise WikiError("The displayed maintenance attempt or vault changed; review current state before controlling it", 409)

    def mark_cancelled(self, job_id, *, expected_attempt=None, expected_vault_id=None):
        with registry._account_lock(self.user_id), registry.registration_lock(self.user_id), self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if registry.config(self.user_id) != self.registration:
                raise WikiError("Wiki registration changed before cancellation", 409)
            self.require_control(self.get(job_id, db=db), expected_attempt, expected_vault_id)
            db.execute(
                "UPDATE jobs SET status='cancelled',updated_at=? WHERE id=? AND status NOT IN ('completed','completed_with_attention','cancelled')",
                (datetime.now(UTC).isoformat(), job_id),
            )
            job = self.get(job_id, db=db)
            if job["status"] == "cancelled":
                for item in job["items"]:
                    if work_id := item.get("worker_request", {}).get("work_id"):
                        close_work(work_id)
        return self.get(job_id)

    async def cancel(self, job_id, manager, *, expected_attempt=None, expected_vault_id=None, operation_id=None):
        async def begin():
            job = await _io(self.mark_cancelled, job_id, expected_attempt=expected_attempt, expected_vault_id=expected_vault_id)
            return (WikiWorker.cancel_attempt(self, job, manager)
                    if job["status"] == "cancelled" and job["settlement_phase"] != "confirmed" else None)

        # A disconnected waiter cannot leave a committed cancellation ownerless.
        stopping = await complete_cleanup(begin())
        if stopping is not None:
            await asyncio.wait({stopping}, timeout=0.05)
            if stopping.done():
                stopping.result()
        return await _io(self.get, job_id)

    async def settle_cancelled(self, job_id, manager):
        """Explicit evidence verification joins the same original cancellation."""
        job = await _io(self.get, job_id)
        if job["status"] != "cancelled" or job["settlement_phase"] == "confirmed":
            return
        await asyncio.shield(WikiWorker.cancel_attempt(self, job, manager, verify=True))

    async def _finish_cancelled(self, job, manager, *, unprepared=False):
        job_id = job["id"]
        job = await _io(self.get, job_id)
        if job["settlement_phase"] == "confirmed":
            return
        try:
            await _io(self.require_retired, job, manager=manager)
        except WikiError as error:
            if error.status == 409:
                refs = job_items.references(job["items"])
                if refs and any(not ref.get("retired") and ref.get("phase") != "rejected" for ref in refs):
                    await _io(self._record_cancellation_phase, job, "attention", manager=manager)
                return
            raise
        refs = job_items.references(job["items"])
        unprepared = unprepared or not refs and not job["session_id"] and job["attempt"] == 0
        phase = "confirmed" if unprepared else "attention"
        confirmed = []
        for ref in refs:
            if ref.get("phase") == "rejected" and ref.get("disposition") in {"closed", "not_admitted"}:
                confirmed.append(True)
                continue
            session_id, request_id = ref.get("session_id"), ref.get("request_id")
            outcome = (await manager.db.get_committed_terminal(session_id, request_id)
                       if manager is not None and session_id and request_id else None)
            confirmed.append(outcome is not None and outcome.get("request_id") == request_id)
            if confirmed[-1] and job_execution.is_current(job, ref):
                await _io(job_execution.transition, self, job_id, ref, "settled", recording_state="committed", terminal=outcome)
        if refs and all(confirmed):
            phase = "confirmed"
        if unprepared and not refs:
            await _io(self._not_admitted, job)
            job = await _io(self.get, job_id)
        if not await _io(job_execution.reconcile_cancelled_effects, self, job, manager=manager):
            phase = "attention"
        # A confirmed cancellation includes release of its unused creation paths;
        # callers must not observe completion before this owner's cleanup ends.
        await _io(self.release_unused, job_id)
        await _io(self._record_cancellation_phase, job, phase, manager=manager)

    def _not_admitted(self, job):
        """The joined live owner knows that no request was ever prepared."""
        with registry._account_lock(self.user_id), registry.registration_lock(self.user_id), self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            current = self.get(job["id"], db=db)
            self.require_control(current, job["attempt"], self.registration["vault_id"])
            if current["status"] != "cancelled" or job_items.references(current["items"]):
                raise WikiError("Original cancellation changed before settlement", 409)
            known = {item.get("doc") for item in current["items"]}
            for receipt in current["receipts"]:
                if receipt["operation"] == "ingest" and receipt["doc_id"] not in known:
                    current["items"].append({"doc": receipt["doc_id"], "path": receipt["path"],
                                             "revision": receipt["output_revision"], "status": "pending"})
                    known.add(receipt["doc_id"])
            for item in current["items"]:
                item["worker_request"] = {"attempt": current["attempt"], "phase": "rejected", "disposition": "not_admitted"}
            db.execute("UPDATE jobs SET items=? WHERE id=?", (job_items.encode(current["items"]), current["id"]))

    def _record_cancellation_phase(self, job, phase, *, manager=None):
        if phase == "confirmed":
            self.require_retired(job, manager=manager)
        with registry._account_lock(self.user_id), registry.registration_lock(self.user_id), self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if registry.config(self.user_id) != self.registration:
                raise WikiError("Wiki registration changed before cancellation confirmation", 409)
            job_events.settlement(db, job["id"], job["attempt"], phase)

    def release_unused(self, job_id):
        """Release only unstarted paths after the stopped execution has retired."""
        if registry.config(self.user_id) != self.registration:
            return 0
        with self.store.connect() as db:
            grants.require(db)
            job = self.get(job_id, db=db)
            if (job["status"] not in {"cancelled", "failed", "interrupted", "completed", "completed_with_attention"}
                    or not db.execute("SELECT 1 FROM wiki_path_reservations WHERE job_id=? AND state='reserved' LIMIT 1", (job_id,)).fetchone()):
                return 0
        try:
            self.require_retired(job)
        except WikiError as error:
            if error.status == 409:
                return 0
            raise
        # Join existing writers, but do not run publication recovery merely to
        # release an unrelated unstarted path. Its original uncertainty stays put.
        with WikiHistory(self.store).locked(shared=True), registry._account_lock(self.user_id), registry.registration_lock(self.user_id):
            if registry.config(self.user_id) != self.registration:
                return 0
            with self.store.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                current = db.execute("SELECT status,session_id,updated_at FROM jobs WHERE id=?", (job_id,)).fetchone()
                if (current["status"], current["session_id"], current["updated_at"]) != (job["status"], job["session_id"], job["updated_at"]):
                    return 0
                self.require_retired(job)
                return db.execute("UPDATE wiki_path_reservations SET state='released' WHERE job_id=? AND state='reserved'", (job_id,)).rowcount

    def recover_jobs(self, *, previous_writers_retired=False):
        with self.store.connect() as db:
            grants.block_ungranted(db, self.registration)
            rows = db.execute("SELECT id,items,session_id,status FROM jobs").fetchall()
            for row in rows:
                items = job_items.decode(row["items"])
                before = copy.deepcopy(items)
                if previous_writers_retired:
                    for item in items:
                        for reference in [*item.get("previous_worker_requests", []), *([item["worker_request"]] if item.get("worker_request") else [])]:
                            reference["retired"] = True
                if row["status"] not in {"running", "interrupted"}:
                    if items != before:
                        db.execute("UPDATE jobs SET items=? WHERE id=?", (job_items.encode(items), row["id"]))
                    if row["status"] == "cancelled" and job_events.metadata(db, row["id"])["settlement_phase"] == "pending":
                        job_events.settlement(db, row["id"], job_events.metadata(db, row["id"])["attempt"], "attention")
                    continue
                for item in items:
                    reference = item.get("worker_request")
                    if item.get("status") in {"done", "excluded"}:
                        continue
                    if reference and reference.get("session_id") and reference.get("request_id") and (previous_writers_retired or reference.get("retired")):
                        item.update(status="verifying")
                    else:
                        item.update(status="unknown",
                                    error="Restart found no durable original execution identity; inspect the retained job before new work")
                db.execute(
                    "UPDATE jobs SET status='interrupted',error=coalesce(error,'Verifying original work after server restart'),items=? WHERE id=? AND status IN ('running','interrupted')",
                    (job_items.encode(items), row["id"]),
                )
        with self.store.connect() as db:
            stopped = [row[0] for row in db.execute("""SELECT DISTINCT j.id FROM jobs j
                JOIN wiki_path_reservations r ON r.job_id=j.id AND r.state='reserved'
                WHERE j.status IN ('cancelled','failed','completed','completed_with_attention')""")]
        for job_id in stopped:
            self.release_unused(job_id)

    def next_queued(self):
        with self.store.connect() as db:
            grants.block_ungranted(db, self.registration)
            row = db.execute(
                """UPDATE jobs SET status='running',error=NULL,
                attempt_errors=CASE WHEN error IS NULL THEN attempt_errors ELSE json_set(attempt_errors,
                  '$."'||(SELECT attempt FROM wiki_job_state WHERE job_id=jobs.id)||'"',error) END
                WHERE id=(SELECT id FROM jobs WHERE status IN ('queued','interrupted') ORDER BY created_at LIMIT 1)
                  AND status IN ('queued','interrupted') RETURNING id"""
            ).fetchone()
        return row[0] if row else None

    def ingest(self, job_id):
        history = WikiHistory(self.store)
        with history.locked(), registry._account_lock(self.user_id), registry.registration_lock(self.user_id):
            if registry.config(self.user_id) != self.registration:
                raise WikiError("Wiki registration changed before material ingestion", 409)
            job = self.get(job_id)
            if job["status"] == "cancelled":
                return None
            payload = job["payload"]
            path = payload.get("target") or f"Library/Inbox/{job_id}.md"
            require_markdown_maintenance(path)
            existing = materials.find(self.store, payload["material"].encode(), markdown_only=True)
            if existing:
                with self.store.connect() as db:
                    own_creation = db.execute("""SELECT 1 FROM changes c JOIN change_retention r ON r.change_id=c.id
                      WHERE c.path=? AND c.reason=? AND c.status='applied' AND c.before_hash IS NULL
                        AND r.document_id=?""", (existing["path"], f"Ingest job {job_id}", existing["id"])).fetchone()
                with self.store.connect() as db:
                    grants.bind_document(db, job_id, existing["id"], existing["path"], existing["revision"], "material")
                return existing | {"reused": not bool(own_creation)}
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
                from .material_proof import ingest

                with self.store.connect() as db:
                    reservation = db.execute("SELECT id FROM wiki_path_reservations WHERE job_id=? AND purpose='framework_material' AND state='reserved'", (job_id,)).fetchone()
                if not reservation:
                    raise WikiError("Material creation needs verification of its admitted reservation", 409)
                history.write_locked(
                    path,
                    payload["material"].encode(),
                    None,
                    payload["actor"],
                    f"Ingest job {job_id}",
                    origin_builder=ingest(job_id),
                    operation="ingest",
                    publication={"job_id": job_id, "session_id": None, "source_doc": None,
                                 "source_path": path, "input_revision": None,
                                 "reservation_id": reservation[0], "framework_material": True},
                )
            return self.store.read(path, length=1)

    def require_retired(self, job, *, manager=None):
        """A terminal database row cannot stand in for execution retirement."""
        job_execution.require_retired(self.store, job, manager=manager)

    def readmit(self, job_id, paths, creation=None, *, expected_attempt=None, expected_vault_id=None, operation_id=None):
        """Owner-only re-admission preserves the old job, key, payload and receipts."""
        from .contracts import GrantReadmissionRequest

        intent = GrantReadmissionRequest(paths=paths, creation=creation).model_dump()
        intent = {key: intent[key] for key in ("paths", "creation")}
        paths = list(dict.fromkeys(intent["paths"]))
        creation = intent["creation"]
        encoded = json.dumps(intent, ensure_ascii=False, sort_keys=True)
        request_hash = hashlib.sha256(encoded.encode()).hexdigest()
        control = job_controls.identity(operation_id, "readmit", intent | {
            "expected_attempt": expected_attempt, "expected_vault_id": expected_vault_id,
        })
        with WikiHistory(self.store).locked(), registry._account_lock(self.user_id), registry.registration_lock(self.user_id):
            if registry.config(self.user_id) != self.registration:
                raise WikiError("Wiki registration changed before scope re-admission", 409)
            job = self.get(job_id)
            with self.store.connect() as db:
                if job_controls.replay(db, job_id, control):
                    return job
            self.require_control(job, expected_attempt, expected_vault_id)
            if job["settlement_phase"] in {"pending", "attention"}:
                raise WikiError("Confirm the original cancellation and result recording before re-admission", 409)
            with self.store.connect() as db:
                accepted = db.execute("SELECT request_hash FROM wiki_grant_readmissions WHERE job_id=? AND registration_generation=?",
                                      (job_id, self.registration["registration_generation"])).fetchone()
                if accepted:
                    if accepted[0] != request_hash:
                        raise WikiError("This job was already re-admitted with a different finite scope", 409)
                    return job
            if job["status"] in {"running", "queued", "completed"}:
                raise WikiError("Only stopped work awaiting scope review can be re-admitted", 409)
            if job["grant"] is not None and job["grant"]["registration_generation"] == self.registration["registration_generation"]:
                raise WikiError("This job already has a current grant; use retry", 409)
            self.require_retired(job)
            payload = job["payload"]
            selected = []
            material_target = None
            material = payload.get("material", "")
            if material:
                if paths:
                    raise WikiError("Material re-admission retains its original input; do not select other existing files", 422)
                if job["grant"]:
                    with self.store.connect() as db:
                        recorded = db.execute("SELECT path,output_revision,change_id FROM wiki_path_reservations WHERE job_id=? AND purpose='framework_material' AND state IN ('prepared','blocked')", (job_id,)).fetchone()
                    if recorded:
                        self.store.read(recorded["path"], length=1, expected_revision=recorded["output_revision"])
                        with self.store.connect() as db:
                            db.execute("BEGIN IMMEDIATE")
                            grants.settle_creation(db, self.store, recorded["change_id"])
                        job = self.get(job_id)
                reused = materials.find(self.store, material.encode(), markdown_only=True)
                if reused:
                    selected = [{"doc": reused["id"], "path": reused["path"], "revision": reused["revision"],
                                 "quality": reused["quality"], "status": "pending"}]
                else:
                    material_target = grants.targets(self.store, [payload.get("target") or f"Library/Inbox/{job_id}.md"])[0]
            else:
                if not paths and not creation:
                    raise WikiError("Select a finite set of existing notes or explicit new pages", 422)
                for path in paths:
                    current = self.store.read(path, length=1)
                    if not grants._literal_target(current["path"], existing=True):
                        raise WikiError("Re-admission is limited to exact Library Markdown targets", 422)
                    selected.append({"doc": current["id"], "path": current["path"], "revision": current["revision"],
                                     "quality": current["quality"], "status": "pending"})
            previous_reservations = job["grant"]["reservations"] if job["grant"] else []
            existing_creations = []
            reacquire = []
            if creation and job["grant"]:
                requested = creation.get("paths") or [f"Library/Inbox/{job_id}-knowledge.md"]
                by_path = {row["path"]: row for row in previous_reservations}
                for path in requested:
                    reservation = by_path.get(path)
                    item = next((item for item in job["items"] if item.get("reservation_id") == (reservation or {}).get("id")), None)
                    if reservation is None or reservation["state"] == "bound" or item is None:
                        raise WikiError("Re-admission must retain an original unfinished creation target", 409)
                    existing_creations.append(dict(item, status="pending"))
                    if reservation["state"] == "released":
                        prepared = grants.targets(self.store, [path])[0]
                        if prepared["path_key"] != reservation["path_key"]:
                            raise WikiError("The original physical creation target changed before re-admission", 409)
                        reacquire.append(reservation)
                outputs: list[dict[str, str]] = []
            else:
                outputs = grants.targets(self.store, creation.get("paths") or [f"Library/Inbox/{job_id}-knowledge.md"]) if creation else []
            with self.store.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                current = db.execute("SELECT status,error,items,session_id FROM jobs WHERE id=?", (job_id,)).fetchone()
                if (current["status"] != job["status"] or current["error"] != job["error"]
                        or current["session_id"] != job["session_id"] or job_items.decode(current["items"]) != job["items"]):
                    raise WikiError("The retained job changed before scope re-admission", 409)
                self.require_retired(job)
                for item in selected:
                    observed = db.execute("SELECT path,revision FROM documents WHERE id=?", (item["doc"],)).fetchone()
                    if not observed or (observed["path"], observed["revision"]) != (item["path"], item["revision"]):
                        raise WikiError("A selected identity changed before scope re-admission", 409)
                header = db.execute("SELECT * FROM wiki_job_grants WHERE job_id=?", (job_id,)).fetchone()
                if header is None:
                    created = grants.issue(db, self.registration, job_id, [] if material else selected, policy="owner-readmission",
                                           creation=creation, outputs=outputs, material=material_target)
                    if material:
                        for item in selected:
                            grants.bind_document(db, job_id, item["doc"], item["path"], item["revision"], "material")
                else:
                    if header["vault_id"] != self.registration["vault_id"]:
                        raise WikiError("Re-admission cannot transfer work to another vault", 409)
                    if material_target:
                        reserved = db.execute("SELECT * FROM wiki_path_reservations WHERE job_id=? AND purpose='framework_material' AND path=? AND state IN ('reserved','released')",
                                              (job_id, material_target["path"])).fetchone()
                        if not reserved:
                            raise WikiError("The original material reservation needs verification", 409)
                        if reserved["path_key"] != material_target["path_key"]:
                            raise WikiError("The original material target changed before re-admission", 409)
                        if reserved["state"] == "released":
                            reacquire.append(dict(reserved))
                    allowed = {row[0] for row in db.execute("SELECT doc_id FROM wiki_grant_documents WHERE job_id=?", (job_id,))}
                    if {item["doc"] for item in selected} - allowed:
                        raise WikiError("The retained grant does not name these identities; submit a new explicit task", 409)
                    created = existing_creations
                grants.reacquire(db, reacquire)
                db.execute("INSERT INTO wiki_grant_readmissions VALUES(?,?,?,?,?,?)", (
                    job_id, self.registration["registration_generation"], request_hash, encoded, job["error"], datetime.now(UTC).isoformat()))
                WikiHistory(self.store).preserve_candidate_owners(db, job)
                for item in job["items"]:
                    self.retain_item_result(item)
                completed = [item for item in job["items"] if item.get("status") == "done"]
                done_ids = {item.get("doc") for item in completed}
                selected = [item for item in selected if item["doc"] not in done_ids]
                # Keep original execution evidence attached to the same identity.
                retained = {item["doc"]: item for item in job["items"] if item.get("doc")}
                for item in selected:
                    prior = retained.get(item["doc"], {})
                    if prior.get("revision") != item["revision"] and prior.get("revision"):
                        prior.setdefault("previous_revisions", []).append(prior["revision"])
                    item.update(prior | item)
                    if job_items.unresolved(item):
                        item["status"] = "verifying"
                    elif prior.get("status") == "unknown":
                        item["status"] = "unknown"
                included = {job_execution.item_key(item) for item in [*completed, *selected, *created]}
                excluded = [item for item in job["items"] if job_execution.item_key(item) not in included]
                for item in excluded:
                    # Finite re-admission removes this item from dispatch, while
                    # its original request and result remain inspectable.
                    if item["status"] == "pending" and not job_items.unresolved(item):
                        item["status"] = "excluded"
                        if item.get("reservation_id"):
                            db.execute("UPDATE wiki_path_reservations SET state='released' WHERE id=? AND state='reserved'",
                                       (item["reservation_id"],))
                items = [*completed, *selected, *created, *excluded]
                recovery = any(job_items.unresolved(item) for item in items if item["status"] != "done")
                db.execute("UPDATE jobs SET status=?,error=NULL,session_id=?,items=?,attempt_errors=?,updated_at=? WHERE id=?",
                           ("interrupted" if recovery else "queued", job["session_id"] if recovery else None,
                            job_items.encode(items), self.retain_error(job), datetime.now(UTC).isoformat(), job_id))
                job_controls.record(db, job_id, control)
        worker.wake(self.user_id)
        return self.get(job_id)

    def retry(self, job_id, *, expected_attempt=None, expected_vault_id=None, operation_id=None):
        control = job_controls.identity(operation_id, "retry", {
            "expected_attempt": expected_attempt, "expected_vault_id": expected_vault_id,
        })
        with WikiHistory(self.store).locked(), registry._account_lock(self.user_id), registry.registration_lock(self.user_id):
            if registry.config(self.user_id) != self.registration:
                raise WikiError("Wiki registration changed before retry", 409)
            job = self.get(job_id)
            with self.store.connect() as db:
                if job_controls.replay(db, job_id, control):
                    return job
            self.require_control(job, expected_attempt, expected_vault_id)
            return self._retry(job_id, control)

    def _retry(self, job_id, control=None):
        job = self.get(job_id)
        if job["status"] == "completed":
            return job
        if job["settlement_phase"] in {"pending", "attention"}:
            raise WikiError("Confirm the original cancellation and result recording before retry", 409)
        if any(item["status"] == "unknown" for item in job["items"]):
            raise WikiError("Inspect the original unknown execution; a new explicit task is required for new work", 409)
        if grants.scope_blocked(job.get("error")) or job["grant"] is None:
            raise WikiError("Complete trusted scope re-admission before retrying this job", 409)
        if job["grant"]["registration_generation"] != self.registration["registration_generation"]:
            raise WikiError("Complete trusted scope re-admission for the current registration", 409)
        if job["status"] in {"running", "queued", "completed"}:
            raise WikiError(
                "Only cancelled, failed, interrupted or attention jobs can be retried",
                409,
            )
        self.require_retired(job)
        with self.store.connect() as db:
            released = [dict(row) for row in db.execute("SELECT * FROM wiki_path_reservations WHERE job_id=? AND state='released'", (job_id,))]
        pending = {item["reservation_id"] for item in job["items"]
                   if item.get("kind") == "creation" and item["status"] not in {"done", "excluded"}}
        needs_material = bool(job["payload"].get("material") and not any(item.get("kind") != "creation" for item in job["items"]))
        reacquire = [row for row in released if row["id"] in pending or row["purpose"] == "framework_material" and needs_material]
        for row in reacquire:
            target = grants.targets(self.store, [row["path"]])[0]
            if target["path_key"] != row["path_key"]:
                raise WikiError("The original physical creation target changed before retry", 409)
        reacquired_ids = {row["id"] for row in reacquire}
        for item in job["items"]:
            self.retain_item_result(item)
            if item["status"] not in {"done", "excluded"}:
                if job_items.unresolved(item):
                    item.update(status="verifying")
                    continue
                if item.get("kind") == "creation":
                    if item["reservation_id"] in reacquired_ids or not self.settle_item(job_id, item):
                        item.update(status="pending")
                        item.pop("error", None)
                    continue
                if item["status"] == "verifying" or self.publication(job_id, item["doc"]):
                    item.update(status="verifying")
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
                    if (item.get("worker_request", {}).get("disposition") == "not_admitted"
                            and current["revision"] == item["revision"]):
                        # The retiring owner proved this attempt never entered
                        # a model. Unchanged accepted input needs no reread.
                        item.update(status="pending", quality=current["quality"])
                        item.pop("error", None)
                        continue
                    if current["revision"] != item["revision"]:
                        item.setdefault("previous_revisions", []).append(
                            item["revision"]
                        )
                        item["revision"] = current["revision"]
                    item["retry_count"] = item.get("retry_count", 0) + 1
                    item["quality"] = current["quality"]
                item.pop("error", None)
                item["status"] = "pending"
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute("SELECT status,error,session_id,updated_at FROM jobs WHERE id=?", (job_id,)).fetchone()
            if tuple(current) != (job["status"], job["error"], job["session_id"], job["updated_at"]):
                raise WikiError("The retained maintenance job changed before retry", 409)
            self.require_retired(job)
            header = db.execute("SELECT * FROM wiki_job_grants WHERE job_id=?", (job_id,)).fetchone()
            if not header or grants.current_generation(db, header) != self.registration["registration_generation"]:
                raise WikiError("The retained grant changed before retry", 409)
            grants.reacquire(db, reacquire)
            recovery = any(item["status"] == "verifying" for item in job["items"])
            db.execute("UPDATE jobs SET status=?,items=?,error=NULL,session_id=?,attempt_errors=?,updated_at=? WHERE id=?",
                       ("interrupted" if recovery else "queued", job_items.encode(job["items"]),
                        job["session_id"] if recovery else None, self.retain_error(job), datetime.now(UTC).isoformat(), job_id))
            job_controls.record(db, job_id, control)
        worker.wake(self.user_id)
        return self.get(job_id)

    @staticmethod
    def retain_error(job):
        """The job owns diagnostics even when admission never produced a ref."""
        errors = dict(job["attempt_errors"])
        if job["error"] is not None:
            errors[str(job["attempt"])] = job["error"]
        return json.dumps(errors, ensure_ascii=False)

    @staticmethod
    def retain_item_result(item):
        if reference := item.get("worker_request"):
            reference.setdefault("item_result", {key: value for key, value in item.items()
                                                  if key not in {"worker_request", "previous_worker_requests"}})

    async def verify(self, job_id, manager, *, expected_attempt=None, expected_vault_id=None, operation_id=None):
        """Reconcile the captured effects and indexes without admitting model work."""
        control = job_controls.identity(operation_id, "verify", {
            "expected_attempt": expected_attempt, "expected_vault_id": expected_vault_id,
        })
        job = await _io(self.get, job_id)
        original = self.verification_input(job)
        with self.store.connect() as db:
            if job_controls.replay(db, job_id, control):
                return job
        self.require_control(job, expected_attempt, expected_vault_id)
        if job["status"] in {"running", "queued"}:
            raise WikiError("Stop the original maintenance work before verifying its effects", 409)
        await _io(self.require_retired, job, manager=manager)
        await _io(self.store.refresh)
        if job["status"] == "cancelled":
            await self.settle_cancelled(job_id, manager)
            job = await _io(self.get, job_id)
            original = self.verification_input(job)
        else:
            verifier = WikiWorker()
            verifier.manager = manager
            for item in job["items"]:
                if item["status"] == "excluded":
                    continue
                if item.get("worker_request", {}).get("request_id"):
                    await verifier._recover_recorded_item(self, job_id, item)
                elif item["status"] not in {"pending", "done"} or await _io(self.publication, job_id, item.get("doc")):
                    await _io(self.settle_item, job_id, item)
        return await _io(self._commit_verification, job, control, original)

    @staticmethod
    def verification_input(job):
        return json.dumps({"attempt": job["attempt"], "status": job["status"], "grant": job["grant"],
                           "items": [{key: value for key, value in item.items() if key != "worker_request"}
                                     for item in job["items"]]}, sort_keys=True)

    def _commit_verification(self, job, control, original):
        with WikiHistory(self.store).locked(), registry._account_lock(self.user_id), registry.registration_lock(self.user_id), self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if registry.config(self.user_id) != self.registration:
                raise WikiError("Wiki registration changed before effect verification", 409)
            current = self.get(job["id"], db=db)
            if job_controls.replay(db, job["id"], control):
                return current
            self.require_control(current, job["attempt"], self.registration["vault_id"])
            self.require_retired(current)
            if self.verification_input(current) != original:
                raise WikiError("Original work or authority changed during verification; inspect current state", 409)
            if current["status"] in {"running", "queued"}:
                raise WikiError("The original work changed during verification", 409)
            if current["status"] != "cancelled":
                status = "completed" if job["items"] and all(item["status"] == "done" for item in job["items"]) else "completed_with_attention"
                db.execute("UPDATE jobs SET items=?,status=?,updated_at=? WHERE id=?",
                           (job_items.encode(job["items"]), status, datetime.now(UTC).isoformat(), job["id"]))
            job_controls.record(db, job["id"], control)
            return self.get(job["id"], db=db)

    def conflicts(self, job_id, document_id):
        history = WikiHistory(self.store)
        with self.store.connect() as db:
            rows = db.execute("""SELECT p.change_id AS id,p.operation,r.document_id FROM change_proposals p
              JOIN change_retention r ON r.change_id=p.change_id
              WHERE json_extract(p.operation,'$.job_id')=?
                AND (json_extract(p.operation,'$.source_doc')=?
                  OR (json_extract(p.operation,'$.source_doc') IS NULL AND r.document_id=?))
                AND r.resolved_at IS NULL""", (job_id, document_id, document_id)).fetchall()
            return [row["id"] for row in rows if history.candidate_recovery_error(db, row, json.loads(row["operation"])) is None]

    def resolve_conflicts(self, job_id, document_id):
        history = WikiHistory(self.store)
        with history.locked():
            history.resolve_proposals(self.conflicts(job_id, document_id))

    def has_conflict(self, job_id, document_id):
        return bool(self.conflicts(job_id, document_id))

    def publication(self, job_id, document_id, reference=None):
        exact = ""
        parameters = ()
        if reference and reference.get("request_id"):
            exact = " AND json_extract(r.details,'$.execution.request_id')=? AND json_extract(r.details,'$.execution.session_id')=? AND json_extract(r.details,'$.execution.attempt')=?"
            parameters = (reference["request_id"], reference.get("session_id"), reference.get("attempt"))
        with self.store.connect() as db:
            receipt = db.execute("""SELECT r.id AS receipt_id,r.job_id,r.item_doc AS source_doc,
                coalesce(json_extract(r.details,'$.input_source_revision'),p.input_revision,r.input_revision) AS input_revision,r.path AS output_path,r.output_revision,r.change_id,
                coalesce(p.indexed,0) AS indexed,coalesce(c.status,'applied') AS change_status,
                r.doc_id AS output_doc,r.operation FROM operation_receipts r
                LEFT JOIN changes c ON c.id=r.change_id
                LEFT JOIN job_publications p ON p.change_id=r.change_id
                WHERE r.job_id=? AND r.item_doc=? AND r.operation IN (""" + ",".join("?" for _ in MAINTENANCE_OPERATIONS)
                + ")" + exact + " ORDER BY r.rowid DESC LIMIT 1", (job_id, document_id, *MAINTENANCE_OPERATIONS, *parameters)).fetchone()
            if receipt:
                return receipt
            if exact:
                return None
            return db.execute("SELECT p.*,c.status AS change_status,r.document_id AS output_doc FROM job_publications p JOIN changes c ON c.id=p.change_id JOIN change_retention r ON r.change_id=c.id WHERE p.job_id=? AND p.source_doc=? ORDER BY p.rowid DESC LIMIT 1", (job_id, document_id)).fetchone()

    def recover_publication(self, job_id, item):
        """Recognize our durable publication before treating it as input drift."""
        history = WikiHistory(self.store)
        with history.locked():
            published = self.publication(job_id, item["doc"], item.get("worker_request"))
            if not published:
                return False
            item.update(input_revision=published["input_revision"], output=published["output_path"],
                        output_revision=published["output_revision"], output_change_id=published["change_id"],
                        index_complete=False)
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
                output = self.store.read(published["output_doc"], length=1)
                item["output"] = output["path"]
                if output["revision"] != published["output_revision"]:
                    raise WikiError("Published output changed; review the existing publication", 409)
                if published["output_doc"] != item["doc"]:
                    self.store.read(item["doc"], length=1, expected_revision=published["input_revision"])
                if output["id"] != published["output_doc"]:
                    raise WikiError("Published output identity was replaced", 409)
                if operation in {"save", "organize"} and output["stale"]:
                    raise WikiError("Published output or its cited sources changed; review the existing publication", 409)
                with self.store.connect() as db:
                    self.store.require_indexed(db, output["path"])
                    db.execute("UPDATE job_publications SET indexed=1 WHERE change_id=?", (published["change_id"],))
            except WikiError as exc:
                item.update(status="attention", index_complete=False,
                            output_revision=published["output_revision"],
                            output_change_id=published["change_id"], error=str(exc))
                return True
            item.update(status="done", input_revision=published["input_revision"], output=output["path"], output_revision=published["output_revision"], output_change_id=published["change_id"], index_complete=True)
            item.pop("error", None)
            self.resolve_conflicts(job_id, item["doc"])
            return True

    def settle_item(self, job_id, item, terminal=None):
        """One rule for normal completion, cancellation and startup verification."""
        recovered = (self.recover_creation(job_id, item) if item.get("kind") == "creation"
                     else self.recover_publication(job_id, item))
        reference = item.get("worker_request") or {}
        if reference.get("request_id"):
            terminal = terminal or reference.get("terminal")
            if (reference.get("phase") != "settled" or reference.get("recording_state") != "committed"
                    or not terminal or terminal.get("request_id") != reference["request_id"]):
                item.update(status="attention", error="Original execution terminal is not verified; saved effects are retained without rerunning the model")
                return recovered
            if terminal.get("status") != "success":
                item.update(status="attention", error=terminal.get("message") or terminal.get("code") or "Original execution did not complete successfully; saved effects are retained")
                return recovered
        if not recovered:
            item.update(status="attention", error="Original execution has no matching durable effect receipt; inspect without regenerating content")
        return recovered

    def recover_creation(self, job_id, item):
        """Verify the original create operation; never draft its content again."""
        with WikiHistory(self.store).locked():
            with self.store.connect() as db:
                row = db.execute("SELECT * FROM wiki_path_reservations WHERE id=? AND job_id=?",
                                 (item["reservation_id"], job_id)).fetchone()
            if row is None:
                item.update(status="attention", error="The exact creation reservation is unavailable")
                return True
            if row["state"] == "reserved":
                return False
            if row["state"] == "released":
                item.update(status="attention", error="The creation path must be reserved again by trusted re-admission")
                return True
            try:
                if row["state"] != "bound":
                    self.store.read(row["path"], length=1, expected_revision=row["output_revision"])
                    with self.store.connect() as db:
                        db.execute("BEGIN IMMEDIATE")
                        grants.settle_creation(db, self.store, row["change_id"])
                        state = db.execute("SELECT state FROM wiki_path_reservations WHERE id=?", (row["id"],)).fetchone()[0]
                        if state != "bound":
                            raise WikiError("The original creation did not reach a verified applied state", 409)
                output = self.store.read(row["document_id"], length=1)
                if output["stale"]:
                    raise WikiError("Created output or its cited sources changed; review the existing publication", 409)
                reference = item.get("worker_request") or {}
                exact = ""
                parameters = ()
                if reference.get("request_id"):
                    exact = " AND json_extract(r.details,'$.execution.request_id')=? AND json_extract(r.details,'$.execution.session_id')=? AND json_extract(r.details,'$.execution.attempt')=?"
                    parameters = (reference["request_id"], reference.get("session_id"), reference.get("attempt"))
                with self.store.connect() as db:
                    self.store.require_indexed(db, output["path"])
                    # Only B's original creation or a later primary review of B
                    # can settle it. Auxiliary navigation receipts prove neither.
                    receipt = db.execute("""SELECT r.*,c.status AS change_status,p.indexed FROM operation_receipts r
                        LEFT JOIN changes c ON c.id=r.change_id LEFT JOIN job_publications p ON p.change_id=r.change_id
                        WHERE r.job_id=? AND r.doc_id=? AND r.output_revision=?
                          AND r.item_doc=? AND r.operation IN (""" + ",".join("?" for _ in MAINTENANCE_OPERATIONS) + """)
                          AND ((r.change_id=? AND json_extract(r.details,'$.creation_reservation_id')=?)
                            OR json_extract(r.details,'$.input_source_doc')=?)
                          AND (r.change_id IS NULL OR c.status='applied')
                          AND (r.change_id=? OR p.indexed=1 OR r.change_id IS NULL)""" + exact + " ORDER BY r.rowid DESC LIMIT 1",
                        (job_id, row["document_id"], output["revision"], row["document_id"], *MAINTENANCE_OPERATIONS,
                         row["change_id"], row["id"], row["document_id"], row["change_id"], *parameters)).fetchone()
                if receipt is None or output["id"] != row["document_id"]:
                    raise WikiError("The created identity or its recorded output changed; verify without recreating it", 409)
            except WikiError as exc:
                item.update(status="attention", index_complete=False, error=str(exc))
                return True
            item.update(status="done", doc=output["id"], output=output["path"], output_revision=output["revision"],
                        input_revision=receipt["input_revision"], receipt_id=receipt["id"],
                        output_change_id=receipt["change_id"], operation=receipt["operation"],
                        index_complete=True)
            item.pop("error", None)
            return True

    def require_maintenance_health(self, identifier):
        from .index_state import maintenance_blockers
        from .maintenance_projection import readiness

        with self.store.connect() as db:
            db.execute("BEGIN")
            if readiness(db, self.store)["index_status"] not in {"ready", "partial"}:
                raise WikiError("Current index must settle before worker admission", 503)
            if maintenance_blockers(db, [identifier]):
                raise WikiError("The target or its current source chain needs repair before maintenance", 409)

    def reread(self, job_id, item, error, *, retryable=True):
        if job_items.unresolved(item):
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
            current, raw = self.store.read_snapshot(item["doc"], length=1)
            candidate = history.record(current["path"], raw, raw, "curator-worker", error, "conflict", document_id=current["id"], proposal={
                "job_id": job_id, "source_doc": current["id"], "source_path": current["path"], "expected_revision": item["revision"],
                "source_revision": current["revision"],
                "operation": "reread-and-organize", "reason": error,
            })
            item.setdefault("candidate_changes", []).append(candidate["id"])
            item.setdefault("input_revision", item["revision"])
            item["path"] = current["path"]
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
    RETRY_INITIAL_SECONDS = 5.0
    RETRY_MAX_SECONDS = 60.0
    executing: dict[tuple[str, str], object] = {}
    cancellations: dict[tuple[str, str, int], asyncio.Task] = {}

    @classmethod
    def cancel_attempt(cls, jobs, job, manager, *, verify=False, owner=None):
        key = (str(jobs.store.state.resolve()), job["id"], job["attempt"])
        stopping = cls.cancellations.get(key)
        if stopping is None or verify and stopping.done():
            owner = owner or cls.executing.get(key[:2])
            stopping = asyncio.create_task(cls._cancel_attempt(jobs, job, manager, owner, verify=verify),
                                          name=f"wiki-cancel-{job['id']}-{job['attempt']}")
            cls.cancellations[key] = stopping
            # Retrieve failures even if every HTTP waiter disconnects. Evidence
            # stays pending and an explicit verification can report the error.
            def observed(task):
                if not task.cancelled() and (error := task.exception()) is not None:
                    logger.error("Wiki cancellation failed for job=%s", job["id"],
                                 exc_info=(type(error), error, error.__traceback__))
            stopping.add_done_callback(observed)
        return stopping

    @staticmethod
    async def _cancel_attempt(jobs, job, manager, owner, *, verify=False):
        try:
            if not verify and manager is not None:
                await job_execution.cancel_requests(manager, job_items.references(job["items"]))
        finally:
            if isinstance(owner, JobOwner):
                await owner.retired.wait()
        jobs = await _io(job_execution.control_jobs, jobs)
        current = await _io(jobs.get, job["id"])
        jobs.require_control(current, job["attempt"], jobs.registration["vault_id"])
        unprepared = (isinstance(owner, JobOwner) and owner.entered_attempt == current["attempt"]
                      and not job_items.references(current["items"]))
        await jobs._finish_cancelled(current, manager, unprepared=unprepared)

    def __init__(self):
        self.manager = None
        self.loop = None
        self.tasks: dict[str | None, asyncio.Task] = {}
        self.pending: set[str | None] = set()
        self._stopping = False
        self.notice_task: asyncio.Task | None = None

    async def recover(self):
        """Recover configured vaults before any curator or notice receiver runs."""
        for user_id, state in registry.states():
            if (state / "config.json").is_file():
                try:
                    await _io(WikiJobs, user_id)
                except WikiError:
                    # Invalid registration or document evidence blocks this
                    # vault, while its authenticated diagnostic remains usable.
                    # Storage failures enter ServiceHealth before being wrapped.
                    logger.exception("Wiki recovery blocked for account=%s; inspect its registration and recovery diagnostics", user_id)

    async def start(self, manager, *, paused=False):
        if self._stopping and (self.tasks or self.notice_task is not None):
            raise RuntimeError("Wiki curator teardown remains unconfirmed")
        if self.notice_task is not None:
            raise RuntimeError("Wiki notification receiver is already running")
        self._stopping = False
        await self.recover()
        self.manager = manager
        self.loop = asyncio.get_running_loop()
        if not paused:
            self.start_producing()

    def start_producing(self):
        if self._stopping or self.manager is None or self.notice_task is not None:
            raise RuntimeError("Wiki worker is not prepared for startup")
        from .job_notifications import run as receive_notices

        self.notice_task = self.loop.create_task(receive_notices(self.manager), name="wiki-job-notifications")
        waiting = set(self.pending)
        states = registry.states()
        for user_id, state in states:
            if (state / "config.json").is_file():
                waiting.add(user_id)
        for user_id in waiting:
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
        if self.notice_task is not None and self.manager is not None and (
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
        if self.notice_task is not None and not self.notice_task.done() and not self.notice_task.cancelling():
            self.notice_task.cancel("shutdown")

    async def stop(self, *, timeout: float = 8.0) -> None:
        """Join captured owners without cancelling their shielded cleanup."""
        self.begin_shutdown()
        tasks = (*self.tasks.values(), *([self.notice_task] if self.notice_task is not None else []),
                 *(task for task in self.cancellations.values() if not task.done()))
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
        self.notice_task = None
        self.pending.clear()
        self.manager = None
        self.loop = None
        for key, task in list(self.cancellations.items()):
            if task.done():
                self.cancellations.pop(key)

    async def run(self, user_id):
        retry_delay = self.RETRY_INITIAL_SECONDS
        unfinished = None
        while not self._stopping:
            self.pending.discard(user_id)
            try:
                # Control-plane state must use the live registration. Content
                # processing below keeps its captured Store and grants.
                jobs = await _io(WikiJobs, user_id)
                if unfinished is not None:
                    await self._finish_failed_job(user_id, *unfinished)
                    unfinished = None
                job_id = await self._claim_job(jobs)
            except WikiError:
                logger.exception("Wiki worker blocked for account=%s; inspect its registration and recovery diagnostics", user_id)
                return
            except Exception:
                logger.exception("Wiki worker unavailable for account=%s; retrying in %.1fs", user_id, retry_delay)
                await asyncio.sleep(retry_delay)
                retry_delay = min(retry_delay * 2, self.RETRY_MAX_SECONDS)
                continue
            retry_delay = self.RETRY_INITIAL_SECONDS
            if job_id is None:
                if user_id in self.pending:
                    continue
                return
            try:
                await self.process(jobs, job_id)
            except asyncio.CancelledError:
                try:
                    await self._finish_failed_job(user_id, job_id, "interrupted", "Worker stopped; retry resumes unfinished items")
                except Exception:
                    logger.exception("Wiki interruption recording unavailable for account=%s job=%s; startup recovery required", user_id, job_id)
                raise
            except Exception as exc:
                unfinished = (job_id, "interrupted" if self._stopping else "failed", f"{type(exc).__name__}: {exc}")
                if self._stopping:
                    await self._finish_failed_job(user_id, *unfinished)
                    raise

    async def _claim_job(self, jobs):
        # A cancelled thread join still has to retain the committed claim's
        # identity. Otherwise shutdown can leave a job permanently running.
        claim = asyncio.create_task(_io(jobs.next_queued))
        try:
            return await asyncio.shield(claim)
        except asyncio.CancelledError:
            async def settle_claim():
                job_id = await claim
                if job_id is not None:
                    await self._finish_failed_job(jobs.user_id, job_id, "interrupted", "Worker stopped while claiming this job; retry resumes unfinished items")
            try:
                await complete_cleanup(settle_claim())
            except Exception:
                logger.exception("Wiki claim interruption recording unavailable for account=%s; startup recovery required", jobs.user_id)
            raise

    async def _finish_failed_job(self, user_id, job_id, status, error):
        jobs = await _io(WikiJobs, user_id)
        job = await _io(jobs.get, job_id)
        # A cancellation already committed through the current Store wins.
        if job["status"] == "running":
            await _io(jobs.update, job_id, expected_status="running", status=status, error=error)
        if job["session_id"]:
            session = self.manager.get_session(job["session_id"])
            if session is not None:
                await complete_cleanup(self.manager._await_backend_retirement(session))
                original = {ref["request_id"] for item in job["items"]
                            if (ref := item.get("worker_request"))
                            and ref.get("session_id") == session.id}
                # A legacy job without an exact original request cannot
                # safely retire a session now used by a human correction.
                if len(original) == 1:
                    await self.manager.archive_session(session.id, replace=False, only_request_id=original.pop())
        await _io(jobs.release_unused, job_id)

    async def process(self, jobs, job_id):
        key = (str(jobs.store.state.resolve()), job_id)
        if key in self.executing:
            raise WikiError("The previous maintenance execution has not retired", 409)
        owner = JobOwner()
        self.executing[key] = owner
        try:
            owner.entered_attempt = (await _io(jobs.get, job_id))["attempt"]
            return await self._process(jobs, job_id)
        finally:
            if self.executing.get(key) is owner:
                self.executing.pop(key)
            owner.retired.set()
            jobs = await _io(job_execution.control_jobs, jobs)
            current = await _io(jobs.get, job_id)
            if current["status"] == "cancelled":
                async def finish_cancellation():
                    await asyncio.shield(self.cancel_attempt(jobs, current, self.manager, owner=owner))
                await complete_cleanup(finish_cancellation())
            else:
                await _io(jobs.release_unused, job_id)

    async def _process(self, jobs, job_id):
        config = registry.config(jobs.user_id)
        if config != jobs.registration:
            raise WikiError("Wiki registration changed; retry with the current binding", 409)
        agent = await self.manager.db.get_agent(
            config["curator_agent_id"], jobs.user_id
        )
        if agent is None:
            raise WikiError("Configured curator is no longer available", 409)
        job = await _io(jobs.get, job_id)
        payload = job["payload"]
        if job["status"] == "cancelled":
            return
        if (job["grant"] is None or job["grant"]["registration_generation"] != config["registration_generation"]
                or job["grant"]["vault_id"] != config["vault_id"]):
            await _io(jobs.update, job_id, status="failed", session_id=None,
                      error=json.dumps({"code": "scope_review_required", "reason": "A current trusted V2 grant is required"}))
            return
        if payload["kind"] == "submission" and payload["material"]:
            if not any(item.get("kind") != "creation" for item in job["items"]):
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
            batch = [i for i in job["items"] if i["status"] in {"pending", "verifying"}][:3]
            if not batch:
                status = (
                    "completed_with_attention"
                    if any(i["status"] in {"failed", "attention", "unknown", "excluded"} for i in job["items"])
                    else "completed"
                )
                await _io(
                    jobs.update,
                    job_id,
                    expected_status="running",
                    status=status,
                    session_id=None,
                    **({"error": None} if status == "completed" else {}),
                )
                return
            briefing = []
            readable = []
            for item in batch:
                if item["status"] == "verifying" and item.get("worker_request", {}).get("request_id"):
                    await self._recover_recorded_item(jobs, job_id, item)
                    continue
                if item.get("kind") == "creation":
                    if await _io(jobs.recover_creation, job_id, item):
                        await _io(jobs.settle_item, job_id, item)
                        continue
                    readable.append(item)
                    briefing.append({key: item[key] for key in ("kind", "path", "reservation_id")})
                    continue
                if await _io(jobs.recover_publication, job_id, item):
                    await _io(jobs.settle_item, job_id, item)
                    continue
                if item["status"] == "verifying":
                    item.update(status="attention", error="Publication recovery receipt is unavailable; review without regenerating the note")
                    continue
                try:
                    require_markdown_maintenance(item["path"])
                    doc = await _io(jobs.store.read, item["doc"], length=1)
                    require_markdown_maintenance(doc["path"])
                    await _io(jobs.require_maintenance_health, doc["id"])
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
            request_id = uuid.uuid4().hex
            session = await self.manager.create_session(
                config["curator_agent_id"],
                name=f"Wiki 维护 {job_id[:8]}",
                # The worker owns this session through grant retirement and
                # publication verification. A schedule origin lets the generic
                # dispatcher archive it as soon as its execution receipt ends.
                origin="wiki",
                backend=agent.get("backend") or settings.default_backend,
            )
            try:
                reference = await _io(job_execution.prepare, jobs, job, readable, session.id, request_id)
            except BaseException as error:
                close_work(f"wiki-{request_id}")
                await self.manager.archive_session(session.id, replace=False, only_request_id=request_id)
                if isinstance(error, WikiError) and error.status == 409:
                    current_jobs = await _io(job_execution.control_jobs, jobs)
                    current = await _io(current_jobs.get, job_id)
                    if current["status"] == "cancelled" and current["attempt"] == job["attempt"]:
                        return
                raise
            from .tasks import tasks as grants

            grants.assign(session.id, job_id, readable, registration=config,
                          request_id=reference["request_id"], attempt=reference["attempt"])
            prompt = (
                "这是已明确授权的持久维护任务。按本次共享维护与写作指南执行。\n"
                + "用户要求: "
                + payload["scope"]
                + "\n正文与新页仅限这些目标；已有导航页可用 relations-only organize 补本批入口，其他关联页按需读取:\n"
                + json.dumps(briefing, ensure_ascii=False)
                + "\n重读原因与已用重读次数:\n"
                + json.dumps([{key: item.get(key) for key in ("path", "retry_reason", "retry_count")} for item in readable], ensure_ascii=False)
                + "\n本队列没有人等待同步答复；以实际发布产物结束。"
            )
            receipt = None
            completion = None
            try:
                receipt = await self.manager.start_message(
                    session.id, prompt, source="wiki", wiki_mode="maintain",
                    request_id=reference["request_id"], delivery_id=f"wiki:{reference['request_id']}",
                    work_id=reference["work_id"],
                )
                if receipt.request_id != reference["request_id"]:
                    raise WikiError("Worker admission returned a different request identity", 409)
                await _io(job_execution.transition, jobs, job_id, reference,
                          "accepted" if receipt.accepted else "rejected", disposition=receipt.disposition)
                for item in readable:
                    item["worker_request"] = dict(reference)
                completion = await receipt.wait()
                job_execution.require_completion(reference, completion)
                if completion.recording_state == "committed" and completion.outcome is not None:
                    await _io(job_execution.transition, jobs, job_id, reference, "settled", recording_state="committed",
                              terminal=completion.outcome.model_dump())
                    for item in readable:
                        item["worker_request"] = dict(reference)
                elif receipt.accepted:
                    await _io(job_execution.transition, jobs, job_id, reference, "unknown",
                              recording_state=completion.recording_state)
            except BaseException as error:
                close_work(reference["work_id"])
                if receipt is not None and receipt.accepted:
                    cancellation = {"cause": "shutdown"} if (
                        isinstance(error, asyncio.CancelledError) and error.args == ("shutdown",)
                    ) else {}
                    try:
                        await complete_cleanup(self.manager.cancel_request(
                            session.id, reference["request_id"], **cancellation,
                        ))
                    except SessionOwnerUnavailable:
                        # The original writer has ended. Its receipt below
                        # still owns the result; finally joins its resources.
                        pass
                    if receipt.request_id != reference["request_id"]:
                        await _io(job_execution.transition, jobs, job_id, reference, "unknown")
                        raise
                    completion = await receipt.wait()
                    job_execution.require_completion(reference, completion)
                    if completion.state in {"recording_failed", "unavailable"}:
                        await self._mark_recording_failure(jobs, job_id, reference, completion)
                    else:
                        if completion.recording_state == "committed" and completion.outcome is not None:
                            await _io(job_execution.transition, jobs, job_id, reference, "settled", recording_state="committed",
                                      terminal=completion.outcome.model_dump())
                        await self.manager.archive_session(session.id, replace=False, only_request_id=reference["request_id"])
                elif receipt is None:
                    await _io(job_execution.transition, jobs, job_id, reference, "unknown")
                raise
            finally:
                # Stop remains fast; the grants owner waits for captured old
                # process retirement before removing its authority.
                await complete_cleanup(job_execution.retire(self.manager, reference))
                close_work(reference["work_id"])
                grants.unassign(session.id)
                await _io(job_execution.transition, jobs, job_id, reference, reference["phase"], retired=True)
            if completion.state == "recording_failed" or receipt.accepted and completion.recording_state != "committed":
                await self._mark_recording_failure(jobs, job_id, reference, completion)
                continue
            errors = []
            if completion.outcome is None:
                errors.append(completion.reason or "Maintenance request was not executed")
            elif completion.outcome.status != "success":
                errors.append(completion.outcome.message or completion.outcome.code or "Maintenance execution failed")
            await self.manager.archive_session(session.id, replace=False, only_request_id=reference["request_id"])
            if (await _io(jobs.get, job_id))["status"] == "cancelled":
                return
            for item in readable:
                item["worker_request"] = dict(reference)
                if await _io(jobs.settle_item, job_id, item):
                    continue
                if item.get("kind") == "creation":
                    continue
                # A rejected publication can be reread within the existing
                # finite budget. Applied or uncertain effects never enter here.
                try:
                    current = await _io(jobs.store.read, item["doc"], length=1)
                except WikiError:
                    continue
                conflict = await _io(jobs.has_conflict, job_id, item["doc"])
                if current["revision"] != item["revision"] or conflict:
                    await _io(jobs.reread, job_id, item, "; ".join(errors) or item["error"])
            await _io(
                jobs.update,
                job_id,
                expected_status="running",
                items=job["items"],
                session_id=None,
            )

    async def _mark_recording_failure(self, jobs, job_id, reference, completion):
        reference = dict(reference)
        job_execution.require_completion(reference, completion)
        # This reference is control metadata, not authority to publish through
        # a replacement binding. Preserve it even if the captured Store retired.
        current_jobs = await _io(job_execution.control_jobs, jobs)
        await _io(job_execution.transition, current_jobs, job_id, reference, "unknown",
                  recording_state=completion.recording_state)

    async def _recover_recorded_item(self, jobs, job_id, item):
        """Storage/publication verification only; never start another model."""
        reference = item["worker_request"]
        session_id, request_id = reference["session_id"], reference["request_id"]
        session = self.manager.get_session(session_id)
        if not reference.get("retired"):
            item.update(status="attention", error="Original execution retirement is not proven; content was not regenerated")
            return
        outcome = await self.manager.db.get_committed_terminal(session_id, request_id)
        if outcome is None or outcome.get("request_id") != request_id:
            item.update(status="attention", error="Original execution result is not committed; content was not regenerated")
            return
        reference = dict(reference)
        current = await _io(jobs.get, job_id)
        if job_execution.is_current(current, reference):
            await _io(job_execution.transition, jobs, job_id, reference, "settled", recording_state="committed", terminal=outcome)
        else:
            # A retained original request supplies evidence, never admission or
            # a state transition in the replacement maintenance attempt.
            reference.update(phase="settled", recording_state="committed", terminal=outcome)
        item["worker_request"] = reference
        await _io(jobs.settle_item, job_id, item, outcome)
        if session is not None:
            await self.manager.archive_session(session_id, replace=False, only_request_id=request_id)


worker = WikiWorker()
