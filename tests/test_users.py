"""Accounts, bearers and invites (multi-tenancy.md §3).

The happy paths here are the least interesting part. What these pin is the
behaviour that is easy to get subtly wrong and impossible to notice afterwards:
a login that says which half was wrong, a bearer that outlives a revocation, an
invite that can be spent twice, a password change that leaves a stolen session
alive.
"""

from __future__ import annotations

import asyncio
import hashlib
import sqlite3
import threading
import time
from datetime import UTC, datetime, timedelta

import anyio.to_thread
import pytest

from server.database import Database
from server.users import (
    UserError,
    UserManager,
    hash_password,
    token_digest,
    validate_password,
    validate_username,
    verify_password,
)


@pytest.fixture
async def users():
    db = Database(":memory:")
    await db.initialize()
    try:
        yield UserManager(db), db
    finally:
        await db.close()


@pytest.mark.parametrize("operation", [
    "setup", "create", "register", "login", "wrong", "unknown", "admin_password", "own_password",
])
async def test_account_password_work_runs_outside_the_event_loop(users, monkeypatch, operation):
    mgr, _db = users
    account = proof = invite = None
    if operation not in {"setup", "create"}:
        account = await mgr.create_user(username="archer", password="password1")
    if operation == "register":
        invite = await mgr.create_invite(created_by=account["id"])
    if operation == "own_password":
        proof = await mgr.authenticate("archer", "password1")
    loop_thread = threading.get_ident()
    computing_threads = []
    scrypt = hashlib.scrypt

    def observed_scrypt(*args, **kwargs):
        computing_threads.append(threading.get_ident())
        return scrypt(*args, **kwargs)

    monkeypatch.setattr(hashlib, "scrypt", observed_scrypt)
    if operation == "setup":
        account = await mgr.setup(username="archer", password="password1")
    elif operation == "create":
        account = await mgr.create_user(username="archer", password="password1")
    elif operation == "register":
        account = await mgr.register(invite_code=invite["code"], username="another", password="password1")
    elif operation == "login":
        logged_in, token = await mgr.login("archer", "password1")
        assert logged_in["id"] == account["id"]
        assert (await mgr.resolve_token(token))["id"] == account["id"]
    elif operation in {"wrong", "unknown"}:
        with pytest.raises(UserError) as error:
            await mgr.login("nobody" if operation == "unknown" else "archer", "wrong-password")
        assert error.value.status_code == 401
    elif operation == "admin_password":
        await mgr.set_password(account["id"], "changed-password")
    else:
        await mgr.change_own_password(proof, "changed-password")
    assert computing_threads and loop_thread not in computing_threads


@pytest.fixture
def block_password_work(monkeypatch):
    """Hold the real scrypt boundary; never substitute a credential result."""
    probes = []

    def install(*, password=None):
        loop = asyncio.get_running_loop()
        release = threading.Event()
        started = asyncio.Queue()
        lock = threading.Lock()
        counts = {"active": 0, "peak": 0, "calls": 0}
        scrypt = hashlib.scrypt

        def held_scrypt(raw, **kwargs):
            if password is not None and raw != password.encode():
                return scrypt(raw, **kwargs)
            with lock:
                counts["calls"] += 1
                counts["active"] += 1
                counts["peak"] = max(counts["peak"], counts["active"])
            loop.call_soon_threadsafe(started.put_nowait, threading.get_ident())
            try:
                assert release.wait(5), "test did not release password computation"
                return scrypt(raw, **kwargs)
            finally:
                with lock:
                    counts["active"] -= 1

        monkeypatch.setattr(hashlib, "scrypt", held_scrypt)
        probes.append(release)
        return release, started, counts

    yield install
    for release in probes:
        release.set()


