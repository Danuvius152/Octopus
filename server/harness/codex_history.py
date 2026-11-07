"""Non-executing native history operations inside the existing fork saga.

The saga owns durable creation uncertainty and returned identity; HarnessRun
and its original supervisor own the short host, pipes and process group.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

from ..config import settings
from ..diagnostics import diagnostic_text
from . import codex_rpc
from .codex import _cleanup_legacy_fork, _codex_sessions_dir
from .codex_app_server import (
    _REQUIRED_FEATURES,
    initialize_connection,
    profile,
)
from .codex_items import NATIVE_LINE_BYTES, diagnostic_frame
from .codex_rpc import NativeProtocolError, NativeRpcError, RpcConnection
from .driver import RunDriver
from .events import HarnessCredential, HarnessEvent
from .fork import ForkArtifact, ForkCheckpoint, ForkHooks
from .profile import ParseOutput, RuntimeProfile, TurnContext
from .registry import get_harness
from .run import HarnessRun, RunConfig

logger = logging.getLogger(__name__)


class _HistoryDriver(RunDriver):
    input_in_argv = False
    multiplexed = True
    stdout_line_limit_bytes = NATIVE_LINE_BYTES

    def __init__(self, selected: RuntimeProfile, operation: Callable[[_HistoryDriver], Awaitable[dict[str, Any]]]) -> None:
        super().__init__(selected)
        self.operation = operation
        self.result: dict[str, Any] = {}
        self._io: HarnessRun | None = None
        self._rpc = RpcConnection()

    @property
    def connection_generation(self) -> str:
        return self._rpc.generation

    async def begin_input(self, io: HarnessRun, resume_id: str | None) -> None:
        self._io = io  # No model input, input recorder or accepted root exists.

    async def start_input(self, io: HarnessRun, context: TurnContext) -> None:
        await initialize_connection(io, context.working_dir, self.request)
        self.result = await self.operation(self)

    async def request(self, method: str, params: dict[str, Any], *,
                      before_write: Callable[[str], Awaitable[None]] | None = None) -> dict[str, Any]:
        assert self._io is not None
        with self._rpc.request(method, params) as request:
            await self._io.authorize_input()
            if before_write is not None:
                await before_write(request.identifier)
            await self._io.write_control(request.data)
            async with asyncio.timeout(codex_rpc.RPC_TIMEOUT):
                return await request.response

    def receive(self, obj: dict[str, Any]) -> ParseOutput:
        try:
            if "method" in obj:
                if obj["method"].startswith(("turn/", "item/")) or "id" in obj:
                    raise NativeProtocolError("Native history operation attempted implicit execution")
            else:
                self._rpc.receive(obj)
            return ParseOutput(activity=False)
        except Exception as error:
            self._rpc.reject_all(error)
            if self._io is not None:
                self._io._note_protocol_issue("fatal_protocol_error", json.dumps(diagnostic_frame(obj)), error=error)
                self._io.retire()
            return ParseOutput(events=[HarnessEvent(type="result", is_error=True,
                error_code="native_protocol_failed", content=str(error))], end_of_stream=True)

    def connection_closed(self) -> None:
        self._rpc.close(NativeProtocolError("Native history connection ended without acknowledgment"))


async def _operate(working_dir: str, credential: HarnessCredential | None, hooks: ForkHooks,
                   operation: Callable[[_HistoryDriver], Awaitable[dict[str, Any]]],
                   artifact: ForkCheckpoint) -> dict[str, Any]:
    selected = profile(get_harness("codex").profile)
    driver = _HistoryDriver(selected, operation)
    run = HarnessRun(replace(selected, new_driver=lambda _: driver), RunConfig(
        session_id=hooks.session_id, user_id=hooks.user_id, mcp_servers=[],
    ))
    try:
        if artifact.get("phase") == "created":
            # A new management host cannot inherit the previous host's proof.
            # Commit the closed gate before it can observe or change history.
            artifact["cleanup_allowed"] = False
            await hooks.checkpoint(artifact)
        try:
            async with hooks.manage(run):
                await run.start("", working_dir, credential=credential)
        finally:
            # ACK proves identity, not non-execution. Only the original host's
            # complete, trusted retirement can grant automatic compensation.
            # A failed checkpoint leaves the previously saved gate closed.
            if (artifact.get("phase") == "created" and run.retirement_state == "proven"
                    and not run.protocol_issues):
                artifact["cleanup_allowed"] = True
                await hooks.checkpoint(artifact)
        if run.protocol_issues:
            raise NativeProtocolError(f"Native history protocol failed: {run.protocol_issues}")
        return driver.result
    except BaseException as error:
        logger.error("Native history failed session=%s connection=%s\n%s\nstderr:\n%s",
                     hooks.session_id, driver.connection_generation, run.format_exception(error),
                     diagnostic_text(run.stderr_text, secrets=(settings.auth_token, credential.secret if credential else "")))
        raise


async def fork_copy(*, parent_working_dir: str, parent_resume_id: str | None, parent_credential: HarnessCredential | None = None,
                    dest_working_dir: str, new_resume_id: str, hooks: ForkHooks | None = None) -> ForkArtifact:
    if not parent_resume_id:
        return ForkArtifact(None, True)
    if hooks is None:
        raise ValueError("Native fork requires its original saga and resource owner")
    home = str(_codex_sessions_dir(parent_credential).parent.resolve())
    state: ForkCheckpoint = {"transport": "app-server", "phase": "unknown", "cleanup_allowed": False, "home": home,
                             "source_thread_id": parent_resume_id}

    async def create(driver: _HistoryDriver) -> dict[str, Any]:
        async def before_write(rpc_id: str) -> None:
            state["rpc_id"] = rpc_id
            state["connection_generation"] = driver.connection_generation
            await hooks.checkpoint(state)

        result = await driver.request("thread/fork", {"threadId": parent_resume_id,
            "cwd": dest_working_dir, "approvalPolicy": "never", "sandbox": "danger-full-access",
            "config": _REQUIRED_FEATURES, "deferGoalContinuation": True, "excludeTurns": True}, before_write=before_write)
        thread = result.get("thread") or {}
        identity = thread.get("id")
        if not isinstance(identity, str) or not identity or identity == parent_resume_id:
            raise NativeProtocolError("Native fork returned no independent history identity")
        # Save the returned identity before subsequent validation or shutdown.
        # A failure after creation must never send cleanup to the old hint.
        state["phase"] = "created"
        state["thread_id"] = identity
        state["path"] = thread.get("path")
        await hooks.checkpoint(state)
        path = thread.get("path")
        if (not isinstance(path, str) or not Path(path).resolve().is_relative_to(home)
                or result.get("cwd") != dest_working_dir):
            raise NativeProtocolError("Native fork returned a different store or working directory")
        return result

    result = await _operate(dest_working_dir, parent_credential, hooks, create, state)
    return ForkArtifact(result["thread"]["id"], False)


async def fork_cleanup(working_dir: str, resume_id_hint: str | None, fork_id: str, *, credential: HarnessCredential | None = None,
                       artifact: ForkCheckpoint | None = None, hooks: ForkHooks | None = None) -> None:
    if artifact is None:
        # Existing format-6 forks predate the native saga checkpoint. Only
        # retire their original, saved copy hint; no new fork uses file copy.
        await _cleanup_legacy_fork(working_dir, resume_id_hint, fork_id, credential=credential)
        return
    if hooks is None:
        raise NativeProtocolError("Native fork cleanup has no original resource owner")
    if artifact.get("phase") != "created":
        raise NativeProtocolError("Native fork creation is unknown; retain the original recovery material")
    if artifact.get("cleanup_allowed") is not True:
        raise NativeProtocolError("Native fork has no confirmed non-executing retirement; retain the original recovery material")
    identity, path = artifact.get("thread_id"), artifact.get("path")
    home = _codex_sessions_dir(credential).parent.resolve()
    if (not isinstance(identity, str) or not identity or identity == artifact.get("source_thread_id")
            or not isinstance(path, str) or artifact.get("home") != str(home)
            or not Path(path).resolve().is_relative_to(home)):
        raise NativeProtocolError("Native fork cleanup has no verified independent artifact")

    async def remove(driver: _HistoryDriver) -> dict[str, Any]:
        try:
            current = (await driver.request("thread/read", {"threadId": identity, "includeTurns": False}))["thread"]
        except NativeRpcError as error:
            if (error.error == {"code": -32600, "message": f"thread not found: {identity}"}
                    and not Path(path).exists()):
                return {}  # The exact original artifact is already absent.
            raise
        if (current.get("id") != identity or current.get("path") != path
                or current.get("cwd") != working_dir):
            raise NativeProtocolError("Native fork cleanup identity, path or ownership changed")
        # This never ran a turn, so it has no spawned subtree. Upstream checks
        # external references; a refusal retains this saga, without expanding it.
        return await driver.request("thread/delete", {"threadId": identity})

    await _operate(working_dir, credential, hooks, remove, artifact)
