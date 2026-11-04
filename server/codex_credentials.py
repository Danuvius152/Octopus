"""The original Codex login's short, acknowledged file/metadata publication."""

from __future__ import annotations

import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from .crypto import encrypt
from .database import Database
from .db.users import owner_is_available
from .users import UserManager

if TYPE_CHECKING:
    from .codex_login import CodexLoginSession


class CodexCredentials:
    def __init__(self, db: Database):
        self.db = db

    async def commit(self, login: CodexLoginSession) -> None:
        """No network, process joins or staging cleanup inside the transaction.

        Only this committer can publish. Its caller shields it through the
        original COMMIT acknowledgment; status observers never enter here.
        """
        key = await UserManager(self.db).data_key_for(login.user_id)
        home = Path(login.codex_home)
        destination = home / "auth.json"
        assert login._login_home is not None
        source = Path(login._login_home) / "auth.json"
        old = None
        published = None
        created_home = False
        try:
            async with self.db.transaction(write=True) as tx:
                if not await owner_is_available(tx, login.user_id):
                    raise ValueError("The sign-in account is no longer available")
                current = await self.db._get_credential(tx, login.credential_id, login.user_id)
                if login._reauth:
                    if (current is None or current["user_id"] != login.user_id
                            or current["backend"] != "codex" or current["status"] == "disabled"):
                        raise ValueError("The signed-in credential is no longer available")
                elif current is not None:
                    raise ValueError("The sign-in target already exists")
                if not home.exists():
                    home.mkdir(mode=0o700)
                    created_home = True
                if destination.exists():
                    old = destination.read_bytes()
                os.replace(source, destination)
                published = destination.stat()
                if login._reauth:
                    # Label and other unrelated edits belong to their writers.
                    await self.db._update_credential(tx, login.credential_id, auth_type="oauth",
                        status="active", needs_reconnect=False, last_refresh_error_code=None,
                        secret_encrypted=encrypt(str(home), key))
                else:
                    created_at = datetime.now(UTC).isoformat()
                    await self.db._save_credential(tx, login.credential_id, "codex", login.label, "oauth",
                        encrypt(str(home), key), created_at, login.user_id)
            # This is the acknowledgment of this original transaction, not a
            # later read that guesses whether a cancelled commit took effect.
            from .codex_login import CodexLoginState

            login.state = CodexLoginState.success
        except BaseException:
            if self.db.health.state == "fatal":
                raise
            if published is not None:
                # Independent native CLI writes are outside this publisher.
                # An identity change forbids compensation over their material.
                try:
                    current_stat = destination.stat() if destination.exists() else None
                    identity = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
                    if current_stat is None or any(getattr(current_stat, key) != getattr(published, key) for key in identity):
                        self.db.health.fail(RuntimeError("Codex publication file ownership was lost"))
                    if old is None:
                        destination.unlink()
                    else:
                        fd, temporary = tempfile.mkstemp(prefix=".auth-restore-", dir=home)
                        try:
                            with os.fdopen(fd, "wb") as output:
                                output.write(old)
                            os.replace(temporary, destination)
                        finally:
                            Path(temporary).unlink(missing_ok=True)
                    if created_home:
                        home.rmdir()
                except OSError as error:
                    self.db.health.fail(error)
            raise
