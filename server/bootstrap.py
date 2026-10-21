"""Turning a single-user install into its first account (multi-tenancy.md §9).

One operation, because the pieces have to happen together or not at all: an
install with a user whose secrets are still keyed to the retired token, or with
rows nobody owns, is worse than either the before or the after.

The account, its secrets, adopted rows and existing working roots share one
short database transaction. Hashing, key-file preparation, process retirement
and filesystem migration happen outside that transaction.

Available exactly once: the route that calls this is refused the moment an
account exists, which is also when the legacy token it authenticates with stops
working. The two facts are the same fact.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

from .config import settings
from .crypto import decrypt, encrypt, master_key
from .db.transaction import Transaction
from .users import UserError, UserManager
from .workspace import paths_for

logger = logging.getLogger(__name__)

# The same three tables `token_rotation` re-keys, for the same reason: they are
# every ciphertext the key protects.
_SECRET_COLUMNS = (
    ("credential_secrets", "credential_id", "secret_encrypted"),
    ("connector_installation_secrets", "installation_id", "secret_encrypted"),
)

# And the install's own, which move to the **master key** instead: an OAuth
# client is this box's app registration with a provider, not a user's secret, so
# handing it to whoever happens to create the first account would be wrong in
# exactly the way that matters when a second one arrives (multi-tenancy.md §4).
_INSTALL_SECRET_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("connector_oauth_clients", "kind", "client_secret_encrypted"),
)


class BootstrapError(Exception):
    def __init__(self, message: str, *, status_code: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


async def _rekey_secrets(tx: Transaction, old_key: str, new_key: str, user_id: str,
                         installation_key: str, *, adopt: bool) -> dict[str, int]:
    """Rekey within the account transaction, respecting each secret's owner."""
    counts: dict[str, int] = {}
    for table, key_col, secret_col in (*_SECRET_COLUMNS, *_INSTALL_SECRET_COLUMNS):
        account_secret = table in {entry[0] for entry in _SECRET_COLUMNS}
        destination = new_key if account_secret else installation_key
        if account_secret:
            owner_table = "backend_credentials" if table == "credential_secrets" else "connector_installations"
            rows = await tx.fetch_all(
                f"SELECT s.{key_col},s.{secret_col},o.user_id FROM {table} s JOIN {owner_table} o ON o.id=s.{key_col} "
                "WHERE o.user_id IS NULL OR o.user_id=?", (user_id,),  # noqa: S608
            )
        else:
            rows = await tx.fetch_all(f"SELECT {key_col},{secret_col},NULL FROM {table}")  # noqa: S608
        counts[table] = 0
        for row_key, ciphertext, owner in rows:
            if not ciphertext or (account_secret and owner is None and not adopt):
                continue
            # Old upgrades could commit rekey before adopting NULL-owner
            # rows. The bound migration owner's exact DEK remains proof even
            # when adoption has not committed yet; other accounts are excluded
            # by the query above. Preserve already-correct ciphertext bytes.
            try:
                decrypt(ciphertext, destination)
                continue
            except ValueError:
                pass
            try:
                plaintext = decrypt(ciphertext, old_key)
            except ValueError as error:
                raise BootstrapError(f"{table}.{key_col}={row_key!r} could not be decrypted; verify the retained secret before upgrading") from error
            await tx.execute(f"UPDATE {table} SET {secret_col}=? WHERE {key_col}=?",  # noqa: S608
                             (encrypt(plaintext, destination), row_key))
            counts[table] += 1
    return counts


async def _unowned_working_dirs(tx: Transaction) -> set[str]:
    """Directories whose sessions are about to be adopted.

    They become the first account's `extra_roots`, because confining them to a
    workspace that did not exist when they were created would break live
    sessions on the first turn after the upgrade — including, on the machine
    this was written on, the ones working on Octopus itself.
    """
    # Owned sessions already received their grants in the account commit.
    # A resume must not reapprove them after links or account grants change.
    rows = await tx.fetch_all("SELECT DISTINCT working_dir FROM sessions WHERE user_id IS NULL")
    return {raw for row in rows if (raw := (row[0] or "").strip())}


def _resolve_roots(roots: list[str]) -> list[str]:
    """Capture authorization targets before entering the account transaction."""
    return [str(Path(root).expanduser().resolve()) for root in roots]


