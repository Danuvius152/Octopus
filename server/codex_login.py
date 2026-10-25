"""In-app Codex (ChatGPT) login orchestrator — `codex login --device-auth`.

The Codex analog of `oauth_login.py`. Where Claude's flow is HTTP + a pasted
code, Codex auth is *directory-backed* (codex-backend.md §7): we run
`codex login --device-auth` against a fresh per-credential `CODEX_HOME`, scrape
the verification URL + one-time code it prints, and show them in the UI. Codex
itself polls the device endpoint; when the user authorizes in their browser it
writes `auth.json` into that dir and exits 0. No secret is stored in Octopus —
the credential *is* the dir, and Codex manages the token + its refresh there.

Lifecycle: `start(label)` → spawn + scrape (returns url+code) → caller polls
`status(login_id)` until `success`/`error` → on success the route persists a
`backend_credentials` row pointing at the dir.
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
from dataclasses import dataclass, field
from enum import Enum

from .config import settings
from .harness.run import _which_with_fallback, augmented_path

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
    persisted: bool = False  # set once the route saves the credential row
    # Who started this login — see `LoginSession.user_id` in oauth_login.py.
    user_id: str | None = None
    # A reauthorization owns only a staging home, never the existing native
    # records. Publish its auth.json atomically after a successful CLI exit.
    _login_home: str | None = field(default=None, repr=False)
    _reauth: bool = field(default=False, repr=False)
    _persist_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    _proc: asyncio.subprocess.Process | None = field(default=None, repr=False)
    _task: asyncio.Task | None = field(default=None, repr=False)
    _watchdog: asyncio.Task | None = field(default=None, repr=False)
    _scraped: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    _created_at: float = field(default=0.0, repr=False)


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


class CodexLoginManager:
    """Holds in-flight Codex device-auth logins. Single instance, app-lifetime."""

    def __init__(self) -> None:
        self._sessions: dict[str, CodexLoginSession] = {}
        self._shutting_down = False

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
        home = codex_home_for(credential_id)
        os.makedirs(home, exist_ok=reauth_credential_id is not None)
        login_home = (
            tempfile.mkdtemp(prefix=f".{credential_id}-login-", dir=codex_home_root())
            if reauth_credential_id else home
        )

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
        spawned = asyncio.Event()

        async def launch_and_drive() -> None:
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
            if session.state == CodexLoginState.cancelled:
                _kill_process_group(session._proc)
            await self._drive(session)

        session._task = asyncio.create_task(launch_and_drive(), name=f"codex-login-{session.id}")
        # Scrape happens asynchronously — `start` returns immediately so the
        # HTTP request can't outlive a proxy/tunnel timeout (codex blocks the
        # whole device-auth window polling for authorization). The UI polls
        # `status` for the URL+code, then for completion.
        session._watchdog = asyncio.create_task(self._scrape_watchdog(session))
        self._gc()
        await spawned.wait()
        if session._proc is None:
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
            if session.state == CodexLoginState.pending and not session.verification_url:
                await self._fail(
                    session,
                    "Codex didn't return a device code in time — check the "
                    "server's network access to auth.openai.com.",
                )
                await self._reap(session)

    async def _drive(self, session: CodexLoginSession) -> None:
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
        if session.state == CodexLoginState.cancelled:
            shutil.rmtree(session._login_home or session.codex_home, ignore_errors=True)
            session._scraped.set()
            return
        login_home = session._login_home or session.codex_home
        auth_path = os.path.join(login_home, "auth.json")
        auth_ok = os.path.isfile(auth_path)
        if rc == 0 and auth_ok:
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
            if session._reauth:
                try:
                    os.replace(auth_path, os.path.join(session.codex_home, "auth.json"))
                except OSError as exc:
                    await self._fail(session, f"Could not save Codex sign-in: {exc}")
                    session._scraped.set()
                    return
                shutil.rmtree(login_home, ignore_errors=True)
            session.state = CodexLoginState.success
            logger.info("Codex login %s: success", session.id)
        else:
            await self._fail(
                session,
                "Codex sign-in did not complete."
                if rc == 0
                else f"Codex sign-in exited with status {rc}.",
            )
        session._scraped.set()  # unblock start() if it never scraped

    def _try_scrape(self, session: CodexLoginSession, buf: str) -> None:
        if session.verification_url is None:
            m = _URL_RE.search(buf)
            if m:
                session.verification_url = m.group(0).rstrip(".")
        if session.user_code is None:
            m = _CODE_RE.search(buf)
            if m:
                session.user_code = m.group(0)
        if session.verification_url and session.user_code:
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
        if not session._task.cancelled():
            session._task.result()

    async def _fail(self, session: CodexLoginSession, message: str) -> None:
        session.state = CodexLoginState.error
        session.message = message
        _kill_process_group(session._proc)
        if session._proc is not None:
            await session._proc.wait()
        # This login owns the fresh home or reauthorization staging home only.
        shutil.rmtree(session._login_home or session.codex_home, ignore_errors=True)
        logger.info("Codex login %s: error — %s", session.id, message)

    async def cancel(self, login_id: str) -> None:
        session = self._sessions.get(login_id)
        if session is None:
            return
        if session.state in (CodexLoginState.success, CodexLoginState.error):
            return
        session.state = CodexLoginState.cancelled
        session.message = "cancelled by user"
        _kill_process_group(session._proc)
        await self._reap(session)
        shutil.rmtree(session._login_home or session.codex_home, ignore_errors=True)
        logger.info("Codex login %s: cancelled", login_id)

    def get(self, login_id: str) -> CodexLoginSession | None:
        return self._sessions.get(login_id)

    def begin_shutdown(self) -> None:
        """Close admission and signal captured login owners without yielding."""
        self._shutting_down = True
        for session in list(self._sessions.values()):
            if session.state == CodexLoginState.pending:
                session.state = CodexLoginState.cancelled
                session.message = "server shutdown"
            _kill_process_group(session._proc)
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
        if failures:
            raise RuntimeError("Codex login shutdown incomplete: " + "; ".join(failures))

    def _gc(self) -> None:
        loop = asyncio.get_running_loop()
        now = loop.time()
        for sid in [
            s.id
            for s in self._sessions.values()
            if now - s._created_at > _LOGIN_TTL_SECONDS
            and (s._task is None or s._task.done())
            and (s._watchdog is None or s._watchdog.done())
            and (s._proc is None or s._proc.returncode is not None)
        ]:
            self._sessions.pop(sid, None)


# Singleton — wired into the FastAPI lifespan in main.py.
codex_login_manager = CodexLoginManager()
