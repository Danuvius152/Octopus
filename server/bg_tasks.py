"""Cross-turn background tasks.

The model calls `mcp__bg__run(command, description?)` to fire-and-forget
a shell command. The MCP tool POSTs into this manager, which:

  1. Inserts a `bg_tasks` row (status=running)
  2. Spawns an asyncio.Task that runs the subprocess to completion in
     the FastAPI process — independent of any `claude --print` lifetime
  3. Returns the task_id to the model immediately, so the model's turn
     can end while the subprocess keeps running

When the subprocess completes (success / failure / cancel / timeout):

  4. The row is updated with exit_code + captured output
  5. A `bg_completed` WS event is broadcast (so the chip animates)
  6. A synthesized user_message is injected into the session via
     SessionManager so the model gets a turn to react to the result

Why a FastAPI-process worker instead of letting the SDK's Bash
run_in_background handle it: that bg lives inside the per-turn `claude`
subprocess and dies with it. We need a persistent owner.

Output capping: each stream is held at MAX_STREAM_BYTES; once exceeded,
we keep the *most recent* bytes and tag the row `truncated=True`. The
synthesized prompt explains the truncation so the model can ask the
user if it needs the head bytes.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shlex
import signal
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .aio import drain_cancelled, stopped_within
from .database import Database
from .mcp_identity import McpScope, require_live_scope, work_is_open

logger = logging.getLogger(__name__)


# 200 KB per stream is plenty for typical script output without
# blowing up the SQLite row size. Higher cap → bigger row → slower
# WS broadcasts and DB reads. If a user really needs giant output
# they can redirect to a file inside working_dir and `/showme` it.
MAX_STREAM_BYTES = 200 * 1024

# A task always has a positive wall-clock cap, even when it explicitly
# disables idle checks. SIGTERM at the limit, then SIGKILL after a grace.
DEFAULT_TIMEOUT_SECONDS = 30 * 60  # 30 min
# Limits are persisted as SQLite INTEGER values.
MAX_TIMEOUT_SECONDS = 2**63 - 1

# The user's default: stop after output followed by 60s of silence on both
# streams. Legitimately quiet tests/builds must select their own idle limit.
IDLE_AFTER_OUTPUT_TIMEOUT_SECS = 60
# How often the watchdog checks the idle clock. Small enough that
# detection is prompt once the threshold is crossed, large enough to
# avoid wakeup churn on a busy event loop.
IDLE_CHECK_INTERVAL_SECS = 5


# Callback type the manager invokes when a task reaches a terminal state.
# Wired up by main.py lifespan; lets bg_tasks stay decoupled from
# session_manager / WS layer for testability.
DeliveryCallback = Callable[["BgTaskRecord"], Awaitable[None]]
BroadcastCallback = Callable[[dict[str, Any]], Awaitable[None]]


class BgTaskLimits(BaseModel):
    """The effective, immutable limits selected when a task starts."""

    model_config = ConfigDict(frozen=True)
    idle_timeout_seconds: int = Field(
        default=IDLE_AFTER_OUTPUT_TIMEOUT_SECS, strict=True, ge=0, le=MAX_TIMEOUT_SECONDS,
    )
    timeout_seconds: int = Field(
        default=DEFAULT_TIMEOUT_SECONDS, strict=True, gt=0, le=MAX_TIMEOUT_SECONDS,
    )


@dataclass
class BgTaskRecord:
    """Snapshot of a bg_tasks row, as the manager hands it to callbacks.

    Kept as a plain dataclass (not a Pydantic model) because callers
    just read fields — there's no validation surface to defend.
    """

    id: str
    session_id: str
    command: str
    description: str | None
    working_dir: str
    status: str  # 'running' | 'completed' | 'failed' | 'cancelled' | 'interrupted'
    exit_code: int | None
    stdout: str
    stderr: str
    truncated: bool
    started_at: str
    completed_at: str | None
    # NULL for records written before limits and stop causes were recorded.
    idle_timeout_seconds: int | None = None
    timeout_seconds: int | None = None
    stop_reason: str | None = None
    work_id: str | None = None


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def describe_limits(idle: int | None, total: int | None) -> str:
    """The same limits description in the tool receipt and result delivery."""
    idle_text = "unknown" if idle is None else "disabled" if idle == 0 else f"{idle}s"
    total_text = "unknown" if total is None else f"{total}s"
    return f"idle timeout: {idle_text}; total timeout: {total_text}"


def _short_id() -> str:
    """Short hex id (12 chars). Long enough to not collide in a single
    session's task list, short enough to type by hand if needed."""
    return uuid.uuid4().hex[:12]


