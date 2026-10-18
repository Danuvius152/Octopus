"""Account-owned vault registrations; no discovery or content on ordinary turns."""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import json
import logging
import os
import tempfile
import threading
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..aio import complete_cleanup
from ..config import settings
from ..workspace import user_root
from . import protocols
from .store import WikiError, WikiStore

logger = logging.getLogger(__name__)


@dataclass(eq=False)
class Construction:
    owner: str | None
    state: Path
    config: dict
    owner_epoch: int
    incarnation: int
    migration: object | None = None
    revoked: bool = False
    done: threading.Event = field(default_factory=threading.Event)
    finished: threading.Event = field(default_factory=threading.Event)
    cleanup_gate: Any = field(default_factory=threading.Lock)
    candidate: WikiStore | None = None


@dataclass(eq=False)
class MigrationConstruction:
    owner: str
    state: Path
    generation: str
    upgrade_id: str
    owner_epoch: int
    incarnation: int


class WikiRegistry:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._stores: OrderedDict[tuple, WikiStore] = OrderedDict()
        self._task: asyncio.Task | None = None
        self._stopping: asyncio.Event | None = None
        self._account_locks: dict[str, threading.RLock] = {}
        self._blocked: set[str | None] = set()
        self._retired_stores: dict[WikiStore, str | None] = {}
        self._legacy_enabled = True
        self._owner_epochs: dict[str | None, int] = {}
        self._drained: dict[str | None, tuple[int, int]] = {}
        self._constructing: dict[Construction, None] = {}
        self._migration_caps: set[MigrationConstruction] = set()
        self._incarnation = 0
        self._closing = False
        self._stopped = False
        self._stop_operations = 0
        self._stop_drained = False
        self._starting: object | None = None
        self._close_gate = threading.Lock()
        self._drain_locks: dict[str | None, Any] = {}

    @property
    def legacy_enabled(self):
        with self._lock:
            return self._legacy_enabled

    @legacy_enabled.setter
    def legacy_enabled(self, enabled):
        with self._lock:
            if enabled != self._legacy_enabled:
                self._legacy_enabled = enabled
                self._advance(None)

    def _advance(self, user_id):
        self._owner_epochs[user_id] = self._owner_epochs.get(user_id, 0) + 1
        for permit in self._constructing:
            if permit.owner == user_id:
                permit.revoked = True
        self._migration_caps = {cap for cap in self._migration_caps if cap.owner != user_id}

    def begin_transition(self, user_id):
        """Stop admission immediately; block() separately confirms the drain."""
        with self._account_lock(user_id), self._lock:
            self._blocked.add(user_id)
            self._advance(user_id)

    def _account_lock(self, user_id):
        key = str(self.state(user_id).resolve())
        with self._lock:
            return self._account_locks.setdefault(key, threading.RLock())

    def states(self):
        states = [(None, self.state(None))] if self.legacy_enabled else []
        states.extend((path.parent.name, path) for path in Path(settings.users_root).expanduser().glob("*/wiki"))
        return [(user_id, state) for user_id, state in states if user_id not in self._blocked]

    def block(self, user_id):
        with self._lock:
            gate = self._drain_locks.setdefault(user_id, threading.Lock())
        with gate:
            self._block(user_id)

    def _block(self, user_id):
        with self._account_lock(user_id), self._lock:
            self._blocked.add(user_id)
            self._advance(user_id)
            epoch = self._owner_epochs[user_id]
            incarnation = self._incarnation
            pending = [permit for permit in self._constructing if permit.owner == user_id]
            captured = [self._stores.pop(key) for key in list(self._stores) if key[0] == user_id]
            captured.extend(store for store, owner in self._retired_stores.items() if owner == user_id)
            self._retired_stores.update((store, user_id) for store in captured)
        for permit in pending:
            if not permit.finished.wait(30):
                raise WikiError("Wiki constructor cleanup is unconfirmed; owner remains blocked", 503)
            self._dispose(permit)
        for store in captured:
            store.close()
        with self._lock:
            if self._owner_epochs[user_id] != epoch or self._incarnation != incarnation or any(permit.owner == user_id for permit in self._constructing):
                raise WikiError("Wiki transition changed during drain; retry cleanup", 503)
            self._retired_stores = {store: owner for store, owner in self._retired_stores.items() if owner != user_id}
            self._drained[user_id] = epoch, incarnation

    def unblock(self, user_id):
        with self._account_lock(user_id), self._lock:
            if self._closing or (user_id in self._blocked and self._drained.get(user_id) != (self._owner_epochs.get(user_id, 0), self._incarnation)):
                raise WikiError("Wiki transition cleanup is not confirmed", 503)
            if any(permit.owner == user_id for permit in self._constructing):
                raise WikiError("Wiki constructors are still draining", 503)
            self._advance(user_id)
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
            if not isinstance(config, dict) or set(config) - {"root", "vault_id", "curator_agent_id", "app_id", "registration_protocol", "registration_generation"}:
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
            if "registration_protocol" in config or "registration_generation" in config:
                if type(config.get("registration_protocol")) is not int or config["registration_protocol"] != 1 or uuid.UUID(config["registration_generation"]).hex != config["registration_generation"]:
                    raise ValueError("invalid registration generation")
            else:
                protocols.registration_preflight(self.state(user_id).resolve(), legacy=True)
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
        account = self._account_lock(user_id)
        state = self.state(user_id).resolve()
        with account, self._lock:
            if not self._allowed(user_id):
                raise WikiError("Wiki registration is unavailable during account transition", 503)
            permit = Construction(user_id, state, {}, self._owner_epochs.get(user_id, 0), self._incarnation)
            self._constructing[permit] = None
        try:
            with account:
                if permit.revoked:
                    raise WikiError("Wiki registration was revoked before commit", 503)
                state.mkdir(parents=True, exist_ok=True, mode=0o700)
                with self.registration_lock(user_id, shared=False):
                    config = self._configure(user_id, root, curator_agent_id, app_id)
        finally:
            try:
                self._dispose(permit)
            finally:
                permit.finished.set()
        try:
            self.store(user_id)
        except Exception as exc:
            raise WikiError("Wiki registration committed, but Store initialization failed; retry the same registration", 503) from exc
        return config

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
        protocols.registration_preflight(state.resolve(), legacy=not old or "registration_generation" not in old)
        same = old and all(config[key] == old.get(key) for key in config)
        config |= {"registration_protocol": 1,
                   "registration_generation": old["registration_generation"] if old and same and old.get("registration_generation") else uuid.uuid4().hex}
        if old == config:
            try:
                self._sync_config(state)
            except OSError as exc:
                raise WikiError("Wiki registration durability is unconfirmed; retry the same registration", 503) from exc
            return config
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
            self._sync_config(state)
        except OSError as exc:
            if replaced:
                raise WikiError("Wiki registration replaced, but durability is unconfirmed; retry the same registration", 503) from exc
            raise
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        return config

    @staticmethod
    def _sync_config(state):
        with (state / "config.json").open("rb") as handle:
            os.fsync(handle.fileno())
        directory = os.open(state, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)

    def store(self, user_id: str | None) -> WikiStore:
        return self._store(user_id)

    def _allowed(self, user_id, capability=None):
        if capability is not None:
            from .account_upgrade import pending_upgrade

            journal = pending_upgrade()
            return (capability in self._migration_caps and capability.owner == user_id
                    and capability.owner_epoch == self._owner_epochs.get(user_id, 0)
                    and capability.incarnation == self._incarnation
                    and self._drained.get(user_id) == (capability.owner_epoch, self._incarnation)
                    and user_id in self._blocked and not self._closing
                    and journal is not None and journal["phase"] == "reopening"
                    and journal.get("user_id") == user_id and journal["upgrade_id"] == capability.upgrade_id)
        return (user_id not in self._blocked and (user_id is not None or self._legacy_enabled)
                and not self._closing and not self._stopped)

    @staticmethod
    def _key(user_id, state, config):
        return (user_id, str(state), config["root"], config["vault_id"],
                config.get("curator_agent_id"), config.get("app_id"), config.get("registration_generation"))

    def _store(self, user_id, capability=None):
        account = self._account_lock(user_id)
        outer_account = account._is_owned()
        state = self.state(user_id).resolve()
        from .history import _owners

        outer_history = str(state) in getattr(_owners, "held", set())
        with account:
            with self._lock:
                if not self._allowed(user_id, capability):
                    raise WikiError("Wiki Store unavailable during account transition", 503)
            # Ordinary reads must not create state for an unregistered owner.
            # The actual capture rechecks both gate and config under the lock.
            self.config(user_id)
            with self.registration_lock(user_id), self._lock:
                if not self._allowed(user_id, capability):
                    raise WikiError("Wiki Store unavailable during account transition", 503)
                config = self.config(user_id)
                if capability is not None and (capability.state != state or capability.generation != config.get("registration_generation")):
                    raise WikiError("Migration registration changed before reopen", 503)
                key = self._key(user_id, state, config)
                existing = self._stores.get(key)
                if existing is not None and not existing._retired:
                    self._stores.move_to_end(key)
                    return existing
                if outer_account or outer_history:
                    raise WikiError("Prepare the current Wiki Store before entering locked work", 503)
                permit = Construction(user_id, state, config, self._owner_epochs.get(user_id, 0), self._incarnation, capability)
                self._constructing[permit] = None
        candidate = None
        retire = []
        try:
            protocols.registration_preflight(state, legacy="registration_generation" not in config)
            if "registration_generation" not in config:
                with account:
                    with self._lock:
                        if permit.revoked or not self._allowed(user_id, capability):
                            raise WikiError("Wiki construction was revoked", 503)
                    with self.registration_lock(user_id, shared=False):
                        if self.config(user_id) != config:
                            raise WikiError("Wiki registration changed during preparation", 503)
                        config = self._configure(user_id, Path(config["root"]), config["curator_agent_id"], config.get("app_id"))
                        permit.config = config
                        key = self._key(user_id, state, config)
            def acquired(store):
                permit.candidate = store
                # The migration uses this captured binding. Publication still
                # rechecks the permit and config after construction completes.
                store.registration_generation = config["registration_generation"]

            candidate = WikiStore(Path(config["root"]), state, config["vault_id"], on_construct=acquired)
            with candidate.connect() as db:
                protocols.require(db, "registration")
                db.execute("INSERT OR IGNORE INTO wiki_protocols VALUES('registration_config',1)")
                protocols.require(db, "registration_config")
            with account, self.registration_lock(user_id), self._lock:
                if (permit.revoked or permit.owner_epoch != self._owner_epochs.get(user_id, 0)
                        or permit.incarnation != self._incarnation or not self._allowed(user_id, capability)
                        or self.config(user_id) != config):
                    raise WikiError("Wiki construction or registration changed before publication", 503)
                winner = self._stores.get(key)
                if winner is None or winner._retired:
                    if winner is not None:
                        self._retired_stores[winner] = user_id
                    winner = candidate
                    candidate.registration_generation = config["registration_generation"]
                    self._stores[key] = candidate
                    candidate = None  # Ownership transfers before the permit can complete.
                    permit.candidate = None
                for previous in list(self._stores):
                    if previous[:2] == key[:2] and previous != key:
                        old = self._stores.pop(previous)
                        self._retired_stores[old] = user_id
                        retire.append(old)
                self._stores.move_to_end(key)
                while len(self._stores) > 32:
                    previous, old = self._stores.popitem(last=False)
                    self._retired_stores[old] = previous[0]
                    retire.append(old)
            for old in retire:
                old.retire()
            return winner
        finally:
            try:
                self._dispose(permit)
            finally:
                permit.finished.set()

    def _dispose(self, permit):
        with permit.cleanup_gate:
            if permit.done.is_set():
                return
            try:
                if permit.candidate is not None:
                    permit.candidate.close()
            except BaseException:
                # Keep the resource and failed permit reachable for confirmed
                # cleanup; another request must not publish over this owner.
                self.begin_transition(permit.owner)
                raise
            with self._lock:
                permit.candidate = None
                self._constructing.pop(permit, None)
                permit.done.set()

    @contextlib.contextmanager
    def registration_lock(self, user_id, *, shared=True):
        """Does not acquire account/history locks or construct a Store."""
        path = self.state(user_id) / "config.lock"
        try:
            handle = path.open("a+b")
        except OSError as exc:
            raise WikiError("Wiki registration lock unavailable; state retained", 503) from exc
        with handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_SH if shared else fcntl.LOCK_EX)
            except OSError as exc:
                raise WikiError("Wiki registration lock unavailable; state retained", 503) from exc
            try:
                yield
            finally:
                try:
                    fcntl.flock(handle, fcntl.LOCK_UN)
                except OSError as exc:
                    raise WikiError("Wiki registration unlock failed; state retained", 503) from exc

    def reopen_for_upgrade(self, user_id, journal):
        from .account_upgrade import pending_upgrade

        with self._account_lock(user_id), self._lock:
            actual = pending_upgrade()
            if (actual is None or actual["phase"] != "reopening" or actual.get("user_id") != user_id
                    or actual["upgrade_id"] != journal["upgrade_id"] or not user_id
                    or user_id not in self._blocked or self._drained.get(user_id) != (self._owner_epochs.get(user_id, 0), self._incarnation)):
                raise WikiError("A drained, durable migration reopen is required", 503)
            config = self.config(user_id)
            capability = MigrationConstruction(user_id, self.state(user_id).resolve(), config["registration_generation"],
                                                actual["upgrade_id"], self._owner_epochs[user_id], self._incarnation)
            self._migration_caps.add(capability)
        try:
            return self._store(user_id, capability)
        finally:
            with self._lock:
                self._migration_caps.discard(capability)

    def close(self) -> None:
        with self._close_gate:
            with self._lock:
                self._closing = True
                self._incarnation += 1
                self._migration_caps.clear()
                pending = list(self._constructing)
                for permit in pending:
                    permit.revoked = True
                captured = [*self._stores.values(), *self._retired_stores]
                self._retired_stores.update((store, key[0]) for key, store in self._stores.items())
                self._stores.clear()
            for permit in pending:
                if not permit.finished.wait(30):
                    raise WikiError("Wiki construction cleanup is unconfirmed; registry remains closed", 503)
                self._dispose(permit)
            for store in captured:
                store.close()
            with self._lock:
                self._retired_stores.clear()
                self._closing = False

    def _check_reopen(self):
        if (self._closing or self._constructing or self._starting is not None
                or self._stop_operations or (self._stopped and not self._stop_drained)):
            raise WikiError("Wiki registry cleanup must finish before reopening", 503)

    def resume_access(self):
        with self._lock:
            self._check_reopen()
            self._incarnation += 1
            self._stopped = False

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
        with self._lock:
            self._check_reopen()
            if self._task is not None:
                return
            self.resume_access()
            starting = self._starting = object()
            incarnation = self._incarnation
        try:
            await self.reconcile()
            with self._lock:
                if self._incarnation != incarnation or self._stopped or self._closing:
                    raise WikiError("Wiki startup was revoked before publication", 503)
                self._stopping = asyncio.Event()
                self._task = asyncio.create_task(self._run(), name="wiki-index-sync")
        finally:
            with self._lock:
                if self._starting is starting:
                    self._starting = None

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
        with self._lock:
            self._stop_operations += 1
            self._stop_drained = False
            self._stopped = True
            self._incarnation += 1
            for permit in self._constructing:
                permit.revoked = True
        drained = False
        try:
            if self._stopping is not None:
                self._stopping.set()
            if self._task is not None:
                # Join any ongoing filesystem/database operation before releasing
                # its observer; cancelling to_thread would leave a writer alive.
                await self._task
            self._task = None
            self._stopping = None
            await asyncio.to_thread(self.close)
            drained = True
        finally:
            with self._lock:
                self._stop_operations -= 1
                if drained and not self._stop_operations:
                    self._stop_drained = True


registry = WikiRegistry()
