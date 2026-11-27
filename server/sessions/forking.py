"""Session forks, full working-directory copies, and startup recovery."""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, ExitStack, asynccontextmanager, nullcontext
from dataclasses import asdict, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .. import fork_helpers
from ..aio import complete_cleanup
from ..attachments import AttachmentError
from ..attachments import get_path as get_attachment_path
from ..credential_bindings import CredentialBindingError
from ..db.applications import ApplicationDeleting
from ..db.base import DatabaseBusy
from ..fork_git import (
    GitRetirementCancelled,
    GitRetirementPending,
    StashObservation,
    WorktreeChanged,
    git_scope,
    matches_worktree,
    require_rewind_worktree,
    worktree_blocked,
    worktree_for,
    worktree_lock,
)
from ..harness import HarnessRun, get_harness
from ..harness.fork import ForkCheckpoint, ForkCwd, ForkHooks, read_fork_checkpoint
from ..models import MessageContent
from ..workspace import WorkspaceError, confine
from .base import (
    ActiveRun,
    ForkError,
    Session,
    SessionManagerBase,
    logger,
)
from .fork_directory import (
    CopyDestinationExists,
    CopyDirectory,
    CopyLocation,
    ForkDirectoryError,
    fd_path,
    fork_copy_root,
    remove_copy,
)
from .fork_git_layout import GitLayoutError, require_independent_git_layout
from .owner_runtime import OwnForkProcess, ReserveFork, RetireProcess

if TYPE_CHECKING:
    from ..database import Database
    from ..fork_git import GitResult, Worktree
    from .owner import SessionOwner


