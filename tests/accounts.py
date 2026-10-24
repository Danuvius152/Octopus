"""Explicit owned baselines for current business tests; setup tests use HTTP."""

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from server import deps
from server.database import Database
from server.users import UserManager


async def create_account(
    database: Database, *, username: str = "test-owner",
    extra_roots: Sequence[Path] = (),
) -> tuple[dict[str, Any], str]:
    """Persist an account and default agent, then issue its real bearer.

    Callers explicitly select any temporary working roots they exercise. This
    helper does not override authorization or alter request bodies.
    """
    users = UserManager(database)
    user = await users.create_user(username=username, password="owned-test-password", is_admin=True)
    if extra_roots:
        await database.update_user_field(user["id"], extra_roots=json.dumps([str(root) for root in extra_roots]))
    deps.set_user_manager(users)
    return user, await users.issue_token(user["id"])