async def test_cancelled_password_work_keeps_capacity_until_computation_finishes(users, block_password_work):
    mgr, db = users
    account = await mgr.create_user(username="archer", password="password1")
    release, started, counts = block_password_work()
    loop = asyncio.get_running_loop()
    default_started = asyncio.Event()
    default_release = threading.Event()
    default_limiter = anyio.to_thread.current_default_thread_limiter()
    old_capacity = default_limiter.total_tokens
    default_limiter.total_tokens = 1

    def occupy_default_pool():
        loop.call_soon_threadsafe(default_started.set)
        assert default_release.wait(5)

    ordinary_work = asyncio.create_task(anyio.to_thread.run_sync(occupy_default_pool))
    tasks = []
    try:
        await asyncio.wait_for(default_started.wait(), 1)
        tasks = [asyncio.create_task(mgr.login("archer", "password1")) for _ in range(4)]
        threads = [await asyncio.wait_for(started.get(), 1) for _ in range(4)]
        assert threading.get_ident() not in threads
        # Account reads still work while KDF and default worker budgets are full.
        assert await mgr.resolve_token("unrelated-invalid-bearer") is None
        waiting = asyncio.create_task(mgr.login("unknown", "password1"))
        tasks.append(waiting)
        await asyncio.sleep(.02)
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(waiting, 1)
        tasks[0].cancel()
        following = asyncio.create_task(mgr.login("archer", "password1"))
        tasks.append(following)
        await asyncio.sleep(.02)
        assert not tasks[0].done(), "running computation lost its owner on cancellation"
        assert counts == {"active": 4, "peak": 4, "calls": 4}
        release.set()
        results = await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 3)
        assert isinstance(results[0], asyncio.CancelledError)
        for result in [*results[1:4], results[5]]:
            logged_in, token = result
            assert logged_in["id"] == account["id"]
            assert (await mgr.resolve_token(token))["id"] == account["id"]
        assert counts == {"active": 0, "peak": 4, "calls": 5}
        assert len(await db.list_tokens(account["id"])) == 4
    finally:
        release.set()
        default_release.set()
        await asyncio.gather(*tasks, ordinary_work, return_exceptions=True)
        default_limiter.total_tokens = old_capacity


@pytest.mark.parametrize("operation", ["create", "login", "password"])
async def test_anyio_cancelled_password_work_cannot_commit(users, block_password_work, operation):
    mgr, db = users
    account = await mgr.create_user(username="archer", password="password1")
    token = await mgr.issue_token(account["id"])
    proof = await mgr.authenticate("archer", "password1")
    before_tokens = await db.list_tokens(account["id"])
    release, started, _counts = block_password_work()
    scopes = []
    completed = []
    statements = []

    async def request():
        with anyio.CancelScope() as scope:
            scopes.append(scope)
            if operation == "create":
                await mgr.create_user(username="another", password="password1")
            elif operation == "login":
                await mgr.login("archer", "password1")
            else:
                await mgr.change_own_password(proof, "changed-password", keep_token_hash=token_digest(token))
            completed.append(True)

    pending = asyncio.create_task(request())
    try:
        await asyncio.wait_for(started.get(), 1)
        await db.conn.set_trace_callback(statements.append)
        scopes[0].cancel()
        await asyncio.sleep(.02)
        assert not pending.done()
        release.set()
        await asyncio.wait_for(pending, 2)
    finally:
        release.set()
        await asyncio.gather(pending, return_exceptions=True)
        await db.conn.set_trace_callback(None)
    assert not completed
    assert not statements, "cancelled computation reached a database operation"
    assert await db.list_tokens(account["id"]) == before_tokens
    assert (await mgr.resolve_token(token))["id"] == account["id"]
    assert (await mgr.authenticate("archer", "password1"))["id"] == account["id"]
    with pytest.raises(UserError):
        await mgr.authenticate("another", "password1")


@pytest.mark.parametrize("operation", ["login", "password"])
@pytest.mark.parametrize("change", ["password", "disable"])
async def test_password_computation_cannot_spend_a_retired_proof(users, block_password_work, operation, change):
    mgr, db = users
    account = await mgr.create_user(username="archer", password="password1")
    proof = await mgr.authenticate("archer", "password1")
    release, started, _counts = block_password_work(
        password="password1" if operation == "login" else "changed-password",
    )
    pending = asyncio.create_task(
        mgr.login("archer", "password1") if operation == "login"
        else mgr.change_own_password(proof, "changed-password")
    )
    try:
        await asyncio.wait_for(started.get(), 1)
        if change == "password":
            await mgr.set_password(account["id"], "replacement-password")
        else:
            await mgr.set_disabled(account["id"], True)
        release.set()
        with pytest.raises(UserError) as error:
            await asyncio.wait_for(pending, 2)
        assert error.value.status_code == (401 if operation == "login" else 409)
    finally:
        release.set()
        await asyncio.gather(pending, return_exceptions=True)
    assert await db.list_tokens(account["id"]) == []
    if change == "disable":
        await mgr.set_disabled(account["id"], False)
    expected = "password1" if change == "disable" else "replacement-password"
    assert (await mgr.authenticate("archer", expected))["id"] == account["id"]


