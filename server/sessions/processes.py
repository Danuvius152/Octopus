"""The pool of held CLI processes: reuse, the idle reaper, the LRU cap, shutdown.

One slice of `SessionManager`, which was a single 4,017-line class with 84
methods (docs/2026-09-25-polish-pass.md). Split by responsibility; the
methods are unchanged and still reached through one `session_manager` object,
so no call site moved.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import replace
from typing import Any

from ..aio import drain_cancelled
from ..harness import HarnessEvent, HarnessRun
from .base import (
    _HELD_PROCESS_IDLE_SECONDS,
    _HELD_STOP_TIMEOUT,
    _MAX_HELD_PROCESSES,
    _REAPER_INTERVAL_SECONDS,
    Session,
    SessionManagerBase,
    logger,
)
from .owner_runtime import RetireHeldProcess


class ProcessesMixin(SessionManagerBase):



    async def _enforce_held_cap(self, *, keep_session_id: str | None = None) -> int:
        """Drop least-recently-used held processes until at most
        `_MAX_HELD_PROCESSES` remain, never touching `keep_session_id` (the
        session that just finished a turn) or any session mid-turn.

        This is the hard bound on the memory reuse costs. The reaper's idle
        timeout is the soft one — it releases processes nobody is using, while
        this stops a busy workspace from holding more than we budgeted for.
        """
        held = [
            s
            for s in self.sessions.values()
            if s._run.backend is not None
            and s._run.held_since is not None
            and s.id != keep_session_id
            and not self.session_is_executing(s)
        ]
        if len(held) < _MAX_HELD_PROCESSES:
            return 0
        held.sort(key=lambda s: s._run.held_since or 0.0)
        # `keep_session_id` occupies one slot of the budget.
        excess = len(held) - (_MAX_HELD_PROCESSES - (1 if keep_session_id else 0))
        stopped = 0
        for session in held[: max(0, excess)]:
            backend = session._run.backend
            held_since = session._run.held_since
            if backend is None or held_since is None:
                continue
            try:
                retirement = await self._session_owner(session).call_runtime(RetireHeldProcess(backend, held_since))
                if retirement is not None:
                    _, pending = await asyncio.wait({retirement}, timeout=_HELD_STOP_TIMEOUT)
                    if pending:
                        raise RuntimeError("Held process retirement remains unconfirmed")
                    await asyncio.shield(retirement)
                    stopped += 1
            except Exception as error:
                logger.error("failed stopping held process session=%s error=%s\n%s",
                             session.id, type(error).__name__, backend.format_exception(error))
        if stopped:
            logger.info("released %d held CLI process(es) to stay under the cap", stopped)
        return stopped

    async def reap_held_processes(self) -> int:
        """Drop held CLI processes that have outstayed their welcome.

        Two rules, both from §7: anything idle longer than
        `_HELD_PROCESS_IDLE_SECONDS`, and — if more are still held than
        `_MAX_HELD_PROCESSES` — the least recently used until the count fits.
        Returns how many were stopped. A session whose process is reaped is
        not harmed: its next turn spawns and resumes.
        """
        now = time.monotonic()
        held = [
            s
            for s in self.sessions.values()
            if s._run.backend is not None
            and s._run.held_since is not None
            and not self.session_is_executing(s)
        ]
        doomed = [s for s in held if now - (s._run.held_since or now) >= _HELD_PROCESS_IDLE_SECONDS]
        survivors = [s for s in held if s not in doomed]
        if len(survivors) > _MAX_HELD_PROCESSES:
            survivors.sort(key=lambda s: s._run.held_since or 0.0)
            doomed.extend(survivors[: len(survivors) - _MAX_HELD_PROCESSES])

        stopped = 0
        for session in doomed:
            backend = session._run.backend
            held_since = session._run.held_since
            if backend is None or held_since is None:
                continue
            try:
                retirement = await self._session_owner(session).call_runtime(RetireHeldProcess(backend, held_since))
                if retirement is not None:
                    _, pending = await asyncio.wait({retirement}, timeout=_HELD_STOP_TIMEOUT)
                    if pending:
                        raise RuntimeError("Held process retirement remains unconfirmed")
                    await asyncio.shield(retirement)
                    stopped += 1
            except Exception as error:
                logger.error("failed stopping held process session=%s error=%s\n%s",
                             session.id, type(error).__name__, backend.format_exception(error))
        if stopped:
            logger.info("reaped %d idle CLI process(es)", stopped)
        return stopped

    async def _reaper_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(_REAPER_INTERVAL_SECONDS)
                await self.reap_held_processes()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("held-process reaper iteration failed")

    def start_reaper(self) -> None:
        """Start the idle-process reaper. Idempotent; called from lifespan."""
        if self._reaper_task is None or self._reaper_task.done():
            self._reaper_task = asyncio.create_task(
                self._reaper_loop(), name="held-process-reaper"
            )

    async def stop_all_held_processes(self) -> int:
        """Stop every held CLI process. Called at shutdown.

        Without this a server restart orphans one ~255MB node process per held
        session: nothing else closes their stdin, and they are waiting on it
        rather than on a parent that just died.
        """
        stopped = 0
        for session in list(self.sessions.values()):
            backend = session._run.backend
            # The same sweep budget covers the original writer handoff and
            # resource confirmation. A blocked writer still owns its command
            # after this waiter times out; shutdown must retain the database.
            deadline = asyncio.timeout(_HELD_STOP_TIMEOUT)
            try:
                async with deadline:
                    if backend is not None:
                        await self._retire_backend(session, backend, reason="service_shutdown")
                    await self._await_backend_retirement(session, timeout=_HELD_STOP_TIMEOUT)
            except TimeoutError as error:
                if not deadline.expired():
                    raise
                raise RuntimeError("Held process retirement remains unconfirmed") from error
            if backend is not None:
                stopped += 1
        return stopped

    async def stop_reaper(self) -> None:
        task = self._reaper_task
        self._reaper_task = None
        await drain_cancelled(task, "held-process reaper")


    def _forget_backend(self, session: Session, expected: HarnessRun | None = None, *, reason: str = "process_ended") -> list[dict[str, Any]]:
        """Detach a session from its CLI process and close out what that
        process was still doing.

        Sub-agents live inside the process: when it goes, anything still
        marked running is over, and saying so beats a card that spins for the
        rest of the session (native-subagents.md §7).
        """
        from ..mcp_identity import revoke_run
        if expected is not None and session._run.backend is not expected:
            revoke_run(getattr(expected, "run_lease_id", None))
            return []
        revoke_run(getattr(session._run.backend, "run_lease_id", None))
        session._run.backend = None
        session._run.held_since = None
        frames = []
        for key, run in list(session._subagents.items()):
            if run.status in {"running", "unknown"}:
                update = replace(run, status="interrupted", stop_reason=reason)
                session._subagents[key] = update
                frame = self._event_to_ws_message(session.id, HarnessEvent(type="subagent", subagent=update))
                if frame is not None:
                    frames.append(frame)
        return frames
