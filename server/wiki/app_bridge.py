"""Observe existing Sapphire mutations under the same cross-process lock."""

import asyncio
import fcntl
import json
import logging
import re
import uuid
from pathlib import Path

import anyio

from .history import WikiHistory, atomic, history_text
from .registry import registry

_pending: set[asyncio.Task] = set()
logger = logging.getLogger(__name__)


async def complete_cleanup(awaitable):
    """A disconnected client must not interrupt writer termination or journaling."""
    task = asyncio.create_task(awaitable)
    cancelled = False
    # ASGI uses level-triggered AnyIO cancellation; shielding asyncio alone
    # allows the surrounding scope to cancel every subsequent cleanup await.
    with anyio.CancelScope(shield=True):
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                cancelled = True
    result = task.result()
    if cancelled:
        raise asyncio.CancelledError
    return result


async def shutdown():
    # The application supervisor has stopped its processes before this call.
    tasks = list(_pending)
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


class AppMutation:
    def __init__(self, history, handle, before, journal, actor, reason, body=None):
        self.history, self.handle, self.before = history, handle, before
        self.journal, self.actor, self.reason = journal, actor, reason
        self.body = body or {}

    async def finish(self):
        if self.handle.closed:
            return
        try:
            await asyncio.to_thread(self._finish)
        finally:
            fcntl.flock(self.handle, fcntl.LOCK_UN)
            self.handle.close()

    async def abort(self, app_id):
        from ..app_backends import backend_supervisor

        await backend_supervisor.stop(app_id)
        await self.finish()

    async def conflict(self, body):
        relative = body.get("path")
        content = body.get("content")
        if isinstance(relative, str) and isinstance(content, str):
            path = self.history.store.path(relative)

            def record_conflict():
                with self.history.under_existing_lock(self.handle):
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
        with self.history.under_existing_lock(self.handle):
            if self.before is not None:
                self._journal_changes()
            self.history.store.synchronize()

    def _journal_changes(self):
        self.history.finish_external(self.before)
        self.journal.unlink(missing_ok=True)

    async def receipt(self, payload):
        if self.before is None or self.reason != "delete":
            return

        def remember():
            trash_id = payload.get("id")
            if isinstance(trash_id, str) and re.fullmatch(r"[a-f0-9]{32}", trash_id):
                self.before["trash_id"] = trash_id
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
                        state = response.json().get("status")
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
        _pending.add(task)

        def finished(task):
            _pending.discard(task)
            if not task.cancelled() and task.exception():
                logger.error(
                    "Wiki application journal failed", exc_info=task.exception()
                )

        task.add_done_callback(finished)


async def begin(app_row, method, endpoint, query, body=b""):
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
    # Sapphire can contain other vaults; this bridge covers only the registered root.
    vault = query.get("vault")
    if not vault:
        file = Path(app_row["app_dir"] + ".data") / "registry.json"
        if file.is_file():
            vaults = json.loads(file.read_text()).get("vaults", [])
            vault = vaults[0].get("id") if vaults else None
    expected = Path(app_row["app_dir"] + ".data") / "vaults" / (vault or "") / "repo"
    if expected.resolve() != Path(config["root"]).resolve():
        return None
    history = await asyncio.to_thread(lambda: WikiHistory(registry.store(user_id)))
    handle = (state / "vault.lock").open("a+b")
    try:
        while True:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                await asyncio.sleep(0.05)
        journal = state / "external-change.json"

        def prepare():
            with history.under_existing_lock(handle):
                history.recover()
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
                    "paths": paths,
                    "before": history.capture_external(paths),
                    "actor": f"app:{app_row['id']}",
                    "reason": endpoint,
                    "restored": restored,
                    "trash_id": trash_id,
                    "trash_root": str(trash_root),
                }
                atomic(journal, json.dumps(entry).encode())
                return entry, payload

        # Cancelling an await does not stop a worker thread. Join preparation
        # before releasing its lock, including its final journal rename.
        before, payload = await complete_cleanup(asyncio.to_thread(prepare))
        return AppMutation(
            history, handle, before, journal, f"app:{app_row['id']}", endpoint, payload
        )
    except BaseException:
        handle.close()
        raise
