"""One login contract per harness, behind `profile.login`.

Claude logs in via an OAuth *redirect* (authorize URL → user pastes the
returned code); Codex via a *device code* (verification URL + user code →
poll until the CLI writes auth.json). The flows are genuinely different —
different endpoints, different UX — so this is not a forced single-shape
"begin/poll" abstraction. It's the thin surface the credentials router
needs so it resolves login via `get_harness(backend).login` instead of
importing the managers directly and branching on backend kind:

  - `start(label, *, reauth_credential_id=None)`
                           begin a login; returns the manager's session.
                           `reauth_credential_id` re-authorizes an existing
                           credential in place (device_code only — Claude
                           carries its re-auth target on `complete_oauth`'s
                           complete route instead). harness-credential-reauth.md §5
  - `complete_oauth(request, credentials)` redirect only — own exchange and commit
  - `get(id)`              current login record; device polling or OAuth owner check
  - `cancel(id)`           backend-specific cancellation; OAuth returns its result
  - `cleanup_credential(id)` revoke local state on delete (Codex: rmtree
                           its CODEX_HOME; Claude: nothing on disk)

Methods a given method doesn't use raise NotImplementedError. The drivers
wrap the existing `OAuthLoginManager` / `CodexLoginManager` singletons
(which keep their state + tests) — this layer only routes through the
harness and owns the per-kind delete cleanup.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol, runtime_checkable

from ..models import CredentialInfo, OAuthCancelResponse
from ..oauth_providers import OAuthTokenSet


@dataclass(frozen=True)
class OAuthCompletion:
    user_id: str | None
    login_id: str
    code: str = field(repr=False)
    label: str
    credential_id: str | None = None


class OAuthCredentialWriter(Protocol):
    """First-use target validation and one complete local credential commit."""

    async def validate(self, request: OAuthCompletion) -> None: ...
    async def commit(self, request: OAuthCompletion, material: str | OAuthTokenSet) -> CredentialInfo: ...


class LoginMethod(str, Enum):
    """How a harness's interactive login presents itself to the user. The
    frontend renders the matching UI from this declared attribute."""

    oauth_redirect = "oauth_redirect"  # authorize URL → user pastes a code
    device_code = "device_code"        # verification URL + user code → poll


@runtime_checkable
class LoginDriver(Protocol):
    method: LoginMethod

    async def start(
        self,
        label: str | None = None,
        *,
        reauth_credential_id: str | None = None,
        user_id: str | None = None,
    ) -> Any: ...
    async def complete_oauth(self, request: OAuthCompletion, credentials: OAuthCredentialWriter) -> CredentialInfo: ...
    def get(self, login_id: str) -> Any: ...  # the record, or None
    async def cancel(self, login_id: str) -> OAuthCancelResponse | None: ...
    def cleanup_credential(self, credential_id: str) -> None: ...