_TRUNC_MARKER = b"\xe2\x80\xa6[truncated; tail bytes shown]\n"


def _finalize_stream(buf: bytearray, truncated: bool) -> bytes:
    """Prepend a marker if any bytes were dropped during capture.

    Cap-enforcement happens *in the reader* (which trims head bytes as
    they arrive); this is just the cosmetic finalization that tells
    the model "what you see is the tail, not the full output." Keeps
    the on-the-wire byte count predictable — marker + remaining bytes
    is still ≤ MAX_STREAM_BYTES, because we shave the marker length
    off when the buffer is right at the cap.
    """
    if not truncated:
        return bytes(buf)
    # If adding the marker would push us back over the cap, drop the
    # leading marker-length bytes to make room. Worst case the user
    # sees a 30-byte indent instead of 30 bytes of "xxxxx".
    if len(buf) + len(_TRUNC_MARKER) > MAX_STREAM_BYTES:
        drop = len(buf) + len(_TRUNC_MARKER) - MAX_STREAM_BYTES
        if drop < len(buf):
            del buf[:drop]
    return _TRUNC_MARKER + bytes(buf)


class _RunningTask:
    """Per-task state held in memory while the subprocess is alive.

    DB has the persistent copy; this class holds the live process
    handle + the asyncio.Task we await + buffers we're filling.
    """

    def __init__(
        self, record: BgTaskRecord, proc: asyncio.subprocess.Process, limits: BgTaskLimits,
        started_monotonic: float,
    ) -> None:
        self.record = record
        self.proc = proc
        # start_new_session makes the spawn pid its process-group identity.
        # Keep it even if that leader exits before descendants close the pipes.
        self.pgid = proc.pid
        self.limits = limits
        self.started_monotonic = started_monotonic
        self.started_notice = asyncio.Event()
        self.registration_done = asyncio.Event()
        self.registered = False
        self.stop_reason: str | None = None
        self.stdout_buf = bytearray()
        self.stderr_buf = bytearray()
        self.stdout_truncated = False
        self.stderr_truncated = False
        # Set when cancel_task is called; affects terminal status mapping.
        self.cancel_requested: bool = False
        # The asyncio.Task running the orchestration coroutine; held so
        # shutdown() can cancel it.
        self.task: asyncio.Task[None] | None = None


@dataclass
class _StartingTask:
    work_id: str | None
    stop_reason: str | None = None
    running: _RunningTask | None = None


