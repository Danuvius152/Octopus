"""`HarnessRun` — the single subprocess + JSONL streaming engine.

One concrete class for both harnesses (no per-framework subclasses). It
owns the subprocess lifecycle, stdout/stderr readers, the normalized
event queue, and graceful shutdown — exactly the machinery that used to
live in `SubprocessJsonlBackend`. The two things that differ per harness
come from the `RuntimeProfile` it's constructed with: how to build argv
(`profile.build_turn_argv`) and how to normalize a stdout line
(`profile.new_event_parser()`), plus whether to close stdin after spawn.
"""

from __future__ import annotations

import asyncio
import glob
import hashlib
import json
import logging
import os
import shutil
import signal
import uuid as uuid_module
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..aio import drain_cancelled
from . import assembly
from .events import HarnessCredential, HarnessEvent
from .profile import ActivityObserver, RuntimeProfile, StdinMode, TurnContext

logger = logging.getLogger(__name__)

# Sentinel pushed onto the event queue to signal EOF on the stdout reader.
_STREAM_END = object()

# Per-line buffer cap for the asyncio StreamReader wrapping the CLI's
# stdout. asyncio's 64 KiB default is easily exceeded by a single
# stream-json event (e.g. a tool_result carrying a big Read output);
# overrun raises LimitOverrunError and crashes the reader. 4 MiB lets
# anything short of pathological emit cleanly; pipe backpressure keeps
# memory bounded.
_STDOUT_LINE_LIMIT_BYTES = 4 * 1024 * 1024


def _fallback_path_dirs() -> list[str]:
    """Per-user install dirs a systemd-style service PATH typically strips:
    ~/.local/bin, npm-global, Homebrew, and every nvm node version's bin."""
    home = os.path.expanduser("~")
    extras = [
        os.path.join(home, ".local/bin"),
        os.path.join(home, ".npm-global/bin"),
        "/usr/local/bin",
        "/opt/homebrew/bin",
    ]
    extras += sorted(glob.glob(os.path.join(home, ".nvm/versions/node/*/bin")))
    return extras


def _which_with_fallback(binary: str) -> str | None:
    """shutil.which, then retry against PATH + common per-user install dirs.

    systemd's default PATH excludes ~/.local/bin and node/npm global bins,
    so a CLI installed for the invoking user is invisible to the service
    unless we add those dirs ourselves."""
    found = shutil.which(binary)
    if found is not None:
        return found
    extra_path = os.pathsep.join(_fallback_path_dirs())
    full_path = os.pathsep.join(p for p in (os.environ.get("PATH", ""), extra_path) if p)
    return shutil.which(binary, path=full_path)


def augmented_path(base: str | None = None, extra_dir: str | None = None) -> str:
    """A PATH that includes the per-user install dirs (and optionally the
    resolved CLI's own dir) ahead of the base PATH. Critical for node-based
    CLIs: `claude`/`codex` are `#!/usr/bin/env node` scripts, so the child
    must find `node` at exec time; the service PATH usually omits the nvm
    bin where node lives (else: exit 127)."""
    if base is None:
        base = os.environ.get("PATH", "")
    dirs = ([extra_dir] if extra_dir else []) + _fallback_path_dirs()
    return os.pathsep.join([d for d in dirs if d] + ([base] if base else []))