# ---------------------------------------------------------------------------
# Passwords
# ---------------------------------------------------------------------------


class TestPasswords:
    def test_the_same_password_hashes_differently_every_time(self):
        """A shared salt would make two users with the same password visible
        to each other as equal rows."""
        a, b = hash_password("correct horse"), hash_password("correct horse")
        assert a != b
        assert verify_password("correct horse", a)
        assert verify_password("correct horse", b)
        assert not verify_password("Correct horse", a)

    def test_the_stored_hash_carries_its_own_cost(self):
        """So the cost can be raised later without invalidating anyone."""
        scheme, n, r, p, _salt, _hash = hash_password("whatever").split("$")
        assert scheme == "scrypt"
        assert int(n) >= 2**14 and int(r) >= 8 and int(p) >= 1

    def test_a_corrupt_hash_fails_the_login_rather_than_the_request(self):
        for broken in ("", "not-a-hash", "scrypt$x$y$z$q$w", "bcrypt$1$2$3$4$5"):
            assert verify_password("anything", broken) is False

    def test_the_rules_reject_what_they_say_they_reject(self):
        validate_password("longenough", username="archer")
        with pytest.raises(UserError):
            validate_password("short")
        with pytest.raises(UserError):
            validate_password("archer", username="archer")


class TestUsernames:
    def test_normalised_and_bounded(self):
        assert validate_username("  Archer  ") == "archer"
        for bad in ("ab", "9lives", "has space", "UPPER!", "a" * 33, "admin"):
            with pytest.raises(UserError):
                validate_username(bad)


# ---------------------------------------------------------------------------
# Accounts
# ---------------------------------------------------------------------------


class TestAccounts:
    @pytest.mark.asyncio
    async def test_a_username_is_taken_case_insensitively(self, users):
        mgr, _db = users
        await mgr.create_user(username="archer", password="password1")
        with pytest.raises(UserError) as e:
            await mgr.create_user(username="Archer", password="password2")
        assert e.value.status_code == 409

    @pytest.mark.asyncio
    async def test_login_never_says_which_half_was_wrong(self, users):
        mgr, _db = users
        await mgr.create_user(username="archer", password="password1")

        with pytest.raises(UserError) as wrong_pw:
            await mgr.authenticate("archer", "password2")
        with pytest.raises(UserError) as no_such:
            await mgr.authenticate("nobody", "password1")

        assert wrong_pw.value.status_code == no_such.value.status_code == 401
        assert wrong_pw.value.message == no_such.value.message

    @pytest.mark.asyncio
    async def test_an_unknown_username_still_pays_for_a_hash(self, users):
        """Otherwise the login route is a way to enumerate accounts: the
        rejection for a name that does not exist would come back an order of
        magnitude faster than one for a name that does."""
        mgr, _db = users
        await mgr.create_user(username="archer", password="password1")

        t0 = time.perf_counter()
        with pytest.raises(UserError):
            await mgr.authenticate("nobody-at-all", "password1")
        unknown = time.perf_counter() - t0

        t0 = time.perf_counter()
        with pytest.raises(UserError):
            await mgr.authenticate("archer", "wrong-password")
        known = time.perf_counter() - t0

        assert unknown > known / 4, (
            f"unknown-user login took {unknown:.4f}s against {known:.4f}s for a "
            "real user — the hash is being skipped, which leaks who exists"
        )

    @pytest.mark.asyncio
    async def test_a_disabled_account_cannot_log_in_or_stay_logged_in(self, users):
        mgr, _db = users
        user = await mgr.create_user(username="archer", password="password1")
        token = await mgr.issue_token(user["id"])
        assert await mgr.resolve_token(token) is not None

        await mgr.set_disabled(user["id"], True)
        assert await mgr.resolve_token(token) is None, "existing bearer outlived it"
        with pytest.raises(UserError) as e:
            await mgr.authenticate("archer", "password1")
        assert e.value.status_code == 403


# ---------------------------------------------------------------------------
# Bearers
# ---------------------------------------------------------------------------


