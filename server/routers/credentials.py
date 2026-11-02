"""REST endpoints for per-backend credentials.

Secrets are encrypted at rest with their owning account's DEK (see
`server/crypto.py`). The wire format never includes the plaintext secret —
the only way to use a credential is to attach it to a session that the
backend then resolves at run time.

Two acquisition paths:
  - POST /api/credentials                — paste an API key (legacy/manual)
  - POST /api/credentials/oauth/start    — sign in via Claude OAuth
    POST /api/credentials/oauth/complete — submit code, store issued token
    POST /api/credentials/oauth/cancel   — abort an in-flight login

The OAuth flow yields a long-lived `sk-ant-…` token, which is stored the
same way as a manually-pasted API key.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from ..auth import verify_token
from ..crypto import encrypt
from ..database import Database
from ..deps import ScopeUser, SessionMgr
from ..harness import LoginDriver, LoginMethod, get_harness, has_backend
from ..harness.login import OAuthCompletion
from ..models import (
    AuthType,
    BackendKind,
    CreateCredentialRequest,
    CredentialInfo,
    CredentialStatus,
    OAuthCancelResponse,
    UpdateCredentialRequest,
)
from ..oauth_credentials import OAuthCredentials
from ..oauth_errors import OAuthLoginError
from ..users import UserManager

if TYPE_CHECKING:
    from ..codex_login import CodexLoginSession

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/credentials", tags=["credentials"])

# Set at app startup. Module-level lets the router stay light without DI.
_db = None


def set_db(db) -> None:
    global _db
    _db = db


def _require_db():
    if _db is None:
        raise HTTPException(
            status_code=503, detail="credential database not yet initialized"
        )
    return _db


def _row_to_info(row: dict) -> CredentialInfo:
    return CredentialInfo.model_validate({
        "id": row["id"], "backend": row["backend"], "label": row["label"],
        "auth_type": row["auth_type"], "created_at": row["created_at"],
        "status": row.get("status") or "active", "token_expires_at": row.get("token_expires_at"),
        "needs_reconnect": bool(row.get("needs_reconnect", False)),
        "last_refresh_error_code": row.get("last_refresh_error_code"),
    })


@router.get("", response_model=list[CredentialInfo])
async def list_credentials(user_id: ScopeUser = None, _: str = Depends(verify_token)):
    rows = await _require_db().load_credentials(user_id)
    return [_row_to_info(r) for r in rows]


@router.post("", response_model=CredentialInfo, status_code=status.HTTP_201_CREATED)
async def create_credential(
    req: CreateCredentialRequest,
    user_id: ScopeUser = None,
    _: str = Depends(verify_token),
):
    db = _require_db()
    cid = uuid.uuid4().hex[:12]
    created_at = datetime.now(UTC).isoformat()
    secret_key = await UserManager(db).data_key_for(user_id)
    await db.save_credential(
        credential_id=cid,
        backend=req.backend.value,
        label=req.label,
        auth_type=req.auth_type.value,
        secret_encrypted=encrypt(req.secret, secret_key),
        created_at=created_at,
        user_id=user_id,
    )
    return CredentialInfo(
        id=cid,
        backend=req.backend,
        label=req.label,
        auth_type=req.auth_type,
        created_at=created_at,
    )


@router.patch("/{credential_id}", response_model=CredentialInfo)
async def update_credential(
    credential_id: str,
    req: UpdateCredentialRequest,
    user_id: ScopeUser = None,
    _: str = Depends(verify_token),
):
    db = _require_db()
    secret_key = await UserManager(db).data_key_for(user_id)
    return await _update_credential(db, credential_id, req, user_id, secret_key)


async def _update_credential(
    db: Database, credential_id: str, req: UpdateCredentialRequest, user_id: str | None, secret_key: str,
) -> CredentialInfo:
    existing = await db.get_credential(credential_id, user_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="credential not found")

    update_kwargs: dict = {}
    if req.label is not None:
        update_kwargs["label"] = req.label
    if req.secret is not None:
        update_kwargs["secret_encrypted"] = encrypt(
            req.secret, secret_key
        )
        # A fresh secret recovers a credential the CLI had rejected — clear
        # the reactive needs_reconnect flag (harness-credential-reauth.md §5).
        update_kwargs["status"] = CredentialStatus.active.value
        update_kwargs["needs_reconnect"] = False
        update_kwargs["last_refresh_error_code"] = None

    if update_kwargs:
        await db.update_credential(credential_id, **update_kwargs)

    updated = await db.get_credential(credential_id)
    assert updated is not None
    return _row_to_info(updated)


@router.delete("/{credential_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_credential(
    session_manager: SessionMgr,
    credential_id: str,
    user_id: ScopeUser = None,
    _: str = Depends(verify_token),
):
    row = await session_manager.delete_credential_binding(credential_id, user_id)
    if row is None:
        raise HTTPException(status_code=404, detail="credential not found")
    # Revoke any on-disk login state for this credential's harness (Codex
    # rmtree's its CODEX_HOME; Claude has nothing on disk). The harness owns
    # the cleanup — no backend-kind branching here.
    driver = (
        get_harness(row["backend"]).login
        if has_backend(row.get("backend"))
        else None
    )
    if driver is not None:
        driver.cleanup_credential(credential_id)


# ---------------------------------------------------------------------------
# OAuth (in-app subscription login)
# ---------------------------------------------------------------------------


def _login_driver(backend: str) -> LoginDriver:
    """The backend's login driver, or a clean 501 instead of an AttributeError.

    `RuntimeProfile.login` is optional because a backend need not offer an
    in-app login at all, while each route below names one that does. Asking
    through here keeps that assumption in a single place.
    """
    driver = get_harness(backend).login
    if driver is None:
        raise HTTPException(
            status_code=501, detail=f"{backend} has no in-app login"
        )
    return driver


class OAuthStartRequest(BaseModel):
    # Defaults to claude-code because this is the *redirect* flow, which is
    # Claude's login method. Codex is wired too, by device code, in the
    # `/codex/*` routes below. `oauth_start` asks the harness and refuses any
    # backend whose driver logs in some other way, so the field is a real
    # choice rather than a placeholder.
    backend: BackendKind = BackendKind.claude_code


class OAuthStartResponse(BaseModel):
    login_id: str
    device_url: str


class OAuthCompleteRequest(BaseModel):
    login_id: str
    code: str = Field(min_length=1)
    label: str = Field(min_length=1)
    # Re-authorization (harness-credential-reauth.md §5): when set, update
    # this EXISTING credential's secret in place + clear its needs_reconnect
    # flag, instead of minting a new row — so agent/session bindings survive.
    credential_id: str | None = None


class OAuthCancelRequest(BaseModel):
    login_id: str


@router.post(
    "/oauth/start",
    response_model=OAuthStartResponse,
    status_code=status.HTTP_201_CREATED,
)
async def oauth_start(
    req: OAuthStartRequest,
    user_id: ScopeUser = None,
    _: str = Depends(verify_token),
):
    # No row is written here — `oauth_complete` is where the credential lands.
    # The owner is remembered on the login session, so only the account that
    # started a sign-in can finish it.
    login = get_harness(req.backend.value).login
    if login is None or login.method != LoginMethod.oauth_redirect:
        raise HTTPException(
            status_code=400,
            detail=f"OAuth redirect login isn't available for {req.backend.value}",
        )
    try:
        session = await login.start(user_id=user_id)
    except OAuthLoginError as e:
        raise HTTPException(e.status_code, str(e)) from e
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e))

    return OAuthStartResponse(login_id=session.id, device_url=session.url)


@router.post("/oauth/complete", response_model=CredentialInfo, status_code=status.HTTP_201_CREATED)
async def oauth_complete(
    req: OAuthCompleteRequest, user_id: ScopeUser = None, _: str = Depends(verify_token),
) -> CredentialInfo:
    """Await the original login's complete credential commit."""
    request = OAuthCompletion(user_id=user_id, **req.model_dump())
    try:
        return await _login_driver(BackendKind.claude_code.value).complete_oauth(
            request, OAuthCredentials(_require_db()),
        )
    except KeyError as error:
        raise HTTPException(404, "unknown login_id") from error
    except OAuthLoginError as error:
        raise HTTPException(error.status_code, str(error)) from error