class BgTaskManager:
    """Singleton manager for cross-turn background tasks.

    Lifecycle:
        - `bind(db, deliver_cb, broadcast_cb)` once at startup
        - `start()` to flip orphaned 'running' rows to 'interrupted'
        - `start_task(...)` per model call
        - `cancel_task(...)` from MCP tool or REST UI
        - `shutdown()` on app teardown — cancels all in-flight tasks
    """

    def __init__(self) -> None:
        self._db: Database | None = None
        self._deliver_cb: DeliveryCallback | None = None
        self._broadcast_cb: BroadcastCallback | None = None
        self._running: dict[str, _RunningTask] = {}
        self._starting: dict[str, _StartingTask] = {}
        self._started: bool = False
        self._shutting_down = False
        self._cleanup_tasks: set[asyncio.Task[None]] = set()

    # ------------------------------------------------------------------ wiring

    def bind(
        self,
        db: Database,
        deliver_cb: DeliveryCallback,
        broadcast_cb: BroadcastCallback,
    ) -> None:
        """Wire up dependencies. Called once from main.py lifespan."""
        self._shutting_down = False
        self._db = db
        self._deliver_cb = deliver_cb
        self._broadcast_cb = broadcast_cb

    async def start(self) -> None:
        """Mark orphaned DB rows as interrupted. Called once after bind()."""
        assert self._db is not None, "bind(db, ...) before start()"
        if self._started:
            return
        affected = await self._db.mark_in_flight_bg_tasks_interrupted(_now_iso())
        if affected:
            logger.info(
                "Marked %d bg_tasks as interrupted (left over from prior process)",
                affected,
            )
        self._started = True

    async def shutdown(self) -> None:
        """Graceful teardown: SIGTERM all running subprocesses and await
        their orchestration tasks. The DB-update path in `_run_task`
        will fire `status=interrupted` for each one so chat history is
        consistent."""
        self._shutting_down = True
        starting = list(self._starting.values())
        for item in starting:
            item.stop_reason = item.stop_reason or "server_shutdown"
        for item in starting:
            if item.running is not None:
                await self._terminate_proc(item.running, reason=item.stop_reason)
        for cleanup in list(self._cleanup_tasks):
            await stopped_within(asyncio.shield(cleanup), "cancelled bg startup", timeout=6.0)
        if not self._running:
            return
        logger.info("Stopping %d in-flight bg tasks for shutdown", len(self._running))
        for rt in list(self._running.values()):
            await self._terminate_proc(rt, reason="server_shutdown")
        # Wait for orchestration tasks to wrap up DB writes.
        for rt in list(self._running.values()):
            if rt.task and not rt.task.done():
                await stopped_within(
                    rt.task, f"bg task {rt.record.id} orchestration", timeout=5.0
                )

    # ------------------------------------------------------------------ public API (called by MCP / REST)

    async def start_task(
        self,
        *,
        session_id: str,
        command: str,
        working_dir: str,
        description: str | None = None,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
        idle_timeout_seconds: int = IDLE_AFTER_OUTPUT_TIMEOUT_SECS,
        work_id: str | None = None,
        scope: McpScope | None = None,
    ) -> BgTaskRecord:
        """Create the row, spawn the process, return immediately.

        Returns the BgTaskRecord *as it was at creation*. The
        orchestration task will update the row in the DB later; callers
        that need the latest state should re-fetch via get_task().
        """
        assert self._db is not None, "BgTaskManager not bound"
        if self._shutting_down:
            raise BgTaskError("Background task manager is shutting down")
        try:
            require_live_scope(scope, session_id)
        except ValueError as exc:
            raise BgTaskError(str(exc)) from exc
        if scope is not None:
            work_id = scope.work_id
        if not work_is_open(work_id):
            raise BgTaskError("Delegated work is closed")
        try:
            limits = BgTaskLimits(
                idle_timeout_seconds=idle_timeout_seconds,
                timeout_seconds=timeout_seconds,
            )
        except ValidationError as exc:
            raise BgTaskError(
                "idle_timeout_seconds must be a non-negative integer; "
                "timeout_seconds must be a positive integer"
            ) from exc
        task_id = _short_id()
        started_at = _now_iso()
        started_monotonic = time.monotonic()
        record = BgTaskRecord(
            id=task_id,
            session_id=session_id,
            command=command,
            description=description,
            working_dir=working_dir,
            status="running",
            exit_code=None,
            stdout="",
            stderr="",
            truncated=False,
            started_at=started_at,
            completed_at=None,
            idle_timeout_seconds=limits.idle_timeout_seconds,
            timeout_seconds=limits.timeout_seconds,
            work_id=work_id,
        )
        reservation = _StartingTask(work_id)
        self._starting[task_id] = reservation

        # Spawn through /bin/sh -c so the model can use shell syntax
        # (pipes, redirects, &&) — same trust model as the SDK's Bash
        # tool. working_dir confines blast radius the same way.
        spawn = asyncio.create_task(asyncio.create_subprocess_exec(
                "/bin/sh",
                "-c",
                command,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=working_dir,
                start_new_session=True,  # isolate process group so we can SIGTERM cleanly
            ))
        try:
            proc = await asyncio.shield(spawn)
        except asyncio.CancelledError:
            # The spawn may already have created an OS process before returning
            # its handle. Its own task retains ownership until we can reap it.
            async def reap_aborted_start() -> None:
                try:
                    abandoned = await asyncio.shield(spawn)
                    running = _RunningTask(record, abandoned, limits, started_monotonic)
                    reservation.running = running
                    await self._terminate_proc(running, reason=reservation.stop_reason or "server_shutdown")
                    await stopped_within(abandoned.wait(), f"aborted bg startup {task_id}", timeout=6.0)
                except Exception:
                    logger.exception("Failed to reap aborted bg startup %s", task_id)
                finally:
                    self._starting.pop(task_id, None)
            cleanup = asyncio.create_task(reap_aborted_start(), name=f"bg-startup-cleanup-{task_id}")
            self._cleanup_tasks.add(cleanup)
            cleanup.add_done_callback(self._cleanup_tasks.discard)
            raise
        except Exception as e:
            self._starting.pop(task_id, None)
            # Don't even persist a row — the task never existed. The
            # MCP tool will surface this as an error string the model
            # can read and adjust its plan.
            raise BgTaskError(f"failed to spawn bg task: {e}") from e

        creation = replace(record)
        rt = _RunningTask(record, proc, limits, started_monotonic)
        reservation.running = rt
        self._running[task_id] = rt
        rt.task = asyncio.create_task(self._run_task(rt), name=f"bg-task-{task_id}")
        if reservation.stop_reason or not work_is_open(work_id):
            await self._terminate_proc(rt, reason=reservation.stop_reason or "work_cancelled")
        try:
            await self._db.create_bg_task(
                task_id=task_id, session_id=session_id, command=command,
                description=description, working_dir=working_dir, started_at=started_at,
                idle_timeout_seconds=limits.idle_timeout_seconds,
                timeout_seconds=limits.timeout_seconds, work_id=work_id,
            )
        except BaseException:
            rt.registration_done.set()
            rt.started_notice.set()
            await self._terminate_proc(rt, reason="server_shutdown")
            await stopped_within(asyncio.shield(rt.task), f"unregistered bg task {task_id}", timeout=7.0)
            raise
        finally:
            self._starting.pop(task_id, None)
        rt.registered = True
        rt.registration_done.set()
        # The process is supervised from spawn, independently of registration
        # and listeners. Terminal persistence waits until the row exists.
        try:
            if self._broadcast_cb:
                await self._broadcast_cb({
                    "type": "bg_started", "session_id": session_id,
                    "task_id": task_id, "command": command, "description": description,
                    "started_at": started_at, **limits.model_dump(),
                    "work_id": work_id,
                })
        except Exception:
            logger.exception("bg_started broadcast failed for %s", task_id)
        finally:
            rt.started_notice.set()
        return creation

    async def cancel_task(self, task_id: str) -> bool:
        """Best-effort cancel. Returns True if a live task was signalled,
        False if the task wasn't running or didn't exist in memory."""
        rt = self._running.get(task_id)
        if rt is None:
            return False
        return await self._terminate_proc(rt, reason="user_cancel")

    async def cancel_work(self, work_id: str) -> None:
        """Retire captured processes and startup reservations owned by a round."""
        starting = [item for item in self._starting.values() if item.work_id == work_id]
        running = [item for item in self._running.values() if item.record.work_id == work_id]
        for item in starting:
            item.stop_reason = item.stop_reason or "work_cancelled"
            if item.running is not None:
                running.append(item.running)
        for task in running:
            await self._terminate_proc(task, reason="work_cancelled")

    async def list_tasks(self, session_id: str) -> list[BgTaskRecord]:
        assert self._db is not None
        rows = await self._db.list_bg_tasks_for_session(session_id)
        return [
            BgTaskRecord(
                id=r["id"],
                session_id=r["session_id"],
                command=r["command"],
                description=r["description"],
                working_dir=r["working_dir"],
                status=r["status"],
                exit_code=r["exit_code"],
                stdout=r["stdout"],
                stderr=r["stderr"],
                truncated=r["truncated"],
                started_at=r["started_at"],
                completed_at=r["completed_at"],
                idle_timeout_seconds=r["idle_timeout_seconds"],
                timeout_seconds=r["timeout_seconds"],
                stop_reason=r["stop_reason"],
                work_id=r["work_id"],
            )
            for r in rows
        ]

    async def get_task(self, task_id: str) -> BgTaskRecord | None:
        assert self._db is not None
        r = await self._db.get_bg_task(task_id)
        if r is None:
            return None
        return BgTaskRecord(
            id=r["id"],
            session_id=r["session_id"],
            command=r["command"],
            description=r["description"],
            working_dir=r["working_dir"],
            status=r["status"],
            exit_code=r["exit_code"],
            stdout=r["stdout"],
            stderr=r["stderr"],
            truncated=r["truncated"],
            started_at=r["started_at"],
            completed_at=r["completed_at"],
            idle_timeout_seconds=r["idle_timeout_seconds"],
            timeout_seconds=r["timeout_seconds"],
            stop_reason=r["stop_reason"],
            work_id=r["work_id"],
        )

    # ------------------------------------------------------------------ orchestration

    def _record(self, rt, *, ok: bool, error_code: str | None = None,
                duration_ms: float | None = None) -> None:
        from .monitor import Event, record

        record(Event(
            kind="bg_task",
            session_id=getattr(rt.record, "session_id", None),
            duration_ms=duration_ms,
            ok=ok,
            error_code=error_code,
            detail={"exit_code": getattr(rt.record, "exit_code", None)},
        ))

    async def _run_task(self, rt: _RunningTask) -> None:
        """Read stdout/stderr concurrently, enforce timeout, persist on exit."""
        assert self._db is not None
        proc = rt.proc
        timeout_seconds = rt.limits.timeout_seconds
        idle_timeout_seconds = rt.limits.idle_timeout_seconds

        # Tracks the wall-clock time of the most recent byte we read
        # on either stdio pipe. Read by the idle watchdog below to
        # decide whether the command has gone quiet for too long. We
        # set `first_byte_seen` on the first byte so commands that
        # legitimately produce no output (e.g. `sleep 300`) are not
        # killed by the idle rule.
        last_byte_at = time.monotonic()
        first_byte_seen = False

        # Track truncation on rt directly — the reader fires concurrently,
        # may be cancelled mid-flight, and we'd lose a local return value
        # in those paths. Setting on rt is the load-bearing channel.
        async def reader(stream, buf: bytearray, which: str) -> None:
            nonlocal last_byte_at, first_byte_seen
            while True:
                # Read in chunks so we can keep buf bounded without
                # waiting for EOF. 8 KB is the typical pipe block size.
                chunk = await stream.read(8192)
                if not chunk:
                    return
                buf.extend(chunk)
                last_byte_at = time.monotonic()
                first_byte_seen = True
                if len(buf) > MAX_STREAM_BYTES:
                    # Drop the oldest bytes; keep the tail.
                    drop = len(buf) - MAX_STREAM_BYTES
                    del buf[:drop]
                    if which == "stdout":
                        rt.stdout_truncated = True
                    else:
                        rt.stderr_truncated = True

        readers = [
            asyncio.create_task(reader(proc.stdout, rt.stdout_buf, "stdout")),
            asyncio.create_task(reader(proc.stderr, rt.stderr_buf, "stderr")),
        ]

        # The default remains first output followed by 60s of silence on
        # BOTH streams. A caller can explicitly select a longer interval or
        # disable this check for a task with legitimate quiet phases.
        async def idle_watchdog() -> None:
            tick = min(IDLE_CHECK_INTERVAL_SECS, max(0.05, idle_timeout_seconds / 4))
            while True:
                await asyncio.sleep(tick)
                if not first_byte_seen:
                    # The command may legitimately be quiet (e.g.
                    # `sleep 60` with no output). Don't apply the
                    # idle rule until we've seen at least one byte.
                    continue
                idle_for = time.monotonic() - last_byte_at
                if idle_for > idle_timeout_seconds:
                    logger.warning(
                        "bg task %s: idle watchdog tripped — proc silent "
                        "for %.1fs after producing output (>%.0fs threshold), "
                        "SIGTERMing pgid",
                        rt.record.id, idle_for, idle_timeout_seconds,
                    )
                    await self._terminate_proc(rt, reason="idle_timeout")
                    return

        idle_task = asyncio.create_task(
            idle_watchdog(), name=f"bg-{rt.record.id}-idle-watchdog"
        ) if idle_timeout_seconds > 0 else None

        timed_out = False
        async def process_and_output() -> None:
            await proc.wait()
            await asyncio.gather(*readers)

        completion = asyncio.create_task(process_and_output(), name=f"bg-{rt.record.id}-completion")
        try:
            try:
                remaining = max(0.0, timeout_seconds - (time.monotonic() - rt.started_monotonic))
                await asyncio.wait_for(asyncio.shield(completion), timeout=remaining)
            except TimeoutError:
                timed_out = True
                logger.warning(
                    "bg task %s timed out after %ds", rt.record.id, timeout_seconds
                )
                await self._terminate_proc(rt, reason="total_timeout")
                try:
                    await asyncio.wait_for(asyncio.shield(completion), timeout=6.0)
                except TimeoutError:
                    pass

        except asyncio.CancelledError:
            # Shutdown path. Make sure proc is gone.
            await self._terminate_proc(rt, reason="server_shutdown")
            try:
                await asyncio.wait_for(proc.wait(), timeout=2.0)
            except TimeoutError:
                pass
            raise

        finally:
            # Stop the idle watchdog regardless of how we got here.
            # If proc already exited it's a no-op; if we're cleaning up
            # after the watchdog itself triggered, this cancels the
            # already-returned task (no-op).
            await drain_cancelled(idle_task, f"bg task {rt.record.id} idle watchdog")
            await drain_cancelled(completion, f"bg task {rt.record.id} completion")
            for reader_task in readers:
                await drain_cancelled(reader_task, f"bg task {rt.record.id} reader")

            # The reader set {stdout,stderr}_truncated when it had to
            # drop head bytes — finalize stamps a visible marker on
            # those streams so the model knows it's seeing the tail.
            stdout_bytes = _finalize_stream(rt.stdout_buf, rt.stdout_truncated)
            stderr_bytes = _finalize_stream(rt.stderr_buf, rt.stderr_truncated)

            exit_code = proc.returncode
            # Negative returncode == process was killed by signal
            # (|returncode| is the signal number). Tells us whether
            # we should distinguish "interrupted by an outside force"
            # from "the command itself ran and exited non-zero", since
            # those are very different user-facing things.
            killed_by_signal = exit_code is not None and exit_code < 0
            if rt.stop_reason is None and killed_by_signal:
                rt.stop_reason = "external_signal"
            if rt.stop_reason in {"user_cancel", "work_cancelled"}:
                status = "cancelled"
            elif rt.stop_reason in {"idle_timeout", "total_timeout"}:
                status = "failed"
            elif rt.stop_reason in {"server_shutdown", "external_signal"}:
                status = "interrupted"
            elif exit_code == 0:
                status = "completed"
            else:
                status = "failed"
                rt.stop_reason = "exit_nonzero"
            # Persistent breadcrumb — next time this fires, the log
            # tells us exactly which branch we hit and why.
            logger.info(
                "bg task %s terminal state: status=%s exit_code=%s "
                "cancel_requested=%s timed_out=%s killed_by_signal=%s stop_reason=%s "
                "stdout_bytes=%d stderr_bytes=%d",
                rt.record.id, status, exit_code,
                rt.cancel_requested, timed_out, killed_by_signal, rt.stop_reason,
                len(stdout_bytes), len(stderr_bytes),
            )

            stdout_str = stdout_bytes.decode("utf-8", errors="replace")
            stderr_str = stderr_bytes.decode("utf-8", errors="replace")
            completed_at = _now_iso()
            truncated = rt.stdout_truncated or rt.stderr_truncated

            try:
                await rt.registration_done.wait()
                if rt.registered:
                    await self._publish_terminal(
                        rt, status=status, exit_code=exit_code, stdout_str=stdout_str,
                        stderr_str=stderr_str, truncated=truncated, completed_at=completed_at,
                    )
            finally:
                self._running.pop(rt.record.id, None)

    async def _publish_terminal(
        self, rt: _RunningTask, *, status: str, exit_code: int | None,
        stdout_str: str, stderr_str: str, truncated: bool, completed_at: str,
    ) -> None:
        assert self._db is not None
        await self._db.update_bg_task(
            rt.record.id,
            status=status,
            exit_code=exit_code,
            stdout=stdout_str,
            stderr=stderr_str,
            truncated=truncated,
            completed_at=completed_at,
            stop_reason=rt.stop_reason,
        )

        rt.record.status = status
        rt.record.exit_code = exit_code
        rt.record.stdout = stdout_str
        rt.record.stderr = stderr_str
        rt.record.truncated = truncated
        rt.record.completed_at = completed_at
        rt.record.stop_reason = rt.stop_reason

        # Preserve started/completed event order while allowing the
        # process and its watchdogs to run independently of notification.
        await rt.started_notice.wait()
        self._running.pop(rt.record.id, None)

        if self._broadcast_cb:
            try:
                await self._broadcast_cb(
                    {
                        "type": "bg_completed",
                        "session_id": rt.record.session_id,
                        "task_id": rt.record.id,
                        "status": status,
                        "exit_code": exit_code,
                        "truncated": truncated,
                        "completed_at": completed_at,
                        "idle_timeout_seconds": rt.record.idle_timeout_seconds,
                        "timeout_seconds": rt.record.timeout_seconds,
                        "stop_reason": rt.record.stop_reason,
                    "work_id": rt.record.work_id,
                    }
                )
            except Exception:
                logger.exception("bg_completed broadcast failed")

        if self._deliver_cb:
            try:
                await self._deliver_cb(rt.record)
            except Exception:
                logger.exception("bg delivery callback failed for %s", rt.record.id)

    async def _terminate_proc(self, rt: _RunningTask, *, reason: str) -> bool:
        """SIGTERM the whole process group; 5s grace; SIGKILL if needed.

        We spawned with start_new_session=True, so killing the group
        sweeps up any children the model's shell command forked off
        (e.g. `bash -c "long_running &"`).
        """
        if rt.stop_reason is not None:
            return False
        pgid = rt.pgid
        try:
            # The first stopping action owns the cause. A later cancel or
            # deadline cannot relabel a task already being terminated.
            # Audit trail: which path called the SIGTERM, and on what
            # task. Pairs with the terminal-state log line so the
            # forensics for an "exit -15" task are local to this file.
            logger.info(
                "bg task %s: SIGTERM pgid=%s stop_reason=%s",
                rt.record.id, pgid, reason,
            )
            os.killpg(pgid, signal.SIGTERM)
            rt.stop_reason = reason
            rt.cancel_requested = reason == "user_cancel"
        except ProcessLookupError:
            if reason in {"idle_timeout", "total_timeout"}:
                rt.stop_reason = reason
            return False
        except PermissionError:
            logger.exception("bg task %s: signal denied for pgid=%s", rt.record.id, pgid)
            if reason in {"idle_timeout", "total_timeout"}:
                rt.stop_reason = reason
            return False
        # Don't await here — let the wait_for in _run_task notice the exit.
        # Schedule a SIGKILL fallback if the process resists SIGTERM.
        async def _kill_later() -> None:
            await asyncio.sleep(5.0)
            # The shell may have exited while a descendant ignores TERM and
            # still holds the output pipes. Reap the captured GROUP, not the
            # shell's current pid/returncode (which no longer identifies it).
            try:
                os.killpg(pgid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except PermissionError:
                logger.exception("bg task %s: SIGKILL denied for pgid=%s", rt.record.id, pgid)
        cleanup = asyncio.create_task(_kill_later(), name=f"bg-kill-{rt.record.id}")
        self._cleanup_tasks.add(cleanup)
        cleanup.add_done_callback(self._cleanup_tasks.discard)
        return True


class BgTaskError(Exception):
    """Raised by manager methods on user-fixable problems (bad working_dir,
    spawn failure). The MCP tool turns this into a text error the model
    can read and adjust."""


def render_delivery_prompt(rec: BgTaskRecord) -> str:
    """Synthesize the user-message text we inject into the session when
    a bg task completes. Format is chosen to be unambiguous to the
    model: explicit status line, clear sections, marked truncation.

    The model is taught (via the system prompt addendum in
    claude_code.py) that messages prefixed with "[bg-task-result]"
    are auto-injected, not user-typed."""
    lines: list[str] = []
    desc = f" ({rec.description})" if rec.description else ""
    lines.append(
        f"[bg-task-result] Background task `{rec.id}`{desc} finished with "
        f"status `{rec.status}` (exit code {rec.exit_code})."
    )
    lines.append(f"Limits: {describe_limits(rec.idle_timeout_seconds, rec.timeout_seconds)}.")
    if rec.stop_reason is not None:
        lines.append(f"Stop reason: {rec.stop_reason}.")
    # Show the literal command so the model has full context even if
    # the original tool_use is far up the chat history.
    lines.append("")
    lines.append("Command:")
    lines.append(f"  {shlex.quote(rec.command) if ' ' in rec.command else rec.command}")
    if rec.truncated:
        lines.append("")
        lines.append("Note: output was truncated (head bytes dropped, tail preserved).")
    if rec.stdout:
        lines.append("")
        lines.append("stdout:")
        lines.append("```")
        lines.append(rec.stdout.rstrip())
        lines.append("```")
    if rec.stderr:
        lines.append("")
        lines.append("stderr:")
        lines.append("```")
        lines.append(rec.stderr.rstrip())
        lines.append("```")
    if not rec.stdout and not rec.stderr:
        lines.append("")
        lines.append("(no output)")
    lines.append("")
    lines.append(
        "Respond to the user with what you learned from this result. "
        "Don't re-quote the entire output — summarize. If this was an "
        "intermediate step toward a larger goal, continue with the next "
        "step."
    )
    return "\n".join(lines)


# Singleton
bg_task_manager = BgTaskManager()
