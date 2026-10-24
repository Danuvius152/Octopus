"""Admit current account bearers and authorized runtime scopes.

Setup proof belongs only to the setup endpoint. These guards return the
presented business bearer; request-scoped dependencies resolve its owner.
"""

from __future__ import annotations

from fastapi import Depends, HTTPException, Query, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

_bearer = HTTPBearer()


async def _allowed(presented: str) -> bool:
    from .deps import resolve_principal

    return await resolve_principal(presented) is not None


async def verify_token(
    creds: HTTPAuthorizationCredentials = Depends(_bearer),
) -> str:
    if not await _allowed(creds.credentials):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid token")
    return creds.credentials


async def verify_ws_token(token: str = Query(...)) -> str:
    if not await _allowed(token):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid token")
    return token
