"""Resolve owned credentials; shared refresh work outlives individual waiters."""

from __future__ import annotations

import asyncio
import json
import os
import time
from datetime import UTC, datetime
from typing import Any

from ..credential_bindings import CredentialBindingError, validate_credential_binding
from ..crypto import decrypt, encrypt
from ..harness import HarnessCredential, get_harness
from ..models import CredentialStatus
from ..oauth_errors import OAuthRefreshError, RefreshErrorCode
from ..oauth_providers import OAuthTokenSet, get_provider
from ..users import UserManager
from .base import (
    Session,
    SessionManagerBase,
    logger,
)


class CredentialsMixin(SessionManagerBase):

    async def _resolve_credential(
        self, session: Session, agent: dict[str, Any] | None, harness
    ) -> HarnessCredential | None:
        """Look up the effective credential and resolve it for `harness`.

        Effective id is `session.credential_id` if set, else the agent's
        (agent-refactor.md §5.2 / decision #2). The harness's profile
        `credential_style` picks the shape: `env_secret` decrypts the secret
        (refreshing an OAuth bundle if near expiry); `home_dir` resolves the
        CODEX_HOME directory. Returns None when nothing's attached / resolvable.
        """
        cred_id = session.credential_id or (
            agent.get("credential_id") if agent else None
        )
        return await self.resolve_credential_by_id(
            cred_id, backend=harness.profile.backend, context=f"session {session.id}", user_id=session.user_id,
        )

    async def resolve_credential_by_id(
        self,
        cred_id: str | None,
        *,
        backend: str,
        context: str = "",
        require_auth: bool = True,
        user_id: str | None = None,
    ) -> HarnessCredential | None:
        """Resolve an owned backend binding; an explicit invalid id never falls
        back to the host identity. Only an absent binding returns the default.

        Artifact lookup still validates owner/backend, but does not require an
        active account authentication: historical rollouts must remain cleanable.
        Execution additionally checks current authority here and at actual spawn.
        """
        if not cred_id:
            return None
        if self.db is None:
            raise CredentialBindingError("Credential not found")
        await validate_credential_binding(self.db, cred_id, backend, user_id)
        if require_auth:
            await self.db.require_run_authority(user_id, cred_id, backend)
        style = get_harness(backend).profile.credential_style
        if style == "home_dir":
            from ..codex_login import codex_home_for

            home = codex_home_for(cred_id)
            if require_auth and not os.path.exists(os.path.join(home, "auth.json")):
                raise CredentialBindingError("Credential has no completed login. Reconnect it before sending new work.")
            return HarnessCredential(backend=backend, auth_type="oauth", home_dir=home)

        if not require_auth:
            # Secret-backed (Claude): no per-credential on-disk artifact store to
            # locate, so artifact copy/cleanup needs no credential.
            return None

        if self._credential_refresh_closed:
            raise CredentialBindingError("Credential resolution is closing")
        identity = (user_id, cred_id)
        operation = self._credential_refreshes.get(identity)
        if operation is None:
            operation = asyncio.create_task(self._resolve_owned_secret(cred_id, backend, user_id))
            self._credential_refreshes[identity] = operation

            def finished(task):
                if self._credential_refreshes.get(identity) is task:
                    del self._credential_refreshes[identity]
                # All waiters may have cancelled; the owner still observes failure.
                if not task.cancelled():
                    task.exception()

            operation.add_done_callback(finished)
        resolved = await asyncio.shield(operation)
        if resolved is None:
            raise CredentialBindingError("Credential could not be resolved. Reconnect it before sending new work.")
        await self.db.require_run_authority(user_id, cred_id, backend)
        return resolved

    async def drain_credential_refreshes(self) -> None:
        self._credential_refresh_closed = True
        while self._credential_refreshes:
            await asyncio.shield(asyncio.gather(*tuple(self._credential_refreshes.values()), return_exceptions=True))

    async def _resolve_owned_secret(self, cred_id, backend, user_id):
        secret_key = await UserManager(self.db).data_key_for(user_id)
        await self.db.require_run_authority(user_id, cred_id, backend)
        row = await self.db.get_credential(cred_id, user_id)
        if row is None or row["user_id"] != user_id or row["backend"] != backend:
            raise CredentialBindingError("Credential not found")
        return await self._resolve_secret_credential(cred_id, row, secret_key)

    async def _resolve_secret_credential(
        self, cred_id: str, row: dict[str, Any], secret_key: str, *, refresh: bool = True,
    ) -> HarnessCredential | None:
        """Read and refresh with the caller's captured account DEK."""
        if row.get("needs_reconnect"):
            logger.warning(
                "Credential %s is in needs_reconnect state (%s)",
                cred_id,
                row.get("last_refresh_error_code"),
            )
            return None
        try:
            plaintext = decrypt(row["secret_encrypted"], secret_key)
        except ValueError:
            logger.warning(
                "Could not decrypt credential %s",
                cred_id,
            )
            return None

        # OAuth-token bundle (Pro/Max subscriber path): the secret is a
        # JSON blob, not a bare key. Refresh if close to expiry, then use
        # the access_token as the runtime secret.
        if row["auth_type"] == "oauth" and plaintext.startswith("{"):
            return await self._refresh_oauth_if_needed(
                credential_id=cred_id,
                backend=row["backend"],
                bundle_json=plaintext,
                secret_key=secret_key,
                captured=row,
                refresh=refresh,
            )

        # Either auth_type=api_key OR legacy auth_type=oauth where the
        # stored secret is the long-lived sk-ant- key from mint_api_key.
        # Both flow through ANTHROPIC_API_KEY at the backend.
        return HarnessCredential(
            backend=row["backend"],
            auth_type="api_key",
            secret=plaintext,
        )

    async def _refresh_oauth_if_needed(
        self,
        *,
        credential_id: str,
        backend: str,
        bundle_json: str,
        secret_key: str,
        captured: dict[str, Any],
        refresh: bool,
    ) -> HarnessCredential | None:
        """Refresh once, conditionally commit, then resolve the current authority."""
        assert self.db is not None  # Owned refreshes drain before this binding closes.
        try:
            bundle = json.loads(bundle_json)
        except json.JSONDecodeError:
            logger.warning(
                "Credential %s: stored OAuth bundle isn't valid JSON",
                credential_id,
            )
            return None

        if not isinstance(bundle, dict):
            return None
        access_token = bundle.get("access_token")
        refresh_token = bundle.get("refresh_token")
        expires_at_epoch = bundle.get("expires_at_epoch", 0)
        if not isinstance(access_token, str):
            logger.warning(
                "Credential %s: OAuth bundle missing access_token",
                credential_id,
            )
            return None

        minimum_lifetime = self._OAUTH_REFRESH_LEEWAY_SEC if refresh else 0
        if (
            isinstance(expires_at_epoch, (int, float))
            and expires_at_epoch - time.time() > minimum_lifetime
        ):
            return HarnessCredential(backend=backend, auth_type="oauth", secret=access_token)

        if not refresh:
            raise CredentialBindingError("Credential changed during refresh; try the current credential again.")

        failure: Exception | None = None
        fields: dict[str, Any] = {}
        if not isinstance(refresh_token, str) or not refresh_token:
            failure = OAuthRefreshError("OAuth bundle has no refresh token", RefreshErrorCode.refresh_token_other, reconnect=True)
        else:
            try:
                provider = get_provider(backend)
                new_ts: OAuthTokenSet = await provider.refresh_access_token(refresh_token)
            except Exception as error:
                failure = error
                logger.exception("Credential %s: OAuth refresh failed", credential_id)
            else:
                new_bundle = {
                    "access_token": new_ts.access_token, "refresh_token": new_ts.refresh_token,
                    "expires_at_epoch": new_ts.expires_at_epoch, "scopes": list(new_ts.scopes),
                    "token_type": new_ts.token_type,
                }
                fields = {
                    "secret_encrypted": encrypt(json.dumps(new_bundle, separators=(",", ":")), secret_key),
                    "token_expires_at": datetime.fromtimestamp(new_ts.expires_at_epoch, tz=UTC).isoformat(),
                    "needs_reconnect": False, "last_refresh_error_code": None,
                }
        if isinstance(failure, OAuthRefreshError) and failure.reconnect:
            fields = {"status": CredentialStatus.needs_reconnect.value, "needs_reconnect": True,
                      "last_refresh_error_code": failure.code.value}
        committed = await self.db.commit_credential_refresh(captured, **fields)
        # This also validates a replacement after a late provider success/failure.
        await self.db.require_run_authority(captured["user_id"], credential_id, backend)
        if failure is not None and committed:
            raise CredentialBindingError("Credential refresh failed; try again later.") from failure
        current = await self.db.get_credential(credential_id, captured["user_id"])
        if current is None or current["user_id"] != captured["user_id"]:
            raise CredentialBindingError("Credential not found")
        return await self._resolve_secret_credential(credential_id, current, secret_key, refresh=False)

    async def _mark_needs_reconnect(
        self, credential_id: str, code: RefreshErrorCode
    ) -> None:
        if self.db is None:
            return
        await self.db.update_credential(
            credential_id,
            status=CredentialStatus.needs_reconnect.value,
            needs_reconnect=True,
            last_refresh_error_code=code.value,
        )
