"""Explicit owned baselines for current business tests; setup tests use HTTP."""

import json
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from functools import cache
from pathlib import Path
from typing import Any

from server import deps
from server.crypto import new_dek, wrap_dek
from server.database import Database
from server.users import (
    UserError,
    UserManager,
    hash_password,
    validate_password,
    validate_username,
)

_BASELINE_PASSWORD = "owned-test-password"


@cache
def _baseline_password_hash() -> str:
    return hash_password(_BASELINE_PASSWORD)


async def create_account(
    database: Database, *, username: str = "test-owner",
    extra_roots: Sequence[Path] = (),
) -> tuple[dict[str, Any], str]:
    """Persist an account and default agent, then issue its real bearer.

    The fixed test password only supplies a valid stored hash. Every account
    has its own key, directories, default agent and issued token. Use the
    normal account APIs in tests of registration, passwords or KDF work.
    """
    users = UserManager(database)
    name = validate_username(username)
    validate_password(_BASELINE_PASSWORD, username=name)
    if await database.get_user_by_username(name) is not None:
        raise UserError("That username is taken", status_code=409)
    user = await users._insert({
        "user_id": uuid.uuid4().hex[:12], "username": name,
        "password_hash": _baseline_password_hash(), "created_at": datetime.now(UTC).isoformat(),
        "is_admin": True, "dek_wrapped": wrap_dek(new_dek()),
    })
    if extra_roots:
        await database.update_user_field(user["id"], extra_roots=json.dumps([str(root) for root in extra_roots]))
    deps.set_user_manager(users)
    return user, await users.issue_token(user["id"])