class TestTokens:
    @pytest.mark.asyncio
    async def test_the_token_itself_is_never_stored(self, users):
        mgr, db = users
        user = await mgr.create_user(username="archer", password="password1")
        token = await mgr.issue_token(user["id"])

        rows = await db.list_tokens(user["id"])
        assert len(rows) == 1
        assert token not in str(rows), "the bearer is recoverable from the table"
        assert rows[0]["token_hash"] == token_digest(token)

    @pytest.mark.asyncio
    async def test_revoking_takes_effect_immediately(self, users):
        mgr, _db = users
        user = await mgr.create_user(username="archer", password="password1")
        token = await mgr.issue_token(user["id"])
        await mgr.revoke_token(token)
        assert await mgr.resolve_token(token) is None

    @pytest.mark.asyncio
    async def test_an_expired_session_stops_resolving(self, users):
        mgr, db = users
        user = await mgr.create_user(username="archer", password="password1")
        token = await mgr.issue_token(user["id"])
        past = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
        await db.conn.execute(
            "UPDATE auth_tokens SET expires_at = ? WHERE token_hash = ?",
            (past, token_digest(token)),
        )
        await db.conn.commit()
        assert await mgr.resolve_token(token) is None

    @pytest.mark.asyncio
    async def test_an_unparseable_expiry_fails_closed(self, users):
        mgr, db = users
        user = await mgr.create_user(username="archer", password="password1")
        token = await mgr.issue_token(user["id"])
        await db.conn.execute(
            "UPDATE auth_tokens SET expires_at = 'whenever' WHERE token_hash = ?",
            (token_digest(token),),
        )
        await db.conn.commit()
        assert await mgr.resolve_token(token) is None

    @pytest.mark.asyncio
    async def test_a_personal_access_token_does_not_expire(self, users):
        mgr, db = users
        user = await mgr.create_user(username="archer", password="password1")
        pat = await mgr.issue_token(user["id"], kind="pat", label="laptop script")
        row = await db.get_token(token_digest(pat))
        assert row["kind"] == "pat" and row["expires_at"] is None
        assert (await mgr.resolve_token(pat))["id"] == user["id"]

    @pytest.mark.asyncio
    async def test_changing_a_password_signs_the_other_devices_out(self, users):
        mgr, _db = users
        user = await mgr.create_user(username="archer", password="password1")
        here = await mgr.issue_token(user["id"])
        elsewhere = await mgr.issue_token(user["id"])

        revoked = await mgr.set_password(
            user["id"], "a-better-password", keep_token_hash=token_digest(here)
        )
        assert revoked == 1
        assert await mgr.resolve_token(elsewhere) is None, "stolen session survived"
        assert await mgr.resolve_token(here) is not None, "logged out the tab that changed it"
        await mgr.authenticate("archer", "a-better-password")


# ---------------------------------------------------------------------------
# Invites
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("interruption", ["password", "commit", "revoke_failure"])
@pytest.mark.parametrize("authority", ["admin", "self"])
async def test_password_change_has_one_durable_outcome(tmp_path, monkeypatch, interruption, authority):
    path = tmp_path / "password.db"
    db = Database(str(path))
    await db.initialize()
    mgr = UserManager(db)
    user = await mgr.create_user(username="archer", password="password1")
    here = await mgr.issue_token(user["id"])
    elsewhere = await mgr.issue_token(user["id"])
    proof = await mgr.authenticate("archer", "password1")
    if interruption == "revoke_failure":
        await db.conn.execute(
            "CREATE TRIGGER refuse_password_revocation BEFORE UPDATE OF revoked_at ON auth_tokens "
            "BEGIN SELECT RAISE(ABORT, 'revocation rejected'); END"
        )
        await db.conn.commit()
    entered, release = asyncio.Event(), asyncio.Event()
    execute, commit = db.conn.execute, db.conn.commit
    password_written = False

    async def interrupted_execute(sql, parameters=()):
        nonlocal password_written
        cursor = await execute(sql, parameters)
        if sql.startswith("UPDATE users SET password_hash"):
            password_written = True
            if interruption == "password":
                entered.set()
                await release.wait()
        return cursor

    async def interrupted_commit():
        if password_written and interruption == "commit":
            entered.set()
            await release.wait()
        await commit()

    with monkeypatch.context() as patch:
        patch.setattr(db.conn, "execute", interrupted_execute)
        patch.setattr(db.conn, "commit", interrupted_commit)
        operation = (
            mgr.change_own_password(proof, "a-better-password", keep_token_hash=token_digest(here))
            if authority == "self" else
            mgr.set_password(user["id"], "a-better-password", keep_token_hash=token_digest(here))
        )
        changing = asyncio.create_task(operation)
        if interruption == "revoke_failure":
            with pytest.raises(sqlite3.IntegrityError, match="revocation rejected"):
                await changing
        else:
            await asyncio.wait_for(entered.wait(), 1)
            changing.cancel()
            await asyncio.sleep(0)
            changing.cancel()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await changing
    await db.close()

    reopened = Database(str(path))
    await reopened.initialize()
    try:
        restored = UserManager(reopened)
        changed = interruption != "revoke_failure"
        await restored.authenticate("archer", "a-better-password" if changed else "password1")
        with pytest.raises(UserError):
            await restored.authenticate("archer", "password1" if changed else "a-better-password")
        assert await restored.resolve_token(here) is not None
        assert (await restored.resolve_token(elsewhere) is None) == changed
    finally:
        await reopened.close()


