"""Account-owned vault registrations; no discovery or content on ordinary turns."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
import threading
import uuid
from collections import OrderedDict
from pathlib import Path
from typing import Any

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
        return json.loads(path.read_text(encoding="utf-8"))

    def configure(
        self,
        user_id: str | None,
        root: Path,
        curator_agent_id: str,
        app_id: str | None = None,
    ) -> dict[str, Any]:
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
        with tempfile.NamedTemporaryFile(
            dir=state, mode="w", encoding="utf-8", delete=False
        ) as handle:
            temporary = Path(handle.name)
            json.dump(config, handle, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, state / "config.json")
        self.store(user_id)
        return config

    def store(self, user_id: str | None) -> WikiStore:
        config = self.config(user_id)
        state = self.state(user_id).resolve()
        key = (
            user_id, str(state), config["root"], config["vault_id"],
            config.get("curator_agent_id"), config.get("app_id"),
        )
        with self._lock:
            for previous in list(self._stores):
                if previous[:2] == key[:2] and previous != key:
                    self._stores.pop(previous).close()
            if key not in self._stores:
                self._stores[key] = WikiStore(Path(config["root"]), state, config["vault_id"])
            self._stores.move_to_end(key)
            while len(self._stores) > 32:
                self._stores.popitem(last=False)[1].close()
            return self._stores[key]

    def close(self) -> None:
        with self._lock:
            for store in self._stores.values():
                store.close()
            self._stores.clear()

    async def reconcile(self) -> list[dict]:
        """Startup/periodic compensation; never called by an ordinary query."""
        def run():
            states = [(None, Path(settings.wiki_dir).expanduser())]
            states.extend((path.parent.name, path) for path in Path(settings.users_root).expanduser().glob("*/wiki"))
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

        return await asyncio.to_thread(run)

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
                    await asyncio.to_thread(store.synchronize)
                except Exception:
                    logger.exception("Wiki event processing failed for vault=%s", store.vault_id)
            if loop.time() >= deadline:
                await self.reconcile()
                deadline = loop.time() + settings.wiki_reconcile_seconds

    async def stop(self) -> None:
        if self._stopping is not None:
            self._stopping.set()
        if self._task is not None:
            # Join any ongoing filesystem/database operation before releasing
            # its observer; cancelling to_thread would leave a writer alive.
            await self._task
        self._task = None
        self._stopping = None
        self.close()


registry = WikiRegistry()
