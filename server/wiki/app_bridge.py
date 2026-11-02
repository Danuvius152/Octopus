"""Observe existing Sapphire mutations under the same cross-process lock."""

import asyncio
import fcntl
import json
import logging
import re
import uuid
from pathlib import Path

import httpx
from starlette.datastructures import QueryParams

from ..aio import complete_cleanup
from .history import WikiHistory, atomic, history_text, sync_directory
from .registry import registry
from .storage import file_effect
from .store import WikiError

_pending: dict[asyncio.Task, str | None] = {}
_preparations: dict[asyncio.Task, str | None] = {}
_active: set = set()
_stopping = False
_ALL = object()
_blocked_owners: set[str | None] = set()
logger = logging.getLogger(__name__)


def drain_blockers() -> tuple[str, ...]:
    if _pending or _preparations or _active:
        return ("preparation, file effect or unconfirmed journal cleanup",)
    if any((state / "external-change.json").exists() for _owner, state in registry.states()):
        return ("unconfirmed external-change journal",)
    return ()


def begin_shutdown() -> None:
    global _stopping
    _stopping = True


async def shutdown(*, timeout: float = 5.0, user_id=_ALL) -> None:
    """Join mutation owners while retaining any unconfirmed cleanup."""
    # The application supervisor has stopped its processes before this call.
    global _stopping
    if user_id is _ALL:
        _stopping = True
    else:
        _blocked_owners.add(user_id)
    tasks = tuple(task for task, owner in (_pending | _preparations).items()
                  if user_id is _ALL or owner == user_id)
    for task in tasks:
        if not task.done() and not task.cancelling():
            task.cancel("shutdown")
    done, pending = await asyncio.wait(tasks, timeout=max(0.0, timeout)) if tasks else (set(), set())
    if pending:
        names = ", ".join(sorted(task.get_name() for task in pending))
        raise RuntimeError(f"Wiki application writer teardown remains unconfirmed: {names}")
    failures = [
        error for task in done
        if not task.cancelled() and (error := task.exception()) is not None
    ]
    if failures:
        raise RuntimeError("Wiki application writer teardown remains unconfirmed") from failures[0]
    for task in tasks:
        _pending.pop(task, None)
    active = [mutation for mutation in _active if user_id is _ALL or mutation.user_id == user_id]
    if active:
        results = await asyncio.gather(*(mutation.finish() for mutation in active), return_exceptions=True)
        if any(isinstance(result, BaseException) for result in results):
            raise RuntimeError("Wiki application journal teardown remains unconfirmed")


def reopen(user_id=_ALL) -> None:
    global _stopping
    if user_id is not _ALL:
        if (any(mutation.user_id == user_id for mutation in _active)
                or user_id in _pending.values()
                or user_id in _preparations.values()):
            raise RuntimeError("Wiki application writers have not retired")
        _blocked_owners.discard(user_id)
        return
    if _active or _preparations or _pending:
        raise RuntimeError("Wiki application writers have not retired")
    _stopping = False
    _blocked_owners.clear()


