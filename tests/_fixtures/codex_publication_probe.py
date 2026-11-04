"""Isolated fail-stop probe; never invokes a provider CLI or real login."""

import asyncio
import json
import os
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from server import codex_login
from server.config import settings
from server.database import Database
from tests.accounts import create_account


async def main():
    root, failure, reauthorize = Path(sys.argv[1]), sys.argv[2], sys.argv[3] == "True"
    settings.users_root = str(root / "users")
    settings.agents_dir = str(root / "agents")
    settings.master_key_file = str(root / "master.key")
    settings.codex_home_dir = str(root / "codex")
    db = Database(str(root / "storage.db"))
    await db.initialize()
    owner, _ = await create_account(db)
    target = "original-target" if reauthorize else None
    if target:
        home = Path(codex_login.codex_home_for(target))
        home.mkdir(parents=True)
        (home / "auth.json").write_text('{"tokens":{"access_token":"old"}}')
        (home / "config.toml").write_text('model="preserved"')
        await db.save_credential(target, "codex", "Original", "oauth", "old", "now", user_id=owner["id"])
        await db.update_credential(target, needs_reconnect=True)
    manager = codex_login.CodexLoginManager(db)
    fake = str(Path(__file__).with_name("fake_codex_login.py"))
    codex_login.build_codex_login_argv = lambda: [sys.executable, fake]
    if failure.startswith("commit"):
        commit = db.conn.commit
        execute = db.conn.execute
        publication_started = False

        def publication_sql(sql, parameters=()):
            nonlocal publication_started
            if sql.startswith(("INSERT INTO backend_credentials", "UPDATE backend_credentials SET")):
                publication_started = True
            return execute(sql, parameters)

        db.conn.execute = publication_sql

        async def uncertain_commit():
            if not publication_started:
                return await commit()
            if failure == "commit-after":
                await commit()
            raise OSError("Original COMMIT acknowledgment unavailable")

        db.conn.commit = uncertain_commit
    else:
        execute = db.conn.execute

        def changed_publication(sql, parameters=()):
            if sql.startswith("UPDATE backend_credentials SET"):
                home = Path(codex_login.codex_home_for(target))
                if failure == "ownership-check":
                    stat = Path.stat

                    def unavailable_stat(path, *args, **kwargs):
                        if path == home / "auth.json":
                            raise PermissionError("Cannot verify publication ownership")
                        return stat(path, *args, **kwargs)

                    Path.stat = unavailable_stat
                else:
                    external = home / "independent-native-auth"
                    external.write_text('{"tokens":{"access_token":"external"}}')
                    os.replace(external, home / "auth.json")
                raise sqlite3.IntegrityError("Known metadata refusal after independent file write")
            return execute(sql, parameters)

        db.conn.execute = changed_publication
    login = await manager.start("New login", reauth_credential_id=target, user_id=owner["id"])
    (root / "identity.json").write_text(json.dumps({"credential_id": login.credential_id, "home": login.codex_home}))
    await asyncio.shield(login._task)
    (root / "incorrect-success").touch()


asyncio.run(main())
