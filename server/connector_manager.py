"""ConnectorManager — business logic over the connector DB layer
(connectors.md §5.5). Mirrors AgentManager: routes wrap a singleton, errors
surface as ConnectorError → HTTP 400/404.

Owns the OAuth-token lifecycle: encrypt-at-rest, dedup upsert on
(kind, external account), and server-side refresh-on-near-expiry guarded by a
per-installation lock so concurrent MCP subprocesses refresh exactly once.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

import httpx

from .aio import complete_cleanup
from .config import settings
from .connectors.base import ConnectorBase, ConnectorInstallation
from .connectors.custom import CustomConnector, resolve_connector
from .connectors.errors import ConnectorError, ConnectorTokenError
from .connectors.grants import GrantDisposition, grant_stamp
from .connectors.registry import all_connectors, get_connector
from .crypto import decrypt, encrypt, master_key
from .database import Database
from .diagnostics import exception_diagnostic
from .monitor import Event as _MonEvent
from .monitor import record as _mon_record
from .oauth_providers import OAuthTokenSet
from .users import UserManager, owner_is_available

# Slugs reserved by routes / built-ins; a custom kind can't take these.
_KIND_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,31}$")
_RESERVED_KINDS = {"oauth", "catalog", "custom"}

# Refresh when the access token expires within this window.
_REFRESH_SKEW_SECONDS = 300
_REFRESH_SHUTDOWN_SECONDS = 30.0
_BINDING_OPERATION_SECONDS = 120
_BINDING_OPERATION_LIMIT_PER_ACCOUNT = 4096
logger = logging.getLogger(__name__)


class ConnectorBindingOperationRetired(ConnectorError):
    """The presented operation cannot start or repeat its effect."""


class ConnectorBindingOperationsFull(ConnectorError):
    """The receipt retention window has no room for a new operation."""


type BindingDisposition = Literal["applied", "retired"]
type BindingSettlement = BindingDisposition | Literal["unavailable"]


@dataclass(frozen=True)
class _BindingScope:
    agent_id: str
    installation_id: str
    user_id: str | None


@dataclass
class _BindingOperation:
    scope: _BindingScope
    intent_id: uuid.UUID
    enabled: bool
    expires_at: float
    disposition: BindingDisposition | None = None

    def finish(self, disposition: BindingDisposition) -> None:
        self.disposition = disposition
        self.expires_at = time.monotonic() + _BINDING_OPERATION_SECONDS


@dataclass(frozen=True)
class PreparedInstallation:
    """Resolved remote identity; no local binding exists until persistence."""

    kind: str
    token_set: OAuthTokenSet = field(repr=False)
    external_id: str
    label: str
    user_id: str | None
    secret_key: str = field(repr=False)
    definition_identity: object | None = field(repr=False)
    target_installation_id: str | None = None


def _serialize_token_set(ts: OAuthTokenSet) -> str:
    return json.dumps(
        {
            "access_token": ts.access_token,
            "refresh_token": ts.refresh_token,
            "expires_at_epoch": ts.expires_at_epoch,
            "scopes": list(ts.scopes),
            "token_type": ts.token_type,
        },
        separators=(",", ":"),
    )


def _deserialize_token_set(blob: str) -> OAuthTokenSet:
    d = json.loads(blob)
    return OAuthTokenSet(
        access_token=d["access_token"],
        refresh_token=d.get("refresh_token"),
        expires_at_epoch=float(d.get("expires_at_epoch") or 0.0),
        scopes=list(d.get("scopes") or []),
        token_type=d.get("token_type") or "Bearer",
    )


def _expires_iso(epoch: float) -> str | None:
    if not epoch:
        return None
    return datetime.fromtimestamp(epoch, tz=UTC).isoformat()


def _env_client_creds(kind: str) -> tuple[str, str] | None:
    """OAuth client creds for a kind from env (the fallback when nothing is
    set in-app): OCTOPUS_<KIND>_OAUTH_CLIENT_ID / _SECRET."""
    cid = getattr(settings, f"{kind}_oauth_client_id", None)
    csec = getattr(settings, f"{kind}_oauth_client_secret", None)
    return (cid, csec) if cid and csec else None


class ConnectorManager:
    def __init__(self, db: Database) -> None:
        self.db = db
        self._locks: dict[str | tuple[str | None, str, str], asyncio.Lock] = {}
        self._token_tasks: dict[str, asyncio.Task[dict[str, Any]]] = {}
        self._definition_lock = asyncio.Lock()
        self._definitions: dict[str, object] = {}
        self._binding_lock = asyncio.Lock()
        self._binding_operations: dict[uuid.UUID, _BindingOperation] = {}
        self._closing = False
        db.add_close_hook(self.shutdown)

    def drain_blockers(self) -> tuple[str, ...]:
        return tuple(f"connector token operation {iid}" for iid in self._token_tasks)

    def begin_shutdown(self) -> None:
        self._closing = True

    async def shutdown(self) -> None:
        """Join owned rotations before DB close, without cancelling their save."""
        self.begin_shutdown()
        tasks = tuple(self._token_tasks.values())
        if not tasks:
            return
        done, pending = await asyncio.wait(tasks, timeout=_REFRESH_SHUTDOWN_SECONDS)
        if pending:
            raise TimeoutError(f"{len(pending)} connector token operations have not retired")
        failures = [task.exception() for task in done if not task.cancelled()]
        unexpected = [error for error in failures if error is not None and not isinstance(error, ConnectorError)]
        if unexpected:
            raise RuntimeError("Connector token operation failed during shutdown") from unexpected[0]

    async def get(self, kind: str) -> ConnectorBase | None:
        """A connector by kind — built-in (code) or custom (DB), else None."""
        async with self._definition_lock:
            connector = await resolve_connector(self.db, kind)
            if isinstance(connector, CustomConnector):
                connector.definition_identity = self._definitions.setdefault(kind, object())
            return connector

    # --- custom (user-defined) connector definitions ---------------------

    async def create_custom_connector(
        self,
        *,
        kind: str,
        display_name: str,
        authorize_url: str,
        token_url: str,
        scopes: list[str],
        pkce: bool,
        api_base: str,
        client_id: str,
        client_secret: str,
    ) -> dict[str, Any]:
        kind = (kind or "").strip().lower()
        if not _KIND_RE.match(kind) or kind in _RESERVED_KINDS:
            raise ConnectorError(
                "kind must be a slug (lowercase letters, digits, - or _) and "
                "not a reserved word"
            )
        if get_connector(kind) is not None:
            raise ConnectorError(f"{kind!r} is a built-in connector")

        async def create() -> dict[str, Any]:
            duplicate = f"a custom connector {kind!r} already exists"
            if await self.db.get_custom_connector(kind) is not None:
                raise ConnectorError(duplicate)
            row = await self.db.create_custom_connector(
                kind=kind, display_name=display_name.strip() or kind,
                authorize_url=authorize_url.strip(), token_url=token_url.strip(),
                scopes=list(scopes), pkce=bool(pkce), api_base=api_base.strip(),
                client_id=client_id, client_secret_encrypted=encrypt(client_secret, master_key()),
                now=datetime.now(UTC).isoformat(),
            )
            if row is None:
                raise ConnectorError(duplicate)
            return self._catalog_entry(CustomConnector(row), available=bool(client_id or _env_client_creds(kind)))

        async with self._definition_lock:
            return await complete_cleanup(create())

    async def delete_custom_connector(self, kind: str) -> None:
        async def delete() -> None:
            if not await self.db.delete_custom_connector(kind):
                raise ConnectorError("custom connector not found")
            self._definitions.pop(kind, None)

        # Finish the known database outcome and identity retirement together.
        async with self._definition_lock:
            await complete_cleanup(delete())

    # --- OAuth client credentials (in-app config, DB-first then env) ------

    async def resolve_client_creds(self, kind: str) -> tuple[str, str] | None:
        """Effective (client_id, client_secret) for a kind: the in-app DB row
        wins, else the env fallback, else None (→ catalog shows unavailable).

        Keyed with the **master key**, not a user's DEK and no longer the
        install token: an OAuth client is this box's registration with the
        provider, which is why it has an env fallback at all
        (multi-tenancy.md §4).
        """
        row = await self.db.get_connector_oauth_client(kind)
        if row and row["client_id"] and row["client_secret_encrypted"]:
            return row["client_id"], decrypt(
                row["client_secret_encrypted"], master_key()
            )
        return _env_client_creds(kind)

    async def set_client_creds(
        self, kind: str, client_id: str, client_secret: str
    ) -> None:
        connector = await self.get(kind)
        if connector is None:
            raise ConnectorError(f"unknown connector kind: {kind}")
        async with self._definition_lock:
            self._require_definition(connector)
            now = datetime.now(UTC).isoformat()
            await self.db.set_connector_oauth_client(
                kind, client_id, encrypt(client_secret, master_key()), now
            )

    async def clear_client_creds(self, kind: str) -> bool:
        connector = await self.get(kind)
        if connector is None:
            return False
        async with self._definition_lock:
            self._require_definition(connector)
            return await self.db.delete_connector_oauth_client(kind)

    def _require_definition(self, connector: ConnectorBase) -> None:
        if isinstance(connector, CustomConnector) and (
            connector.definition_identity is None
            or self._definitions.get(connector.kind) is not connector.definition_identity
        ):
            raise ConnectorError("Custom connector definition was deleted; start a new connection")

    async def resolve_authorization_creds(self, connector: ConnectorBase) -> tuple[str, str] | None:
        """Capture credentials only while they still belong to this definition."""
        async with self._definition_lock:
            self._require_definition(connector)
            return await self.resolve_client_creds(connector.kind)

    async def client_config(self, kind: str, public_base: str) -> dict[str, Any]:
        """Non-secret view of a kind's OAuth client config for the UI: whether
        it's configured, the client_id, the source, and the redirect URI to
        register with the provider. The router resolves `public_base` from
        explicit configuration or the browser-facing request."""
        if await resolve_connector(self.db, kind) is None:
            raise ConnectorError(f"unknown connector kind: {kind}")
        row = await self.db.get_connector_oauth_client(kind)
        client_id: str | None
        source: str | None
        if row and row["client_id"]:
            client_id, source = row["client_id"], "db"
        else:
            env = _env_client_creds(kind)
            client_id, source = (env[0], "env") if env else (None, None)
        return {
            "kind": kind,
            "configured": client_id is not None,
            "client_id": client_id,
            "source": source,
            "redirect_uri": f"{public_base}/api/connectors/oauth/callback",
        }

    # --- catalog + installations -----------------------------------------

    @staticmethod
    def _catalog_entry(connector: ConnectorBase, *, available: bool) -> dict[str, Any]:
        return {
            "kind": connector.kind,
            "display_name": connector.display_name,
            "category": connector.category,
            "allows_multiple": connector.allows_multiple,
            "available": available,
            "scopes": list(getattr(connector.oauth, "default_scopes", [])),
            "custom": getattr(connector, "is_custom", False),
            "setup_url": getattr(connector, "setup_url", "") or None,
            "setup_steps": list(getattr(connector, "setup_steps", [])),
        }

    async def catalog(self) -> list[dict[str, Any]]:
        connectors = list(all_connectors()) + [
            CustomConnector(row) for row in await self.db.list_custom_connectors()
        ]
        return [
            self._catalog_entry(connector, available=(await self.resolve_client_creds(connector.kind)) is not None)
            for connector in connectors
        ]

    async def list_installations(
        self, user_id: str | None = None
    ) -> list[dict[str, Any]]:
        """This account's connected accounts. An installation *is* somebody's
        access to a third-party account, so this is the per-account half of a
        connector (multi-tenancy.md §5)."""
        return await self.db.load_connector_installations(user_id=user_id)

    async def get_installation(
        self, installation_id: str, user_id: str | None = None
    ) -> dict[str, Any] | None:
        return await self.db.get_connector_installation(installation_id, user_id)

    async def complete_install(
        self,
        *,
        kind: str,
        token_set: OAuthTokenSet,
        requested_label: str | None = None,
        user_id: str | None = None,
    ) -> dict[str, Any]:
        """Persist a freshly-authorized installation, upserting on the
        provider account so re-auth of the same account overwrites.

        `user_id` comes from the pending login the *authenticated* `oauth/start`
        created, because the provider's redirect carries no bearer of ours
        (multi-tenancy.md §5). The upsert is scoped to it too: two accounts
        connecting the same GitHub account get one installation each, since the
        tokens are separately theirs.
        """
        connector = await self.get(kind)
        if connector is None:
            raise ConnectorError(f"unknown connector kind: {kind}")
        prepared = await self.prepare_install(connector=connector, token_set=token_set, requested_label=requested_label, user_id=user_id)
        return await self.persist_install(prepared)

    async def prepare_install(
        self, *, connector: ConnectorBase, token_set: OAuthTokenSet, requested_label: str | None = None,
        user_id: str | None = None, target_installation_id: str | None = None,
    ) -> PreparedInstallation:
        definition = connector.definition_identity if isinstance(connector, CustomConnector) else None
        if isinstance(connector, CustomConnector) and definition is None:
            raise ConnectorError("Custom connector definition identity was not captured")
        await self.require_available_owner(user_id)
        external_id, identity_label = await connector.fetch_external_identity(
            token_set
        )
        label = requested_label or identity_label
        secret_key = await UserManager(self.db).data_key_for(user_id)
        return PreparedInstallation(connector.kind, token_set, external_id, label, user_id, secret_key, definition,
                                    target_installation_id)

    async def require_reconnect_target(
        self, connector: ConnectorBase, installation_id: str, user_id: str | None,
    ) -> None:
        """Check the original login target; binding checks again in its transaction."""
        await self.require_available_owner(user_id)
        async with self._definition_lock:
            self._require_definition(connector)
            row = await self.db.get_connector_installation(installation_id, user_id)
            if row is None or row["user_id"] != user_id or row["kind"] != connector.kind:
                raise ConnectorError("connector installation not found")

    async def require_available_owner(self, user_id: str | None) -> None:
        """Captured login ownership does not authorize a later remote call."""
        if not await owner_is_available(self.db, user_id):
            raise ConnectorError("Account is unavailable")

    async def persist_install(self, prepared: PreparedInstallation) -> dict[str, Any]:
        if prepared.external_id:
            # Installation identity does not exist yet on a first callback.
            # Serialize its resolution before taking the chosen installation's
            # normal refresh/update/delete lock. No provider I/O occurs here.
            async with self._lock((prepared.user_id, prepared.kind, prepared.external_id)):
                return await self._store_installation(prepared)
        return await self._store_installation(prepared)

    async def _store_installation(self, prepared: PreparedInstallation) -> dict[str, Any]:
        """Persist an authorization under its captured account DEK."""
        kind, token_set = prepared.kind, prepared.token_set
        external_id, user_id = prepared.external_id, prepared.user_id
        blob = encrypt(_serialize_token_set(token_set), prepared.secret_key)
        token_expires_at = _expires_iso(token_set.expires_at_epoch)

        existing_id = prepared.target_installation_id
        if existing_id is None and external_id:
            existing = await self.db.get_connector_installation_by_account(kind, external_id, user_id)
            existing_id = existing["id"] if existing is not None else None
        iid = existing_id if existing_id is not None else uuid.uuid4().hex[:12]
        async with self._lock(iid), self._definition_lock:
            # No remote I/O, and no wait for an installation lock, holds the
            # definition owner. Deletion and this commit share its ordering.
            if (prepared.definition_identity is not None
                    and self._definitions.get(kind) is not prepared.definition_identity):
                raise ConnectorError("Custom connector definition was deleted; start a new connection")
            try:
                return await self.db.authorize_connector_installation(
                    installation_id=iid, kind=kind, label=prepared.label,
                    secret_encrypted=blob, created_at=datetime.now(UTC).isoformat(),
                    external_account_id=external_id or None, scopes=list(token_set.scopes),
                    token_expires_at=token_expires_at, user_id=user_id,
                    require_custom_definition=prepared.definition_identity is not None,
                    expected_existing_id=existing_id,
                )
            except ValueError as exc:
                raise ConnectorError(str(exc)) from exc

    async def update_installation(
        self, installation_id: str, user_id: str | None = None, **fields: Any
    ) -> dict[str, Any]:
        async with self._lock(installation_id):
            if await self.db.get_connector_installation(installation_id, user_id) is None:
                raise ConnectorError("connector installation not found")
            updated = await self.db.update_connector_installation(installation_id, **fields)
            if updated is None:
                raise ConnectorError("connector installation not found")
            return updated

    async def delete_installation(
        self, installation_id: str, user_id: str | None = None
    ) -> None:
        # Resolved within the account first: a DELETE straight to the id would
        # remove anybody's, and report the same success either way.
        async with self._lock(installation_id):
            if await self.db.get_connector_installation(installation_id, user_id) is None:
                raise ConnectorError("connector installation not found")
            if not await self.db.delete_connector_installation(installation_id):
                raise ConnectorError("connector installation not found")

    async def mark_needs_reconnect(
        self,
        installation_id: str,
        stamp: str,
        error_code: str = "auth_rejected",
        user_id: str | None = None,
    ) -> GrantDisposition:
        async with self._lock(installation_id):
            return await self._reject_grant(installation_id, user_id, stamp, error_code)

    async def _reject_grant(self, installation_id, user_id, stamp, error_code) -> GrantDisposition:
        disposition, changed = await self.db.reject_connector_grant(installation_id, user_id, stamp, error_code)
        # The other half of the Gmail case (§9 G0): the app knew the connector
        # was broken, but nothing counted how long it stayed that way.
        if changed:
            _mon_record(_MonEvent(
                kind="connector_reconnect_needed", ok=False, error_code=error_code,
                detail={"installation_id": installation_id},
            ))
        return disposition

    # --- token access (the internal /token route) ------------------------

    def _lock(self, installation_id: str | tuple[str | None, str, str]) -> asyncio.Lock:
        lock = self._locks.get(installation_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[installation_id] = lock
        return lock

    async def _save_refresh_result(self, installation_id: str, **fields: Any) -> None:
        """Commit under the original installation lock and current account."""
        try:
            saved = await self.db.update_connector_installation(
                installation_id, require_available_owner=True, **fields,
            )
        except ValueError as exc:
            raise ConnectorError(str(exc)) from exc
        if saved is None:
            raise ConnectorError("connector installation not found")

    async def get_access_token(
        self, installation_id: str, user_id: str | None = None
    ) -> dict[str, Any]:
        """Each authorized waiter joins the installation's original owned work."""
        if self._closing:
            raise ConnectorTokenError("Connector manager is shutting down", kind="unavailable", code="shutdown")
        await self._require_token_owner(user_id)
        inst = await self.db.get_connector_installation(installation_id, user_id)
        if inst is None or inst.get("user_id") != user_id:
            raise ConnectorTokenError("connector installation not found", kind="unavailable", code="gone")
        if self._closing:
            raise ConnectorTokenError("Connector manager is shutting down", kind="unavailable", code="shutdown")
        task = self._token_tasks.get(installation_id)
        if task is None:
            secrets: list[str] = []
            task = asyncio.create_task(self._get_access_token(installation_id, user_id, secrets),
                                       name=f"connector-token-{installation_id}")
            self._token_tasks[installation_id] = task

            def retired(original: asyncio.Task[dict[str, Any]]) -> None:
                try:
                    if original.cancelled():
                        logger.error("Owned connector token operation was cancelled installation=%s", installation_id)
                    elif (error := original.exception()) is not None:
                        logger.error("Connector token operation failed installation=%s\n%s", installation_id,
                                     exception_diagnostic(error, secrets=tuple(secrets)))
                finally:
                    secrets.clear()
                    if self._token_tasks.get(installation_id) is original:
                        self._token_tasks.pop(installation_id)

            task.add_done_callback(retired)
        result = await asyncio.shield(task)
        await self._require_token_owner(user_id)
        return result

    async def _require_token_owner(self, user_id: str | None) -> None:
        try:
            await self.require_available_owner(user_id)
        except ConnectorError as error:
            raise ConnectorTokenError(str(error), kind="unavailable", code="account_unavailable") from error

    async def _get_access_token(
        self, installation_id: str, user_id: str | None, secrets: list[str],
    ) -> dict[str, Any]:
        """Resolve a token, endpoint and rejection stamp under one installation lock.

        Refresh near expiry; only a proven permanent grant rejection can mark
        this captured authorization as needing reconnection.

        The caller supplies its captured current account; a missing owner
        cannot become an unscoped read of a stored secret.
        """
        try:
            secret_key = await UserManager(self.db).data_key_for(user_id)
        except ValueError as error:
            raise ConnectorTokenError("Account is unavailable", kind="unavailable", code="account_unavailable") from error
        async with self._lock(installation_id):
            # Kind deletion bypasses individual installation locks. Capture the
            # installation, endpoint and client together before remote I/O, so
            # a rebuilt kind cannot contribute credentials to an old refresh.
            async with self._definition_lock:
                await self._require_token_owner(user_id)
                inst = await self.db.get_connector_installation(installation_id, user_id)
                if inst is None or inst.get("user_id") != user_id:
                    raise ConnectorTokenError("connector installation not found", kind="unavailable", code="gone")
                if inst["needs_reconnect"]:
                    raise ConnectorTokenError("Connector authorization requires reconnecting", kind="reconnect",
                                              code=inst["last_refresh_error_code"] or "auth_rejected")
                blob = await self.db.get_connector_secret(installation_id)
                if blob is None:
                    raise ConnectorTokenError("connector secret missing", kind="unavailable", code="secret_missing")
                ts = _deserialize_token_set(decrypt(blob, secret_key))
                secrets.extend(value for value in (ts.access_token, ts.refresh_token) if value)
                connector = await resolve_connector(self.db, inst["kind"])
                api_base = getattr(connector, "api_base", None)
                stamp = grant_stamp(inst, blob, api_base)
                near_expiry = (
                    ts.expires_at_epoch
                    and ts.expires_at_epoch - time.time() < _REFRESH_SKEW_SECONDS
                )
                creds = await self.resolve_client_creds(inst["kind"]) if near_expiry and ts.refresh_token else None
            if near_expiry and ts.refresh_token:
                provider = getattr(connector, "oauth", None)
                if provider is None:
                    raise ConnectorTokenError(f"no provider for kind {inst['kind']!r}", kind="configuration", code="provider_missing")
                if creds is None:
                    raise ConnectorTokenError(
                        f"{inst['kind']} OAuth client is not configured", kind="configuration", code="client_missing"
                    )
                secrets.append(creds[1])
                try:
                    new_ts = await provider.refresh(
                        client_id=creds[0],
                        client_secret=creds[1],
                        refresh_token=ts.refresh_token,
                    )
                except ConnectorTokenError as error:
                    if error.kind == "reconnect":
                        disposition = await self._reject_grant(installation_id, user_id, stamp, error.code)
                        if disposition != "applied":
                            raise ConnectorTokenError("Old authorization was rejected; current authorization was left unchanged",
                                                      kind="unknown", code=disposition) from error
                    raise
                except httpx.HTTPError as error:
                    raise ConnectorTokenError("Token refresh transport failed; try again later", kind="temporary", code="transport") from error
                except Exception as error:
                    if not await owner_is_available(self.db, user_id):
                        raise ConnectorTokenError("Account is unavailable", kind="unavailable", code="account_unavailable") from error
                    raise ConnectorTokenError("Token refresh failed with an unclassified provider error", kind="unknown", code="refresh_failed") from error
                # Providers may omit the refresh token when the old one stands.
                if new_ts.refresh_token is None:
                    new_ts.refresh_token = ts.refresh_token
                ts = new_ts
                secrets.extend(value for value in (ts.access_token, ts.refresh_token) if value)
                blob = encrypt(_serialize_token_set(ts), secret_key)
                await self._save_refresh_result(
                    installation_id,
                    secret_encrypted=blob,
                    token_expires_at=_expires_iso(ts.expires_at_epoch),
                    needs_reconnect=False,
                    last_refresh_error_code=None,
                )

            return {
                "access_token": ts.access_token,
                "expires_at_epoch": ts.expires_at_epoch,
                "api_base": api_base,
                "grant_stamp": grant_stamp(inst, blob, api_base),
            }

    # --- agent-scoped enablement -----------------------------------------

    def _prune_binding_operations(self) -> None:
        now = time.monotonic()
        for operation_id in [oid for oid, op in self._binding_operations.items() if op.expires_at <= now]:
            del self._binding_operations[operation_id]

    def _retire_binding_operations(
        self, agent_id: str, user_id: str | None, installation_id: str | None = None,
    ) -> None:
        for op in self._binding_operations.values():
            scope = op.scope
            if (op.disposition is None and scope.agent_id == agent_id and scope.user_id == user_id
                and (installation_id is None or scope.installation_id == installation_id)):
                op.finish("retired")

    async def get_agent_connector_bindings(
        self, agent_id: str, user_id: str | None,
    ) -> tuple[list[str], dict[str, uuid.UUID]]:
        async with self._binding_lock:
            try:
                ids = await self.db.list_owned_agent_connectors(agent_id, user_id)
            except ValueError as error:
                raise ConnectorError(str(error)) from error
            self._prune_binding_operations()
            pending = {
                op.scope.installation_id: oid for oid, op in self._binding_operations.items()
                if op.disposition is None and op.scope.agent_id == agent_id and op.scope.user_id == user_id
            }
            return ids, pending

    async def _binding_enabled(self, scope: _BindingScope) -> bool:
        try:
            return await self.db.read_owned_agent_connector(scope.agent_id, scope.installation_id, scope.user_id)
        except ValueError as error:
            raise ConnectorError(str(error)) from error

    async def reserve_agent_connector(
        self, agent_id: str, installation_id: str, enabled: bool, intent_id: uuid.UUID,
        user_id: str | None,
    ) -> tuple[uuid.UUID, bool]:
        scope = _BindingScope(agent_id, installation_id, user_id)
        async with self._binding_lock:
            await self._binding_enabled(scope)
            self._prune_binding_operations()
            account_operations = 0
            for oid, op in self._binding_operations.items():
                if op.scope.user_id != user_id:
                    continue
                account_operations += 1
                if op.scope == scope and op.disposition is None:
                    # A lost preparation response can be retried without granting another intent the write ID.
                    return oid, op.intent_id == intent_id and op.enabled == enabled
            if account_operations >= _BINDING_OPERATION_LIMIT_PER_ACCOUNT:
                raise ConnectorBindingOperationsFull("Connector binding operations are busy")
            operation_id = uuid.uuid4()
            self._binding_operations[operation_id] = _BindingOperation(
                scope, intent_id, enabled, time.monotonic() + _BINDING_OPERATION_SECONDS,
            )
            return operation_id, True

    async def apply_agent_connector(
        self, agent_id: str, installation_id: str, operation_id: uuid.UUID, user_id: str | None,
    ) -> None:
        scope = _BindingScope(agent_id, installation_id, user_id)
        async with self._binding_lock:
            self._prune_binding_operations()
            operation = self._binding_operations.get(operation_id)
            if operation is None or operation.scope != scope or operation.disposition == "retired":
                raise ConnectorBindingOperationRetired("Connector binding operation is retired")
            if operation.disposition == "applied":
                await self._binding_enabled(scope)
                return
            await complete_cleanup(self._write_agent_connector(scope, operation.enabled, operation))

    async def settle_agent_connector(
        self, agent_id: str, installation_id: str, operation_id: uuid.UUID, user_id: str | None,
    ) -> tuple[BindingSettlement, bool]:
        scope = _BindingScope(agent_id, installation_id, user_id)
        async with self._binding_lock:
            enabled = await self._binding_enabled(scope)
            self._prune_binding_operations()
            operation = self._binding_operations.get(operation_id)
            if operation is None or operation.scope != scope:
                return "unavailable", enabled
            if operation.disposition is not None:
                return operation.disposition, enabled
            operation.finish("retired")
            return "retired", enabled

    async def set_agent_connector(
        self,
        agent_id: str,
        installation_id: str,
        enabled: bool,
        user_id: str | None,
    ) -> None:
        scope = _BindingScope(agent_id, installation_id, user_id)
        async with self._binding_lock:
            await complete_cleanup(self._write_agent_connector(scope, enabled))

    async def _write_agent_connector(
        self, scope: _BindingScope, enabled: bool, operation: _BindingOperation | None = None,
    ) -> None:
        # Keep the commit and receipt together even if the HTTP waiter is cancelled.
        try:
            await self.db.set_owned_agent_connector(scope.agent_id, scope.installation_id, enabled, scope.user_id)
        except ValueError as error:
            if operation is not None:
                operation.finish("retired")
            raise ConnectorError(str(error)) from error
        if operation is not None:
            operation.finish("applied")
        else:
            self._prune_binding_operations()
            self._retire_binding_operations(scope.agent_id, scope.user_id, scope.installation_id)

    async def replace_agent_connectors(
        self,
        agent_id: str,
        installation_ids: list[str],
        user_id: str | None = None,
    ) -> list[str]:
        async def replace() -> list[str]:
            try:
                ids = await self.db.replace_agent_connectors(agent_id, installation_ids, user_id)
            except ValueError as error:
                raise ConnectorError(str(error)) from error
            self._prune_binding_operations()
            self._retire_binding_operations(agent_id, user_id)
            return ids

        async with self._binding_lock:
            return await complete_cleanup(replace())

    async def enabled_installations_for_agent(
        self, agent_id: str
    ) -> list[ConnectorInstallation]:
        """The agent's enabled connectors as ConnectorInstallation views — what
        SessionManager passes to the backend at spawn time."""
        rows = await self.db.get_enabled_connectors_for_agent(agent_id)
        return [ConnectorInstallation.from_row(r) for r in rows]