class AppMutation:
    def __init__(self, history, handle, before, journal, actor, reason, body=None, borrow=None, lease=None, user_id=None):
        self.history, self.handle, self.before = history, handle, before
        self.journal, self.actor, self.reason = journal, actor, reason
        self.body = body or {}
        self.borrow, self.lease = borrow, lease
        self.user_id = user_id
        self._finish_lock = asyncio.Lock()
        _active.add(self)

    async def forward(self, app_id, request):
        """Own dispatch, receipt interpretation and the original client transfer."""
        client = None
        dispatched = transferred = False
        response = None
        try:
            client = httpx.AsyncClient(timeout=httpx.Timeout(30.0, read=300.0))
            dispatched = True
            response = await client.send(request, stream=True)
            await response.aread()
            await response.aclose()
            # aread decodes; validators for the compressed representation no
            # longer describe these bytes. Ordinary proxy streams remain raw.
            if response.headers.pop("content-encoding", None):
                for header in ("etag", "content-md5", "digest", "content-digest", "repr-digest"):
                    response.headers.pop(header, None)
            endpoint = request.url.path.removeprefix("/api/")
            receipt = None
            if response.status_code == 202 or endpoint == "delete" and response.is_success:
                try:
                    receipt = response.json()
                except ValueError as error:
                    raise WikiError("Application returned an invalid mutation receipt", 502) from error
                if not isinstance(receipt, dict):
                    raise WikiError("Application returned an invalid mutation receipt", 502)
            if endpoint == "save" and response.status_code == 409:
                await self.conflict(self.body)
            if endpoint == "delete" and response.is_success:
                await self.receipt(receipt)
            if response.status_code == 202:
                job_id = receipt.get("id")
                if not isinstance(job_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", job_id):
                    raise WikiError("Application returned no valid async job receipt", 502)
                self.follow_job(client, str(request.url.copy_with(path="/", query=None)).rstrip("/"), job_id, app_id)
                transferred = True
            else:
                await self.finish()
            return response
        except BaseException:
            if not transferred:
                if dispatched:
                    await complete_cleanup(self.abort(app_id))
                else:
                    await complete_cleanup(self.finish())
            raise
        finally:
            if not transferred:
                async def close():
                    try:
                        if response is not None:
                            await response.aclose()
                    finally:
                        if client is not None:
                            await client.aclose()
                await complete_cleanup(close())

    async def finish(self):
        async with self._finish_lock:
            await complete_cleanup(self._finish_owned())

    async def _finish_owned(self):
        if self.handle.closed:
            return
        try:
            await complete_cleanup(asyncio.to_thread(self._finish))
        finally:
            fcntl.flock(self.handle, fcntl.LOCK_UN)
            self.handle.close()
            if self.borrow is not None:
                self.borrow.__exit__(None, None, None)
            _active.discard(self)

    async def abort(self, app_id):
        from ..app_backends import backend_supervisor

        await backend_supervisor.stop(app_id)
        await self.finish()

    async def conflict(self, body):
        async with self._finish_lock:
            if self.handle.closed:
                return
            await self._conflict_owned(body)

    async def _conflict_owned(self, body):
        relative = body.get("path")
        content = body.get("content")
        if isinstance(relative, str) and isinstance(content, str):
            path = self.history.store.path(relative)

            def record_conflict():
                with self.history.under_existing_lock(self.handle, self.lease):
                    if self.before is not None:
                        self._require_journal()
                    return self.history.record(
                        relative,
                        path.read_bytes() if path.is_file() else None,
                        content.encode(),
                        self.actor,
                        "Sapphire save revision conflict",
                        "conflict",
                    )

            await complete_cleanup(asyncio.to_thread(record_conflict))

    def _finish(self):
        with self.history.under_existing_lock(self.handle, self.lease):
            if self.before is not None:
                self._journal_changes()
            self.history.store.synchronize()

    def _journal_changes(self):
        self._require_journal()
        self.history.finish_external(self.before)
        self._require_journal()
        with file_effect(self.history.store.health):
            self.journal.unlink(missing_ok=True)
            sync_directory(self.journal.parent)

    def _require_journal(self):
        self.history.read_external(expected=self.before)

    async def receipt(self, payload):
        async with self._finish_lock:
            if self.handle.closed:
                return
            await self._receipt_owned(payload)

    async def _receipt_owned(self, payload):
        if self.before is None or self.reason != "delete":
            return

        def remember():
            with self.history.under_existing_lock(self.handle, self.lease):
                self._require_journal()
                trash_id = payload.get("id")
                if isinstance(trash_id, str) and re.fullmatch(r"[a-f0-9]{32}", trash_id):
                    self.before["trash_id"] = trash_id
                    with file_effect(self.history.store.health):
                        atomic(self.journal, json.dumps(self.before).encode())

        await complete_cleanup(asyncio.to_thread(remember))

    def follow_job(self, client, base, job_id, app_id):
        async def wait():
            terminal = False
            failures = 0
            try:
                while True:
                    try:
                        response = await client.get(f"{base}/api/jobs/{job_id}")
                        response.raise_for_status()
                        payload = response.json()
                        if payload.get("id", job_id) != job_id:
                            raise ValueError("Sapphire returned another job's state")
                        state = payload.get("status")
                        if state in {"done", "error"}:
                            terminal = True
                            break
                        if state != "running":
                            raise ValueError("Unrecognized Sapphire job status")
                        failures = 0
                    except Exception:
                        failures += 1
                        if failures >= 6:
                            logger.warning(
                                "Stopping app %s: Git job %s cannot be supervised",
                                app_id,
                                job_id,
                            )
                            break
                    await asyncio.sleep(0.5)
            finally:

                async def cleanup():
                    try:
                        if terminal:
                            await self.finish()
                        else:
                            await self.abort(app_id)
                    finally:
                        await client.aclose()

                await complete_cleanup(cleanup())

        task = asyncio.create_task(wait(), name=f"wiki-app-{job_id}")
        _pending[task] = self.user_id

        def finished(task):
            if not task.cancelled() and (error := task.exception()) is not None:
                logger.error(
                    "Wiki application journal failed", exc_info=error
                )
                return  # Shutdown must still report this unconfirmed journal.
            _pending.pop(task, None)

        task.add_done_callback(finished)


async def begin(app_row, method, endpoint, query, body=b""):
    task = asyncio.create_task(_begin(app_row, method, endpoint, query, body), name="wiki-app-prepare")
    _preparations[task] = app_row.get("user_id")
    try:
        return await complete_cleanup(_join_preparation(task))
    except asyncio.CancelledError:
        if task.done() and not task.cancelled() and task.exception() is None:
            mutation = task.result()
            if mutation is not None:
                await complete_cleanup(mutation.finish())
        raise
    finally:
        _preparations.pop(task, None)


async def forward(app_row, target, request):
    """Return a loaded managed receipt, or leave ordinary forwarding to proxy."""
    from ..app_backends import backend_supervisor

    mutation = await begin(app_row, request.method, request.url.path.removeprefix("/api/"),
                           QueryParams(request.url.query), request.content)
    if mutation is None:
        return None
    try:
        backend_supervisor.require_running(app_row["id"], target)
    except BaseException:
        await complete_cleanup(mutation.finish())
        raise
    return await mutation.forward(app_row["id"], request)


async def _join_preparation(task):
    return await task


async def _begin(app_row, method, endpoint, query, body=b""):
    if method in {"GET", "HEAD", "OPTIONS"} or endpoint not in {
        "save",
        "upload",
        "rename",
        "delete",
        "restore",
        "git",
    }:
        return None
    user_id = app_row.get("user_id")
    state = registry.state(user_id)
    if not (state / "config.json").is_file():
        return None
    config = registry.config(user_id)
    if config.get("app_id") != app_row["id"]:
        return None
    if _stopping or user_id in _blocked_owners:
        raise WikiError("Wiki application writes are stopped during service transition", 503)
    # Sapphire's parse_qs drops blanks and selects the first remaining vault.
    # Missing/all-blank vaults are rejected downstream, without a default vault.
    vault = next((value for value in QueryParams(query).getlist("vault") if value), None)
    if vault is None:
        return None
    # Sapphire can contain other vaults; this bridge covers only the registered root.
    expected = Path(app_row["app_dir"] + ".data") / "vaults" / vault / "repo"
    if expected.resolve() != Path(config["root"]).resolve():
        return None
    history = await complete_cleanup(asyncio.to_thread(lambda: WikiHistory(registry.store(user_id))))
    borrow = history.store.borrow()
    lease = borrow.__enter__()
    handle = None
    try:
        handle = (state / "vault.lock").open("a+b")
        while True:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                await asyncio.sleep(0.05)
        journal = state / "external-change.json"
        if _stopping or user_id in _blocked_owners:
            raise WikiError("Wiki application writes are stopped during service transition", 503)

        def prepare():
            with history.under_existing_lock(handle, lease):
                if journal.exists():
                    raise WikiError("An earlier application mutation needs explicit recovery before another write", 409)
                try:
                    payload = json.loads(body)
                    if not isinstance(payload, dict):
                        payload = {}
                except (ValueError, UnicodeError):
                    payload = {}
                paths = []
                restored = None
                trash_root = Path(app_row["app_dir"] + ".data") / "trash" / str(vault)
                trash_id = payload.get("id")
                if endpoint == "git":
                    paths = None if payload.get("action") in {"pull", "sync"} else []
                elif endpoint == "restore":
                    if isinstance(trash_id, str) and re.fullmatch(
                        r"[a-f0-9]{32}", trash_id
                    ):
                        info = trash_root / trash_id / "info.json"
                        if info.is_file():
                            relative = json.loads(info.read_text()).get("path")
                            if isinstance(relative, str) and history_text(relative):
                                history.store.path(relative)
                                paths.append(relative)
                                with history.store.connect() as db:
                                    row = db.execute(
                                        "SELECT * FROM app_trash WHERE trash_id=?",
                                        (trash_id,),
                                    ).fetchone()
                                    restored = dict(row) if row else None
                else:
                    for key in ("path", "to") if endpoint == "rename" else ("path",):
                        relative = payload.get(key)
                        if isinstance(relative, str) and history_text(relative):
                            history.store.path(relative)
                            paths.append(relative)
                if paths == []:
                    return None, payload
                entry = {
                    "id": uuid.uuid4().hex,
                    "version": 2,
                    "source_binding_protocol": 1,
                    "paths": paths,
                    "before": history.capture_external(paths),
                    "actor": f"app:{app_row['id']}",
                    "reason": endpoint,
                    "restored": restored,
                    "trash_id": trash_id,
                    "trash_root": str(trash_root),
                }
                with file_effect(history.store.health):
                    atomic(journal, json.dumps(entry).encode())
                return entry, payload

        # Cancelling an await does not stop a worker thread. Join preparation
        # before releasing its lock, including its final journal rename.
        before, payload = await complete_cleanup(asyncio.to_thread(prepare))
        return AppMutation(
            history, handle, before, journal, f"app:{app_row['id']}", endpoint, payload, borrow, lease, user_id
        )
    except BaseException:
        if handle is not None:
            handle.close()
        borrow.__exit__(None, None, None)
        raise