@router.post("/oauth/cancel", response_model=OAuthCancelResponse)
async def oauth_cancel(
    req: OAuthCancelRequest, user_id: ScopeUser = None, _: str = Depends(verify_token),
) -> OAuthCancelResponse:
    driver = _login_driver(BackendKind.claude_code.value)
    started = driver.get(req.login_id)
    if started is None or started.user_id != user_id:
        raise HTTPException(404, "unknown login_id")
    try:
        result = await driver.cancel(req.login_id)
    except KeyError as error:
        raise HTTPException(404, "unknown login_id") from error
    except OAuthLoginError as error:
        raise HTTPException(error.status_code, str(error)) from error
    assert result is not None  # OAuth redirect drivers return the original result.
    return result


# ---------------------------------------------------------------------------
# Codex (ChatGPT) in-app device-auth login
# ---------------------------------------------------------------------------
#
# Unlike Claude (HTTP + pasted code), Codex auth is directory-backed: we run
# `codex login --device-auth` against a per-credential CODEX_HOME and surface
# the URL + one-time code. Codex polls for authorization and writes auth.json
# into that dir on success; the credential row points at the dir, no secret.


class CodexLoginStartRequest(BaseModel):
    label: str = Field(min_length=1)
    # Re-authorization (harness-credential-reauth.md §5): when set, re-run
    # `codex login` against this existing credential's own CODEX_HOME so its
    # auth.json is refreshed in place (no new row, no new dir, bindings kept).
    reauth_credential_id: str | None = None


