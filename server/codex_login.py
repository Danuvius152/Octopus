"""In-app Codex (ChatGPT) login orchestrator — `codex login --device-auth`.

The Codex analog of `oauth_login.py`. Where Claude's flow is HTTP + a pasted
code, Codex auth is *directory-backed* (codex-backend.md §7): we run
`codex login --device-auth` against a fresh per-credential `CODEX_HOME`, scrape
the verification URL + one-time code it prints, and show them in the UI. Codex
itself polls the device endpoint; when the user authorizes in their browser it
writes `auth.json` into that dir and exits 0. No secret is stored in Octopus —
the credential *is* the dir, and Codex manages the token + its refresh there.

The original task owns spawn, staging, publication and confirmed cleanup.
Status polling only observes that operation and current credential availability.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import signal
import tempfile
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Literal

from .aio import complete_cleanup
from .codex_credentials import CodexCredentials
from .config import settings
from .database import Database
from .harness.run import _which_with_fallback, augmented_path
from .process_groups import wait_group_exit

logger = logging.getLogger(__name__)

_LOGIN_TTL_SECONDS = 20 * 60  # device codes expire in ~15 min; pad a little
_SCRAPE_TIMEOUT_SECONDS = 30.0
_REAP_TIMEOUT_SECONDS = 5.0

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_URL_RE = re.compile(r"https?://\S+")
# One-time code like "FGAF-NRHWK" (groups of uppercase letters/digits).
_CODE_RE = re.compile(r"\b[A-Z0-9]{3,8}-[A-Z0-9]{3,8}\b")


class CodexLoginState(str, Enum):
    pending = "pending"  # url+code shown, waiting for the user to authorize
    success = "success"
    error = "error"
    cancelled = "cancelled"


class CodexLoginPhase(str, Enum):
    authorizing = "authorizing"
    publishing = "publishing"
    cleanup = "cleanup"
    retired = "retired"


class CodexLoginConflict(RuntimeError):
    """The original operation still owns this credential target."""


@dataclass
class CodexLoginSession:
    id: str
    credential_id: str
    codex_home: str
    label: str
    verification_url: str | None = None
    user_code: str | None = None
    state: CodexLoginState = CodexLoginState.pending
    message: str | None = None
    phase: CodexLoginPhase = CodexLoginPhase.authorizing
    # Who started this login — see `LoginSession.user_id` in oauth_login.py.
    user_id: str | None = None
    # A reauthorization owns only a staging home, never the existing native
    # records. Publish its auth.json atomically after a successful CLI exit.
    _login_home: str | None = field(default=None, repr=False)
    _reauth: bool = field(default=False, repr=False)
    _committer: asyncio.Task[None] | None = field(default=None, repr=False)
    _cleanup_task: asyncio.Task[None] | None = field(default=None, repr=False)
    _group_retired: bool = field(default=False, repr=False)
    _proc: asyncio.subprocess.Process | None = field(default=None, repr=False)
    _task: asyncio.Task | None = field(default=None, repr=False)
    _watchdog: asyncio.Task | None = field(default=None, repr=False)
    _scraped: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    _created_at: float = field(default=0.0, repr=False)

    @property
    def device_code_ready(self) -> bool:
        return bool(self.verification_url and self.user_code)


@dataclass
class _CredentialDeletion:
    """Original deletion proof survives a failed local home cleanup.

    Only a confirmed removed row authorizes permanent-home deletion. Retrying
    cleaning_home reuses that proof and never replays the database operation.
    """

    credential_id: str
    user_id: str | None
    login: CodexLoginSession | None
    remove: Callable[[], Awaitable[dict[str, Any] | None]]
    phase: Literal["retiring_login", "deleting_binding", "cleaning_home"] = "retiring_login"
    removed_row: dict[str, Any] | None = None
    task: asyncio.Task[dict[str, Any] | None] | None = None


def build_codex_login_argv() -> list[str] | None:
    """`[<codex>, "login", "--device-auth"]`, or None if codex isn't on PATH.
    A module function so tests can monkeypatch it to point at a fake CLI."""
    binary = _which_with_fallback("codex")
    if binary is None:
        return None
    return [binary, "login", "--device-auth"]


def codex_home_root() -> str:
    return os.path.expanduser(settings.codex_home_dir)


def codex_home_for(credential_id: str) -> str:
    return os.path.join(codex_home_root(), credential_id)


def _kill_process_group(proc: asyncio.subprocess.Process | None) -> None:
    """SIGKILL the whole process group. `codex login` spawns helpers that hold
    the stdout pipe open, so killing only the parent leaves our reader blocked
    until the device code expires (~15 min). We spawn it as a group leader
    (`start_new_session=True`) and kill the group so the pipe EOFs at once."""
    if proc is None:
        return
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except ProcessLookupError:
            pass


def _kill_login_group(session: CodexLoginSession) -> None:
    if not session._group_retired:
        _kill_process_group(session._proc)


async def _remove_owned_home(path: str | Path) -> None:
    directory = Path(path)
    try:
        directory.stat()
    except FileNotFoundError:
        return
    # exists() can turn permission/I/O failures into False. Only a known
    # missing root or successful deletion confirms this original cleanup.
    await asyncio.to_thread(shutil.rmtree, directory)


class CodexLoginManager:
    """Holds in-flight Codex device-auth logins. Single instance, app-lifetime."""

    def __init__(self, db: Database | None = None) -> None:
        self._sessions: dict[str, CodexLoginSession] = {}
        self._targets: dict[str, CodexLoginSession] = {}
        self._deletions: dict[str, _CredentialDeletion] = {}
        self._credentials = CodexCredentials(db) if db is not None else None
        self._shutting_down = False

    def bind(self, db: Database) -> None:
        self._credentials = CodexCredentials(db)

    async def start(
        self,
        label: str,
        *,
        reauth_credential_id: str | None = None,
        user_id: str | None = None,
    ) -> CodexLoginSession:
        if self._shutting_down:
            raise RuntimeError("Codex login manager is shutting down")
        argv = build_codex_login_argv()
        if argv is None:
            raise RuntimeError(
                "The `codex` CLI isn't installed on the server, so Codex "
                "sign-in isn't available."
            )

        # Keep the credential's permanent home and bindings. A reauthorization
        # runs in an owned staging home so a failed CLI cannot damage valid
        # auth, configuration or native session records in the existing home.
        credential_id = reauth_credential_id or uuid.uuid4().hex[:12]
        if credential_id in self._targets or credential_id in self._deletions:
            raise CodexLoginConflict("A sign-in for this credential is still in progress")
        home = codex_home_for(credential_id)
        os.makedirs(codex_home_root(), exist_ok=True)
        login_home = tempfile.mkdtemp(prefix=f".{credential_id}-login-", dir=codex_home_root())

        env = os.environ.copy()
        env["CODEX_HOME"] = login_home
        # codex is a `#!/usr/bin/env node` script; make sure the child finds
        # node even when the service PATH omits the nvm bin (else exit 127).
        env["PATH"] = augmented_path(env.get("PATH"), os.path.dirname(argv[0]))

        loop = asyncio.get_running_loop()
        session = CodexLoginSession(
            id=uuid.uuid4().hex[:16],
            credential_id=credential_id,
            codex_home=home,
            label=label,
            _created_at=loop.time(),
            user_id=user_id,
            _login_home=login_home,
            _reauth=reauth_credential_id is not None,
        )
        # Register before yielding to spawn. The drive owns the process even
        # if the HTTP waiter is cancelled or shutdown starts during creation.
        self._sessions[session.id] = session
        self._targets[credential_id] = session
        credentials = self._credentials
        spawned = asyncio.Event()

        async def launch_and_drive() -> None:
            try:
                try:
                    session._proc = await asyncio.create_subprocess_exec(
                        *argv, cwd=login_home, env=env,
                        stdin=asyncio.subprocess.DEVNULL,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.STDOUT,
                        start_new_session=True,
                    )
                except Exception as exc:
                    await self._fail(session, f"Could not launch the `codex` CLI for sign-in: {exc}")
                    return
                finally:
                    spawned.set()
                if session.state != CodexLoginState.pending:
                    _kill_login_group(session)
                await self._drive(session, credentials)
            except asyncio.CancelledError:
                if session.state == CodexLoginState.pending:
                    session.state = CodexLoginState.cancelled
                    session.message = "Sign-in execution cancelled"
                raise
            except Exception as exc:
                await self._fail(session, f"Could not complete Codex sign-in: {exc}")
            finally:
                session._scraped.set()
                await complete_cleanup(self._join_cleanup(session))

        session._task = asyncio.create_task(launch_and_drive(), name=f"codex-login-{session.id}")
        def finished(task):
            if not task.cancelled() and (error := task.exception()) is not None:
                session.message = f"Sign-in cleanup remains unconfirmed: {error}"
                logger.error("Codex login %s cleanup remains unconfirmed", session.id,
                             exc_info=(type(error), error, error.__traceback__))
            self._release_target(session)

        session._task.add_done_callback(finished)
        # Scrape happens asynchronously — `start` returns immediately so the
        # HTTP request can't outlive a proxy/tunnel timeout (codex blocks the
        # whole device-auth window polling for authorization). The UI polls
        # `status` for the URL+code, then for completion.
        session._watchdog = asyncio.create_task(self._scrape_watchdog(session))
        self._gc()
        await spawned.wait()
        if session._proc is None:
            await self._reap(session)
            raise RuntimeError(session.message)

        logger.info(
            "Codex login %s: spawned (cred=%s)", session.id, credential_id
        )
        return session

    async def _scrape_watchdog(self, session: CodexLoginSession) -> None:
        """Fail the login if codex never emits a device code (e.g. it can't
        reach auth.openai.com), so it doesn't sit pending forever."""
        try:
            await asyncio.wait_for(session._scraped.wait(), _SCRAPE_TIMEOUT_SECONDS)
        except TimeoutError:
            if (session.phase == CodexLoginPhase.authorizing
                    and session.state == CodexLoginState.pending and not session.device_code_ready):
                await self._fail(
                    session,
                    "Codex didn't return a device code in time — check the "
                    "server's network access to auth.openai.com.",
                )
                await self._reap(session)

    async def _drive(self, session: CodexLoginSession, credentials: CodexCredentials | None) -> None:
        """Read codex's output: scrape url+code, then await exit and classify."""
        proc = session._proc
        assert proc is not None and proc.stdout is not None
        buf = ""
        try:
            async for raw in proc.stdout:
                line = _ANSI_RE.sub("", raw.decode(errors="replace"))
                buf += line
                if not session._scraped.is_set():
                    self._try_scrape(session, buf)
        except Exception:
            logger.debug("Codex login %s: stdout read error", session.id, exc_info=True)

        rc = await proc.wait()
        if session.state != CodexLoginState.pending:
            return
        login_home = session._login_home or session.codex_home
        auth_path = os.path.join(login_home, "auth.json")
        auth_ok = os.path.isfile(auth_path)
        if rc == 0 and auth_ok:
            # Retire possible descendants before validating their final bytes.
            _kill_login_group(session)
            await wait_group_exit(proc.pid, timeout=_REAP_TIMEOUT_SECONDS, after_kill=True)
            session._group_retired = True
            if session.state != CodexLoginState.pending:
                return
            try:
                with open(auth_path) as auth_file:
                    auth = json.load(auth_file)
                if not isinstance(auth, dict) or not isinstance(auth.get("tokens"), dict) or not auth["tokens"].get("access_token"):
                    raise ValueError("missing device authorization token")
                os.chmod(auth_path, 0o600)
            except (OSError, ValueError):
                await self._fail(session, "Codex sign-in produced invalid auth.json.")
                session._scraped.set()
                return
            if credentials is None:
                raise RuntimeError("Codex credential database is not bound")
            session.phase = CodexLoginPhase.publishing
            session._committer = asyncio.create_task(credentials.commit(session), name=f"codex-login-{session.id}-commit")
            await complete_cleanup(self._join_commit(session))
            logger.info("Codex login %s: success", session.id)
        else:
            await self._fail(
                session,
                "Codex sign-in did not complete."
                if rc == 0
                else f"Codex sign-in exited with status {rc}.",
            )
        session._scraped.set()  # unblock start() if it never scraped

    async def _join_commit(self, session: CodexLoginSession) -> None:
        assert session._committer is not None
        await asyncio.shield(session._committer)

    async def _cleanup(self, session: CodexLoginSession) -> None:
        """Only confirmed group exit and staging removal release the target."""
        proc = session._proc
        if proc is not None and not session._group_retired:
            _kill_login_group(session)
            await proc.wait()
            await wait_group_exit(proc.pid, timeout=_REAP_TIMEOUT_SECONDS, after_kill=True)
            session._group_retired = True
        if session._login_home is not None:
            await _remove_owned_home(session._login_home)
        session.phase = CodexLoginPhase.retired
        self._release_target(session)

    def _release_target(self, session: CodexLoginSession) -> None:
        if (session.phase == CodexLoginPhase.retired and session._task is not None and session._task.done()
                and self._targets.get(session.credential_id) is session):
            self._targets.pop(session.credential_id)

    async def _join_cleanup(self, session: CodexLoginSession) -> None:
        """Explicit cancel/shutdown can retry failed cleanup, never publication.

        Retain the same target and result. Group-exit proof is kept so a later
        staging cleanup retry never signals a retired process group's old pid.
        """
        await asyncio.shield(self._start_cleanup(session))

    def _start_cleanup(self, session: CodexLoginSession) -> asyncio.Task[None]:
        task = session._cleanup_task
        if task is None or (task.done() and not task.cancelled() and task.exception() is not None):
            session.phase = CodexLoginPhase.cleanup
            task = asyncio.create_task(self._cleanup(session), name=f"codex-login-{session.id}-cleanup")
            session._cleanup_task = task
        return task

    def _try_scrape(self, session: CodexLoginSession, buf: str) -> None:
        if session.verification_url is None:
            m = _URL_RE.search(buf)
            if m:
                session.verification_url = m.group(0).rstrip(".")
        if session.user_code is None:
            m = _CODE_RE.search(buf)
            if m:
                session.user_code = m.group(0)
        if session.device_code_ready:
            session._scraped.set()

    async def _reap(self, session: CodexLoginSession) -> None:
        """Let the drive task observe the (killed) process and reap it within
        the running loop — otherwise its transport is finalized after loop
        close (a noisy "Event loop is closed" unraisable). Bounded so a
        misbehaving child can never wedge a request."""
        if session._task is None:
            return
        done, _ = await asyncio.wait({session._task}, timeout=_REAP_TIMEOUT_SECONDS)
        if not done or (session._proc is not None and session._proc.returncode is None):
            raise RuntimeError(f"Codex login {session.id} shutdown did not confirm process exit")
        if session.phase == CodexLoginPhase.cleanup:
            # An explicit retirement request may finish a prior local cleanup
            # failure. The original failed task and diagnostic remain recorded.
            cleanup = self._start_cleanup(session)
            done, _ = await asyncio.wait({cleanup}, timeout=_REAP_TIMEOUT_SECONDS)
            if not done:
                raise RuntimeError(f"Codex login {session.id} cleanup did not exit")
            cleanup.result()
        elif not session._task.cancelled() and session.phase != CodexLoginPhase.retired:
            session._task.result()
        if session.phase != CodexLoginPhase.retired:
            raise RuntimeError(f"Codex login {session.id} cleanup remains unconfirmed")

    async def _fail(self, session: CodexLoginSession, message: str) -> None:
        if session.state != CodexLoginState.pending:
            return
        session.state = CodexLoginState.error
        session.message = message
        session._scraped.set()
        _kill_login_group(session)
        logger.info("Codex login %s: error — %s", session.id, message)

    async def cancel(self, login_id: str) -> None:
        session = self._sessions.get(login_id)
        if session is None:
            return
        if session.phase == CodexLoginPhase.authorizing and session.state == CodexLoginState.pending:
            session.state = CodexLoginState.cancelled
            session.message = "cancelled by user"
            _kill_login_group(session)
        await self._reap(session)
        logger.info("Codex login %s: cancelled", login_id)

    def get(self, login_id: str) -> CodexLoginSession | None:
        return self._sessions.get(login_id)

    def target_for(self, credential_id: str, user_id: str | None) -> CodexLoginSession | None:
        target = self._targets.get(credential_id)
        return target if target is not None and target.user_id == user_id else None

    def owns_credential(self, credential_id: str, user_id: str | None) -> bool:
        deletion = self._deletions.get(credential_id)
        return (self.target_for(credential_id, user_id) is not None
                or deletion is not None and deletion.user_id == user_id)

    async def delete_credential(
        self, credential_id: str, user_id: str | None,
        remove: Callable[[], Awaitable[dict[str, Any] | None]],
    ) -> dict[str, Any] | None:
        """Retire the original login before the short binding/file deletion.

        Waiting here holds neither the configuration lock nor a DB transaction.
        The deletion's original task owns cleanup if its observer disappears.
        """
        existing = self._deletions.get(credential_id)
        if existing is not None:
            if existing.user_id != user_id:
                return None
            return await asyncio.shield(self._deletion_task(existing))
        deletion = _CredentialDeletion(credential_id, user_id, self.target_for(credential_id, user_id), remove)
        self._deletions[credential_id] = deletion
        return await asyncio.shield(self._deletion_task(deletion))

    def _deletion_task(self, deletion: _CredentialDeletion) -> asyncio.Task[dict[str, Any] | None]:
        task = deletion.task
        if task is None or task.done():
            task = asyncio.create_task(self._delete_original(deletion), name=f"codex-credential-{deletion.credential_id}-delete")
            deletion.task = task

            def finished(task):
                if not task.cancelled() and (error := task.exception()) is not None and self._deletions.get(deletion.credential_id) is deletion:
                    logger.error("Codex credential %s deletion cleanup remains unconfirmed", deletion.credential_id,
                                 exc_info=(type(error), error, error.__traceback__))

            task.add_done_callback(finished)
        return task

    async def _delete_original(self, deletion: _CredentialDeletion) -> dict[str, Any] | None:
        if deletion.phase == "retiring_login":
            if deletion.login is not None:
                await self.cancel(deletion.login.id)
            deletion.phase = "deleting_binding"
        if deletion.phase == "deleting_binding":
            try:
                deletion.removed_row = await deletion.remove()
            except Exception:
                # The DB's healthy, explicit refusal changed no binding. There
                # is no file-delete authority or unfinished effect to retain.
                self._deletions.pop(deletion.credential_id, None)
                raise
            if deletion.removed_row is None:
                self._deletions.pop(deletion.credential_id, None)
                login = deletion.login
                if login is not None and not login._reauth and login.state != CodexLoginState.success:
                    # An unpublished first login owned staging only. Its
                    # confirmed cancellation needs no permanent-home cleanup.
                    return {"id": deletion.credential_id, "backend": "codex", "user_id": deletion.user_id}
                return None
            deletion.phase = "cleaning_home"
        await _remove_owned_home(codex_home_for(deletion.credential_id))
        self._deletions.pop(deletion.credential_id, None)
        return deletion.removed_row

    def drain_blockers(self) -> tuple[str, ...]:
        if self._deletions or any(item.phase != CodexLoginPhase.retired or (item._task is not None and not item._task.done())
               or (item._watchdog is not None and not item._watchdog.done())
               or (item._proc is not None and item._proc.returncode is None)
               for item in self._sessions.values()):
            return ("login task or process",)
        return ()

    def begin_shutdown(self) -> None:
        """Close admission and signal captured login owners without yielding."""
        self._shutting_down = True
        for session in list(self._sessions.values()):
            if session.phase == CodexLoginPhase.authorizing and session.state == CodexLoginState.pending:
                session.state = CodexLoginState.cancelled
                session.message = "server shutdown"
                _kill_login_group(session)
            if session._watchdog is not None and not session._watchdog.done() and not session._watchdog.cancelling():
                session._watchdog.cancel()

    async def shutdown(self) -> None:
        self.begin_shutdown()
        failures: list[str] = []
        for session in list(self._sessions.values()):
            try:
                await self._reap(session)
                if session._watchdog is not None:
                    done, _ = await asyncio.wait({session._watchdog}, timeout=_REAP_TIMEOUT_SECONDS)
                    if not done:
                        raise RuntimeError("scrape watchdog did not exit")
                    if not session._watchdog.cancelled():
                        session._watchdog.result()
            except Exception as exc:
                failures.append(f"{session.id}: {exc}")
                continue
            self._sessions.pop(session.id, None)
        for credential_id, deletion in tuple(self._deletions.items()):
            task = self._deletion_task(deletion)
            done, _ = await asyncio.wait({task}, timeout=_REAP_TIMEOUT_SECONDS)
            if not done:
                failures.append(f"{credential_id}: credential deletion did not exit")
            else:
                try:
                    task.result()
                except Exception as error:
                    if self._deletions.get(credential_id) is deletion:
                        failures.append(f"{credential_id}: {error}")
        if failures:
            raise RuntimeError("Codex login shutdown incomplete: " + "; ".join(failures))

    def _gc(self) -> None:
        loop = asyncio.get_running_loop()
        now = loop.time()
        for sid in [
            s.id
            for s in self._sessions.values()
            if now - s._created_at > _LOGIN_TTL_SECONDS
            and s.phase == CodexLoginPhase.retired
            and (s._task is None or s._task.done())
            and (s._watchdog is None or s._watchdog.done())
            and (s._proc is None or s._proc.returncode is not None)
        ]:
            self._sessions.pop(sid, None)


# Singleton — wired into the FastAPI lifespan in main.py.
codex_login_manager = CodexLoginManager()