def prepare_spawn(
    argv: list[str], kwargs: dict[str, Any]
) -> tuple[list[str], dict[str, Any]]:
    """Resolve a bare binary name to an absolute path (PATH + per-user
    fallback dirs) and augment the child's PATH so its node shebang resolves.
    Shared by the streaming engine and `Harness.run_oneshot`."""
    if argv and not os.path.isabs(argv[0]):
        resolved = _which_with_fallback(argv[0])
        if resolved is None:
            raise FileNotFoundError(
                f"{argv[0]} not found on PATH — install the CLI first"
            )
        argv = [resolved, *argv[1:]]
    # The working directory must exist, or `create_subprocess_exec` raises
    # FileNotFoundError on the *cwd* — a failure that reads like "the CLI is
    # missing" but is not. An account is provisioned with its workspace when it
    # is created (users._insert), so in normal operation it already exists; this
    # is the safety net for every other way it could be absent (a dir removed
    # under a live session, an older account created before provisioning), so a
    # turn never dies on a missing directory. Confinement has already vetted the
    # path by the time it reaches here.
    cwd = kwargs.get("cwd")
    if cwd:
        os.makedirs(cwd, exist_ok=True)
    env = kwargs.get("env") or os.environ.copy()
    cli_dir = os.path.dirname(argv[0]) if argv and os.path.isabs(argv[0]) else None
    env["PATH"] = augmented_path(env.get("PATH"), cli_dir)
    # Own process group (session leader) so the whole tree the CLI spawns —
    # MCP servers, nested subagents (Claude `Task`/Workflow) — is reapable as a
    # unit via killpg, instead of orphaning on stop()/interrupt()
    # (turn-safety.md §2). Shared by the streaming engine and run_oneshot.
    # bg_tasks / codex_login already do this.
    return argv, {**kwargs, "env": env, "start_new_session": True}


def _terminate_process_group(proc: asyncio.subprocess.Process, sig: int) -> bool:
    """Signal the whole process group led by `proc` (so nested CLI children die
    with it), falling back to the direct child if the group can't be resolved.
    Returns True if a group signal was sent. Idempotent / best-effort —
    swallows the races where the process already exited (turn-safety.md §2)."""
    if proc.returncode is not None:
        return False
    try:
        pgid = os.getpgid(proc.pid)
    except (ProcessLookupError, PermissionError):
        pgid = None
    if pgid is not None:
        try:
            os.killpg(pgid, sig)
            return True
        except (ProcessLookupError, PermissionError):
            return False
    try:
        proc.send_signal(sig)
    except (ProcessLookupError, PermissionError):
        pass
    return False


def parse_json_line(line: str) -> dict[str, Any] | None:
    """Parse a JSONL line, returning None on parse error (logs a warning)."""
    try:
        obj = json.loads(line)
    except json.JSONDecodeError as e:
        logger.warning("Skipping unparseable harness line: %s — %s", line[:200], e)
        return None
    if not isinstance(obj, dict):
        logger.warning("Unexpected non-object harness line: %s", line[:200])
        return None
    return obj


@dataclass
class RunConfig:
    """Agent-derived per-run configuration (resolved fresh each turn by
    session_manager). Distinct from the start() args (prompt/working_dir/
    resume_id/credential), which arrive per invocation."""

    session_id: str | None = None
    # Whose turn this is (multi-tenancy.md §7). Travels into the MCP bearer, so
    # a tool call carries an owner without a round trip and the signature
    # covers it.
    user_id: str | None = None
    wiki_task_id: str | None = None
    wiki_mode: str | None = None
    work_id: str | None = None
    system_prompt: str | None = None   # agent persona
    model: str | None = None
    reasoning_effort: str | None = None
    service_tier: str | None = None
    mcp_servers: list[str] | None = None
    tool_allow: list[str] | None = None
    tool_deny: list[str] | None = None
    connectors: list[tuple[Any, Any]] = field(default_factory=list)
    # Per-agent native memory (docs/plans/memory.md). None when there's no
    # owning agent (legacy/tests) → memory wiring is fully inert.
    memory_dir: str | None = None
    # Fork first-turn context note (session-rewind.md §5.6.4): framing
    # appended to the system addendum on a fork's first turn only. None
    # otherwise. NOT the replay transcript (that lives in the user channel).
    fork_note: str | None = None
    # Native-deep-research web leaf (native-deep-research.md §4): render a
    # scoped, web-enabled, read-only-ish turn (no destructive/fan-out tools).
    web_research: bool = False
    # Sub-agents the owning agent brings with it (native-subagents.md §6).
    # Rendered by the profile if it has a surface for them; ignored otherwise,
    # so an agent that defines some still works on a harness that can't take
    # them (the CLI's built-in sub-agents remain available either way).
    subagents: list[dict[str, Any]] = field(default_factory=list)