class CodexLoginStartResponse(BaseModel):
    # Returns immediately after spawning — the URL + code aren't ready yet
    # (codex fetches them from auth.openai.com); the UI polls `status` for them.
    login_id: str


class CodexLoginStatusResponse(BaseModel):
    state: str  # CodexLoginState value
    verification_url: str | None = None
    user_code: str | None = None
    message: str | None = None
    credential: CredentialInfo | None = None


class CodexLoginCancelRequest(BaseModel):
    login_id: str


@router.post(
    "/codex/start",
    response_model=CodexLoginStartResponse,
    status_code=status.HTTP_201_CREATED,
)
async def codex_login_start(
    req: CodexLoginStartRequest,
    user_id: ScopeUser = None,
    _: str = Depends(verify_token),
):
    if req.reauth_credential_id is not None:
        db = _require_db()
        existing = await db.get_credential(req.reauth_credential_id, user_id)
        if existing is None:
            raise HTTPException(status_code=404, detail="credential not found")
        if existing["backend"] != BackendKind.codex.value:
            raise HTTPException(
                status_code=400,
                detail="credential backend mismatch for re-authorization",
            )
    try:
        session = await _login_driver(BackendKind.codex.value).start(
            req.label,
            reauth_credential_id=req.reauth_credential_id,
            user_id=user_id,
        )
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e))
    return CodexLoginStartResponse(login_id=session.id)


@router.get("/codex/{login_id}/status", response_model=CodexLoginStatusResponse)
async def codex_login_status(
    login_id: str, user_id: ScopeUser = None, _: str = Depends(verify_token)
):
    """Poll an in-flight Codex login. On success, persist the credential row
    (pointing at the CODEX_HOME dir) once and return it."""
    from ..codex_login import CodexLoginState

    session = _login_driver(BackendKind.codex.value).get(login_id)
    if session is None or getattr(session, "user_id", None) != user_id:
        raise HTTPException(status_code=404, detail="unknown login_id")

    credential: CredentialInfo | None = None
    if session.state == CodexLoginState.success:
        db = _require_db()
        secret_key = await UserManager(db).data_key_for(user_id)
        credential = await _persist_codex_login(db, session, user_id, secret_key)

    return CodexLoginStatusResponse(
        state=session.state.value,
        verification_url=session.verification_url,
        user_code=session.user_code,
        message=session.message,
        credential=credential,
    )


async def _persist_codex_login(
    db: Database, session: CodexLoginSession, user_id: str | None, secret_key: str,
) -> CredentialInfo | None:
    from ..codex_login import CodexLoginState

    # Concurrent status pollers share this login's one persistence operation.
    async with session._persist_lock:
        existing = await db.get_credential(session.credential_id, user_id)
        if session.persisted or session._reauth:
            if (existing is None or existing["user_id"] != user_id
                    or existing["backend"] != BackendKind.codex.value):
                session.state = CodexLoginState.error
                session.message = "The signed-in credential is no longer available. Start a new sign-in."
                return None
            if session.persisted:
                return _row_to_info(existing)
        # No real secret for Codex — the credential *is* the CODEX_HOME dir.
        # Store the dir path (encrypted, harmless) so the row is self-describing;
        # session_manager resolves the dir deterministically from credential_id.
        if existing is not None:
            # Re-authorization: the row already exists (its CODEX_HOME just got
            # a fresh auth.json) — clear the needs_reconnect flag in place so
            # every binding keeps working (harness-credential-reauth.md §5).
            updated = await db.reauthorize_credential(
                session.credential_id,
                user_id=user_id,
                backend=BackendKind.codex.value,
                label=session.label,
                status=CredentialStatus.active.value,
                needs_reconnect=False,
                last_refresh_error_code=None,
            )
            if updated is None:
                session.state = CodexLoginState.error
                session.message = "The signed-in credential is no longer available. Start a new sign-in."
                return None
            session.persisted = True
            credential = _row_to_info(updated)
        else:
            created_at = datetime.now(UTC).isoformat()
            await db.save_credential(
                credential_id=session.credential_id,
                backend=BackendKind.codex.value,
                label=session.label,
                auth_type=AuthType.oauth.value,
                secret_encrypted=encrypt(
                    session.codex_home, secret_key
                ),
                created_at=created_at,
                user_id=user_id,
            )
            session.persisted = True
            credential = CredentialInfo(
                id=session.credential_id,
                backend=BackendKind.codex,
                label=session.label,
                auth_type=AuthType.oauth,
                created_at=created_at,
            )

        return credential


@router.post("/codex/cancel", status_code=status.HTTP_204_NO_CONTENT)
async def codex_login_cancel(
    req: CodexLoginCancelRequest,
    user_id: ScopeUser = None,
    _: str = Depends(verify_token),
):
    driver = _login_driver(BackendKind.codex.value)
    session = driver.get(req.login_id)
    if session is not None and getattr(session, "user_id", None) == user_id:
        await driver.cancel(req.login_id)
