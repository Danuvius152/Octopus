"""Persist the complete OAuth credential under the original login task."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime

from .crypto import encrypt
from .database import Database
from .harness.login import OAuthCompletion
from .models import AuthType, BackendKind, CredentialInfo
from .oauth_errors import OAuthLoginError
from .oauth_providers import OAuthTokenSet
from .users import UserManager


class OAuthCredentials:
    def __init__(self, database: Database) -> None:
        self.db = database

    async def validate(self, request: OAuthCompletion) -> None:
        if request.user_id is None:
            raise OAuthLoginError("A current account is required", status_code=401)
        if request.credential_id is not None:
            existing = await self.db.get_credential(request.credential_id, request.user_id)
            if existing is None or existing["user_id"] != request.user_id:
                raise OAuthLoginError("credential not found", status_code=404)
            if existing["backend"] != BackendKind.claude_code.value:
                raise OAuthLoginError("credential backend mismatch for re-authorization")

    async def commit(self, request: OAuthCompletion, material: str | OAuthTokenSet) -> CredentialInfo:
        key = await UserManager(self.db).data_key_for(request.user_id)
        expires_at = None
        if isinstance(material, OAuthTokenSet):
            secret = json.dumps({
                "access_token": material.access_token,
                "refresh_token": material.refresh_token,
                "expires_at_epoch": material.expires_at_epoch,
                "scopes": list(material.scopes),
                "token_type": material.token_type,
            }, separators=(",", ":"))
            expires_at = datetime.fromtimestamp(material.expires_at_epoch, tz=UTC).isoformat()
        else:
            secret = material
        encrypted = encrypt(secret, key)
        if request.credential_id is not None:
            row = await self.db.reauthorize_credential(
                request.credential_id, user_id=request.user_id, backend=BackendKind.claude_code.value,
                label=request.label, auth_type=AuthType.oauth.value,
                secret_encrypted=encrypted, token_expires_at=expires_at,
                status="active", needs_reconnect=False, last_refresh_error_code=None,
            )
            if row is None:
                raise OAuthLoginError("credential not found", status_code=404)
            return CredentialInfo(**row)
        identity = uuid.uuid4().hex[:12]
        created_at = datetime.now(UTC).isoformat()
        try:
            await self.db.save_credential(
                identity, BackendKind.claude_code.value, request.label, AuthType.oauth.value,
                encrypted, created_at, user_id=request.user_id, token_expires_at=expires_at,
                require_available_owner=True,
            )
        except ValueError as error:
            raise OAuthLoginError(str(error), status_code=403) from error
        return CredentialInfo(
            id=identity, backend=BackendKind.claude_code, label=request.label,
            auth_type=AuthType.oauth, created_at=created_at, token_expires_at=expires_at,
        )
