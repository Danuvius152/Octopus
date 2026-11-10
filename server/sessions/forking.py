"""Forking and duplicating a session, and recovering a fork that was interrupted.

One slice of `SessionManager`, which was a single 4,017-line class with 84
methods (docs/post-mortems/2026-09-25-polish-pass.md). Split by responsibility; the
methods are unchanged and still reached through one `session_manager` object,
so no call site moved.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .. import fork_helpers
from ..aio import complete_cleanup
from ..attachments import AttachmentError
from ..attachments import get_path as get_attachment_path
from ..credential_bindings import CredentialBindingError
from ..harness import BackendForkNotSupported, HarnessRun, get_harness
from ..harness.fork import ForkCheckpoint, ForkHooks, read_fork_checkpoint
from ..models import MessageContent
from .base import (
    ActiveRun,
    ForkError,
    Session,
    SessionManagerBase,
    logger,
)
from .owner_runtime import OwnForkProcess, ReserveFork, RetireProcess

if TYPE_CHECKING:
    from .owner import SessionOwner


class ForkingMixin(SessionManagerBase):

    def _fork_hooks(self, owner: SessionOwner, fork: Session, metadata: dict[str, Any]) -> ForkHooks:
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

        return ForkHooks(checkpoint, manage, fork.id, fork.user_id)

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
        Dispatch on `fork_status`:
          - 'initializing': PURGE in order — (1) call the harness cleanup hook
            so backend-specific artifacts go via the harness (no reach into
            ~/.claude/projects/ here); on cleanup failure leave the row for the
            next boot to retry idempotently; (2) only after cleanup succeeds,
            delete the row + copied messages.
          - 'reverting': FINALIZE — git ops were in flight at crash; mark the
            durable revert record `unknown_post_crash` and promote to 'ready'.
        Idempotent — safe on every boot."""
        if self.db is None:
            return
        try:
            rows = await self.db.load_incomplete_forks()
        except Exception:
            logger.exception("fork recovery: could not query incomplete forks")
            return
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
                    # the wrong CODEX_HOME — Vera review); else fall back to the
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
                    if not eff_cred_id and row.get("agent_id") and self.db:
                        ag = await self.db.get_agent(row["agent_id"])
                        eff_cred_id = ag.get("credential_id") if ag else None
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
                    self._remove_fork_copy_directory(row["working_dir"], metadata)
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
                    rec = (
                        json.loads(row["fork_revert_record"])
                        if row["fork_revert_record"]
                        else {}
                    )
                except json.JSONDecodeError:
                    rec = {}
                rec.setdefault("ran", True)
                rec.setdefault("files", [])
                rec.setdefault("stash_ref", None)
                rec.setdefault("refused_reason", None)
                rec["status"] = "unknown_post_crash"
                rec["error"] = (
                    "Server crashed during fork file-revert. Inspect `git "
                    "status` and `git stash list` for "
                    f"'octopus: pre-fork stash {fork_id}'."
                )
                await self.db.update_session_field(
                    fork_id,
                    fork_revert_record=json.dumps(rec),
                    fork_status="ready",
                )
                sess = self.sessions.get(fork_id)
                if sess is not None:
                    sess.fork_revert_record = json.dumps(rec)
                    sess.fork_status = "ready"
                logger.info(
                    "fork %s: finalized interrupted revert as unknown_post_crash",
                    fork_id,
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
        try:
            return await self._run_agent_operation(
                parent.agent_id, lambda: self._fork_session(
                    parent_id, rewind_to_msg_seq, revert_files=revert_files,
                    label=label, user_id=user_id,
                ),
            )
        except ValueError as exc:
            raise ForkError(str(exc), reason="agent_admission_closed", status_code=409) from exc

    async def _fork_session(
        self,
        parent_id: str,
        rewind_to_msg_seq: int,
        *,
        revert_files: bool = False,
        label: str | None = None,
        user_id: str | None = None,
    ) -> Session:
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

        harness = get_harness(parent.backend)
        if not harness.can_fork:
            raise BackendForkNotSupported(parent.backend)

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

        fork_id = uuid.uuid4().hex[:12]
        try:
            assert self.db is not None  # The reservation retains its database binding.
            snapshot = await self.db.snapshot_fork_messages(parent_id, fork_after_seq)
            copied = [MessageContent(**row) for row in snapshot.messages]
            summary = await fork_helpers.classify_side_effects(self.db, parent_id, M)
            metadata = {
                "prefilled_prompt": prefilled_prompt,
                "side_effect_summary": summary,
                "fork_label": label,
                "first_turn_note": fork_helpers.render_first_turn_note(
                    parent_label=source.name, n=M, summary=summary, reverted=False),
            }
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
                harness.validate_replay(config, fork_helpers.wrap_for_fork_replay("x" * 4096, copied),
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
            await self.db.create_fork_session(
                fork_id=fork_id,
                name=fork_name,
                working_dir=source.working_dir,
                created_at=now,
                parent_id=parent_id,
                backend=source.backend,
                model=source.model,
                reasoning_effort=source.reasoning_effort,
                service_tier=source.service_tier,
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
                agent_id=source.agent_id,
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
                await self.db.update_session_field(fork_id, fork_status="reverting")
            else:
                fork.fork_status = "ready"
                await self.db.update_session_field(fork_id, fork_status="ready")

            # 8. Safe-revert as a SEPARATE post-create step (durable record).
            if revert_files:
                record = await fork_helpers.safe_revert_files(
                    source.working_dir,
                    summary["agent_touched_paths"],
                    target.get("git_head"),
                    target.get("git_status_clean"),
                    fork_id,
                )
                fork.fork_revert_record = json.dumps(record)
                await self.db.update_session_field(
                    fork_id, fork_revert_record=fork.fork_revert_record
                )
                # Re-render the first-turn note with the real revert outcome.
                reverted = record["ran"] and record["status"] == "completed"
                metadata["first_turn_note"] = fork_helpers.render_first_turn_note(
                    parent_label=source.name, n=M, summary=summary, reverted=reverted
                )
                fork.fork_metadata = json.dumps(metadata)
                await self.db.update_session_field(
                    fork_id, fork_metadata=fork.fork_metadata
                )
                fork.fork_status = "ready"
                await self.db.update_session_field(fork_id, fork_status="ready")

            # Only the fully prepared candidate is visible to session commands.
            # Install its original owner in the same event-loop turn.
            self.sessions[fork_id] = fork
            self._session_owner(fork)

            # 9. Rewind, not branch: the fork takes the parent's place. Archive
            # the parent and surface the swap with the same `session_archived`
            # event `/archive` uses, so the fork slots into the sidebar wearing
            # the original's identity. The fork is already fully ready; an
            # archival hiccup must not fail it (worst case the parent lingers,
            # recoverable by a manual archive), so only announce the swap when
            # the parent actually went away.
            try:
                await self._archive_original(parent, successor=fork)
            except Exception:
                logger.exception(
                    "fork %s is ready; parent %s archive did not complete",
                    fork.id,
                    parent_id,
                )
            return fork
        finally:
            if self.sessions.get(parent.id) is parent:
                await self._release_session_reservation(owner, ReserveFork(False))

    @staticmethod
    def _fork_copy_base() -> str:
        """Base dir holding every /fork working-dir copy (session-fork.md)."""
        return os.path.expanduser(os.path.join("~", ".octopus", "fork"))

    @staticmethod
    def _fork_copy_dest(src_working_dir: str, fork_id: str) -> str:
        """Destination for a /fork working-dir copy: ~/.octopus/fork/<name>-<id>
        (session-fork.md). Keeps the project basename for readability +
        the fork id for uniqueness. Creates the base dir."""
        base = ForkingMixin._fork_copy_base()
        os.makedirs(base, exist_ok=True)
        name = os.path.basename(os.path.normpath(src_working_dir)) or "session"
        return os.path.join(base, f"{name}-{fork_id}")

    @staticmethod
    def _is_fork_copy_dir(working_dir: str | None) -> bool:
        """Check the copy path's scope, without granting deletion authority.

        A rewind can borrow a parent's working directory inside this scope.
        """
        if not working_dir:
            return False
        # Normalize both sides (expanduser + abspath) so a `~/.octopus/fork/...`
        # style row still classifies — otherwise it would leak rather than be
        # swept (Vera review hardening).
        def norm(p: str) -> str:
            return os.path.normpath(os.path.abspath(os.path.expanduser(p)))

        base = norm(ForkingMixin._fork_copy_base())
        wd = norm(working_dir)
        return wd != base and (wd + os.sep).startswith(base + os.sep)

    def _remove_fork_copy_directory(self, working_dir: str, metadata: dict[str, Any]) -> None:
        """Only an initializing full-copy saga can retire its own directory."""
        if metadata.get("full_copy") is not True:
            if self._is_fork_copy_dir(working_dir):
                logger.info("Retaining borrowed or unproven fork directory %s", working_dir)
            return
        if not self._is_fork_copy_dir(working_dir):
            raise ValueError("Incomplete fork copy is outside its directory scope")
        if metadata.get("owned_copy_dir") != working_dir:
            if not os.path.lexists(working_dir):
                return
            raise ValueError("Incomplete fork has no confirmed directory creation; retain recovery material")
        try:
            shutil.rmtree(working_dir)
        except FileNotFoundError:
            if os.path.lexists(working_dir):
                raise

    @staticmethod
    def _copy_tree(src: str, dest: str) -> None:
        # Literal full copy (the user's explicit choice — incl. .git /
        # node_modules / .venv). Symlinks copied AS symlinks so we don't follow
        # them into huge targets / loops.
        # The saga exclusively created and recorded dest before this writer.
        shutil.copytree(src, dest, symlinks=True, dirs_exist_ok=True)

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
        harness = get_harness(parent.backend)
        if not harness.can_fork:
            raise BackendForkNotSupported(parent.backend)

        # Same idle guard as fork_session — a copy of a mid-turn workspace would
        # be inconsistent, and the resume artifact needs a settled transcript.
        if self.session_is_executing(parent):
            raise ForkError("Parent session has an active turn",
                            reason="fork_blocked_parent_turn_active", status_code=409)
        owner = self._session_owner(parent)
        source = await owner.call_runtime(ReserveFork(True))

        fork_id = uuid.uuid4().hex[:12]
        resume_id_hint = str(uuid.uuid4())
        try:
            dest = self._fork_copy_dest(parent.working_dir, fork_id)
            # Snapshot the transcript AFTER claiming `_forking` (Vera review):
            # loading it earlier risks a fast turn slipping in between the read
            # and the guard, which would copy a post-turn working dir against a
            # pre-turn message list. With the guard held, both are consistent.
            messages = await self.db.load_messages(parent_id) if self.db else []
            last_seq = max((m["seq"] for m in messages), default=-1)
            # Resolve the EFFECTIVE credential (session override, else agent's)
            # only to locate the backend's on-disk transcript store for the copy
            # + cleanup — Codex needs its CODEX_HOME; Claude needs none (→ None).
            # require_auth=False: the rollout must be locatable even if the login
            # later lapses, and we make no API call here (Vera review).
            agent = await self._load_agent(parent)
            eff_cred_id = source.credential_id or (
                agent.get("credential_id") if agent else None
            )
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
            # credential later changes (Vera review). Omitted when there's none.
            fork_meta_dict: dict[str, Any] = {
                "full_copy": True, "duplicated_from": parent.name,
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
            #    `full_copy` flag, not by nulling fork_after_seq (Vera review).
            assert self.db is not None  # Original saga drains before DB close.
            await self.db.create_fork_session(
                fork_id=fork_id, name=fork_name, working_dir=dest,
                created_at=now, parent_id=parent_id, backend=parent.backend,
                model=source.model,
                reasoning_effort=source.reasoning_effort,
                service_tier=source.service_tier,
                user_id=parent.user_id,
                agent_id=parent.agent_id, credential_id=source.credential_id,
                resume_id=resume_id_hint, fork_after_seq=last_seq,
                fork_metadata=fork_meta,
            )
            fork = Session(
                id=fork_id, name=fork_name, working_dir=dest, created_at=now,
                claude_session_id=resume_id_hint, credential_id=source.credential_id,
                agent_id=parent.agent_id, origin="fork", backend=parent.backend,
                model=source.model,
                reasoning_effort=source.reasoning_effort,
                service_tier=source.service_tier,
                user_id=parent.user_id,
                forked_from_session_id=parent_id, fork_after_seq=last_seq,
                fork_metadata=fork_meta, fork_status="initializing",
            )
            fork.restore_message_state(len(messages), last_seq + 1, 0)

            # A preexisting destination belongs to somebody else. Exclusive
            # mkdir establishes creation; its committed receipt grants cleanup.
            # A crash between them retains the row and directory for inspection.
            try:
                os.mkdir(dest)
            except OSError as error:
                await self.db.delete_session(fork_id)
                raise ForkError(
                    f"Failed to allocate the working directory: {error}",
                    reason="copy_failed", status_code=500,
                ) from error
            fork_meta_dict["owned_copy_dir"] = dest
            fork.fork_metadata = json.dumps(fork_meta_dict)
            # Storage failure is not a copy failure: do not compensate a write
            # whose COMMIT is unconfirmed through a second mutation path.
            await self.db.update_session_field(fork_id, fork_metadata=fork.fork_metadata)

            # 2. Copy the directory, then prepare the native transcript
            #    with the WHOLE conversation as genuine context — no history
            #    replay dumped into the first prompt (session-fork.md). On a
            #    backend/parent with no transcript yet, the harness returns
            #    needs_replay=True and we fall back to the replay path.
            try:
                try:
                    # Cancellation ends this command only after its original
                    # thread exits. Neither compensation nor reservation release
                    # can race a writer still populating the destination.
                    await complete_cleanup(asyncio.to_thread(self._copy_tree, parent.working_dir, dest))
                except Exception as error:
                    raise ForkError(
                        f"Failed to copy the working directory: {error}",
                        reason="copy_failed", status_code=500,
                    ) from error
                artifact = await harness.prepare_fork_copy(
                    parent_working_dir=parent.working_dir,
                    parent_resume_id=parent.claude_session_id,
                    parent_credential=parent_cred,
                    dest_working_dir=dest,
                    new_resume_id=resume_id_hint,
                    hooks=self._fork_hooks(owner, fork, fork_meta_dict),
                )
            except BaseException:
                # Retire artifacts, then the owned directory, then its recovery
                # row. Failed cleanup retains responsibility for the next boot.
                try:
                    self.db.health.require_healthy()
                    await harness.cleanup_incomplete_fork_artifacts(
                        dest, fork.claude_session_id, fork_id, credential=parent_cred,
                        artifact=(read_fork_checkpoint(fork_meta_dict["native_fork"])
                                  if "native_fork" in fork_meta_dict else None),
                        hooks=self._fork_hooks(owner, fork, fork_meta_dict),
                    )
                    self._remove_fork_copy_directory(dest, fork_meta_dict)
                    await self.db.delete_session(fork_id)
                except Exception:
                    logger.exception(
                        "fork %s: cleanup failed; leaving row + copied "
                        "dir for next-boot retry",
                        fork_id,
                    )
                    raise
                raise

            # 4. Apply resume state + finalize. fork_after_seq stays = last_seq
            #    (replay cutoff, see above); the `full_copy` flag tells the UI to
            #    render "full copy of the working dir" instead of "@msg N".
            fork.claude_session_id = artifact.resume_id
            fork.fork_needs_replay = artifact.needs_replay
            fork.fork_status = "ready"
            # fork_metadata already holds full_copy/duplicated_from/cleanup id
            # from the INSERT — only the resume state + status change here.
            await self.db.update_session_field(
                fork_id,
                claude_session_id=artifact.resume_id,
                fork_needs_replay=artifact.needs_replay,
                fork_status="ready",
            )

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
            return fork
        finally:
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
        available, reason, _dirty = await fork_helpers.safe_revert_preflight(
            parent.working_dir,
            summary["agent_touched_paths"],
            target.get("git_head"),
            target.get("git_status_clean"),
        )
        return {
            "rewind_to_msg_seq": M,
            "prefilled_prompt": (
                target["content"] if isinstance(target["content"], str) else ""
            ),
            "side_effect_summary": summary,
            "revert": {"available": available, "refused_reason": reason},
            "can_fork": get_harness(parent.backend).can_fork,
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

        owner_id = await self.db.get_session_owner(session_id)
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
