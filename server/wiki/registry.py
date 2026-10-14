"""Account-owned vault registrations; no discovery or content on ordinary turns."""

from __future__ import annotations

import asyncio
import fcntl
import json
import logging
import os
import tempfile
import threading
import uuid
from collections import OrderedDict
from pathlib import Path
from typing import Any

from ..aio import complete_cleanup
from ..config import settings
from ..workspace import user_root
from .store import WikiError, WikiStore

logger = logging.getLogger(__name__)


class WikiRegistry:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._stores: OrderedDict[tuple, WikiStore] = OrderedDict()
        self._task: asyncio.Task | None = None
        self._stopping: asyncio.Event | None = None
        self._account_locks: dict[str, threading.RLock] = {}
        self._blocked: set[str | None] = set()
        self._retired_stores: dict[WikiStore, str | None] = {}
        self.legacy_enabled = True

    def _account_lock(self, user_id):
        key = str(self.state(user_id).resolve())
        with self._lock:
            return self._account_locks.setdefault(key, threading.RLock())

    def states(self):
        states = [(None, self.state(None))] if self.legacy_enabled else []
        states.extend((path.parent.name, path) for path in Path(settings.users_root).expanduser().glob("*/wiki"))
        return [(user_id, state) for user_id, state in states if user_id not in self._blocked]

    def block(self, user_id):
        with self._account_lock(user_id), self._lock:
            self._blocked.add(user_id)
            captured = [self._stores.pop(key) for key in list(self._stores) if key[0] == user_id]
            captured.extend(store for store, owner in self._retired_stores.items() if owner == user_id)
        for store in captured:
            store.close()

    def unblock(self, user_id):
        with self._account_lock(user_id):
            self._blocked.discard(user_id)

    def state(self, user_id: str | None) -> Path:
        return (
            user_root(user_id) / "wiki"
            if user_id
            else Path(settings.wiki_dir).expanduser()
        )

    def config(self, user_id: str | None) -> dict[str, Any]:
        path = self.state(user_id) / "config.json"
        if not path.is_file():
            raise WikiError("No local wiki is registered for this account", 404)
        try:
            config = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(config, dict) or set(config) - {"root", "vault_id", "curator_agent_id", "app_id"}:
                raise ValueError("unsupported configuration structure")
            if not isinstance(config.get("root"), str) or not Path(config["root"]).is_absolute():
                raise ValueError("root must be an absolute path")
            if not Path(config["root"]).is_dir():
                raise ValueError("vault root is unavailable")
            if not isinstance(config.get("curator_agent_id"), str) or not config["curator_agent_id"]:
                raise ValueError("curator binding is missing")
            if config.get("app_id") is not None and not isinstance(config["app_id"], str):
                raise ValueError("invalid application binding")
            uuid.UUID(config["vault_id"])
            return config
        except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
            raise WikiError(f"Wiki registration is invalid; configuration retained: {exc}", 503) from exc

    def configure(
        self,
        user_id: str | None,
        root: Path,
        curator_agent_id: str,
        app_id: str | None = None,
    ) -> dict[str, Any]:
        with self._account_lock(user_id):
            if user_id in self._blocked or (user_id is None and not self.legacy_enabled):
                raise WikiError("Wiki registration is unavailable during account transition", 503)
            state = self.state(user_id)
            state.mkdir(parents=True, exist_ok=True, mode=0o700)
            with (state / "config.lock").open("a+b") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                try:
                    return self._configure(user_id, root, curator_agent_id, app_id)
                finally:
                    fcntl.flock(lock, fcntl.LOCK_UN)

    def _configure(self, user_id, root, curator_agent_id, app_id):
        if not root.is_dir():
            raise WikiError("Vault root must be an existing directory")
        state = self.state(user_id)
        state.mkdir(parents=True, exist_ok=True, mode=0o700)
        old = self.config(user_id) if (state / "config.json").exists() else None
        if old and Path(old["root"]).resolve() != root.resolve():
            raise WikiError(
                "Changing the authoritative vault requires a separate migration", 409
            )
        config = {
            "root": str(root.resolve()),
            "curator_agent_id": curator_agent_id,
            "app_id": app_id,
            "vault_id": old["vault_id"] if old else str(uuid.uuid4()),
        }
        temporary = None
        replaced = False
        try:
            with tempfile.NamedTemporaryFile(
                dir=state, mode="w", encoding="utf-8", delete=False
            ) as handle:
                temporary = Path(handle.name)
                json.dump(config, handle, ensure_ascii=False)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, state / "config.json")
            replaced = True
            directory = os.open(state, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except OSError as exc:
            if replaced:
                raise WikiError("Wiki registration replaced, but durability is unconfirmed; retry the same registration", 503) from exc
            raise
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        try:
            self.store(user_id)
        except Exception as exc:
            raise WikiError("Wiki registration committed, but Store initialization failed; retry the same registration", 503) from exc
        return config

    def store(self, user_id: str | None) -> WikiStore:
        with self._account_lock(user_id):
            if user_id in self._blocked or (user_id is None and not self.legacy_enabled):
                raise WikiError("Wiki Store unavailable during account transition", 503)
            return self._store(user_id)

    def _store(self, user_id):
        config = self.config(user_id)
        state = self.state(user_id).resolve()
        key = (
            user_id, str(state), config["root"], config["vault_id"],
            config.get("curator_agent_id"), config.get("app_id"),
        )
        with self._lock:
            existing = self._stores.get(key)
        if existing is None:
            existing = WikiStore(Path(config["root"]), state, config["vault_id"])
        with self._lock:
            for previous in list(self._stores):
                if previous[:2] == key[:2] and previous != key:
                    retired = self._stores.pop(previous)
                    retired.retire()
                    self._retired_stores[retired] = previous[0]
            self._stores[key] = existing
            self._stores.move_to_end(key)
            while len(self._stores) > 32:
                previous, retired = self._stores.popitem(last=False)
                retired.retire()
                self._retired_stores[retired] = previous[0]
            self._retired_stores = {store: owner for store, owner in self._retired_stores.items() if store._users}
            return self._stores[key]

    def close(self) -> None:
        with self._lock:
            captured = [*self._stores.values(), *self._retired_stores]
            self._stores.clear()
            self._retired_stores.clear()
        for store in captured:
            store.close()

    async def reconcile(self) -> list[dict]:
        """Startup/periodic compensation; never called by an ordinary query."""
        def run():
            states = self.states()
            results = []
            for user_id, state in states:
                if not (state / "config.json").is_file():
                    continue
                try:
                    store = self.store(user_id)
                    store.refresh()
                    results.append({"user_id": user_id, **store.index_state()})
                except Exception:
                    logger.exception("Wiki reconciliation failed for account=%s", user_id)
            return results

        return await complete_cleanup(asyncio.to_thread(run))

    async def start(self) -> None:
        if self._task is not None:
            return
        await self.reconcile()
        self._stopping = asyncio.Event()
        self._task = asyncio.create_task(self._run(), name="wiki-index-sync")

    async def _run(self) -> None:
        assert self._stopping is not None
        loop = asyncio.get_running_loop()
        deadline = loop.time() + settings.wiki_reconcile_seconds
        while not self._stopping.is_set():
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=0.25)
                break
            except TimeoutError:
                pass
            with self._lock:
                stores = list(self._stores.values())
            for store in stores:
                try:
                    await complete_cleanup(asyncio.to_thread(store.synchronize))
                except Exception:
                    logger.exception("Wiki event processing failed for vault=%s", store.vault_id)
            if loop.time() >= deadline:
                await self.reconcile()
                deadline = loop.time() + settings.wiki_reconcile_seconds

    async def stop(self) -> None:
        await complete_cleanup(self._stop())

    async def _stop(self) -> None:
        if self._stopping is not None:
            self._stopping.set()
        if self._task is not None:
            # Join any ongoing filesystem/database operation before releasing
            # its observer; cancelling to_thread would leave a writer alive.
            await self._task
        self._task = None
        self._stopping = None
        await asyncio.to_thread(self.close)


registry = WikiRegistry()