class ForkingMixin(SessionManagerBase):

    def _fork_hooks(
        self, owner: SessionOwner, fork: Session, metadata: dict[str, Any],
        directory: CopyDirectory | None = None,
    ) -> ForkHooks:
        async def checkpoint(artifact: ForkCheckpoint) -> None:
            assert self.db is not None
            metadata["native_fork"] = dict(artifact)
            serialized = json.dumps(metadata)
            identity = artifact.get("thread_id") if artifact["phase"] == "created" else None
            await self.db.update_session_field(fork.id, fork_metadata=serialized, claude_session_id=identity)
            fork.fork_metadata, fork.claude_session_id = serialized, identity

        @asynccontextmanager
        async def manage(backend: HarnessRun) -> AsyncIterator[None]:
            await owner.call_runtime(OwnForkProcess(backend))
            try:
                yield
            finally:
                if owner.failed:
                    retirement = await self._failed_owner_retirement(owner)
                else:
                    retirement = await owner.call_runtime(RetireProcess(backend, "fork_finished"))
                if retirement is not None:
                    await asyncio.shield(retirement)

        cwd = ForkCwd(str(fd_path(directory.target)), directory.require_current) if directory is not None else None
        return ForkHooks(checkpoint, manage, fork.id, fork.user_id, cwd)

    async def _require_unambiguous_transcript(self, parent: Session) -> None:
        ambiguous = ((await self.db.message_sequence_state(parent.id))[2] != 0
                     if self.db else parent._transcript_integrity != "ok")
        if ambiguous:
            raise ForkError(
                "Transcript has duplicate sequences; export and review the original rows",
                reason="duplicate_message_sequences", status_code=409,
            )

    async def _recover_incomplete_forks(self) -> None:
        """Sweep forks left mid-saga by a crash (session-rewind.md §5.6.7).
        Startup requires the complete scan before recovering individual rows.
        Dispatch on `fork_status`:
          - 'initializing': PURGE in order — (1) call the harness cleanup hook
            so backend-specific artifacts go via the harness (no reach into
            ~/.claude/projects/ here); on cleanup failure leave the row for the
            next boot to retry idempotently; (2) only after cleanup succeeds,
            delete the row + copied messages.
          - 'reverting': retain the history until evidence covers the original
            Git writer, then finalize without repeating the file effects.
        Idempotent — safe on every boot."""
        if self.db is None:
            return
        rows = await self.db.load_incomplete_forks()
        for row in rows:
            fork_id = row["id"]
            status = row["fork_status"]
            if status == "initializing":
                # A retained recovery row is evidence of incomplete artifacts,
                # not an installed session, even when cleanup cannot finish.
                incomplete = self.sessions.pop(fork_id, None)
                if incomplete is not None:
                    self._session_removed(incomplete)
                recovery = None
                try:
                    harness = get_harness(row["backend"])
                    # Codex needs the credential to find its CODEX_HOME rollout
                    # store; Claude ignores it. Prefer the FORK-TIME effective
                    # credential pinned in fork_metadata (so an agent whose
                    # credential changed after the fork doesn't send cleanup to
                    # the wrong CODEX_HOME); else fall back to the
                    # row's credential_id, else its agent's. require_auth=False
                    # so a since-revoked login still yields its home dir.
                    eff_cred_id = None
                    if row.get("fork_metadata"):
                        try:
                            eff_cred_id = json.loads(
                                row["fork_metadata"]
                            ).get("cleanup_credential_id")
                        except (json.JSONDecodeError, AttributeError):
                            eff_cred_id = None
                    if not eff_cred_id:
                        eff_cred_id = row.get("credential_id")
                    if not eff_cred_id and row["defaults_source"] == "agent" and row.get("agent_id"):
                        eff_cred_id = await self.db.get_agent_credential_id(row["agent_id"], row.get("user_id"))
                    cred = await self.resolve_credential_by_id(
                        eff_cred_id,
                        backend=harness.profile.backend,
                        context=f"fork recovery {fork_id}",
                        require_auth=False,
                        user_id=row.get("user_id"),
                    )
                    metadata = json.loads(row["fork_metadata"]) if row.get("fork_metadata") else {}
                    artifact = None
                    hooks = None
                    if "native_fork" in metadata:
                        artifact = read_fork_checkpoint(metadata["native_fork"])
                        recovery = Session(id=fork_id, name="Incomplete fork cleanup", working_dir=row["working_dir"],
                            backend=row["backend"], user_id=row["user_id"], agent_id=row["agent_id"],
                            origin="fork", fork_status="initializing")
                        recovery_owner = self._session_owner(recovery)
                        hooks = self._fork_hooks(recovery_owner, recovery, metadata)
                    await harness.cleanup_incomplete_fork_artifacts(
                        row["working_dir"], row["resume_id"], fork_id,
                        credential=cred, artifact=artifact, hooks=hooks,
                    )
                    remove_copy(row["working_dir"], row["user_id"], fork_id, metadata)
                    await self.db.delete_session(fork_id)
                except Exception:
                    logger.exception(
                        "fork %s: cleanup failed; leaving "
                        "'initializing' for next boot",
                        fork_id,
                    )
                    continue
                finally:
                    if recovery is not None:
                        self._session_removed(recovery)
                logger.info("fork %s: purged incomplete (initializing) saga", fork_id)
            elif status == "reverting":
                try:
                    metadata = json.loads(row["fork_metadata"] or "{}")
                except json.JSONDecodeError:
                    metadata = {}
                scope = self._git_writer_scope
                retired = isinstance(metadata, dict) and (
                    metadata.get("revert_git_retired") is True
                    or (scope is not None and scope.covers_previous(metadata.get("revert_writer")))
                )
                if not retired:
                    self.sessions.pop(fork_id, None)
                    continue
                try:
                    record = json.loads(row["fork_revert_record"] or "{}")
                except json.JSONDecodeError:
                    record = {}
                self._record_interrupted_revert(fork_id, record)
                result, details = self._revert_fields(
                    row["forked_from_session_id"], (row["fork_after_seq"] or 0) + 1, metadata, record,
                )
                saved_identity = metadata.get("rewind_worktree_id")

                def require_directory(identity=saved_identity, working_dir=row["working_dir"]) -> None:
                    if not matches_worktree(working_dir, identity):
                        raise WorktreeChanged("The reserved Git worktree moved or was replaced")

                try:
                    await self.db.finish_fork_revert(fork_id, result, details, require_current_directory=require_directory)
                    require_directory()
                except WorktreeChanged:
                    metadata["revert_git_retired"] = True
                    await self.db.update_session_field(fork_id, fork_status="reverting",
                                                       fork_revert_record=result, fork_metadata=json.dumps(metadata))

    @staticmethod
    def _record_interrupted_revert(fork_id: str, record: dict[str, Any]) -> None:
        if record.get("status") in {"completed", "refused", "failed", "unknown_post_crash"}:
            return
        record.setdefault("ran", True)
        record.setdefault("files", [])
        record.setdefault("stash_ref", None)
        record.setdefault("refused_reason", None)
        record.update(status="unknown_post_crash", error=(
            "Fork file revert was interrupted. Inspect `git status` and "
            f"`git stash list` for 'octopus: pre-fork stash {fork_id}'."
        ))

    @staticmethod
    def _revert_fields(
        parent_label: str, message_number: int, metadata: dict[str, Any], record: dict[str, Any],
    ) -> tuple[str, str]:
        metadata = metadata.copy()
        metadata.pop("revert_worktree", None)
        metadata.pop("revert_writer", None)
        metadata.pop("revert_git_retired", None)
        metadata["first_turn_note"] = fork_helpers.render_first_turn_note(
            parent_label=parent_label, n=message_number,
            summary=metadata.get("side_effect_summary", {}), revert_record=record,
        )
        return json.dumps(record), json.dumps(metadata)

    @staticmethod
    def _retirement_pending() -> ForkError:
        return ForkError(
            "The original Git execution has not been confirmed retired",
            reason="git_retirement_pending", status_code=409,
        )

    async def _install_reverted_fork(
        self, db: Database, fork: Session, *, parent_label: str, message_number: int,
        metadata: dict[str, Any], record: dict[str, Any], worktree: Worktree, interrupted: bool = False,
    ) -> bool:
        result, details = self._revert_fields(parent_label, message_number, metadata, record)

        def require_directory() -> None:
            worktree.require_current(fork.working_dir)

        while True:
            try:
                async with self._agent_lifecycle_lock if interrupted else nullcontext():
                    admit = not interrupted or (
                        not self._admission_closed and fork.agent_id not in self._closed_agents
                    )
                    try:
                        disposition = await db.finish_fork_revert(
                            fork.id, result, details, require_current_directory=require_directory,
                        )
                        require_directory()
                    except WorktreeChanged:
                        # Exit is proved, but this candidate cannot use today's
                        # path. Retain the file result without fencing the tree.
                        metadata["revert_git_retired"] = True
                        await db.update_session_field(fork.id, fork_status="reverting",
                                                      fork_revert_record=result, fork_metadata=json.dumps(metadata))
                        if interrupted:
                            return False
                        raise
                    if disposition is None:
                        raise RuntimeError(f"Fork {fork.id} no longer has its original reverting state")
                    archived, application_deleting = disposition
                    fork.fork_revert_record = result
                    fork.fork_metadata = details
                    fork.fork_status = "ready"
                    if admit and not archived:
                        fork._admission_closed = application_deleting
                        self.sessions[fork.id] = fork
                        self._session_owner(fork)
                    return not archived
            except DatabaseBusy:
                # BEGIN never acquired the write. Keep the original result
                # and retry only this commit, without replaying any Git work.
                await asyncio.sleep(0.05)

    async def _finish_interrupted_revert(
        self, db: Database, fork: Session, worktree: Worktree, *,
        execution: asyncio.Task[GitResult] | None, timed_out: bool, file_operation: bool,
        stash: StashObservation | None,
        parent_label: str, message_number: int, metadata: dict[str, Any], record: dict[str, Any],
    ) -> None:
        # The partial observation can retain a verified stash identity.
        # Its file result is still unknown until recovery records the outcome.
        record.pop("status", None)
        partial = record.copy()
        if stash is not None and stash.verify:
            partial["stash_ref"] = None
        if file_operation and not partial.get("ran"):
            partial.pop("ran", None)
        try:
            await db.update_session_field(fork.id, fork_revert_record=json.dumps(partial))
        except DatabaseBusy:
            # Optional partial observation. The original owner still holds
            # the record for the required terminal commit.
            pass
        if execution is not None:
            observed = await asyncio.shield(execution)
            fork_helpers.record_git_result(record, replace(observed, timed_out=timed_out),
                                           file_operation=file_operation, stash=stash)
        self._record_interrupted_revert(fork.id, record)
        async with worktree_lock(worktree):
            await self._install_reverted_fork(
                db, fork, parent_label=parent_label, message_number=message_number, metadata=metadata, record=record,
                worktree=worktree, interrupted=True,
            )

    async def fork_session(
        self, parent_id: str, rewind_to_msg_seq: int, *,
        revert_files: bool = False, label: str | None = None,
        user_id: str | None = None,
    ) -> Session:
        parent = self.get_session(parent_id, user_id)
        if parent is None:
            raise ForkError(f"Session {parent_id} not found", reason="parent_not_found", status_code=404)
        await self._require_unambiguous_transcript(parent)

        async def rewind() -> Session:
            worktree = worktree_for(parent.working_dir)
            require_rewind_worktree(parent.working_dir, parent.fork_metadata, worktree=worktree)
            with ExitStack() as relationship:
                async with worktree_lock(worktree):
                    if await worktree_blocked(self.input_database(), worktree):
                        raise self._retirement_pending()
                    settle = relationship.enter_context(self._session_relationship(parent_id).changing())
                    original, fork = await self._prepare_rewind(
                        parent_id, rewind_to_msg_seq, revert_files=revert_files, label=label,
                        user_id=user_id, worktree=worktree,
                    )
                try:
                    await self._archive_original(original, successor=fork, settle_relationship=settle)
                except Exception:
                    logger.exception("fork %s is ready; parent %s archive did not complete", fork.id, parent_id)
                return fork

        try:
            return await self._run_agent_operation(parent.agent_id, rewind)
        except WorktreeChanged as exc:
            raise ForkError(str(exc), reason="fork_worktree_changed", status_code=409) from exc
        except ValueError as exc:
            raise ForkError(str(exc), reason="agent_admission_closed", status_code=409) from exc

    async def _prepare_rewind(
        self,
        parent_id: str,
        rewind_to_msg_seq: int,
        *,
        worktree: Worktree,
        revert_files: bool = False,
        label: str | None = None,
        user_id: str | None = None,
    ) -> tuple[Session, Session]:
        """Fork `parent_id` by rewinding to *before* the user message at
        `seq=rewind_to_msg_seq` and re-spawning as a new branch
        (session-rewind.md §5.1). Returns the new fork Session. The saga is
        ordered (NOT a single transaction — SQLite rollback can't undo FS/git):
        validate → lock+`_forking` → snapshot+capacity → DB-only insert → stamp replay
        metadata → optional safe-revert. Rewind replays product messages;
        native history copying belongs to the separate full-copy operation."""

        # Scoped, and through the same error as a missing parent: "belongs to
        # somebody else" must be indistinguishable from "does not exist", or the
        # difference between the two is an id oracle (multi-tenancy.md §5).
        parent = self.get_session(parent_id, user_id)
        if parent is None:
            raise ForkError(
                f"Session {parent_id} not found",
                reason="parent_not_found",
                status_code=404,
            )

        # 1. Validate the target user message (loaded for EVERY M, incl. M=0 —
        #    it supplies the prefilled prompt + the git anchor for revert).
        M = rewind_to_msg_seq
        messages = await self.db.load_messages(parent_id) if self.db else []
        by_seq = {m["seq"]: m for m in messages}
        if M < 0 or M not in by_seq:
            raise ForkError(
                f"No message at seq {M} in session {parent_id}",
                reason="invalid_rewind_seq",
                status_code=400,
            )
        target = by_seq[M]
        if target["role"] != "user":
            raise ForkError(
                f"Message at seq {M} is not a user message",
                reason="target_not_user_message",
                status_code=400,
            )
        fork_after_seq = M - 1
        prefilled_prompt = (
            target["content"] if isinstance(target["content"], str) else ""
        )

        # A running turn holds this lock until it exits. Refuse it before
        # waiting, so adoption can join this command and then stop that turn.
        if self.session_is_executing(parent):
            raise ForkError(
                "Parent session has an active turn",
                reason="fork_blocked_parent_turn_active", status_code=409,
            )
        # 2. Acquire the lock, validate no live parent work, claim `_forking`.
        owner = self._session_owner(parent)
        source = await owner.call_runtime(ReserveFork(True))
        harness = get_harness(source.backend)

        reservation_held = True
        fork_id = uuid.uuid4().hex[:12]
        try:
            assert self.db is not None  # The reservation retains its database binding.
            snapshot = await self.db.snapshot_fork_messages(parent_id, fork_after_seq)
            copied = [MessageContent(**row) for row in snapshot.messages]
            summary = await fork_helpers.classify_side_effects(self.db, parent_id, M)
            metadata: dict[str, Any] = {
                "prefilled_prompt": prefilled_prompt,
                "side_effect_summary": summary,
                "fork_label": label,
                "first_turn_note": fork_helpers.render_first_turn_note(
                    parent_label=source.name, n=M, summary=summary),
            }
            if worktree.identity is not None:
                metadata["rewind_worktree_id"] = worktree.identity
            candidate = replace(source, id=fork_id, claude_session_id=None,
                                fork_metadata=json.dumps(metadata), _run=ActiveRun())
            async with self.db.configuration_lock:
                agent = await self._load_agent(candidate)
            connectors = await self._load_connectors(agent)
            config = self._run_config(candidate, agent, connectors)
            try:
                credential = await self.read_credential_for_validation(
                    config.credential_id, backend=source.backend, user_id=source.user_id)
            except CredentialBindingError as error:
                raise ForkError(str(error), reason="fork_credential_unavailable", status_code=400) from error
            try:
                from .handoff import recheck_replay_scope

                replay = await recheck_replay_scope(self.db, copied, user_id=source.user_id)
                harness.validate_replay(config, fork_helpers.wrap_for_fork_replay("x" * 4096, replay),
                                        source.working_dir, credential)
            except ValueError as error:
                raise ForkError(
                    f"Rewind history cannot fit with 4096 bytes of new-input reserve: {error}",
                    reason="replay_capacity_exceeded", status_code=400,
                ) from error
            # 5. DB-only transaction: insert fork row + copied messages.
            assert self.db is not None  # Fork reservation and compensation retain the binding.
            now = datetime.now(UTC).isoformat()
            # A fork is a rewind, not a sibling branch: it inherits the
            # parent's exact name so it slots into the sidebar as the original
            # session (the parent is archived below). An explicit `label` still
            # wins when the caller wants a distinct name.
            fork_name = label or source.name
            app_id = await self.db.create_fork_session(
                fork_id=fork_id,
                name=fork_name,
                working_dir=source.working_dir,
                created_at=now,
                parent_id=parent_id,
                backend=source.backend,
                model=source.model,
                reasoning_effort=source.reasoning_effort,
                service_tier=source.service_tier,
                defaults_source=source.defaults_source,
                user_id=source.user_id,
                agent_id=source.agent_id,
                credential_id=source.credential_id,
                resume_id=None,
                fork_after_seq=fork_after_seq,
                message_snapshot=snapshot,
            )
            fork = Session(
                id=fork_id,
                name=fork_name,
                working_dir=source.working_dir,
                created_at=now,
                credential_id=source.credential_id,
                model=source.model,
                reasoning_effort=source.reasoning_effort,
                service_tier=source.service_tier,
                defaults_source=source.defaults_source,
                agent_id=source.agent_id,
                app_id=app_id,
                origin="fork",
                backend=source.backend,
                user_id=source.user_id,
                forked_from_session_id=parent_id,
                fork_after_seq=fork_after_seq,
                fork_status="initializing",
            )
            # Copied M messages (seq 0..M-1); next assigned seq is M.
            fork.restore_message_state(len(copied), M, 0)

            # Stamp replay metadata before promoting the candidate to ready.
            fork.fork_metadata = candidate.fork_metadata
            fork.fork_needs_replay = True
            await self.db.update_session_field(
                fork_id, fork_metadata=fork.fork_metadata, fork_needs_replay=True
            )
            if revert_files:
                fork.fork_status = "reverting"
                metadata["revert_worktree"] = str(worktree.path)
                if self._git_writer_scope is not None:
                    metadata["revert_writer"] = asdict(self._git_writer_scope)
                fork.fork_metadata = json.dumps(metadata)
                progress: dict[str, Any] = {"ran": False}
                try:
                    await complete_cleanup(self.db.update_session_field(
                        fork_id, fork_status="reverting", fork_metadata=fork.fork_metadata,
                    ))
                    with git_scope(worktree, self.db, parent.id):
                        record = await fork_helpers.safe_revert_files(
                            source.working_dir, summary["agent_touched_paths"], target.get("git_head"),
                            target.get("git_status_clean"), fork_id, target.get("git_ignored_baseline"),
                            worktree=worktree, progress=progress,
                        )
                except (GitRetirementPending, asyncio.CancelledError) as error:
                    pending = error if isinstance(error, (GitRetirementPending, GitRetirementCancelled)) else None
                    self._own_task(asyncio.create_task(
                        self._finish_interrupted_revert(
                            self.db, fork, worktree, execution=pending.execution if pending else None,
                            timed_out=pending.timed_out if pending else False,
                            file_operation=pending.file_operation if pending else False,
                            stash=pending.stash if pending else None,
                            parent_label=source.name, message_number=M, metadata=metadata, record=progress,
                        ),
                        name=f"fork-revert-retirement-{fork.id}",
                    ))
                    if isinstance(error, GitRetirementPending):
                        raise self._retirement_pending() from error
                    raise
                if not await complete_cleanup(self._install_reverted_fork(
                    self.db, fork, parent_label=source.name, message_number=M, metadata=metadata, record=record,
                    worktree=worktree,
                )):
                    raise ForkError("The fork was archived during file revert", reason="fork_archived", status_code=409)
            else:
                db = self.db

                async def admit_rewind() -> None:
                    def require_directory() -> None:
                        worktree.require_current(source.working_dir)

                    try:
                        if await worktree_blocked(db, worktree):
                            raise self._retirement_pending()
                        await db.update_session_field(
                            fork_id, fork_status="ready", require_current_directory=require_directory,
                        )
                        require_directory()
                    except (WorktreeChanged, ForkError):
                        await db.delete_session(fork_id)
                        raise
                    fork.fork_status = "ready"
                    self.sessions[fork_id] = fork
                    self._session_owner(fork)

                await complete_cleanup(admit_rewind())

            # The installed successor is now capturable by application deletion.
            # External archive must obey this reservation until this point too.
            await self._release_session_reservation(owner, ReserveFork(False))
            reservation_held = False

            return parent, fork
        finally:
            if reservation_held and self.sessions.get(parent.id) is parent:
                await self._release_session_reservation(owner, ReserveFork(False))

    @staticmethod
    def _fork_copy_dest(src_working_dir: str, fork_id: str, user_id: str | None) -> str:
        """Plan a readable, unique copy location without touching the filesystem."""
        name = os.path.basename(os.path.normpath(src_working_dir)) or "session"
        return str(fork_copy_root(user_id) / f"{name}-{fork_id}")

    def _fork_allocation_lock(self, user_id: str | None) -> asyncio.Lock:
        lock = self._fork_allocation_locks.get(user_id)
        if lock is None:
            lock = asyncio.Lock()
            self._fork_allocation_locks[user_id] = lock
        return lock

    @staticmethod
    async def _check_fork_git_layout(source: Path, destination: Path) -> None:
        try:
            await complete_cleanup(asyncio.to_thread(require_independent_git_layout, source, destination))
        except GitLayoutError as error:
            raise ForkError(
                f"The Git layout cannot be copied independently: {error}. "
                "Use a self-contained working directory.",
                reason="unsupported_git_layout", status_code=409,
            ) from error

    async def duplicate_session(
        self, parent_id: str, *, label: str | None = None,
        user_id: str | None = None,
    ) -> Session:
        parent = self.get_session(parent_id, user_id)
        if parent is None:
            raise ForkError(f"Session {parent_id} not found", reason="parent_not_found", status_code=404)
        await self._require_unambiguous_transcript(parent)
        try:
            return await self._run_agent_operation(
                parent.agent_id, lambda: self._duplicate_session(parent_id, label=label, user_id=user_id),
            )
        except ValueError as exc:
            raise ForkError(str(exc), reason="agent_admission_closed", status_code=409) from exc

    async def _duplicate_session(
        self,
        parent_id: str,
        *,
        label: str | None = None,
        user_id: str | None = None,
    ) -> Session:
        """`/fork`: duplicate `parent_id` at HEAD onto an INDEPENDENT full copy
        of its working directory (session-fork.md). The new session carries
        the parent's whole conversation and continues it at the copied path; the
        PARENT is left untouched (not archived). Distinct from `fork_session`
        (/rewind), which rewinds to a message and archives the parent."""

        parent = self.get_session(parent_id, user_id)
        if parent is None:
            raise ForkError(
                f"Session {parent_id} not found",
                reason="parent_not_found", status_code=404,
            )
        # Same idle guard as fork_session — a copy of a mid-turn workspace would
        # be inconsistent, and the resume artifact needs a settled transcript.
        if self.session_is_executing(parent):
            raise ForkError("Parent session has an active turn",
                            reason="fork_blocked_parent_turn_active", status_code=409)
        owner = self._session_owner(parent)
        source = await owner.call_runtime(ReserveFork(True))
        harness = get_harness(source.backend)

        fork_id = uuid.uuid4().hex[:12]
        resume_id_hint = str(uuid.uuid4())
        directory = ExitStack()
        allocation = AsyncExitStack()
        try:
            await allocation.enter_async_context(self._fork_allocation_lock(parent.user_id))
            dest = os.path.abspath(os.path.expanduser(self._fork_copy_dest(
                source.working_dir, fork_id, parent.user_id,
            )))
            # Resolve existing ancestors too: a symlink in the target's parent
            # can put an apparently separate copy back inside the source tree.
            source_path = Path(source.working_dir).resolve()
            dest_path = Path(dest).resolve()
            if dest_path == source_path or dest_path.is_relative_to(source_path):
                raise ForkError(
                    "The copy destination must be outside the source working directory",
                    reason="invalid_copy_destination", status_code=400,
                )
            try:
                location = directory.enter_context(CopyLocation.planned(Path(dest), parent.user_id))
                confine(str(dest_path), user_id=parent.user_id,
                        extra_roots=await self._owner_extra_roots(parent.user_id))
            except (OSError, ForkDirectoryError, WorkspaceError) as error:
                raise ForkError(str(error), reason="invalid_copy_destination", status_code=400) from error
            await self._check_fork_git_layout(source_path, dest_path)
            # Snapshot the transcript after reserving the parent against turns:
            # loading it earlier risks a fast turn slipping in between the read
            # and the guard, which would copy a post-turn working dir against a
            # pre-turn message list. With the guard held, both are consistent.
            messages = await self.db.load_messages(parent_id) if self.db else []
            last_seq = max((m["seq"] for m in messages), default=-1)
            # Resolve the EFFECTIVE credential (session override, else agent's)
            # only to locate the backend's on-disk transcript store for the copy
            # + cleanup — Codex needs its CODEX_HOME; Claude needs none (→ None).
            # require_auth=False: the rollout must be locatable even if the login
            # later lapses. Fork preparation makes no model inference call;
            # a native backend may use management RPCs to copy its history.
            eff_cred_id = source.credential_id
            if not eff_cred_id and source.defaults_source == "agent" and self.db is not None and source.agent_id:
                eff_cred_id = await self.db.get_agent_credential_id(source.agent_id, source.user_id)
            parent_cred = await self.resolve_credential_by_id(
                eff_cred_id, backend=harness.profile.backend,
                context=f"fork {parent.id}", require_auth=False,
                user_id=source.user_id,
            )

            now = datetime.now(UTC).isoformat()
            fork_name = label or f"{parent.name} (fork)"
            # The fork's metadata, written at INSERT so it survives a prepare
            # failure: `full_copy`/`duplicated_from` drive the UI, and
            # `cleanup_credential_id` pins the FORK-TIME effective credential so
            # the startup sweep finds the right CODEX_HOME even if the agent's
            # credential later changes. Omitted when there's none.
            creation = uuid.uuid4().hex
            fork_meta_dict: dict[str, Any] = {
                "full_copy": True, "duplicated_from": parent.name,
                "copy_layout": location.layout, "copy_creation": creation, "copy_delivery_pending": True,
            }
            # Pin only when the backend has a per-credential on-disk store to
            # clean up (parent_cred resolved → home_dir-style, i.e. Codex);
            # Claude's transcript isn't credential-scoped, so no pin.
            if eff_cred_id and parent_cred is not None:
                fork_meta_dict["cleanup_credential_id"] = eff_cred_id
            fork_meta = json.dumps(fork_meta_dict)
            # 1. Commit recovery responsibility before any directory copy.
            #    fork_after_seq = last_seq is the replay cutoff (it's also what
            #    HISTORY_REPLAY backends inject on turn 1 — so it MUST stay set,
            #    or the duplicate would continue with no copied context). The UI
            #    distinguishes a full copy from a rewind via fork_metadata's
            #    `full_copy` flag.
            assert self.db is not None  # Original saga drains before DB close.
            await self.db.create_fork_session(
                fork_id=fork_id, name=fork_name, working_dir=dest,
                created_at=now, parent_id=parent_id, backend=source.backend,
                model=source.model,
                reasoning_effort=source.reasoning_effort,
                service_tier=source.service_tier,
                defaults_source=source.defaults_source,
                user_id=parent.user_id,
                agent_id=parent.agent_id, credential_id=source.credential_id,
                resume_id=resume_id_hint, fork_after_seq=last_seq,
                fork_metadata=fork_meta,
                inherit_application=False,
            )
            fork = Session(
                id=fork_id, name=fork_name, working_dir=dest, created_at=now,
                claude_session_id=resume_id_hint, credential_id=source.credential_id,
                agent_id=parent.agent_id, origin="fork", backend=source.backend,
                model=source.model,
                reasoning_effort=source.reasoning_effort,
                service_tier=source.service_tier,
                defaults_source=source.defaults_source,
                user_id=parent.user_id,
                forked_from_session_id=parent_id, fork_after_seq=last_seq,
                fork_metadata=fork_meta, fork_status="initializing",
            )
            fork.restore_message_state(len(messages), last_seq + 1, 0)

            # The copy starts in a private slot while the logical cwd is vacant.
            # The committed receipt proves which private and installed objects
            # this saga may use or retire.
            try:
                owned = directory.enter_context(location.create(creation))
            except ForkDirectoryError as error:
                raise ForkError(str(error), reason="invalid_copy_destination", status_code=409) from error
            except OSError as error:
                if isinstance(error, CopyDestinationExists):
                    # Exclusive installation found an existing entry.
                    await self.db.delete_session(fork_id)
                else:
                    try:
                        remove_copy(dest, parent.user_id, fork_id, fork_meta_dict)
                    except (OSError, ForkDirectoryError):
                        logger.exception("fork %s: allocation outcome retained for recovery", fork_id)
                    else:
                        await self.db.delete_session(fork_id)
                raise ForkError(
                    f"Failed to allocate the working directory: {error}",
                    reason="copy_failed", status_code=500,
                ) from error
            await allocation.aclose()
            fork_meta_dict["owned_copy_dir"] = owned.receipt()
            fork.fork_metadata = json.dumps(fork_meta_dict)
            # Storage failure is not a copy failure: do not compensate a write
            # whose COMMIT is unconfirmed through a second mutation path.
            await self.db.update_session_field(fork_id, fork_metadata=fork.fork_metadata)

            # 2. Build the copy privately and install its cwd, then prepare the native transcript
            #    with the WHOLE conversation as genuine context — no history
            #    replay dumped into the first prompt (session-fork.md). On a
            #    backend/parent with no transcript yet, the harness returns
            #    needs_replay=True and we fall back to the replay path.
            native_started = False
            finishing = False
            try:
                try:
                    # Cancellation ends this command only after its original
                    # thread exits. Neither compensation nor reservation release
                    # can race a writer still populating the destination.
                    owned.require_staged()
                    await complete_cleanup(asyncio.to_thread(owned.copy_from, source_path))
                    owned.install()
                except ForkDirectoryError:
                    raise
                except Exception as error:
                    raise ForkError(
                        f"Failed to copy the working directory: {error}",
                        reason="copy_failed", status_code=500,
                    ) from error
                owned.require_current()
                await self._check_fork_git_layout(Path(dest), Path(dest))
                owned.require_current()
                native_started = True
                artifact = await harness.prepare_fork_copy(
                    parent_working_dir=parent.working_dir,
                    parent_resume_id=parent.claude_session_id,
                    parent_credential=parent_cred,
                    dest_working_dir=dest,
                    new_resume_id=resume_id_hint,
                    hooks=self._fork_hooks(owner, fork, fork_meta_dict, owned),
                )
                finishing = True
                await self.db.finish_full_copy_fork(
                    fork_id, artifact.resume_id, artifact.needs_replay, owned.require_current,
                )
                try:
                    # The bound cwd must still name this copy after SQLite
                    # acknowledges the write, at admission to the live map.
                    owned.require_current()
                except ForkDirectoryError:
                    await self.db.update_session_field(fork_id, fork_status="initializing")
                    raise
            except BaseException as failure:
                if finishing and not isinstance(failure, ForkDirectoryError):
                    # A failed or cancelled storage wait does not prove rollback.
                    raise
                # Retire artifacts, then the owned directory, then its recovery
                # row. Failed cleanup retains responsibility for the next boot.
                try:
                    self.db.health.require_healthy()
                    if native_started:
                        await harness.cleanup_incomplete_fork_artifacts(
                            dest, fork.claude_session_id, fork_id, credential=parent_cred,
                            artifact=(read_fork_checkpoint(fork_meta_dict["native_fork"])
                                      if "native_fork" in fork_meta_dict else None),
                            hooks=self._fork_hooks(owner, fork, fork_meta_dict),
                        )
                    remove_copy(dest, parent.user_id, fork_id, fork_meta_dict)
                    await self.db.delete_session(fork_id)
                except ForkDirectoryError:
                    logger.exception("fork %s: directory ownership could not be verified", fork_id)
                except Exception:
                    logger.exception(
                        "fork %s: cleanup failed; leaving row + copied "
                        "dir for next-boot retry",
                        fork_id,
                    )
                    raise
                if isinstance(failure, ForkDirectoryError):
                    raise ForkError(str(failure), reason="invalid_copy_destination", status_code=409) from failure
                raise

            # 4. Apply resume state + finalize. fork_after_seq stays = last_seq
            #    (replay cutoff, see above); the `full_copy` flag tells the UI to
            #    render "full copy of the working dir" instead of "@msg N".
            fork.claude_session_id = artifact.resume_id
            fork.fork_needs_replay = artifact.needs_replay
            fork.fork_status = "ready"
            self.sessions[fork_id] = fork
            self._session_owner(fork)

            # 5. Parent is left untouched (NOT archived). Announce the new fork
            #    so other tabs add it.
            await self._broadcast({
                "type": "session_forked",
                "parent_session_id": parent_id,
                "fork_session_id": fork.id,
                "name": fork.name,
            }, user_id=parent.user_id)
            # If the process exits before recording delivery, startup must
            # check the saved binding or use this fork's own admitted input.
            before = fork.fork_metadata
            acknowledged = await self.db.acknowledge_full_copy_delivery(fork.id)
            if acknowledged is not None and fork.fork_metadata == before:
                fork.fork_metadata = acknowledged
            return fork
        finally:
            directory.close()
            await allocation.aclose()
            if self.sessions.get(parent.id) is parent:
                await self._release_session_reservation(owner, ReserveFork(False))

    async def fork_preview(
        self,
        parent_id: str,
        rewind_to_msg_seq: int,
        user_id: str | None = None,
    ) -> dict[str, Any]:
        """Run the side-effect classifier + revert preflight for the popover
        WITHOUT committing anything (session-rewind.md §5.6.2). Powers
        `GET /api/sessions/{id}/fork-preview`."""
        parent = self.get_session(parent_id, user_id)
        if parent is None:
            raise ForkError(
                f"Session {parent_id} not found",
                reason="parent_not_found",
                status_code=404,
            )
        M = rewind_to_msg_seq
        await self._require_unambiguous_transcript(parent)
        messages = await self.db.load_messages(parent_id) if self.db else []
        by_seq = {m["seq"]: m for m in messages}
        if M < 0 or M not in by_seq:
            raise ForkError(
                f"No message at seq {M} in session {parent_id}",
                reason="invalid_rewind_seq",
                status_code=400,
            )
        target = by_seq[M]
        if target["role"] != "user":
            raise ForkError(
                f"Message at seq {M} is not a user message",
                reason="target_not_user_message",
                status_code=400,
            )
        summary = await fork_helpers.classify_side_effects(self.db, parent_id, M)
        try:
            worktree = worktree_for(parent.working_dir)
            require_rewind_worktree(parent.working_dir, parent.fork_metadata, worktree=worktree)
        except WorktreeChanged as error:
            raise ForkError(str(error), reason="fork_worktree_changed", status_code=409) from error
        async with worktree_lock(worktree):
            blocked = await worktree_blocked(self.input_database(), worktree)
            available = False
            reason: str | None = str(self._retirement_pending())
            if not blocked:
                try:
                    await self.input_database().require_application_session_writable(parent.id)
                    with git_scope(worktree, self.input_database(), parent.id):
                        available, reason, _dirty = await fork_helpers.safe_revert_preflight(
                            parent.working_dir, summary["agent_touched_paths"], target.get("git_head"),
                            target.get("git_status_clean"), target.get("git_ignored_baseline"),
                        )
                except GitRetirementPending:
                    blocked = True
                except ApplicationDeleting as error:
                    blocked, reason = True, str(error)
        return {
            "rewind_to_msg_seq": M,
            "prefilled_prompt": (
                target["content"] if isinstance(target["content"], str) else ""
            ),
            "side_effect_summary": summary,
            "revert": {"available": available, "refused_reason": reason},
            "can_fork": not blocked and get_harness(parent.backend).can_fork,
        }

    async def fork_ancestor_ids(self, session_id: str) -> list[str]:
        """The `forked_from_session_id` chain above `session_id` (nearest
        first), reading archived rows too so the walk survives parent archive
        (session-rewind.md §5.1 step 5.2 read-time fallback). Visited-set
        guarded against corrupted pointers."""
        if self.db is None:
            return []
        rows = {r["id"]: r for r in await self.db.load_sessions(include_archived=True)}
        out: list[str] = []
        seen: set[str] = set()
        cur = rows.get(session_id)
        while cur and cur.get("forked_from_session_id"):
            pid = cur["forked_from_session_id"]
            if pid in seen:
                break
            seen.add(pid)
            out.append(pid)
            cur = rows.get(pid)
        return out

    async def fork_descendant_ids(self, session_id: str) -> list[str]:
        """Every fork descending from `session_id` at ANY depth (breadth-first,
        visited-set guard — session-rewind.md §5.5). Uncapped depth: fork
        chains are a static DAG of past branches, so 'don't loop' is the only
        invariant worth enforcing."""
        if self.db is None:
            return []
        rows = await self.db.load_sessions(include_archived=True)
        children: dict[str, list[str]] = {}
        for r in rows:
            p = r.get("forked_from_session_id")
            if p:
                children.setdefault(p, []).append(r["id"])
        out: list[str] = []
        seen: set[str] = set()
        frontier = [session_id]
        while frontier:
            nxt: list[str] = []
            for sid in frontier:
                for child in children.get(sid, []):
                    if child in seen:
                        continue
                    seen.add(child)
                    out.append(child)
                    nxt.append(child)
            frontier = nxt
        return out

    async def _fork_attachment_source(self, session_id: str, attachment_id: str, *, user_id: str) -> Path | None:
        """Authorize only the ancestry prefix actually needed to find a file."""
        if self.db is None:
            return None
        for ancestor_id in await self.fork_ancestor_ids(session_id):
            owner_id = await self.db.get_session_owner(ancestor_id)
            if owner_id is None:
                # Deleted parents are valid dangling links, not permission to
                # guess the owner of files beyond this point.
                return None
            if owner_id != user_id:
                raise AttachmentError("invalid attachment ancestor owner")
            path = get_attachment_path(ancestor_id, attachment_id, user_id=user_id)
            if path is not None:
                return path
        return None

    async def _blit_attachments_to_descendant_forks(self, session_id: str) -> None:
        """Before a session's row is removed, materialize into each
        descendant fork's own dir any attachment file the fork references only
        by metadata (session-rewind.md §5.5) — keeping the read-time
        fallback valid after this parent is gone."""
        if self.db is None:
            return
        descendants = await self.fork_descendant_ids(session_id)
        if not descendants:
            return
        from ..attachments import blit_attachment

        binding = await self.db.get_session_binding(session_id)
        if binding is None:
            # The original delete committed after materializing these files.
            # A retry may still owe the post-commit hooks.
            return
        owner_id = binding["user_id"]
        if owner_id is None:
            raise ValueError("The session has no attachment owner")
        for fork_id in descendants:
            msgs = await self.db.load_messages(fork_id)
            for m in msgs:
                for att in m.get("attachments") or []:
                    aid = att.get("id")
                    if not aid:
                        continue
                    if await self.db.get_session_owner(fork_id) != owner_id:
                        raise ValueError("The attachment descendant has a different owner")
                    src = get_attachment_path(fork_id, aid, user_id=owner_id)
                    if src is None:
                        src = await self._fork_attachment_source(fork_id, aid, user_id=owner_id)
                    if src is not None:
                        await asyncio.to_thread(blit_attachment, fork_id, src, user_id=owner_id)
                    else:
                        raise ValueError("The descendant attachment has no retained source")
