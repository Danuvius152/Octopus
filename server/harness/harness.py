"""`Harness` — the single front door for all model/runtime interaction.

One concrete class, parameterized by a `RuntimeProfile`. Feature code goes
through it for everything: creating per-turn runs, lean one-shot model
calls, login, and transcript export/import. Per-framework behavior is the
profile's data + collaborators — there are no harness subclasses.

Capabilities are *derived* from what the profile provides (e.g.
`can_export` ⇐ a transcript codec is present), not a hand-maintained flag
matrix.
"""

from __future__ import annotations

import asyncio
import logging
import signal
import time

from ..config import settings
from ..diagnostics import diagnostic_text, exception_diagnostic
from .assembly import assemble_turn
from .events import HarnessCredential, HarnessOneshotError
from .launch_limits import LaunchConfigurationError, validate_launch
from .login import LoginDriver
from .model_settings import ModelOption, ModelSettings
from .profile import OneShotContext, RuntimeProfile
from .run import HarnessRun, RunConfig, _which_with_fallback, prepare_spawn

logger = logging.getLogger(__name__)


class Harness:
    """Kind-level adapter for one model runtime. Stateless; one instance per
    backend lives in the registry."""

    def __init__(self, profile: RuntimeProfile) -> None:
        self.profile = profile

    def model_settings(self, credential: HarnessCredential | None = None) -> ModelSettings:
        if self.profile.model_settings:
            return self.profile.model_settings(credential)
        return ModelSettings(models=[ModelOption(m, m) for m in self.profile.models])

    @property
    def backend(self) -> str:
        return self.profile.backend

    # ------------------------------------------------------------------ availability

    def is_available(self) -> bool:
        """Whether this harness's CLI is resolvable on this host."""
        return _which_with_fallback(self.profile.binary) is not None

    # ------------------------------------------------------------------ turns

    def create_run(self, config: RunConfig | None = None) -> HarnessRun:
        """Build the per-turn streaming run engine for this harness."""
        return HarnessRun(self.runtime_profile(config), config)

    def runtime_profile(self, config: RunConfig | None) -> RuntimeProfile:
        """Select the same capabilities for runtime creation and API views."""
        profile = self.profile
        if config is not None and config.transport is not None:
            if profile.select_transport is None:
                raise ValueError(f"{profile.backend} does not select transports")
            profile = profile.select_transport(profile, config.transport)
        return profile

    def validate_configuration(self, config: RunConfig) -> None:
        """Validate the known static render without resolving or launching a CLI.
        Final dynamic instructions, MCP entries and environment are checked again
        by prepare_spawn with the actual invocation.
        """
        profile = self.runtime_profile(config)
        ctx = assemble_turn(profile, config, "", ".", work_id=config.work_id)
        argv, kwargs = profile.new_driver(profile).build_argv(ctx)
        validate_launch(argv, kwargs.get("env"))

    def is_auth_error(self, text: str, *, code: str | None = None) -> bool:
        """Whether `text` (a failed turn's combined error output + stderr)
        looks like this backend's auth-credential rejection — a 401 / expired
        or revoked token (harness-credential-reauth.md §3). Case-insensitive
        substring match over the profile's `auth_error_patterns`; pure, no I/O.
        Callers gate on the turn actually having failed so a tool that merely
        returns a 401 to the model can't trip it."""
        if code in {"unauthorized", "auth_error"}:
            return True
        if code in self.profile.transient_error_codes or code in {
            "usage_limit_reached", "usage_limit_exceeded", "quota_exceeded",
        }:
            return False
        if not text or not self.profile.auth_error_patterns:
            return False
        low = text.lower()
        return any(p in low for p in self.profile.auth_error_patterns)

    def is_stale_session_error(self, text: str) -> bool:
        """Whether `text` says the engine has no such conversation — i.e. the
        session's stored resume id is dangling. Same case-insensitive
        substring match as the other predicates; pure, no I/O. Callers gate on
        the turn having failed AND a resume id being set, so a tool echoing
        the phrase can't trip it."""
        if not text or not self.profile.stale_session_patterns:
            return False
        low = text.lower()
        return any(p in low for p in self.profile.stale_session_patterns)

    def is_transient_error(self, text: str, *, code: str | None = None) -> bool:
        """Whether `text` (a failed turn's combined error output + stderr) looks
        like a TRANSIENT provider-reliability failure — a 5xx / overloaded /
        dropped-connection / timeout that a bounded retry can ride out
        (harness-transient-retry.md §3). Case-insensitive substring match over
        the profile's `transient_error_patterns`; pure. Mutually exclusive with
        `is_auth_error`, and excludes quota/credit (never retried). Callers gate
        on the attempt having failed; resuming partial work also requires a
        captured conversation id."""
        if code in {"usage_limit_reached", "usage_limit_exceeded", "quota_exceeded", "unauthorized", "auth_error"}:
            return False
        if code and code in self.profile.transient_error_codes:
            return True
        if not text or not self.profile.transient_error_patterns:
            return False
        low = text.lower()
        return any(p in low for p in self.profile.transient_error_patterns)

    @property
    def premature_exit_recovery(self) -> bool:
        """Whether the session_manager run loop should apply the Claude-CLI
        premature-exit respawn (internal quirk, not a product capability)."""
        return self.profile.premature_exit_recovery

    # ------------------------------------------------------------------ derived predicates

    @property
    def can_export(self) -> bool:
        return self.profile.transcript_codec is not None

    @property
    def can_import(self) -> bool:
        return self.profile.transcript_codec is not None

    @property
    def login(self) -> LoginDriver | None:
        return self.profile.login

    @property
    def transcript_codec(self):
        return self.profile.transcript_codec

    # ------------------------------------------------------------------ fork

    @property
    def can_fork(self) -> bool:
        return self.profile.can_fork

    async def prepare_fork(
        self,
        messages: list,
        working_dir: str,
        resume_id_hint: str | None,
        fork_id: str,
    ):
        """Prepare backend-specific state so a new fork session can spawn at the
        branch point (session-rewind.md §3.1). Returns a `ForkArtifact`.

        NATIVE_TRANSCRIPT (Claude) synthesizes a resumable transcript on disk
        named by `resume_id_hint`; HISTORY_REPLAY (Codex) returns
        `needs_replay=True` and does no on-disk work. Raises
        `BackendForkNotSupported` only when the backend has no strategy."""
        from .fork import BackendForkNotSupported

        if not self.profile.can_fork or self.profile.fork_prepare is None:
            raise BackendForkNotSupported(self.backend)
        return await self.profile.fork_prepare(
            messages, working_dir, resume_id_hint, fork_id
        )

    async def prepare_fork_copy(
        self,
        *,
        parent_working_dir: str,
        parent_resume_id: str | None,
        parent_credential=None,
        dest_working_dir: str,
        new_resume_id: str,
    ):
        """Full-copy fork (session-fork.md): copy the backend's NATIVE
        transcript (the parent's real conversation, identified by
        `parent_resume_id`) into `new_resume_id` at `dest_working_dir`, so a
        `/fork` duplicate resumes with real context — no history replay into the
        first prompt. Returns a `ForkArtifact`. Falls back to
        `needs_replay=True` when the backend has no native-copy strategy or the
        parent has no transcript yet (e.g. it never ran a turn)."""
        from .fork import BackendForkNotSupported, ForkArtifact

        if not self.profile.can_fork:
            raise BackendForkNotSupported(self.backend)
        if self.profile.fork_copy is None:
            return ForkArtifact(resume_id=None, needs_replay=True)
        return await self.profile.fork_copy(
            parent_working_dir=parent_working_dir,
            parent_resume_id=parent_resume_id,
            parent_credential=parent_credential,
            dest_working_dir=dest_working_dir,
            new_resume_id=new_resume_id,
        )

    async def cleanup_incomplete_fork_artifacts(
        self,
        working_dir: str,
        resume_id_hint: str | None,
        fork_id: str,
        *,
        credential=None,
    ) -> None:
        """Sweep any backend-specific files a fork strategy may have left when a
        saga didn't complete (session-rewind.md §3.1, session-fork.md).
        Idempotent — safe on every boot for every 'initializing' row. No-op when
        the backend has no on-disk fork artifacts to sweep. `credential` lets a
        directory-backed backend (Codex / CODEX_HOME) locate its rollout store."""
        if self.profile.fork_cleanup is None:
            return
        await self.profile.fork_cleanup(
            working_dir, resume_id_hint, fork_id, credential=credential
        )

    # ------------------------------------------------------------------ one-shot

    async def run_oneshot(self, ctx: OneShotContext, *, timeout: float = 90.0) -> str:
        """A lean, non-interactive, tool-free single model call. No MCP, no
        Octopus-tools blurb, no connectors — distinct from a full turn run.
        Returns the model's text. Raises `HarnessOneshotError` (with a stable
        `.code`) on failure; the caller maps it to a domain message.

        The profile builds the argv (applying the credential its own way)
        and extracts the result text from stdout."""
        started_at = time.monotonic()
        diagnostic_secrets = (settings.auth_token, ctx.credential.secret if ctx.credential else "")
        try:
            argv, kwargs = self.profile.build_oneshot_argv(ctx)
            private_settings = kwargs.pop("private_settings", None)
            argv, kwargs = prepare_spawn(argv, kwargs)
        except LaunchConfigurationError as error:
            raise HarnessOneshotError("configuration", str(error)) from error
        except FileNotFoundError:
            raise HarnessOneshotError("not_found", f"{self.profile.binary} CLI not found")
        from ..process_groups import wait_group_exit
        from .run import _terminate_process_group

        # The transport may create its process before delivering the handle.
        # Capture that launch before cancellation can surrender ownership.
        async def spawn_process():
            if ctx.authorize_spawn is not None:
                try:
                    await ctx.authorize_spawn()
                except asyncio.CancelledError:
                    return None  # authorization ended before OS submission
                except ValueError as error:
                    raise HarnessOneshotError("unauthorized", str(error)) from error
            launch_argv = private_settings.materialize(argv) if private_settings else argv
            try:
                try:
                    validate_launch(launch_argv, kwargs.get("env"))
                except LaunchConfigurationError as error:
                    raise HarnessOneshotError("configuration", str(error)) from error
                return await asyncio.create_subprocess_exec(
                    *launch_argv,
                    # DEVNULL gives a stdin-reading CLI EOF immediately.
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    **kwargs,
                )
            except BaseException:
                if private_settings is not None:
                    private_settings.close()
                raise

        spawn = asyncio.create_task(spawn_process(), name="one-shot-process-startup")
        retirement: asyncio.Task[None] | None = None

        async def retire_spawn() -> None:
            try:
                captured = await asyncio.shield(spawn)
            except (FileNotFoundError, HarnessOneshotError):
                return  # launch failed before creating a process
            if captured is None:
                if private_settings is not None:
                    private_settings.close()
                return
            _terminate_process_group(captured, signal.SIGKILL)
            await captured.wait()
            await wait_group_exit(captured.pid, timeout=2.0, after_kill=True)
            if private_settings is not None:
                private_settings.close()

        def retirement_finished(done: asyncio.Task[None]) -> None:
            if not done.cancelled() and (error := done.exception()) is not None:
                # Retirement can finish after the bounded caller has already
                # reported uncertainty; still observe its actual failure.
                logger.error("Harness one-shot retirement failed provider=%s error_type=%s\n%s",
                             self.profile.backend, type(error).__name__,
                             exception_diagnostic(error, secrets=diagnostic_secrets))

        async def confirm_retirement() -> None:
            nonlocal retirement
            if retirement is None:
                retirement = asyncio.create_task(retire_spawn(), name="one-shot-process-retirement")
                retirement.add_done_callback(retirement_finished)
            try:
                _, pending = await asyncio.wait({retirement}, timeout=2.0)
            except asyncio.CancelledError as error:
                # The captured wait still owns this process; a second cancel
                # cannot turn an unconfirmed exit into a clean interruption.
                raise RuntimeError("One-shot process retirement remains unconfirmed") from error
            if pending:
                raise RuntimeError("One-shot process retirement remains unconfirmed")
            try:
                retirement.result()
            except (asyncio.CancelledError, Exception) as error:
                raise RuntimeError("One-shot process retirement remains unconfirmed") from error

        try:
            proc = await asyncio.shield(spawn)
            if proc is None:
                raise asyncio.CancelledError
        except FileNotFoundError:
            raise HarnessOneshotError("not_found", f"{self.profile.binary} CLI not found")
        except asyncio.CancelledError:
            await confirm_retirement()
            raise

        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except TimeoutError:
            await confirm_retirement()
            raise HarnessOneshotError("timeout", "one-shot call timed out")
        except asyncio.CancelledError:
            # Job cancelled (or the job-level wait_for expired) while we were in
            # communicate() — confirm group retirement before propagating.
            await confirm_retirement()
            raise
        except Exception:
            await confirm_retirement()
            raise
        await confirm_retirement()
        if proc.returncode != 0:
            logger.warning(
                "Harness one-shot failed provider=%s exit_code=%s elapsed_ms=%s stderr=%s",
                self.profile.backend,
                proc.returncode,
                int((time.monotonic() - started_at) * 1000),
                diagnostic_text(err.decode(errors="replace"), secrets=diagnostic_secrets),
            )
            raise HarnessOneshotError("failed", "one-shot call failed")
        text = self.profile.parse_oneshot_stdout(out.decode(errors="replace"))
        if not text or not text.strip():
            raise HarnessOneshotError("empty", "one-shot returned an empty response")
        return text
