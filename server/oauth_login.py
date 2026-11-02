"""In-app OAuth login orchestrator — provider-agnostic.

Drives the PKCE+manual-paste flow:

  1. `start(provider_name)` generates a PKCE pair + state, asks the
     provider for an authorize URL, returns both to the caller.
  2. User opens URL, signs in, copies the `<code>#<state>` blob shown
     on the provider's callback page.
  3. `complete(request, credentials)` owns state validation, token exchange,
     api-key mint and the complete local credential commit in one task.

Per-provider knowledge (endpoints, client id, scopes, payload shapes)
lives in `server/oauth_providers.py`. This module owns lifecycle:
in-flight session bookkeeping, PKCE, state validation, error surfacing.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import secrets
import uuid
from dataclasses import dataclass, field
from enum import Enum

from .harness.login import OAuthCompletion, OAuthCredentialWriter
from .models import CredentialInfo, OAuthCancelResponse
from .oauth_errors import OAuthCancelled, OAuthLoginError, ScopeMissingError
from .oauth_providers import OAuthProvider, OAuthTokenSet, get_provider

logger = logging.getLogger(__name__)

# Default provider — used when callers don't specify one (back-compat for
# the pre-multi-provider API where everything was Claude).
DEFAULT_PROVIDER = "claude-code"

# Login attempts that never complete shouldn't accumulate forever.
_LOGIN_TTL_SECONDS = 15 * 60
# Match the device-login retirement grace; a slow provider remains owned.
_CANCEL_TIMEOUT_SECONDS = 5.0


class LoginState(str, Enum):
    awaiting_code = "awaiting_code"
    finalizing = "finalizing"
    committing = "committing"
    success = "success"
    error = "error"
    cancelled = "cancelled"


@dataclass
class LoginSession:
    id: str
    url: str
    provider_name: str = DEFAULT_PROVIDER
    state: LoginState = LoginState.awaiting_code
    message: str | None = None
    # Who started this login (multi-tenancy.md §5). The id is unguessable, which
    # is why nothing has gone wrong, not a reason it is authorised: without this
    # another account could poll somebody's sign-in and have the credential
    # persisted into their own.
    user_id: str | None = None
    _verifier: str = field(default="", repr=False)
    _state: str = field(default="", repr=False)
    _created_at: float = field(default=0.0, repr=False)
    _request: OAuthCompletion | None = field(default=None, repr=False)
    _task: asyncio.Task[CredentialInfo] | None = field(default=None, repr=False)


class OAuthLoginManager:
    """Holds in-flight OAuth attempts. Single instance, app-lifetime."""

    def __init__(self) -> None:
        self._sessions: dict[str, LoginSession] = {}
        self._closing = False

    # ---------------------------------------------------------------- public

    async def start(
        self,
        provider_name: str = DEFAULT_PROVIDER,
        *,
        user_id: str | None = None,
    ) -> LoginSession:
        """Begin an OAuth login for the named provider.

        Returns the LoginSession with `state == awaiting_code` and `url`
        populated. Raises KeyError if the provider isn't registered.
        """
        if self._closing:
            raise OAuthLoginError("OAuth login admission is closed", status_code=503)
        provider = get_provider(provider_name)
        verifier = _gen_verifier()
        state = _gen_state()
        challenge = _challenge_from(verifier)

        url = provider.build_authorize_url(code_challenge=challenge, state=state)

        login_id = uuid.uuid4().hex[:16]
        loop = asyncio.get_running_loop()
        session = LoginSession(
            id=login_id,
            url=url,
            provider_name=provider_name,
            _verifier=verifier,
            _state=state,
            _created_at=loop.time(),
            user_id=user_id,
        )
        self._sessions[login_id] = session
        self._gc()

        logger.info(
            "OAuth login %s: started (%s), url=%s", login_id, provider_name, url
        )
        return session

    async def complete(self, request: OAuthCompletion, credentials: OAuthCredentialWriter) -> CredentialInfo:
        session = self._sessions.get(request.login_id)
        if session is None or session.user_id != request.user_id:
            raise KeyError("unknown login_id")
        if session._request is not None and session._request != request:
            raise OAuthLoginError("Login completion parameters conflict with the original request", status_code=409)
        if session._task is None:
            if self._closing:
                raise OAuthLoginError("OAuth login admission is closed", status_code=503)
            if session.state == LoginState.cancelled:
                raise OAuthCancelled()
            # Fix identity and retain the only task before validation can await.
            session._request = request
            session.state = LoginState.finalizing
            session._task = asyncio.create_task(self._complete(session, request, credentials), name=f"oauth-complete-{session.id}")
            session._task.add_done_callback(self._observe)
        return await self._wait(session)

    @staticmethod
    def _observe(task: asyncio.Task[CredentialInfo]) -> None:
        if not task.cancelled():
            task.exception()

    @staticmethod
    async def _wait(session: LoginSession) -> CredentialInfo:
        assert session._task is not None
        try:
            return await asyncio.shield(session._task)
        except asyncio.CancelledError:
            if session._task.cancelled() and session.state == LoginState.cancelled:
                raise OAuthCancelled() from None
            raise

    @staticmethod
    def _require_active(session: LoginSession) -> None:
        if session.state == LoginState.cancelled:
            raise OAuthCancelled()

    async def _complete(self, session: LoginSession, request: OAuthCompletion, credentials: OAuthCredentialWriter) -> CredentialInfo:
        try:
            await credentials.validate(request)
            self._require_active(session)
            code, code_state = _split_code(request.code)
            if code_state and code_state != session._state:
                raise OAuthLoginError("state mismatch — possible CSRF; restart the login")
            provider = get_provider(session.provider_name)
            try:
                tokens = await provider.exchange_code(code=code, code_verifier=session._verifier, state=session._state)
            except Exception as error:
                raise OAuthLoginError(f"token exchange failed: {error}") from error
            self._require_active(session)
            material: str | OAuthTokenSet
            try:
                material = await provider.mint_api_key(tokens.access_token)
                if not material:
                    raise OAuthLoginError("API key creation returned no usable token")
            except ScopeMissingError as error:
                if tokens.refresh_token is None:
                    raise OAuthLoginError("API key creation failed and the OAuth response didn't include a refresh token") from error
                material = tokens
            except Exception as error:
                raise OAuthLoginError(f"API key creation failed: {error}") from error
            self._require_active(session)
            # Explicit cancellation and this transition run without an await.
            session.state = LoginState.committing
            result = await credentials.commit(request, material)
            session.state = LoginState.success
            logger.info("OAuth login %s: credential committed", session.id)
            return result
        except asyncio.CancelledError:
            if session.state == LoginState.cancelled:
                raise OAuthCancelled() from None
            raise
        except OAuthCancelled:
            raise
        except Exception as error:
            if session.state == LoginState.cancelled:
                raise OAuthCancelled() from error
            session.state = LoginState.error
            session.message = str(error)
            raise
        finally:
            session._verifier = ""

    async def cancel(self, login_id: str) -> OAuthCancelResponse:
        session = self._sessions.get(login_id)
        if session is None:
            raise KeyError("unknown login_id")
        if session.state not in (LoginState.committing, LoginState.success, LoginState.error, LoginState.cancelled):
            session.state = LoginState.cancelled
            session.message = "cancelled by user"
            if session._task is not None:
                session._task.cancel()
        if session._task is not None:
            try:
                async with asyncio.timeout(_CANCEL_TIMEOUT_SECONDS):
                    await self._wait(session)
            except OAuthCancelled:
                pass
            except TimeoutError as error:
                raise OAuthLoginError("Login retirement is not confirmed; wait and try again", status_code=503) from error
        return OAuthCancelResponse(status="already_completed" if session.state == LoginState.success else "cancelled")

    def get(self, login_id: str) -> LoginSession | None:
        return self._sessions.get(login_id)

    def drain_blockers(self) -> tuple[str, ...]:
        return ("OAuth completion",) if any(session._task is not None and not session._task.done() for session in self._sessions.values()) else ()

    def begin_shutdown(self) -> None:
        self._closing = True

    async def shutdown(self) -> None:
        self.begin_shutdown()
        results = await asyncio.gather(*(self.cancel(session.id) for session in tuple(self._sessions.values())
                                        if session._task is None or not session._task.done()), return_exceptions=True)
        failures = [result for result in results if isinstance(result, BaseException)]
        if failures:
            raise RuntimeError("OAuth login retirement is unconfirmed") from failures[0]
        self._sessions.clear()

    # ---------------------------------------------------------------- internals

    def _gc(self) -> None:
        """Drop login sessions older than _LOGIN_TTL_SECONDS."""
        loop = asyncio.get_running_loop()
        now = loop.time()
        stale = [
            sid
            for sid, s in self._sessions.items()
            if now - s._created_at > _LOGIN_TTL_SECONDS
            and (s._task is None or s._task.done())
        ]
        for sid in stale:
            self._sessions.pop(sid, None)


# --------------------------------------------------------------------------- helpers


def _b64url(raw: bytes) -> str:
    """Base64-url-encode without padding (RFC 7636 §4.1)."""
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _gen_verifier() -> str:
    return _b64url(secrets.token_bytes(32))


def _gen_state() -> str:
    return _b64url(secrets.token_bytes(32))


def _challenge_from(verifier: str) -> str:
    return _b64url(hashlib.sha256(verifier.encode("ascii")).digest())


def _split_code(raw: str) -> tuple[str, str | None]:
    """Parse the OAuth code the user pasted.

    The Anthropic callback page formats it as `<code>#<state>`. We accept
    either form so users who only pasted the code half still work.
    """
    raw = raw.strip()
    if "#" in raw:
        code, _, state = raw.partition("#")
        return code.strip(), state.strip() or None
    return raw, None


# Singleton — wired into the FastAPI lifespan in main.py.
oauth_login_manager = OAuthLoginManager()


# --------------------------------------------------------------------------- back-compat


# The Claude-specific constants used to live at module scope; keeping
# re-exports so existing tests (and any external callers) don't break.
# New code should reach into `server.oauth_providers` instead.
def _claude() -> OAuthProvider:
    return get_provider("claude-code")


CLIENT_ID = _claude().CLIENT_ID  # type: ignore[attr-defined]
AUTHORIZE_URL = _claude().AUTHORIZE_URL  # type: ignore[attr-defined]
TOKEN_URL = _claude().TOKEN_URL  # type: ignore[attr-defined]
API_KEY_URL = _claude().API_KEY_URL  # type: ignore[attr-defined]
MANUAL_REDIRECT_URI = _claude().MANUAL_REDIRECT_URI  # type: ignore[attr-defined]
SCOPES = list(_claude().SCOPES)  # type: ignore[attr-defined]