class TestInvites:
    @pytest.mark.asyncio
    async def test_registration_consumes_the_code(self, users):
        mgr, _db = users
        admin = await mgr.create_user(username="archer", password="password1", is_admin=True)
        invite = await mgr.create_invite(created_by=admin["id"])

        user = await mgr.register(
            invite_code=invite["code"], username="vera", password="password2"
        )
        assert user["username"] == "vera" and user["is_admin"] is False

        with pytest.raises(UserError) as e:
            await mgr.register(
                invite_code=invite["code"], username="pete", password="password3"
            )
        assert e.value.status_code == 403

    @pytest.mark.asyncio
    async def test_a_single_use_code_cannot_be_spent_twice_concurrently(self, users):
        """The guard is in the UPDATE, not in a read-then-write, so two
        registrations racing for the last use cannot both win."""
        mgr, db = users
        invite = await mgr.create_invite(created_by=None)
        results = await asyncio.gather(
            db.claim_invite(invite["code"]),
            db.claim_invite(invite["code"]),
            db.claim_invite(invite["code"]),
        )
        assert sum(1 for r in results if r) == 1
        assert (await db.get_invite(invite["code"]))["used_count"] == 1

    @pytest.mark.asyncio
    async def test_a_multi_use_code_is_good_exactly_that_many_times(self, users):
        mgr, db = users
        invite = await mgr.create_invite(created_by=None, max_uses=2)
        assert await db.claim_invite(invite["code"]) is True
        assert await db.claim_invite(invite["code"]) is True
        assert await db.claim_invite(invite["code"]) is False

    @pytest.mark.asyncio
    async def test_revoked_and_expired_codes_are_refused(self, users):
        mgr, db = users
        admin = await mgr.create_user(username="owner", password="password1", is_admin=True)
        revoked = await mgr.create_invite(created_by=admin["id"])
        await db.revoke_invite(revoked["code"], at=datetime.now(UTC).isoformat())
        expired = await mgr.create_invite(created_by=admin["id"])
        await db.conn.execute(
            "UPDATE invites SET expires_at = ? WHERE code = ?",
            ((datetime.now(UTC) - timedelta(days=1)).isoformat(), expired["code"]),
        )
        await db.conn.commit()

        for code in (revoked["code"], expired["code"], "never-existed"):
            with pytest.raises(UserError) as e:
                await mgr.register(
                    invite_code=code, username="vera", password="password2"
                )
            assert e.value.status_code == 403

    @pytest.mark.asyncio
    async def test_a_bad_code_cannot_be_used_to_probe_usernames(self, users):
        """The code is judged before the name is, so "that username is taken"
        never comes back to someone who does not hold a good invite."""
        mgr, _db = users
        await mgr.create_user(username="archer", password="password1")
        with pytest.raises(UserError) as e:
            await mgr.register(
                invite_code="nope", username="archer", password="password2"
            )
        assert e.value.status_code == 403 and "invite" in e.value.message.lower()


