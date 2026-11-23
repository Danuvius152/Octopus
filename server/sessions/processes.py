"""The pool of held CLI processes: reuse, the idle reaper, the LRU cap, shutdown.

One slice of `SessionManager`, which was a single 4,017-line class with 84
methods (docs/post-mortems/2026-09-25-polish-pass.md). Split by responsibility; the
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



    def _held_processes(self) -> list[tuple[Session, RetireHeldProcess]]:
        held = []
        for session in self.sessions.values():
            backend = session._run.backend
            held_since = session._run.held_since
            if backend is not None and held_since is not None and not self.session_is_executing(session):
                held.append((session, RetireHeldProcess(backend, held_since)))
        return held

    async def _enforce_held_cap(self, *, keep_session_id: str | None = None) -> int:
        """Retire the oldest excess held processes, protecting the caller's held slot."""
        held = self._held_processes()
        excess = len(held) - _MAX_HELD_PROCESSES
        if excess <= 0:
            return 0
        candidates = [entry for entry in held if entry[0].id != keep_session_id]
        candidates.sort(key=lambda entry: entry[1].held_since)
        stopped = await self._retire_held_processes(candidates[:excess])
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
        doomed, survivors = [], []
        for entry in self._held_processes():
            if now - entry[1].held_since >= _HELD_PROCESS_IDLE_SECONDS:
                doomed.append(entry)
            else:
                survivors.append(entry)
        if len(survivors) > _MAX_HELD_PROCESSES:
            survivors.sort(key=lambda entry: entry[1].held_since)
            doomed.extend(survivors[: len(survivors) - _MAX_HELD_PROCESSES])

        stopped = await self._retire_held_processes(doomed)
        if stopped:
            logger.info("reaped %d idle CLI process(es)", stopped)
        return stopped

    async def _retire_held_processes(self, candidates: list[tuple[Session, RetireHeldProcess]]) -> int:
        stopped = 0
        for session, command in candidates:
            if self.sessions.get(session.id) is not session:
                continue
            try:
                retirement = await self._session_owner(session).call_runtime(command)
                if retirement is None:
                    continue
                _, pending = await asyncio.wait({retirement}, timeout=_HELD_STOP_TIMEOUT)
                if pending:
                    raise RuntimeError("Held process retirement remains unconfirmed")
                await asyncio.shield(retirement)
                stopped += 1
            except Exception as error:
                logger.error("failed stopping held process session=%s error=%s\n%s",
                             session.id, type(error).__name__, command.backend.format_exception(error))
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
            if run.status in {"running", "unknown"} and not run.card_id:
                update = replace(run, status="interrupted", stop_reason=reason)
                session._subagents[key] = update
                frame = self._event_to_ws_message(session.id, HarnessEvent(type="subagent", subagent=update))
                if frame is not None:
                    frames.append(frame)
        return frames
