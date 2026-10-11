"""Observe existing Sapphire mutations under the same cross-process lock."""

import asyncio
import fcntl
import json
import logging
from pathlib import Path

import anyio

from .history import WikiHistory, atomic
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
    def __init__(self, history, handle, before, journal, actor, reason):
        self.history, self.handle, self.before = history, handle, before
        self.journal, self.actor, self.reason = journal, actor, reason

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
            await asyncio.to_thread(
                lambda: self.history.record(
                    relative,
                    path.read_bytes() if path.is_file() else None,
                    content.encode(),
                    self.actor,
                    "Sapphire save revision conflict",
                    "conflict",
                )
            )

    def _finish(self):
        with self.history.under_existing_lock(self.handle):
            self._journal_changes()
            self.history.store.refresh()

    def _journal_changes(self):
        after = self.history.inventory()
        self.history.record_external(self.before, after, self.actor, self.reason)
        self.journal.unlink(missing_ok=True)

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


async def begin(app_row, method, endpoint, query):
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
            history.recover()
            before = history.inventory()
            atomic(
                journal,
                json.dumps(
                    {
                        "before": before,
                        "actor": f"app:{app_row['id']}",
                        "reason": endpoint,
                    }
                ).encode(),
            )
            return before

        # Cancelling an await does not stop a worker thread. Join preparation
        # before releasing its lock, including its final journal rename.
        before = await complete_cleanup(asyncio.to_thread(prepare))
        return AppMutation(
            history, handle, before, journal, f"app:{app_row['id']}", endpoint
        )
    except BaseException:
        handle.close()
        raise
