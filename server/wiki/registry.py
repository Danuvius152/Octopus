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
from ..service_health import ServiceHealth
from ..service_installation import InstallationError, guard_vault
from ..workspace import user_root
from . import protocols, storage
from .store import WikiError, WikiStore

logger = logging.getLogger(__name__)


@dataclass(eq=False)
class Construction:
    owner: str | None
    state: Path
    config: dict
    owner_epoch: int
    incarnation: int
    revoked: bool = False
    done: threading.Event = field(default_factory=threading.Event)
    finished: threading.Event = field(default_factory=threading.Event)
    cleanup_gate: Any = field(default_factory=threading.Lock)
    candidate: WikiStore | None = None
    failure: BaseException | None = None




class WikiRegistry:
    def __init__(self) -> None:
        self.health = ServiceHealth()
        self._lock = threading.RLock()
        self._stores: OrderedDict[tuple, WikiStore] = OrderedDict()
        self._task: asyncio.Task | None = None
        self._stopping: asyncio.Event | None = None
        self._account_locks: dict[str, threading.RLock] = {}
        self._blocked: set[str | None] = set()
        self._retired_stores: dict[WikiStore, str | None] = {}
        self._owner_epochs: dict[str | None, int] = {}
        self._drained: dict[str | None, tuple[int, int]] = {}
        self._constructing: dict[Construction, None] = {}
        self._incarnation = 0
        self._closing = False
        self._stopped = False
        self._stop_operations = 0
        self._stop_drained = False
        self._starting: object | None = None
        self._close_gate = threading.Lock()
        self._drain_locks: dict[str | None, Any] = {}
        self._recoveries = None
        # One startup recovery result per vault in this service generation.
        # A failed pipeline remains closed for explicit owner recovery.
        self._recovery_results: dict[tuple[str, str], BaseException | None] = {}
        self._previous_writers_retired = False

    def bind_health(self, health: ServiceHealth) -> None:
        """A new service generation may bind only after every old owner exits."""
        if not self._close_gate.acquire(blocking=False):
            raise WikiError("Wiki cleanup must finish before binding service health", 503)
        try:
            with self._lock:
                self.health.require_healthy()
                health.require_healthy()
                if self.health is health:
                    return
                self._check_reopen()
                if (self._stores or self._retired_stores or self._constructing or self._task is not None
                        or self._recoveries is not None and not self._recoveries.is_drained()):
                    raise WikiError("Wiki owners must finish cleanup before binding service health", 503)
                self.health = health
                # A retained manager keeps its old health and its closed gates.
                self._recoveries = None
                self._recovery_results.clear()
                self._previous_writers_retired = False
        finally:
            self._close_gate.release()



    def _advance(self, user_id):
        self._owner_epochs[user_id] = self._owner_epochs.get(user_id, 0) + 1
        for permit in self._constructing:
            if permit.owner == user_id:
                permit.revoked = True

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
        states = [(path.parent.name, path) for path in Path(settings.users_root).expanduser().glob("*/wiki")]
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

    def config(self, user_id: str | None, *, require_root=True) -> dict[str, Any]:
        return protocols.read_registration(self.state(user_id), require_root=require_root)

    def configure(
        self,
        user_id: str | None,
        root: Path,
        curator_agent_id: str,
        app_id: str | None = None,
    ) -> dict[str, Any]:
        self.health.require_healthy()
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
        config_path = state / "config.json"
        old = self.config(user_id) if config_path.exists() or config_path.is_symlink() else None
        if old and Path(old["root"]).resolve() != root.resolve():
            raise WikiError(
                "Changing the authoritative vault requires a separate migration", 409
            )
        database = state / "index.sqlite3"
        binding = None
        if old or database.exists() or database.is_symlink():
            binding = protocols.registration_preflight(state.resolve(), complete=old is None, health=self.health)
        if binding is not None and (binding["root"] != str(root.resolve()) or old and binding["vault_id"] != old["vault_id"]):
            raise WikiError("Wiki ledger belongs to another root or vault binding; durable state retained", 409)
        vault_id = old["vault_id"] if old else binding["vault_id"] if binding else str(uuid.uuid4())
        try:
            uuid.UUID(vault_id)
        except (ValueError, TypeError, AttributeError) as exc:
            raise WikiError("Wiki ledger has an invalid vault binding; durable state retained", 503) from exc
        try:
            guard_vault(settings, root, state, bind=True, health=self.health)
        except InstallationError as error:
            raise WikiError(str(error), 409) from error
        if not old and binding is None:
            self._create_ledger(root, state, vault_id)
        elif not old:
            # A prior process may have stopped immediately after link(), before
            # confirming its namespace. Complete that sync before config exists.
            with storage.file_effect(self.health):
                with database.open("rb") as handle:
                    os.fsync(handle.fileno())
                self._sync_parents(state)
        config = {
            "root": str(root.resolve()),
            "curator_agent_id": curator_agent_id,
            "app_id": app_id,
            "vault_id": vault_id,
        }
        same = old and all(config[key] == old.get(key) for key in config)
        config |= {"registration_protocol": 1,
                   "registration_generation": old["registration_generation"] if same else uuid.uuid4().hex}
        if old == config:
            with storage.file_effect(self.health):
                self._sync_config(state)
            return config
        temporary = None
        try:
            with storage.file_effect(self.health):
                with tempfile.NamedTemporaryFile(
                    dir=state, mode="w", encoding="utf-8", delete=False
                ) as handle:
                    temporary = Path(handle.name)
                    json.dump(config, handle, ensure_ascii=False)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, state / "config.json")
                self._sync_config(state)
        finally:
            if temporary is not None:
                with contextlib.suppress(OSError):
                    temporary.unlink(missing_ok=True)
        return config

    def _create_ledger(self, root, state, vault_id):
        """The registration lock owns first creation through config publication."""
        temporary = None
        try:
            with storage.file_effect(self.health):
                descriptor, name = tempfile.mkstemp(prefix=".index-", suffix=".sqlite3", dir=state)
                temporary = Path(name)
                os.close(descriptor)
            WikiStore.create_database(root, state, vault_id, temporary, health=self.health)
            with storage.file_effect(self.health):
                with temporary.open("rb") as handle:
                    os.fsync(handle.fileno())
                try:
                    os.link(temporary, state / "index.sqlite3")
                except FileExistsError as exc:
                    raise WikiError("Wiki ledger appeared during registration; retained without replacement", 409) from exc
                temporary.unlink()
                self._sync_parents(state)
        finally:
            if temporary is not None:
                # An unpublished private file carries no business facts. Its
                # cleanup is independent of the published database's durability.
                with contextlib.suppress(OSError):
                    temporary.unlink(missing_ok=True)

    @staticmethod
    def _sync_config(state):
        with (state / "config.json").open("rb") as handle:
            os.fsync(handle.fileno())
        WikiRegistry._sync_parents(state)

    @staticmethod
    def _sync_parents(path):
        for parent in (path, *path.parents):
            WikiRegistry._sync_directory(parent)

    @staticmethod
    def _sync_directory(path):
        directory = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)

    def store(self, user_id: str | None) -> WikiStore:
        return self._store(user_id)

    def recoveries(self):
        from .recovery import RecoveryManager

        with self._lock:
            if self._recoveries is None:
                if self._closing or self._stopped:
                    raise WikiError("Wiki recovery owners are unavailable during cleanup", 503)
                self._recoveries = RecoveryManager(self)
            return self._recoveries

    def publish_recovery(self, user_id, store, permit):
        """Publish the verified same-observer receiver after its durable receipt."""
        with self._account_lock(user_id), self.registration_lock(user_id), self._lock:
            config = self.config(user_id)
            if (config != permit.config or permit.manager.registry is not self or store.events is not permit.observer
                    or self._closing or self._stopped or user_id not in self._blocked
                    or self._drained.get(user_id) != (self._owner_epochs.get(user_id, 0), self._incarnation)):
                raise WikiError("Filesystem recovery publication binding or drain changed", 503)
            with store.connect() as db:
                permit.check(db)
                receipt = db.execute("SELECT phase,completion_receipt FROM filesystem_recoveries WHERE id=?", (permit.recovery_id,)).fetchone()
                if receipt is None or receipt["phase"] != "complete" or not receipt["completion_receipt"]:
                    raise WikiError("Filesystem recovery handoff is not durably complete", 503)
            key = self._key(user_id, self.state(user_id).resolve(), config)
            self._recovery_results[(str(store.state.resolve()), store.vault_id)] = None
            self._stores[key] = store
            store._recovery = None
            self._advance(user_id)
            self._blocked.discard(user_id)

    def _allowed(self, user_id):
        return user_id not in self._blocked and not self._closing and not self._stopped

    @staticmethod
    def _key(user_id, state, config):
        return (user_id, str(state), config["root"], config["vault_id"],
                config.get("curator_agent_id"), config.get("app_id"), config.get("registration_generation"))

    def _store(self, user_id):
        self.health.require_healthy()
        account = self._account_lock(user_id)
        outer_account = account._is_owned()
        state = self.state(user_id).resolve()
        from .history import _owners

        outer_history = str(state) in getattr(_owners, "held", set())
        with account:
            with self._lock:
                if not self._allowed(user_id):
                    raise WikiError("Wiki Store unavailable during account transition", 503)
            # Ordinary reads must not create state for an unregistered owner.
            # The actual capture rechecks both gate and config under the lock.
            self.config(user_id)
            with self.registration_lock(user_id), self._lock:
                if not self._allowed(user_id):
                    raise WikiError("Wiki Store unavailable during account transition", 503)
                config = self.config(user_id)
                key = self._key(user_id, state, config)
                existing = self._stores.get(key)
                if existing is not None and not existing._retired:
                    self._stores.move_to_end(key)
                    return existing
                failure = self._recovery_results.get((str(state), config["vault_id"]))
                if failure is not None:
                    raise WikiError(f"Wiki recovery failed; durable evidence retained: {failure}", 503) from failure
                if outer_account or outer_history:
                    raise WikiError("Prepare the current Wiki Store before entering locked work", 503)
                waiting = next((entry for entry in self._constructing if entry.owner == user_id
                                and entry.state == state and entry.config.get("vault_id") == config["vault_id"]), None)
                if waiting is None:
                    permit = Construction(user_id, state, config, self._owner_epochs.get(user_id, 0), self._incarnation)
                    self._constructing[permit] = None
        if waiting is not None:
            if not waiting.finished.wait(30):
                raise WikiError("Wiki recovery is still running; ordinary access remains closed", 503)
            if waiting.failure is not None and not waiting.revoked and waiting.config == config:
                raise WikiError(f"Wiki recovery failed; durable evidence retained: {waiting.failure}", 503) from waiting.failure
            return self._store(user_id)
        candidate = None
        retire = []
        try:
            binding = protocols.registration_preflight(state, health=self.health)
            if binding is not None and binding != {"root": config["root"], "vault_id": config["vault_id"]}:
                raise WikiError("Wiki ledger registration binding changed; durable state retained", 503)
            try:
                guard_vault(settings, Path(config["root"]), state, health=self.health)
            except InstallationError as error:
                raise WikiError(str(error), 503) from error
            def acquired(store):
                permit.candidate = store
                # Publication rechecks this captured binding and the permit
                # after construction completes.
                store.registration_generation = config["registration_generation"]

            candidate = WikiStore(Path(config["root"]), state, config["vault_id"], on_construct=acquired,
                                  health=self.health, defer_recovery=True)
            with candidate.connect() as db:
                protocols.require(db, "registration")
                db.execute("INSERT OR IGNORE INTO wiki_protocols VALUES('registration_config',1)")
                protocols.require(db, "registration_config")
            self._prepare_store(candidate, permit)
            with account, self.registration_lock(user_id), self._lock:
                self._require_construction(permit)
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
        except BaseException as error:
            permit.failure = error
            raise
        finally:
            try:
                self._dispose(permit)
            finally:
                permit.finished.set()

    def _require_construction(self, permit):
        if (permit.revoked or permit.owner_epoch != self._owner_epochs.get(permit.owner, 0)
                or permit.incarnation != self._incarnation or not self._allowed(permit.owner)
                or self.config(permit.owner) != permit.config):
            raise WikiError("Wiki construction or registration changed before admission", 503)

    def _prepare_store(self, candidate, permit):
        """Recover only the captured candidate; never borrow our unpublished Store."""
        from .history import WikiHistory
        from .jobs import WikiJobs

        user_id, config = permit.owner, permit.config
        with self._account_lock(user_id), self.registration_lock(user_id), self._lock:
            self._require_construction(permit)
        key = (str(candidate.state.resolve()), config["vault_id"])
        if key in self._recovery_results:
            if error := self._recovery_results[key]:
                raise WikiError(f"Wiki recovery failed; durable evidence retained: {error}", 503) from error
            with candidate.connect() as db:
                checked = db.execute("SELECT checked_at FROM index_state WHERE singleton=1").fetchone()[0]
            if checked is None:
                candidate.refresh()
            return
        try:
            WikiHistory(candidate).recover()
            candidate.refresh()
        except BaseException as error:
            self._recovery_results[key] = error
            raise
        # Registration may change while history or indexing is running. Only a
        # still-valid permit may classify jobs or stamp old-writer retirement.
        with WikiHistory(candidate).locked(shared=True), self._account_lock(user_id), self.registration_lock(user_id), self._lock:
            self._require_construction(permit)
            try:
                jobs = WikiJobs(user_id, store=candidate, registration=config)
                jobs.recover_jobs(previous_writers_retired=self._previous_writers_retired)
            except BaseException as error:
                self._recovery_results[key] = error
                raise
            self._recovery_results[key] = None

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


    def close(self) -> None:
        with self._close_gate:
            with self._lock:
                self._closing = True
                self._incarnation += 1
                pending = list(self._constructing)
                for permit in pending:
                    permit.revoked = True
                captured = [*self._stores.values(), *self._retired_stores]
                self._retired_stores.update((store, key[0]) for key, store in self._stores.items())
                self._stores.clear()
            if self._recoveries is not None:
                self._recoveries.close()
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
            self.health.require_healthy()
            if self._recoveries is not None and self._recoveries.is_drained():
                self._recoveries.resume()
            self._incarnation += 1
            self._stopped = False

    async def reconcile(self, *, initial=False) -> list[dict]:
        """Startup/periodic compensation; never called by an ordinary query."""
        def run():
            states = self.states()
            results = []
            for user_id, state in states:
                if not (state / "config.json").is_file():
                    continue
                try:
                    store = self.store(user_id)
                    if not initial:
                        store.refresh()
                    results.append({"user_id": user_id, **store.index_state()})
                except Exception:
                    logger.exception("Wiki reconciliation failed for account=%s", user_id)
            return results

        return await complete_cleanup(asyncio.to_thread(run))

    async def start(self, *, paused: bool = False, previous_writers_retired: bool = False) -> None:
        with self._lock:
            self._check_reopen()
            if self._task is not None:
                return
            self.resume_access()
            self._previous_writers_retired = previous_writers_retired
            starting = self._starting = object()
            incarnation = self._incarnation
        try:
            await self.reconcile(initial=True)
            with self._lock:
                if self._incarnation != incarnation or self._stopped or self._closing:
                    raise WikiError("Wiki startup was revoked before publication", 503)
                if not paused:
                    self.start_reconciler()
        finally:
            with self._lock:
                if self._starting is starting:
                    self._starting = None

    def start_reconciler(self) -> None:
        with self._lock:
            if self._stopped or self._closing:
                raise WikiError("Wiki registry is stopping", 503)
            if self._task is None:
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
