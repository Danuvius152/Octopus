"""Serialize consumers of the legacy installation key with its handoff.

The lock spans key capture, external refresh and ciphertext persistence. SQL
transactions inside the scope remain short; stable account keys need no such
serialization. Acquire this scope before a Connector's own refresh lock.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from .config import settings
from .database import Database
from .users import UserManager


class InstallationKeyRetired(Exception):
    """A captured legacy caller cannot mutate secrets after account adoption."""


@asynccontextmanager
async def secret_key_scope(db: Database, user_id: str | None) -> AsyncIterator[str]:
    if user_id is not None:
        yield await UserManager(db).data_key_for(user_id)
        return
    async with db.installation_key_lock:
        if await db.count_users():
            raise InstallationKeyRetired("The installation token has retired; sign in to the owning account")
        yield settings.auth_token