async def _bootstrap_account(
    db: Any,
    *,
    username: str,
    password: str,
    adopt: bool = True,
    session_manager: Any = None,
    existing_user: dict | None = None,
    on_created: Any = None,
    wiki_root: str | None = None,
) -> dict[str, Any]:
    """Create the install's first account and hand it everything.

    Returns a summary of what moved, so the operator sees the upgrade rather
    than trusting it.

    `session_manager` is the live one, when there is one. Rows are only half
    the install: the sessions already loaded into memory carry the owner too,
    and a server that is not restarted would go on serving them as unowned.
    """
    users = UserManager(db)
    try:
        prepared = users.prepare_user(username=username, password=password, is_admin=True) if existing_user is None else None
    except UserError as exc:
        raise BootstrapError(exc.message, status_code=exc.status_code) from exc
    installation_key = master_key()

    # Ownership changes invalidate the authority baked into a running CLI.
    # Stop old requests before the account commit; new turns resume
    # their transcripts with the account's freshly scoped runtime.
    if session_manager is not None:
        await session_manager.retire_ownerless_runs()
    else:
        from .mcp_identity import revoke_ownerless_runs
        revoke_ownerless_runs()

    async with db.installation_key_lock:
        expected_extra_roots = "[]"
        expected_working_dirs: set[str] = set()
        roots: list[str] = []
        if adopt:
            # Filesystem resolution runs without SQLite ownership. Session
            # roots and account grants can change meanwhile, so the final
            # transaction checks both SQL sources before applying this result.
            async with db.transaction(write=False) as tx:
                if existing_user is not None:
                    current = await db._get_user(tx, existing_user["id"])
                    if current is None:
                        raise BootstrapError("The migration owner no longer exists", status_code=409)
                    expected_extra_roots = current.get("extra_roots") or "[]"
                expected_working_dirs = await _unowned_working_dirs(tx)
            newly_granted_roots = await asyncio.to_thread(_resolve_roots, sorted(expected_working_dirs))
            # Existing grants and the registered Wiki root are captured
            # canonical paths. Only newly adopted sessions acquire grants;
            # retrying publication cannot authorize new symlink targets.
            roots = list(dict.fromkeys([
                *json.loads(expected_extra_roots), *newly_granted_roots, *([wiki_root] if wiki_root else []),
            ]))

        async with db.transaction() as tx:
            if prepared is not None:
                if await db._count(tx, "SELECT COUNT(*) FROM users"):
                    raise BootstrapError("This install already has an account", status_code=409)
                user = await db._create_user(tx, **prepared)
            else:
                assert existing_user is not None
                user = await db._get_user(tx, existing_user["id"])
                if user is None:
                    raise BootstrapError("The migration owner no longer exists", status_code=409)
            if adopt and (
                (user.get("extra_roots") or "[]") != expected_extra_roots
                or await _unowned_working_dirs(tx) != expected_working_dirs
            ):
                raise BootstrapError("Existing working roots changed during account preparation; retry the upgrade", status_code=409)
            summary: dict[str, Any] = {"user_id": user["id"], "username": user["username"]}
            summary["reencrypted"] = await _rekey_secrets(
                tx, settings.auth_token, users.data_key(user, key=installation_key), user["id"], installation_key, adopt=adopt,
            )
            if adopt:
                summary["adopted"] = await db._adopt_orphan_rows(tx, user["id"])
                user["extra_roots"] = json.dumps(roots)
                await tx.execute("UPDATE users SET extra_roots=? WHERE id=?", (user["extra_roots"], user["id"]))
                summary["extra_roots"] = roots

    from . import deps

    deps.forget_accounts_exist()
    if on_created is not None:
        await on_created(user)
    paths_for(user["id"]).ensure()
    if adopt:
        from .memory_handoff import MemoryHandoffConflict, handoff_agent_memory
        try:
            summary["agent_memory_moved"] = await handoff_agent_memory(db, user_id=user["id"])
        except MemoryHandoffConflict as error:
            raise BootstrapError(str(error), status_code=409) from error
        if session_manager is not None:
            summary["live_sessions_adopted"] = await session_manager.adopt_orphan_sessions(
                user["id"]
            )

    logger.info("bootstrapped the first account: %s", summary)
    return summary


async def bootstrap_first_account(db: Any, *, username: str, password: str, adopt: bool = True,
                                  session_manager: Any = None) -> dict[str, Any]:
    from .aio import complete_cleanup
    from .wiki.account_upgrade import upgrade_with_wiki
    from .wiki.registry import registry

    async def command():
        # The command owns serialization through journal preparation, drain
        # and publication, including when its HTTP waiter has gone away.
        async with db._bootstrap_lock:
            if await db.count_users():
                raise BootstrapError("This install already has an account", status_code=409)
            if (registry.state(None) / "config.json").is_file():
                return await upgrade_with_wiki(
                    db, username=username, password=password, adopt=adopt, manager=session_manager,
                )
            if session_manager is not None:
                session_manager._legacy_transition = True
            try:
                summary = await _bootstrap_account(
                    db, username=username, password=password, adopt=adopt, session_manager=session_manager,
                )
            except BootstrapError:
                if session_manager is not None and not await db.count_users():
                    session_manager._legacy_transition = False
                raise
            if session_manager is not None:
                session_manager._legacy_transition = False
            return summary

    return await complete_cleanup(command())
