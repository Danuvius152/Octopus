"""Sensitive account operations retain one durable transaction outcome."""

import asyncio
import sqlite3

import pytest

from server.database import Database
from server.users import UserError, UserManager, token_digest


@pytest.mark.parametrize("phase", ["before", "disabled", "commit", "revoke_failure"])
async def test_disable_and_revocation_have_one_durable_outcome(tmp_path, monkeypatch, phase):
    path = tmp_path / "accounts.db"
    db = Database(str(path))
    await db.initialize()
    users = UserManager(db)
    user = await users.create_user(username="archer", password="password1")
    tokens = [await users.issue_token(user["id"], kind=kind) for kind in ("session", "pat")]
    entered, release = asyncio.Event(), asyncio.Event()
    execute, commit = db.conn.execute, db.conn.commit
    disabled = False

    async def gated_execute(sql, parameters=()):
        nonlocal disabled
        cursor = await execute(sql, parameters)
        if sql.startswith("UPDATE users SET disabled_at"):
            disabled = True
            if phase == "disabled":
                entered.set()
                await release.wait()
        return cursor

    async def gated_commit():
        if disabled and phase == "commit":
            entered.set()
            await release.wait()
        await commit()

    if phase == "revoke_failure":
        await db.conn.execute(
            "CREATE TRIGGER refuse_revocation BEFORE UPDATE OF revoked_at ON auth_tokens "
            "BEGIN SELECT RAISE(ABORT, 'revocation rejected'); END"
        )
        await db.conn.commit()
    try:
        with monkeypatch.context() as patch:
            patch.setattr(db.conn, "execute", gated_execute)
            patch.setattr(db.conn, "commit", gated_commit)
            if phase == "before":
                await db._write_lock.acquire()
            pending = asyncio.create_task(users.set_disabled(user["id"], True))
            try:
                if phase == "revoke_failure":
                    with pytest.raises(sqlite3.IntegrityError, match="revocation rejected"):
                        await pending
                else:
                    if phase == "before":
                        await asyncio.sleep(0)  # Enter the real database lock wait.
                    else:
                        await asyncio.wait_for(entered.wait(), 2)
                    pending.cancel()
                    await asyncio.sleep(0)
                    pending.cancel()
                    release.set()
                    with pytest.raises(asyncio.CancelledError):
                        await pending
            finally:
                release.set()
                if phase == "before":
                    db._write_lock.release()
                await asyncio.gather(pending, return_exceptions=True)
    finally:
        await db.close()

    reopened = Database(str(path))
    await reopened.initialize()
    try:
        restored = UserManager(reopened)
        changed = phase in {"disabled", "commit"}
        assert bool((await reopened.get_user(user["id"]))["disabled_at"]) == changed
        await restored.set_disabled(user["id"], False)
        for token in tokens:
            assert (await restored.resolve_token(token) is None) == changed
        fresh = await restored.issue_token(user["id"])
        assert await restored.resolve_token(fresh) is not None
    finally:
        await reopened.close()


@pytest.mark.parametrize("cancel", [False, True])
async def test_restore_and_new_issuance_follow_the_entire_disable_transaction(tmp_path, monkeypatch, cancel):
    db = Database(str(tmp_path / "accounts.db"))
    await db.initialize()
    users = UserManager(db)
    user = await users.create_user(username="archer", password="password1")
    old = await users.issue_token(user["id"])
    entered, release = asyncio.Event(), asyncio.Event()
    execute = db.conn.execute

    async def gated(sql, parameters=()):
        cursor = await execute(sql, parameters)
        if sql.startswith("UPDATE users SET disabled_at") and parameters[0] is not None:
            entered.set()
            await release.wait()
        return cursor

    async def restore_and_issue():
        await users.set_disabled(user["id"], False)
        return await users.issue_token(user["id"])

    try:
        with monkeypatch.context() as patch:
            patch.setattr(db.conn, "execute", gated)
            disabling = asyncio.create_task(users.set_disabled(user["id"], True))
            await asyncio.wait_for(entered.wait(), 2)
            restoring = asyncio.create_task(restore_and_issue())
            await asyncio.sleep(0)
            if cancel:
                disabling.cancel()
            release.set()
            results = await asyncio.gather(disabling, restoring, return_exceptions=True)
            if cancel:
                assert isinstance(results[0], asyncio.CancelledError)
            else:
                assert results[0] is None
            assert isinstance(results[1], str)
        assert await users.resolve_token(old) is None
        assert await users.resolve_token(results[1]) is not None
    finally:
        release.set()
        await db.close()


@pytest.mark.parametrize("kind", ["session", "pat"])
async def test_disabled_account_cannot_mint_bearers_that_survive_restore(tmp_path, kind):
    db = Database(str(tmp_path / "accounts.db"))
    await db.initialize()
    users = UserManager(db)
    try:
        user = await users.create_user(username="archer", password="password1")
        await users.set_disabled(user["id"], True)
        with pytest.raises(UserError):
            await users.issue_token(user["id"], kind=kind)
        await users.set_disabled(user["id"], False)
        assert await db.list_tokens(user["id"]) == []
    finally:
        await db.close()


async def test_failed_old_password_proof_cannot_revoke_winner_or_new_tokens(tmp_path):
    db = Database(str(tmp_path / "accounts.db"))
    await db.initialize()
    users = UserManager(db)
    try:
        user = await users.create_user(username="archer", password="password1")
        here = await users.issue_token(user["id"])
        old_proof = await users.authenticate("archer", "password1")
        await users.change_own_password(old_proof, "winning-password", keep_token_hash=token_digest(here))
        _, fresh = await users.login("archer", "winning-password")
        with pytest.raises(UserError) as rejected:
            await users.change_own_password(old_proof, "losing-password")
        assert rejected.value.status_code == 409
        assert await users.resolve_token(here) is not None
        assert await users.resolve_token(fresh) is not None
        await users.authenticate("archer", "winning-password")
    finally:
        await db.close()


async def test_cancel_before_password_transaction_does_not_spend_the_proof(tmp_path):
    db = Database(str(tmp_path / "accounts.db"))
    await db.initialize()
    users = UserManager(db)
    try:
        user = await users.create_user(username="archer", password="password1")
        here = await users.issue_token(user["id"])
        proof = await users.authenticate("archer", "password1")
        async with db._write_lock:
            pending = asyncio.create_task(users.change_own_password(proof, "changed-password"))
            await asyncio.sleep(0)
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending
        await users.authenticate("archer", "password1")
        assert await users.resolve_token(here) is not None
    finally:
        await db.close()