class TestDataKeys:
    @pytest.mark.parametrize("resolver", ["manager", "background"])
    async def test_ownerless_secrets_never_use_setup_proof_as_a_data_key(self, users, resolver):
        from server import deps

        manager, database = users
        deps.set_user_manager(manager)
        resolve = manager.data_key_for if resolver == "manager" else deps.data_key_for
        with pytest.raises(UserError) as refused:
            await resolve(None)
        assert refused.value.status_code == 401
        assert await database.count_users() == 0
        assert await database.load_credentials() == []

    """Every user gets a data key at creation, wrapped by the server's master
    key (multi-tenancy.md §4). The password is not involved, and that is the
    point: a schedule fires while its owner is asleep, and the server still has
    to decrypt their credential to run it.
    """

    @pytest.mark.asyncio
    async def test_a_new_account_has_a_wrapped_key_that_unwraps(self, users):
        mgr, _db = users
        user = await mgr.create_user(username="archer", password="password1")
        assert user["dek_wrapped"], "no data key was minted"
        key = mgr.data_key(user)
        assert len(key) > 20
        # It really is this user's key: a secret sealed with it comes back.
        from server.crypto import decrypt, encrypt

        assert decrypt(encrypt("sk-ant-secret", key), key) == "sk-ant-secret"

    @pytest.mark.asyncio
    async def test_two_users_do_not_share_a_key(self, users):
        mgr, _db = users
        a = await mgr.create_user(username="archer", password="password1")
        b = await mgr.create_user(username="vera", password="password2")
        assert mgr.data_key(a) != mgr.data_key(b)

        from server.crypto import decrypt, encrypt

        sealed = encrypt("archer's api key", mgr.data_key(a))
        with pytest.raises(ValueError):
            decrypt(sealed, mgr.data_key(b))

    @pytest.mark.asyncio
    async def test_an_invited_account_gets_one_too(self, users):
        """The two creation paths mint it through one helper, so they cannot
        drift into one of them forgetting."""
        mgr, _db = users
        admin = await mgr.create_user(username="owner", password="password1", is_admin=True)
        invite = await mgr.create_invite(created_by=admin["id"])
        user = await mgr.register(
            invite_code=invite["code"], username="vera", password="password2"
        )
        assert user["dek_wrapped"]
        assert mgr.data_key(user)

    @pytest.mark.asyncio
    async def test_changing_a_password_leaves_the_data_key_alone(self, users):
        """The whole reason for the hierarchy: re-keying on every password
        change would mean a password change could half-fail and strand
        secrets."""
        mgr, db = users
        user = await mgr.create_user(username="archer", password="password1")
        before = mgr.data_key(user)

        await mgr.set_password(user["id"], "a-better-password")

        after = mgr.data_key(await db.get_user(user["id"]))
        assert after == before




@pytest.mark.asyncio
async def test_a_session_token_is_long_lived_but_still_expires(users):
    """A signed-in browser is not asked to log in again for 30 days — but the
    token must still carry an expiry, so a forgotten one does not live for
    ever."""
    from datetime import UTC, datetime

    from server.users import SESSION_TTL

    assert SESSION_TTL.days == 30, "the remember-me window"

    mgr, db = users
    user = await mgr.create_user(username="trip", password="password1")
    token = await mgr.issue_token(user["id"])
    row = await db.get_token(token_digest(token))
    assert row["expires_at"] is not None, "a session token with no expiry lives for ever"
    remaining = datetime.fromisoformat(row["expires_at"]) - datetime.now(UTC)
    assert 28 <= remaining.days <= 30


@pytest.mark.asyncio
async def test_a_registered_account_gets_its_workspace_provisioned(users):
    """The bug Nancy hit: an invite-registered account had no workspace
    directory, so its first session spawned the CLI with a cwd that did not
    exist and the turn died on FileNotFoundError. Bootstrap provisioned the
    first account's dirs; registration did not. `_insert` now does it for every
    account, so this holds for register, setup and admin-created alike."""
    from server.workspace import paths_for

    mgr, db = users
    admin = await mgr.create_user(username="owner", password="password1", is_admin=True)
    invite = await mgr.create_invite(created_by=admin["id"], max_uses=1, ttl_days=1)
    nancy = await mgr.register(
        invite_code=invite["code"], username="nancy", password="password2"
    )

    ws = paths_for(nancy["id"]).workspace
    assert ws.is_dir(), "a registered account's workspace was not created"