class HarnessRun:
    """One streamed turn. Lifecycle: `start()`, iterate `stream()` until a
    terminal event closes it, then `stop()`. `interrupt()` cancels in-flight."""

    def __init__(self, profile: RuntimeProfile, config: RunConfig | None = None) -> None:
        self._profile = profile
        self._tool_identity_tracker: Any | None = None
        self._tool_context: tuple[str, str] | None = None
        self._config = config or RunConfig()
        self.run_lease_id = uuid_module.uuid4().hex
        self.work_id = self._config.work_id
        self._lease_closed = False
        self._spawn_task: asyncio.Task[asyncio.subprocess.Process] | None = None
        self._spawn_cleanup: asyncio.Task[None] | None = None
        self._preparation_task: asyncio.Task[None] | None = None
        self._stop_task: asyncio.Task[None] | None = None
        self._activity_observer: ActivityObserver | None = None
        self._parser = profile.new_event_parser()
        self._process: asyncio.subprocess.Process | None = None
        self._event_queue: asyncio.Queue[HarnessEvent | object] = asyncio.Queue()
        self._stdout_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._stderr_lines: list[str] = []
        self._stream_closed: bool = False
        self._activity_generation = 0
        self._activity_callback: Callable[[float | None], None] | None = None
        # The uuid of this run's opening prompt frame, remembered so S3 can
        # tell the CLI's echo of our own prompt from a genuine user message.
        #
        # Every frame uuid MUST be unique: the CLI reports it back as
        # `command_uuid` on `command_lifecycle` and deduplicates on it, so
        # reusing one silently drops the command and the turn hangs waiting
        # for a reply that will never come. A derived id (session + turn
        # counter) is exactly the kind of thing that repeats — this is
        # random per frame, and we simply remember what we sent.
        self._initial_uuid: str | None = None
        # Events that arrive with no turn in flight (native-subagents.md §7):
        # an asynchronous sub-agent finishing, the follow-up turn the CLI
        # wakes to report it, a native cron tick. Without a handler they are
        # dropped, which is what the CLI's own "Async agent launched
        # successfully" path used to look like from the UI: a card that never
        # finished and an answer that never arrived.
        self._idle_handler: Callable[[HarnessEvent], Awaitable[None]] | None = None

    @property
    def profile(self) -> RuntimeProfile:
        return self._profile

    # ------------------------------------------------------------------ lifecycle

    def _make_context(
        self,
        prompt: str,
        working_dir: str,
        resume_id: str | None,
        credential: HarnessCredential | None,
    ) -> TurnContext:
        """Run the shared assembly (MCP selection, system-prompt composition,
        working-dir absolutization) into a neutral TurnContext. Side-effect
        free, so both `build_argv` (argv inspection) and `start()` use it."""
        # Resolve working_dir to ABSOLUTE before handing it to the CLI: MCP
        # grandchildren inherit cwd, so a relative path would be double-resolved.
        abs_wd = str(Path(working_dir).resolve())
        callback_env = assembly.build_callback_env(self._config.session_id)
        mcp_servers = assembly.select_mcp_servers(
            self._config.mcp_servers,
            self._config.connectors,
            callback_env,
            session_id=self._config.session_id,
            user_id=self._config.user_id,
            wiki_task_id=self._config.wiki_task_id,
            run_lease_id=self.run_lease_id,
            work_id=self.work_id,
        )
        system_prompt = assembly.compose_system_prompt(
            self._config.system_prompt,
            self._profile.tools_prompt,
            self._config.connectors,
            memory_dir=self._config.memory_dir,
            inject_memory=self._profile.injects_memory_prompt,
            fork_note=self._config.fork_note,
            wiki_mode=self._config.wiki_mode,
            wiki_capability=True,
            wiki_task_id=self._config.wiki_task_id,
        )
        return TurnContext(
            prompt=prompt,
            working_dir=abs_wd,
            resume_id=resume_id,
            system_prompt=system_prompt,
            model=self._config.model,
            reasoning_effort=self._config.reasoning_effort,
            service_tier=self._config.service_tier,
            tool_allow=self._config.tool_allow,
            tool_deny=self._config.tool_deny,
            mcp_servers=mcp_servers,
            credential=credential,
            memory_dir=self._config.memory_dir,
            web_research=self._config.web_research,
            subagents=self._config.subagents,
        )

    def build_argv(
        self,
        prompt: str,
        working_dir: str,
        resume_id: str | None = None,
        credential: HarnessCredential | None = None,
    ) -> tuple[list[str], dict[str, Any]]:
        """The pre-spawn half of a turn: assemble the context and let the
        profile render the argv. Returns `(argv, kwargs)` without spawning and
        without FS side effects — `start()` calls this then spawns; tests call
        it to inspect the command/env a turn would use."""
        ctx = self._make_context(prompt, working_dir, resume_id, credential)
        return self._profile.build_turn_argv(ctx)

    async def start(
        self,
        prompt: str,
        working_dir: str,
        resume_id: str | None = None,
        credential: HarnessCredential | None = None,
    ) -> None:
        if self._process is not None:
            raise RuntimeError("HarnessRun already started")
        if self._lease_closed:
            raise RuntimeError("HarnessRun has been retired")

        from ..mcp_identity import work_is_open
        if not work_is_open(self.work_id):
            raise RuntimeError("Delegated work is closed")

        ctx = self._make_context(prompt, working_dir, resume_id, credential)
        argv, kwargs = self._profile.build_turn_argv(ctx)
        argv, kwargs = prepare_spawn(argv, kwargs)
        if ctx.resume_id and self._profile.prepare_resume is not None:
            preparation = asyncio.create_task(self._profile.prepare_resume(ctx, argv, kwargs), name="harness-controller-update")
            self._preparation_task = preparation
            try:
                await preparation
            except BaseException:
                self._begin_retirement()
                raise
            finally:
                if preparation.done():
                    self._preparation_task = None
        if self._lease_closed or not work_is_open(self.work_id):
            raise asyncio.CancelledError
        if self._profile.activity_observer is not None:
            generation = self._activity_generation
            self._activity_observer = self._profile.activity_observer(
                kwargs.get("env") or os.environ,
                lambda timestamp: self._activity(timestamp, generation=generation),
                resume_id=resume_id,
            )
            attach = getattr(self._activity_observer, "set_event_handler", None)
            if attach is not None:
                attach(lambda events: self._publish_observations(events, generation=generation))
            if self._activity_callback is not None or self._tool_context is not None:
                try:
                    await self._activity_observer.start()
                except BaseException:
                    self._begin_retirement()
                    raise
        if self._lease_closed:
            raise asyncio.CancelledError

        from ..mcp_identity import register_run
        if self._config.session_id:
            register_run(self.run_lease_id, self._config.session_id, self._config.user_id, self.work_id)

        logger.info("Spawning harness %s: %s (cwd=%s)", self._profile.backend, argv, kwargs.get("cwd"))
        spawn = asyncio.create_task(asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=_STDOUT_LINE_LIMIT_BYTES,
            **kwargs,
        ))
        self._spawn_task = spawn
        try:
            process = await asyncio.shield(spawn)
        except asyncio.CancelledError:
            # A transport can have spawned the process before returning its
            # handle. Keep that reservation alive until the captured group is
            # reaped; cancellation must not lose the process reference.
            self._lease_closed = True
            from ..mcp_identity import revoke_run
            revoke_run(self.run_lease_id)

            async def reap_aborted_spawn() -> None:
                try:
                    abandoned = await spawn
                    _terminate_process_group(abandoned, signal.SIGKILL)
                    await asyncio.wait_for(abandoned.wait(), timeout=4.0)
                except Exception:
                    logger.exception("Failed to reap aborted harness startup")
                    raise
                finally:
                    self._spawn_task = None
            self._spawn_cleanup = asyncio.create_task(reap_aborted_spawn(), name="harness-startup-cleanup")
            self._begin_retirement()
            raise
        except Exception:
            self._begin_retirement()
            raise
        finally:
            if spawn.done():
                self._spawn_task = None
        if self._lease_closed or not work_is_open(self.work_id):
            self._process = process
            _terminate_process_group(process, signal.SIGKILL)
            await asyncio.wait_for(process.wait(), timeout=4.0)
            raise asyncio.CancelledError
        self._process = process
        self._stdout_task = asyncio.create_task(
            self._read_stdout(), name=f"{self._profile.backend}-stdout"
        )
        self._stderr_task = asyncio.create_task(
            self._read_stderr(), name=f"{self._profile.backend}-stderr"
        )

        # CLOSE_AFTER_SPAWN: the prompt is in argv and the CLI must not wait
        # on stdin. Codex reads stdin even with a positional prompt and blocks
        # forever waiting on EOF, so the close is what lets the turn proceed.
        # STREAM_JSON leaves the pipe open — the prompt is written to it.
        if (
            self._profile.stdin_mode is StdinMode.CLOSE_AFTER_SPAWN
            and self._process.stdin is not None
        ):
            try:
                self._process.stdin.close()
            except Exception:
                logger.debug("closing stdin failed", exc_info=True)
        elif self._profile.stdin_mode is StdinMode.STREAM_JSON:
            # Write the prompt immediately. The pipe must never sit open and
            # idle: that is what made the CLI stall ~3s per turn waiting for
            # input that wasn't coming (inline-steering.md §10).
            self._initial_uuid = await self.send_user_frame(prompt)

    async def send_user_frame(self, text: str, *, frame_uuid: str | None = None) -> str:
        """Write one user message to the CLI's stdin as a JSON line.

        This is the whole input channel under `STREAM_JSON`: the opening
        prompt goes through it, and (S3) so does anything the user says while
        the turn is running. Returns the frame's uuid so the caller can match
        the CLI's echo of it.

        A failed write is terminal for the turn — if we can't reach the CLI's
        stdin the prompt simply hasn't been delivered, and pretending otherwise
        would hang the turn waiting for a reply to a question never asked.
        """
        if self._profile.stdin_mode is not StdinMode.STREAM_JSON:
            raise RuntimeError(
                f"{self._profile.backend} does not take input on stdin"
            )
        proc = self._process
        if proc is None or proc.stdin is None or proc.stdin.is_closing():
            raise RuntimeError("CLI stdin is not open")
        frame_uuid = frame_uuid or str(uuid_module.uuid4())
        line = json.dumps(
            {
                "type": "user",
                "uuid": frame_uuid,
                "parent_tool_use_id": None,
                "message": {"role": "user", "content": text},
            }
        ) + "\n"
        proc.stdin.write(line.encode())
        await proc.stdin.drain()
        return frame_uuid

    def spawn_signature(
        self, working_dir: str, credential: HarnessCredential | None
    ) -> str:
        """Identity of the *process* a turn would need.

        Everything a `STREAM_JSON` process bakes in at spawn — system prompt,
        model, tool policy, MCP set, memory dir, credential, working dir —
        lives in argv or env and cannot be changed afterwards. A held process
        may therefore only be reused for a turn whose signature matches;
        edit the agent's persona or swap its credential and the next turn
        must respawn, or it would silently run under the old configuration.

        `resume_id` is deliberately absent: a live process already holds the
        conversation, and that's what makes reuse worth having.
        """
        parts = [
            self._profile.backend,
            self.work_id or "",
            self._config.wiki_task_id or "",
            self._config.wiki_mode or "",
            self._config.system_prompt or "",
            self._config.model or "",
            self._config.reasoning_effort or "",
            self._config.service_tier or "default",
            ",".join(sorted(self._config.mcp_servers or [])),
            # WHERE those namespaces are served, and with what credential. Since
            # they moved in-process (polish-2026-09.md §4 B1) each entry's URL
            # carries the server's port and its bearer is derived from the
            # session and the access token — all of it baked into argv at spawn,
            # and all of it derived from this env. A held process reused after
            # the port moved is pointed at a socket that is not there: it
            # reports every namespace as unreachable and the model loses its
            # tools mid-conversation, with nothing in our logs to say why.
            # (Hashed like everything else here, so the token is not held in
            # readable form.)
            ",".join(
                f"{k}={v}"
                for k, v in sorted(
                    assembly.build_callback_env(self._config.session_id).items()
                )
            ),
            ",".join(sorted(self._config.tool_allow or [])),
            ",".join(sorted(self._config.tool_deny or [])),
            self._config.memory_dir or "",
            self._config.fork_note or "",
            str(self._config.web_research),
            # (kind, installation id) — content, not object identity. A
            # connector object's default repr carries its memory address, which
            # changes between turns and would make every signature differ, so
            # reuse would silently never happen for an agent with connectors.
            ",".join(
                sorted(
                    f"{getattr(conn, 'kind', '?')}:{getattr(inst, 'id', '?')}"
                    for conn, inst in self._config.connectors
                )
            ),
            str(Path(working_dir).resolve()),
            (credential.auth_type + ":" + (credential.secret or "")) if credential else "",
        ]
        return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()

    def is_alive(self) -> bool:
        """Whether the subprocess is up and could serve another turn."""
        return self._process is not None and self._process.returncode is None

    @property
    def reusable(self) -> bool:
        """Whether this backend can serve more than one turn per process.

        Only `STREAM_JSON` can: a backend whose prompt lives in argv is, by
        construction, one process per turn.
        """
        return self._profile.stdin_mode is StdinMode.STREAM_JSON

    async def send_turn(self, prompt: str) -> None:
        """Run another turn on the process already up.

        This is the whole point of keeping it: spawning the CLI costs ~1.5s
        that a live process doesn't pay, and its prompt cache stays warm
        (inline-steering.md §3). The per-turn stream state is reset so
        `stream()` can be iterated again; the process, its parser (which holds
        the engine's session id) and its MCP children all carry over.
        """
        if not self.reusable:
            raise RuntimeError(f"{self._profile.backend} cannot reuse a process")
        if not self.is_alive():
            raise RuntimeError("CLI process is not running")
        # A fresh queue rather than draining the old one: anything still in it
        # belongs to the turn that just ended, and replaying that into the new
        # turn would duplicate messages.
        self._event_queue = asyncio.Queue()
        self._stream_closed = False
        self._initial_uuid = await self.send_user_frame(prompt)

    async def stream(self) -> AsyncIterator[HarnessEvent]:
        while True:
            item = await self._event_queue.get()
            if item is _STREAM_END:
                return
            assert isinstance(item, HarnessEvent)
            yield item

    def _begin_retirement(self) -> asyncio.Task[None]:
        self._lease_closed = True
        self._tool_context = None
        self.invalidate_activity()
        from ..mcp_identity import revoke_run
        revoke_run(self.run_lease_id)
        if self._stop_task is None:
            self._stop_task = asyncio.create_task(self._stop(), name="harness-retirement")
        return self._stop_task

    async def stop(self) -> None:
        """Owned retirement includes a late spawn; caller cancellation cannot orphan it."""
        await asyncio.shield(self._begin_retirement())

    async def _stop(self) -> None:
        if self._preparation_task is not None:
            preparation = self._preparation_task
            if not preparation.done():
                preparation.cancel()
            try:
                await asyncio.shield(preparation)
            except asyncio.CancelledError:
                pass  # the preparation owns and confirms its subprocess cleanup
            except Exception:
                logger.debug("Controller preparation failed while stopping", exc_info=True)
            self._preparation_task = None
        if self._spawn_cleanup is not None:
            await self._spawn_cleanup
            self._spawn_cleanup = None
        elif self._spawn_task is not None:
            try:
                pending = await asyncio.shield(self._spawn_task)
            except Exception:
                pending = None
            if pending is not None and self._process is None:
                _terminate_process_group(pending, signal.SIGKILL)
                await pending.wait()
        proc = self._process
        if proc is None:
            self._close_stream()
            if self._activity_observer is not None:
                await self._activity_observer.close()
            return

        # Closing stdin lets the CLI exit gracefully (flush its result first).
        if proc.stdin and not proc.stdin.is_closing():
            try:
                proc.stdin.close()
            except (OSError, RuntimeError, ValueError) as e:
                # A transport that is already gone, or an event loop that has
                # closed under us during interpreter shutdown. Neither is worth
                # failing a teardown over, but neither is worth hiding either.
                logger.debug("closing stdin failed: %s", e)

        if proc.returncode is None:
            try:
                await asyncio.wait_for(proc.wait(), timeout=2.0)
            except TimeoutError:
                # Escalate to the whole process GROUP so nested CLI children
                # (MCP servers, subagents) die too, not just the direct child
                # (turn-safety.md §2).
                logger.warning("CLI didn't exit on stdin close, terminating group")
                _terminate_process_group(proc, signal.SIGTERM)
                try:
                    await asyncio.wait_for(proc.wait(), timeout=2.0)
                except TimeoutError:
                    logger.warning("CLI didn't exit on SIGTERM, killing group")
                    _terminate_process_group(proc, signal.SIGKILL)
                    await proc.wait()

        for task, name in (
            (self._stdout_task, "stdout reader"),
            (self._stderr_task, "stderr reader"),
        ):
            await drain_cancelled(task, name)

        if not self._stream_closed:
            self._stream_closed = True
            try:
                self._event_queue.put_nowait(_STREAM_END)
            except asyncio.QueueFull:
                pass

        self._process = None
        self._stdout_task = None
        self._stderr_task = None
        if self._activity_observer is not None:
            await self._activity_observer.close()

    async def interrupt(self) -> None:
        """Best-effort cancel of the in-flight turn. stop() does
        stdin-close → SIGTERM → SIGKILL escalation, which is sufficient;
        MCP-server children die with their parent."""
        await self.stop()

    # ------------------------------------------------------------------ helpers

    def _emit(self, event: HarnessEvent) -> None:
        if self._stream_closed:
            return
        self._event_queue.put_nowait(event)

    def _close_stream(self) -> None:
        """Signal end-of-stream to `stream()` consumers at a logical boundary
        (the terminal `result` event) before the subprocess actually exits."""
        if self._stream_closed:
            return
        self._stream_closed = True
        try:
            self._event_queue.put_nowait(_STREAM_END)
        except asyncio.QueueFull:
            pass

    @property
    def stderr_text(self) -> str:
        return "\n".join(self._stderr_lines)

    # ------------------------------------------------------------------ readers

    def set_idle_handler(
        self, handler: Callable[[HarnessEvent], Awaitable[None]] | None
    ) -> None:
        """Where events go when no turn is in flight.

        A held process keeps working after a turn ends — an async sub-agent
        is still running, and the CLI wakes the agent to report it when it
        lands. Those events belong to the session, not to the turn that
        happened to be open, so the session manager takes them here.
        """
        self._idle_handler = handler

    def bind_tool_context(self, request_id: str, attempt_id: str) -> None:
        from .tool_identity import ToolIdentityTracker
        if self._tool_identity_tracker is None:
            self._tool_identity_tracker = ToolIdentityTracker()
        self._tool_context = (request_id, attempt_id)

    def bind_activity(self, callback: Callable[[float | None], None]) -> int:
        """Attach one attempt clock; no display, database or network awaits."""
        self._activity_generation += 1
        self._activity_callback = callback
        return self._activity_generation

    def unbind_activity(self, token: int) -> None:
        if token == self._activity_generation:
            self.invalidate_activity()

    def invalidate_activity(self) -> None:
        """Revoke liveness synchronously, before cancellation can await cleanup."""
        self._activity_generation += 1
        self._activity_callback = None
        if self._activity_observer is not None:
            self._activity_observer.invalidate()

    def _activity(self, timestamp: float | None = None, *, generation: int | None = None) -> None:
        if (self._lease_closed or self._stream_closed
                or (generation is not None and generation != self._activity_generation)):
            return
        callback = self._activity_callback
        if callback is not None:
            callback(timestamp)

    def _publish_observations(self, events: list[HarnessEvent], *, generation: int) -> None:
        """Adapter observations share the current run's captured tool context."""
        context = self._tool_context
        if (self._lease_closed or self._stream_closed or context is None
                or generation != self._activity_generation or self._tool_identity_tracker is None):
            return
        for event in self._tool_identity_tracker.normalize_observations(events, *context):
            self._emit(event)

    async def _drain_observations(self) -> None:
        drain = getattr(self._activity_observer, "drain_observations", None)
        if drain is not None and not self._lease_closed:
            await drain()

    async def _handle_line(self, line: str) -> None:
        obj = parse_json_line(line)
        if obj is None:
            return
        if self._activity_observer is not None:
            self._activity_observer.observe_stdout(obj)
        out = self._parser.parse(obj)
        if out.activity is True or (out.activity is None and out.events):
            self._activity()
        if out.end_of_stream:
            # Consume the adapter's finite, already-written native prefix
            # before revoking the attempt and publishing the parent result.
            await self._drain_observations()
        context = self._tool_context
        if self._tool_identity_tracker is not None:
            for event in out.events:
                if context is not None:
                    self._tool_identity_tracker.normalize(event, *context)
                else:
                    self._tool_identity_tracker.normalize_idle(event)
        if out.end_of_stream:
            self._tool_context = None
            self.invalidate_activity()
        for event in out.events:
            if self._lease_closed and event.type == "subagent":
                continue
            if self._stream_closed and self._idle_handler is not None:
                # Out of turn: hand it to the session rather than dropping it.
                try:
                    await self._idle_handler(event)
                except Exception:
                    logger.exception("idle event handler failed")
            else:
                self._emit(event)
        if out.end_of_stream:
            self._close_stream()

    async def _read_stdout(self) -> None:
        assert self._process and self._process.stdout
        try:
            async for raw in self._process.stdout:
                line = raw.decode(errors="replace").rstrip("\r\n")
                if not line:
                    continue
                try:
                    await self._handle_line(line)
                except Exception:
                    logger.exception(
                        "%s event parse crashed on: %s", self._profile.backend, line[:200]
                    )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("stdout reader crashed")
        finally:
            if not self._stream_closed:
                try:
                    await self._drain_observations()
                finally:
                    self._tool_context = None
                    self.invalidate_activity()
                    self._close_stream()

    async def _read_stderr(self) -> None:
        assert self._process and self._process.stderr
        try:
            async for raw in self._process.stderr:
                line = raw.decode(errors="replace").rstrip("\r\n")
                if line:
                    if self._activity_observer is not None and self._activity_observer.consume_stderr(line):
                        continue
                    self._stderr_lines.append(line)
                    logger.debug("CLI stderr: %s", line)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("stderr reader crashed")
