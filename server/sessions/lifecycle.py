"""A session's life: create, archive, unarchive, import, delete, evict.

One slice of `SessionManager`, which was a single 4,017-line class with 84
methods (docs/post-mortems/2026-09-25-polish-pass.md). Split by responsibility; the
methods are unchanged and still reached through one `session_manager` object,
so no call site moved.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager, nullcontext
from datetime import UTC, datetime
from typing import Any

from ..attachments import AttachmentRecord, save_upload
from ..config import settings
from ..credential_bindings import validate_credential_binding
from ..db.applications import ApplicationDeleting, application_deletion_blocks_session
from ..db.users import owner_is_available as owner_is_available_in_transaction
from ..harness import get_harness
from ..knowledge import KnowledgeContext, OwnedTaskContext, receive_return
from ..models import (
    MessageContent,
    SessionArchiveFailureStage,
    SessionArchiveResult,
    SessionDetail,
    SessionInfo,
    SessionRestoreRequest,
    SessionRestoreStatus,
)
from ..users import owner_is_available
from ..workspace import confine
from .base import (
    _BACKEND_RETIRE_TIMEOUT,
    ForkError,
    Session,
    SessionManagerBase,
    _session_from_row,
    resolve_working_dir,
)
from .handoff import TICKET_BATCH_RETURN_STATES
from .owner import SessionOwner, SessionOwnerUnavailable
from .owner_runtime import (
    AutoArchive,
    CloseExecution,
    ReleaseApplication,
    ReleaseArchive,
)
from .presentation import session_info
from .restoration import RestoreOperation, RestoreRefused
from .runtime_types import SessionAdmissionBusy, SessionAdmissionClosed
from .submission import ReturnRefused

# The window an archived open returns, matching the live one in
# `routers/sessions.MESSAGE_WINDOW`. Declared here rather than imported to keep
# the slice free of a router import; the read-only manage view pages back
# through the same `…/messages` cursor.
_ARCHIVED_MESSAGE_WINDOW = 200


class SessionArchiveFailed(RuntimeError):
    """Archival committed; a later business step failed with these known facts."""

    def __init__(self, result: SessionArchiveResult):
        super().__init__(result.message)
        self.result = result


class LifecycleMixin(SessionManagerBase):

    async def save_attachment_upload(
        self, session: Session, *, user_id: str, filename: str, content: bytes, declared_mime: str | None,
    ) -> AttachmentRecord | None:
        """Publish an uploaded file only for its original live session and account."""
        db = self.input_database()
        async with db.transaction(write=False) as tx:
            account_available = await owner_is_available_in_transaction(tx, user_id)
            app_deleting = await application_deletion_blocks_session(tx, session.id)

        # Transaction exit releases the main DB writer lock without yielding
        # after the last driver wait. The final live check and file write stay
        # in the same event loop step, with file I/O outside the SQL deadline.
        if not account_available or self.get_session(session.id, user_id) is not session:
            return None
        owner = self._session_owners.get(id(session))
        if (app_deleting or self._admission_closed or session._admission_closed
                or session._archive_reservation is not None or self.session_deleting(session.id)
                or (owner is not None and owner.failed)):
            raise SessionAdmissionClosed("Session is closed for attachment uploads")
        return save_upload(session.id, filename, content, declared_mime, user_id=user_id)

    async def _close_original_execution(self, owner: SessionOwner, command: CloseExecution) -> bool:
        """Close through the original writer, or join its Manager handoff.

        Failed resource cleanup is separate from publisher drain: a subscriber
        may itself be waiting for this close. No replacement Owner is installed.
        """
        try:
            if not owner.failed:
                return await owner.call_runtime(command)
        except SessionOwnerUnavailable:
            if command.only_empty_application is not None:
                return False
            if not owner.failed:
                raise
        if command.only_empty_application is not None:
            return False  # A failed writer cannot certify this conditional close.
        async with asyncio.timeout(_BACKEND_RETIRE_TIMEOUT):
            retirement = await self._failed_owner_retirement(owner)
            if retirement is None:
                raise SessionOwnerUnavailable("Original Session resource cleanup is unavailable")
            session = owner.session
            if (session._forking and command.reason in {"archived", "deleted", "application_deleted"}
                    or command.reason in {"deleted", "application_deleted"} and session._archive_reservation is not None):
                raise ForkError("A fork or archive is still using this session; retry deletion when it completes",
                                reason="session_lifecycle_busy", status_code=409)
            if not owner.runtime.can_close_request(command.only_request_id):
                return False
            if command.reservation is not None:
                if (session._archive_reservation not in {None, command.reservation}
                        or self.session_deleting(session.id)):
                    raise ValueError(f"Session {session.id} is already being archived or deleted")
                session._archive_reservation = command.reservation
            await asyncio.shield(retirement)
            if self.db:
                self.db.health.require_healthy()
        return True

    async def retire_application_writers(self, session_ids: list[str]) -> None:
        # Close every original Session before draining sources that may return
        # to another captured Session. Archived histories stay unloaded.
        captured = [self.sessions[sid] for sid in session_ids if sid in self.sessions]

        async def close(session: Session) -> None:
            owner = self._session_owners.get(id(session)) or self._session_owner(session)
            await self._close_original_execution(owner, CloseExecution("application_deleted"))

        results = await asyncio.gather(*(close(session) for session in captured), return_exceptions=True)
        for result in results:
            if isinstance(result, BaseException):
                raise result
        for session in captured:
            await self.await_session_execution(session.id, timeout=_BACKEND_RETIRE_TIMEOUT)
            await self._join_session_owner_commands(session)
        for sid in session_ids:
            await self.retire_application_session_work(sid)

    async def release_application_writers(self, session_ids: list[str]) -> None:
        for sid in session_ids:
            session = self.sessions.get(sid)
            if session is not None:
                owner = self._session_owners.get(id(session))
                if owner is None:
                    # A file-cleanup retry can finish after startup without
                    # ever creating an Owner for the closed builder.
                    self._release_application_admission(session)
                elif not owner.failed:
                    await owner.call_runtime(ReleaseApplication())
                self.reopen_application_session_work(session.id)

    def _release_application_admission(self, session: Session) -> None:
        if (self.sessions.get(session.id) is session and session._archive_reservation is None
                and not self.session_deleting(session.id)):
            session._admission_closed = False

    async def retire_application_session_work(self, session_id: str) -> None:
        """Retire independent writers before their session relationship can vanish.

        Original owners retain startup and failed-cleanup handles. This method
        runs outside SessionOwner; no Owner waits for another Owner's work.
        """
        from ..bg_tasks import bg_task_manager
        from ..delegations import delegation_manager

        self._close_independent_session_work(session_id)
        if delegation_manager.db is self.db:
            await delegation_manager.retire_session(session_id)
        if bg_task_manager._db is self.db:
            await bg_task_manager.retire_session(session_id)

    def _close_independent_session_work(self, session_id: str, *, permanent: bool = False) -> None:
        from ..bg_tasks import bg_task_manager
        from ..delegations import delegation_manager
        from ..research import research_manager

        if bg_task_manager._db is self.db:
            bg_task_manager.close_session(session_id)
        if delegation_manager.db is self.db:
            delegation_manager.close_session(session_id, permanent=permanent)
        if permanent and research_manager.db is self.db:
            research_manager.release_session_publications(session_id)

    async def _retire_application_history(self, session: Session) -> None:
        if session.app_id is None and self.db is not None:
            session.app_id = await self.db.retain_application_session_owner(session.id)
        if session.app_id is not None:
            await self.retire_application_session_work(session.id)

    def reopen_application_session_work(self, session_id: str) -> None:
        from ..bg_tasks import bg_task_manager
        from ..delegations import delegation_manager

        if self.session_deleting(session_id) or session_id not in self.sessions:
            return
        if bg_task_manager._db is self.db:
            bg_task_manager.reopen_session(session_id)
        if delegation_manager.db is self.db:
            delegation_manager.reopen_session(session_id)

    def _session_lifecycle_lock(self, session_id: str) -> asyncio.Lock:
        lock = self._session_lifecycle_locks.get(session_id)
        if lock is None:
            lock = asyncio.Lock()
            self._session_lifecycle_locks[session_id] = lock
        return lock

    async def require_active_agent(self, agent_id: str | None) -> None:
        if agent_id in self._closed_agents:
            raise SessionAdmissionClosed("Agent admission is closed")
        if self.db is None:
            return
        agent = await self.db.get_agent_identity(agent_id) if agent_id is not None else None
        if agent is None or agent["archived"]:
            raise SessionAdmissionClosed("Agent not found or archived")
        if not await owner_is_available(self.db, agent["user_id"]):
            raise SessionAdmissionClosed("Account is unavailable")
        if agent_id in self._closed_agents:
            raise SessionAdmissionClosed("Agent admission is closed")

    @asynccontextmanager
    async def agent_operation(self, agent_id: str | None) -> AsyncIterator[None]:
        """Reserve agent work; archive joins it outside the lock.

        Global turn admission is checked by start_message, which also records
        a refused application/delegation attempt. Session restoration must
        remain available to confirm earlier receipts during shutdown recovery.
        """
        async with self._agent_lifecycle_lock:
            await self.require_active_agent(agent_id)
            completion = asyncio.get_running_loop().create_future()
            owners = self._agent_operations.setdefault(agent_id, set())
            owners.add(completion)
        try:
            yield
        finally:
            owners.discard(completion)
            completion.set_result(None)

    async def _run_agent_operation[T](
        self, agent_id: str | None, command: Callable[[], Awaitable[T]],
    ) -> T:
        """Keep the original lifecycle command alive when its waiter leaves."""
        async def run() -> T:
            async with self.agent_operation(agent_id):
                return await command()

        task = asyncio.create_task(run(), name=f"agent-operation-{agent_id}")
        self._own_task(task)
        return await asyncio.shield(task)

    @asynccontextmanager
    async def _retirement_operation(
        self, agent_id: str | None, *, only_request_id: str | None = None,
    ) -> AsyncIterator[None]:
        """Register cleanup without requiring the retired agent to stay open.

        Wiki shutdown closes a captured request after transition starts. That
        exact request is still checked by _retire_archived_session; an external
        archive or delete has no such cleanup authority.
        """
        async with self._agent_lifecycle_lock:
            completion = asyncio.get_running_loop().create_future()
            owners = self._agent_operations.setdefault(agent_id, set())
            owners.add(completion)
        try:
            yield
        finally:
            owners.discard(completion)
            completion.set_result(None)

    async def archive_agent(self, agent_id: str) -> None:
        from ..agent_manager import AgentError, AgentManager

        assert self.db is not None
        async with self._agent_lifecycle_lock:
            agent = await self.db.get_agent_identity(agent_id)
            if agent is None:
                raise AgentError("Agent not found")
            if agent["is_system"]:
                raise AgentError("The Default Agent cannot be archived")
            task = self._agent_archive_tasks.get(agent_id)
            if task is None or task.done():
                self._closed_agents.add(agent_id)

                async def finish() -> None:
                    assert self.db is not None  # This operation drains before DB close.
                    # No lifecycle lock spans creation, model retirement or DB
                    # settlement. New creation and old-session sends are closed.
                    pending = tuple(self._agent_operations.get(agent_id, ()))
                    if pending:
                        await asyncio.gather(*(asyncio.shield(item) for item in pending))
                    if self._schedule_runner is not None:
                        await self._schedule_runner.suspend_agent(agent_id)
                    await self.evict_agent_sessions(agent_id)
                    await AgentManager(self.db).archive_agent(agent_id)

                task = asyncio.create_task(finish(), name=f"archive-agent-{agent_id}")
                self._agent_archive_tasks[agent_id] = task
                self._own_task(task)
        await asyncio.shield(task)

    async def unarchive_agent(self, agent_id: str, user_id: str | None) -> dict[str, Any]:
        from ..agent_manager import AgentError, AgentManager

        assert self.db is not None
        async with self._agent_lifecycle_lock:
            agent = await self.db.get_agent_identity(agent_id, user_id=user_id)
            if agent is None or agent.get("user_id") != user_id:
                raise AgentError("Agent not found")
            operation = self._agent_archive_tasks.get(agent_id)
            if operation is not None:
                if not operation.done() or operation.cancelled() or operation.exception() is not None:
                    raise ValueError("Agent archive retirement is not confirmed")
            agent = await AgentManager(self.db).unarchive_agent(agent_id, user_id)
            self._closed_agents.discard(agent_id)
            self._agent_archive_tasks.pop(agent_id, None)
        if self._schedule_runner is not None:
            await self._schedule_runner.restore_agent(agent_id)
        return agent

    async def _owner_extra_roots(self, user_id: str | None) -> list[str]:
        """The additional paths this account may work in (§6).

        A single-box affordance: the first user develops Octopus itself at a
        path outside any workspace, and moving that repository is not something
        accounts should force. Empty for everyone in the cloud version.
        """
        if not user_id or not self.db:
            return []
        user = await self.db.get_user(user_id)
        raw = (user or {}).get("extra_roots")
        if not raw:
            return []
        try:
            roots = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return []
        return [str(r) for r in roots] if isinstance(roots, list) else []

    async def create_session(
        self, agent_id: str | None, name: str | None = None,
        working_dir: str | None = None, credential_id: str | None = None,
        origin: str = "user", backend: str | None = None,
        parent_session_id: str | None = None, delegation_request: str | None = None,
        app_id: str | None = None,
    ) -> Session:
        self.input_database()
        return await self._run_agent_operation(
            agent_id, lambda: self._create_session(
                agent_id, name, working_dir, credential_id, origin, backend,
                parent_session_id, delegation_request, app_id,
            ),
        )

    async def _create_session(
        self,
        agent_id: str | None,
        name: str | None = None,
        working_dir: str | None = None,
        credential_id: str | None = None,
        origin: str = "user",
        backend: str | None = None,
        parent_session_id: str | None = None,
        delegation_request: str | None = None,
        app_id: str | None = None,
        *, persist: bool = True, register: bool = True,
    ) -> Session:
        """Create a conversation thread owned by `agent_id`.

        A session is *an instance of talking to an agent* (agent-refactor.md
        §5.2). Refuses a missing/unknown agent. `working_dir` defaults to
        `settings.default_working_dir` — agents are not path-aware. `name`
        defaults to a generated "{agent} — {timestamp}" label.

        `parent_session_id` + `delegation_request` are set together for
        agent-to-agent delegation children (agent-collaboration.md §4.1):
        the child carries a pointer back to the parent session and a
        verbatim copy of the original delegation prompt for UI display.
        """
        if not agent_id:
            raise ValueError("agent_id is required to create a session")
        agent = await self.db.get_agent(agent_id) if self.db else None
        if self.db and agent is None:
            raise ValueError(f"Agent {agent_id} not found")
        backend = backend or (agent or {}).get("backend") or settings.default_backend
        get_harness(backend)  # Reject unknown kinds rather than a silent fallback.
        if self.db:
            await validate_credential_binding(
                self.db, credential_id or (agent or {}).get("credential_id"),
                backend, (agent or {}).get("user_id"),
            )

        if not name:
            label = (agent or {}).get("name", "Agent")
            stamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M")
            name = f"{label} — {stamp}"

        owner_id = (agent or {}).get("user_id")
        extra_roots = await self._owner_extra_roots(owner_id)
        sid = uuid.uuid4().hex[:12]
        session = Session(
            id=sid,
            name=name,
            # Confined to the owner's workspace (multi-tenancy.md §6), then
            # frozen absolute as it always was — in that order, because a path
            # that escapes must be refused before it is stored, not after.
            working_dir=resolve_working_dir(
                confine(working_dir, user_id=owner_id, extra_roots=extra_roots)
            ),
            credential_id=credential_id,
            agent_id=agent_id,
            origin=origin,
            backend=backend,
            parent_session_id=parent_session_id,
            delegation_request=delegation_request,
            app_id=app_id,
            # A conversation belongs to whoever owns the agent having it
            # (multi-tenancy.md §5). Derived rather than passed in, so every
            # path that creates a session — a route, a schedule firing, a
            # delegation, an application build — gets the owner right without
            # each of them having to remember. Persistent business admission
            # requires that agent's current account.
            user_id=owner_id,
        )
        if self.db and persist:
            await self.db.save_session(
                session_id=session.id,
                name=session.name,
                working_dir=session.working_dir,
                created_at=session.created_at,
                claude_session_id=session.claude_session_id,
                credential_id=session.credential_id,
                agent_id=session.agent_id,
                origin=session.origin,
                backend=session.backend,
                parent_session_id=session.parent_session_id,
                delegation_request=session.delegation_request,
                app_id=session.app_id,
                user_id=session.user_id,
            )
        if persist and register:
            self.sessions[sid] = session
        return session

    async def import_session(
        self, name: str, working_dir: str | None = None,
        claude_session_id: str | None = None, credential_id: str | None = None,
        messages: list[MessageContent] | None = None, agent_id: str | None = None,
        origin: str = "user", backend: str | None = None,
    ) -> Session:
        return await self._run_agent_operation(
            agent_id, lambda: self._import_session(
                name, working_dir, claude_session_id, credential_id, messages,
                agent_id, origin, backend,
            ),
        )

    async def _import_session(
        self,
        name: str,
        working_dir: str | None = None,
        claude_session_id: str | None = None,
        credential_id: str | None = None,
        messages: list[MessageContent] | None = None,
        agent_id: str | None = None,
        origin: str = "user",
        backend: str | None = None,
    ) -> Session:
        agent = await self.db.get_agent(agent_id) if (self.db and agent_id) else None
        backend = backend or (agent or {}).get("backend") or settings.default_backend
        get_harness(backend)
        if self.db:
            await validate_credential_binding(
                self.db, credential_id or (agent or {}).get("credential_id"),
                backend, (agent or {}).get("user_id"),
            )
        owner_id = (agent or {}).get("user_id")
        extra_roots = await self._owner_extra_roots(owner_id)
        sid = uuid.uuid4().hex[:12]
        session = Session(
            id=sid,
            name=name,
            working_dir=resolve_working_dir(
                confine(working_dir, user_id=owner_id, extra_roots=extra_roots)
            ),
            claude_session_id=claude_session_id,
            credential_id=credential_id,
            agent_id=agent_id,
            origin=origin,
            backend=backend,
            # As in `create_session`: an imported transcript belongs to whoever
            # owns the agent it is imported under.
            user_id=owner_id,
        )
        if self.db:
            await self.db.save_session(
                session_id=session.id,
                name=session.name,
                working_dir=session.working_dir,
                created_at=session.created_at,
                claude_session_id=session.claude_session_id,
                credential_id=session.credential_id,
                agent_id=session.agent_id,
                origin=session.origin,
                backend=session.backend,
                user_id=session.user_id,
            )
        if messages:
            for msg in messages:
                await self._persist_message(session, msg)
        self.sessions[sid] = session
        return session

    async def archive_session(
        self, session_id: str, *, replace: bool = True, only_request_id: str | None = None,
    ) -> Session | None:
        """Hide the current session and return a fresh one with the same
        user-visible settings (name / working_dir / credential_id).

        `replace=False` is the sidebar's archive button (sidebar-pins.md §6):
        the session is put away and nothing takes its place, so this returns
        None. A schedule that was set up from it then fires into throwaway
        sessions — there is no successor thread for it to follow.

        The old session row stays in the DB (with `archived = 1`) so the
        message history isn't lost — it just disappears from the default
        sessions list. The new session starts with no `claude_session_id`
        so the CLI begins a clean conversation, under the same agent.

        Agent-owned schedules follow the successor when their origin is rebound.

        If the old session has a running turn, it's interrupted first.
        An automatic owner may supply only_request_id: under the admission
        lock, any later request leaves the session open and is never stopped.
        """
        old = self.sessions.get(session_id)
        if old is None:
            raise ValueError(f"Session {session_id} not found")

        task = asyncio.create_task(self._archive_live_session(old, replace=replace, only_request_id=only_request_id),
                                   name=f"archive-session-{session_id}")
        self._own_task(task)
        return await asyncio.shield(task)

    async def _retire_archived_session(
        self, session: Session, reservation: object, *, only_request_id: str | None = None,
    ) -> bool:
        """Close explicit admission and finish captured work before hiding it."""
        owner = self._session_owners.get(id(session)) or self._session_owner(session)
        accepted = await self._close_original_execution(owner, CloseExecution("archived", reservation, only_request_id))
        if not accepted:
            return False
        await self.await_session_execution(session.id, timeout=_BACKEND_RETIRE_TIMEOUT)
        await self._join_session_owner_commands(session)
        return True

    async def _archive_live_session(
        self, old: Session, *, replace: bool, only_request_id: str | None = None,
    ) -> Session | None:
        operation = (
            self.agent_operation(old.agent_id) if replace
            else self._retirement_operation(old.agent_id, only_request_id=only_request_id)
        )
        async with operation:
            archived, successor = await self._archive_original(old, replace=replace, only_request_id=only_request_id)
            if not archived and only_request_id is None:
                raise ValueError(f"Session {old.id} not found")
            return successor

    async def _archive_original(
        self, session: Session, *, replace: bool = False, successor: Session | None = None,
        only_request_id: str | None = None, automatic: bool = False,
        release_notice: asyncio.Future[None] | None = None,
        settle_relationship: Callable[[], None] | None = None,
    ) -> tuple[bool, Session | None]:
        # A competing explicit archive cannot wait on the operation whose gate
        # its caller may own. The lock covers the window before the Owner has
        # processed the first archive's reservation command.
        lifecycle_lock = self._session_lifecycle_lock(session.id)
        if self.sessions.get(session.id) is session and (
            lifecycle_lock.locked() or session._archive_reservation is not None
        ):
            if automatic:
                return False, None
            raise ValueError(f"Session {session.id} is already being archived")
        relationship = (self._session_relationship(session.id).changing() if settle_relationship is None
                        else nullcontext(settle_relationship))
        with relationship as settle:
            async with lifecycle_lock:
                if self.sessions.get(session.id) is not session:
                    return False, None
                if self.session_deleting(session.id):
                    if automatic:
                        return False, None
                    raise ValueError(f"Session {session.id} is being deleted")
                archived = await self._archive_reserved_session(
                    session, only_request_id=only_request_id, automatic=automatic,
                )
            if archived is None:
                return False, None
            replacing = replace or successor is not None
            new, failure = await self._prepare_archive_successor(session, successor) if replacing else (None, None)
            settle()

        # Subscribers can restore the same session or wait for a fire that
        # uses the settled relationship.
        event: dict[str, Any] = {
            "type": "session_archived",
            "old_session_id": session.id,
            "new_session_id": new.id if new else None,
            "name": new.name if new else session.name,
            "dispatch_state": archived,
        }
        if replacing:
            event.update(model=new.model if new else None,
                         reasoning_effort=new.reasoning_effort if new else None,
                         service_tier=new.service_tier if new else None)
        if failure is not None:
            event["archive_result"] = failure.result.model_dump()
        await self._publish_lifecycle(event, user_id=session.user_id, wrap_required_cancellation=automatic,
                                      release_notice=release_notice)
        if failure is not None:
            raise failure
        return True, new

    async def _archive_reserved_session(
        self, session: Session, *,
        only_request_id: str | None = None, automatic: bool = False,
    ) -> dict[str, Any] | None:
        """Own one reservation, resource deadline, archive effect and release.

        Explicit close may interrupt; autoarchive instead keeps its original
        idle/revision decision and rolls back a refused database projection.
        """
        owner = self._session_owners.get(id(session)) or self._session_owner(session)
        decision = None
        reservation = object()
        if automatic:
            if owner.failed:
                return None
            operation = asyncio.current_task()
            assert operation is not None
            decision = await owner.call_runtime(AutoArchive(operation))
            if decision is None:
                return None
            reservation = decision.reservation
        try:
            if not automatic:
                accepted = await self._close_original_execution(owner, CloseExecution("archived", reservation, only_request_id))
                if not accepted:
                    return None
            await self.await_session_execution(session.id, timeout=_BACKEND_RETIRE_TIMEOUT)
            await self._join_session_owner_commands(session)
            await self._retire_application_history(session)
            if automatic and owner.failed:
                return None
            if self.db:
                await self.db.update_session_field(session.id, archived=True)
            if decision is not None:
                try:
                    removed = await owner.call_runtime(decision)
                except SessionOwnerUnavailable:
                    if not owner.failed:
                        raise
                    removed = False
                if not removed:
                    if self.db:
                        await self.db.update_session_field(session.id, archived=False)
                    return None
            else:
                self.sessions.pop(session.id, None)
                self._session_removed(session)
            snapshot = self._session_dispatch_snapshot(session)
            snapshot["revision"] += 1
            self._archived_dispatch[session.id] = (session._dispatch_paused, snapshot["revision"], session._dispatch_order)
            return snapshot
        finally:
            if self.sessions.get(session.id) is session and self._session_owners.get(id(session)) is owner:
                await self._release_session_reservation(owner, ReleaseArchive(reservation))

    async def _prepare_archive_successor(
        self, old: Session, new: Session | None,
    ) -> tuple[Session | None, SessionArchiveFailed | None]:
        """Finish the durable relationship before subscribers see the archive."""
        # New session inherits agent / name / working_dir / credential_id /
        # origin but starts with no claude_session_id (fresh conversation).
        stage: SessionArchiveFailureStage = "successor_creation"
        try:
            if new is None:
                # Credential deletion propagates through the live registry.
                # Keep private creation, initialization and registration inside
                # its existing configuration boundary so it cannot miss us.
                async with self.input_database().configuration_lock:
                    new = await self._create_session(
                        agent_id=old.agent_id, name=old.name, working_dir=old.working_dir,
                        credential_id=old.credential_id, origin=old.origin, backend=old.backend, app_id=old.app_id,
                        register=False,
                    )
                    stage = "successor_configuration"
                    try:
                        if self.db:
                            await self.db.update_session_field(new.id, model=old.model,
                                reasoning_effort=old.reasoning_effort, service_tier=old.service_tier)
                        new.model, new.reasoning_effort, new.service_tier = old.model, old.reasoning_effort, old.service_tier
                    finally:
                        # Creation already committed. A failed configuration write
                        # retains that real successor with its actual saved defaults.
                        self.sessions[new.id] = new

            # Rebinding changes only the origin; keep the schedule's cadence
            # and the registered Runner's ownership unchanged.
            stage = "schedule_rebinding"
            if self.db:
                await self.db.repoint_schedules_origin(old.id, new.id)
        except Exception as error:
            failure = SessionArchiveFailed(SessionArchiveResult(
                old_session_id=old.id, new_session_id=new.id if new is not None else None,
                failure_stage=stage, message=str(error),
            ))
            failure.__cause__ = error
            return new, failure
        return new, None

    async def auto_archive_scheduled_session(
        self, session_id: str, *, expected_session: Session | None = None, release_notice: asyncio.Future[None] | None = None,
    ) -> bool:
        """Apply the schedule or abandoned-delegation-setup archive policy.

        Unlike `archive_session`, no replacement thread is created — the
        next fire or fresh delegation creates its own session. Delegation
        setup cleanup calls this only before admission; completing an admitted
        round never requests archival. No-op if the session is gone,
        not auto-archivable by origin, or still running. Returns True
        if it archived.
        """
        session = self.sessions.get(session_id)
        if (
            session is None
            or expected_session is not None and session is not expected_session
            or session.origin not in self._AUTO_ARCHIVE_ELIGIBLE
        ):
            return False
        task = asyncio.create_task(self._archive_original(session, automatic=True, release_notice=release_notice),
                                   name=f"auto-archive-{session.id}")
        self._own_task(task)
        archived, _ = await asyncio.shield(task)
        return archived

    async def evict_agent_sessions(self, agent_id: str) -> list[str]:
        """Drop all live sessions owned by an agent from the in-memory map
        (used before the agent's archive is persisted). Stops any running turn
        and confirms result recording before removing its live sessions.
        Returns the evicted session ids.
        """
        task = asyncio.create_task(self._evict_agent_sessions(agent_id),
                                   name=f"evict-agent-sessions-{agent_id}")
        self._own_task(task)
        return await asyncio.shield(task)

    async def _evict_agent_sessions(self, agent_id: str) -> list[str]:
        sessions = [session for session in self.sessions.values() if session.agent_id == agent_id]
        reservations = {session.id: object() for session in sessions}
        try:
            for session in sessions:
                if self.sessions.get(session.id) is session:
                    await self._retire_archived_session(session, reservations[session.id])
            # A storage failure in any captured request keeps every session
            # available for verification, instead of partially evicting them.
            evicted = []
            for session in sessions:
                if self.sessions.get(session.id) is not session:
                    continue
                self.sessions.pop(session.id, None)
                self._session_removed(session)
                evicted.append(session.id)
                await self._broadcast(
                    {"type": "session_archived", "old_session_id": session.id,
                     "new_session_id": None, "name": session.name},
                    user_id=session.user_id,
                )
            return evicted
        finally:
            for session in sessions:
                if self.sessions.get(session.id) is session:
                    owner = self._session_owners.get(id(session))
                    if owner is not None:
                        await self._release_session_reservation(owner, ReleaseArchive(reservations[session.id]))

    async def list_archived_sessions(
        self, user_id: str | None = None
    ) -> list[dict[str, Any]]:
        """Return SessionInfo-shaped dicts for every archived DB row.

        Pulled lazily from the DB (archived sessions aren't kept in the
        in-memory `self.sessions` map), so the owner filter is the database's
        job here rather than `list_sessions`'. Caller turns them into Pydantic
        models for the response.
        """
        database = self.input_database()
        rows = await database.load_archived_sessions(user_id=user_id)
        out: list[dict[str, Any]] = []
        for row in rows:
            out.append(session_info(row, row["message_count"], archived=True,
                                    transcript_integrity="duplicate_seq" if row["duplicate_seqs"] else "ok").model_dump())
        return out

    async def load_archived_session_info(
        self, session_id: str, user_id: str | None = None,
    ) -> SessionInfo | None:
        rows = await self.input_database().load_archived_sessions(user_id=user_id, session_id=session_id)
        if not rows:
            return None
        row = rows[0]
        return session_info(row, row["message_count"], archived=True,
                            transcript_integrity="duplicate_seq" if row["duplicate_seqs"] else "ok")

    async def load_archived_session_detail(
        self, session_id: str, user_id: str | None = None
    ) -> SessionDetail | None:
        """Read full message history for an archived session straight
        from the DB. Returns None if the id isn't an archived row — or isn't
        one of `user_id`'s, which a caller must not be able to tell apart.
        """
        database = self.input_database()
        rows = await database.load_archived_sessions(user_id=user_id, session_id=session_id)
        match = rows[0] if rows else None
        if match is None:
            return None
        # Windowed like a live open, and counted separately: `len(messages)`
        # stopped being the message count once a window is all we fetch
        # (polish-2026-09.md §4 B2).
        messages_raw = await database.load_messages(
            session_id, limit=_ARCHIVED_MESSAGE_WINDOW, newest_first=True
        )
        messages = [MessageContent(**m) for m in messages_raw]
        total, duplicates = match["message_count"], match["duplicate_seqs"]
        if duplicates:
            messages = []
        oldest = messages[0].seq if messages else None
        return SessionDetail(
            **session_info(match, total, archived=True, transcript_integrity="duplicate_seq" if duplicates else "ok").model_dump(),
            context_state=_session_from_row(match).context_state,
            context_generation=self._dispatch_generation,
            messages=messages,
            pending_questions=[],
            oldest_loaded_seq=oldest,
            has_more_messages=await database.has_messages_before(session_id, oldest),
            next_message_seq=max((m.seq for m in messages if m.seq is not None), default=-1) + 1,
        )

    async def join_browser_restorations(self) -> None:
        # Initialization joins originals before replacing locks, owners or DB.
        tasks = [record.task for record in self._browser_restorations.values() if record.task is not None]
        if tasks:
            await asyncio.gather(*(asyncio.shield(task) for task in tasks), return_exceptions=True)

    async def _browser_restore_target(self, session_id: str, user_id: str | None) -> dict[str, Any]:
        binding = await self.db.get_session_binding(session_id) if self.db else None
        if binding is None or user_id is not None and binding["user_id"] != user_id:
            raise RestoreRefused("Session not found", 404)
        return binding

    def _browser_restore_record(self, session_id: str, user_id: str | None, operation_id: str) -> RestoreOperation | None:
        record = self._browser_restorations.get(operation_id)
        if record is not None and (record.actor, record.session_id) != (user_id, session_id):
            raise RestoreRefused("Restore identity not found", 404)
        return record

    def _browser_restore_status(self, operation_id: str | None, record: RestoreOperation | None,
                                state: str = "not_registered") -> SessionRestoreStatus:
        return SessionRestoreStatus.model_validate({
            "service_generation": record.generation if record else self._dispatch_generation, "operation_id": operation_id,
            "state": record.state if record else state,
            "status_code": record.status_code if record else None, "message": record.message if record else None,
        })

    async def browser_restore_status(self, session_id: str, user_id: str | None,
                                     request: SessionRestoreRequest | None = None, *, seal: bool = False) -> SessionRestoreStatus:
        if request is not None and request.service_generation == self._dispatch_generation:
            record = self._browser_restore_record(session_id, user_id, request.operation_id)
            if record is not None:
                return self._browser_restore_status(request.operation_id, record)
        await self._browser_restore_target(session_id, user_id)
        if request is None:
            operation_id = self._active_browser_restorations.get((user_id, session_id))
            record = self._browser_restorations.get(operation_id) if operation_id else None
            return self._browser_restore_status(operation_id, record, "idle")
        if request.service_generation != self._dispatch_generation:
            return self._browser_restore_status(request.operation_id, None,
                "generation_retired" if self._restore_previous_writers_retired else "generation_unknown")
        # Registration and sealing linearize here, after the last storage await.
        record = self._browser_restore_record(session_id, user_id, request.operation_id)
        if record is None and seal:
            record = RestoreOperation(user_id, session_id, request.service_generation, "sealed")
            self._browser_restorations[request.operation_id] = record
        return self._browser_restore_status(request.operation_id, record)

    async def browser_restore(self, session_id: str, user_id: str | None,
                               request: SessionRestoreRequest) -> SessionRestoreStatus:
        if request.service_generation != self._dispatch_generation:
            await self._browser_restore_target(session_id, user_id)
            raise RestoreRefused("Restore service generation changed; confirm the original identity", 409)
        record = self._browser_restore_record(session_id, user_id, request.operation_id)
        if record is None:
            binding = await self._browser_restore_target(session_id, user_id)
            # No await separates readiness, shutdown/generation checks and
            # registration. Internal task returns keep their distinct admission.
            if (self._input_state != "ready" or self._admission_closed
                    or request.service_generation != self._dispatch_generation):
                raise RestoreRefused("Browser restoration admission is closed", 503)
            record = self._browser_restore_record(session_id, user_id, request.operation_id)
            if record is None:
                record = RestoreOperation(user_id, session_id, request.service_generation, "running")
                self._browser_restorations[request.operation_id] = record
                if (user_id, session_id) in self._active_browser_restorations:
                    record.state, record.status_code, record.message = "finished", 409, "Another restoration is still running"
                else:
                    self._active_browser_restorations[user_id, session_id] = request.operation_id

                    async def run() -> tuple[int, str] | None:
                        try:
                            async with self.agent_operation(binding["agent_id"]):
                                await self._unarchive_session(session_id, user_id)
                            return None
                        except ValueError as error:
                            return 404, str(error)
                        except ForkError as error:
                            return error.status_code, str(error)
                        except Exception:
                            return 500, "Restoration could not finish. Confirm the current session state."

                    task = asyncio.create_task(run(), name=f"browser-restore-{session_id}")
                    record.task = task
                    original = record

                    def ended(done: asyncio.Task[tuple[int, str] | None]) -> None:
                        result = (500, "Restoration ended without a confirmed result") if done.cancelled() else done.result()
                        original.state, original.task = "finished", None
                        if result is not None:
                            original.status_code, original.message = result
                        if self._active_browser_restorations.get((user_id, session_id)) == request.operation_id:
                            self._active_browser_restorations.pop((user_id, session_id))

                    task.add_done_callback(ended)
                    self._own_task(task)
        if record.task is not None:
            await asyncio.shield(record.task)
        return self._browser_restore_status(request.operation_id, record)

    async def unarchive_session(
        self, session_id: str, user_id: str | None = None, *, release_notice: asyncio.Future[None] | None = None,
    ) -> Session:
        binding = await self.db.get_session_binding(session_id) if self.db else None
        if binding is None or (user_id is not None and binding["user_id"] != user_id):
            raise ValueError(f"Archived session {session_id} not found")
        return await self._run_agent_operation(
            binding["agent_id"], lambda: self._unarchive_session(session_id, user_id, release_notice=release_notice),
        )

    async def _publish_lifecycle(
        self, event: dict[str, Any], *, user_id: str | None, returning: bool = False,
        release_notice: asyncio.Future[None] | None = None, wrap_required_cancellation: bool = False,
    ) -> None:
        """Publish the committed change to its captured audience. An independent return ends at handoff."""
        notice = self._broadcast(event, user_id=user_id, wrap_required_cancellation=wrap_required_cancellation)
        if not returning and release_notice is None:
            await notice
            return
        task = asyncio.create_task(notice, name=f"lifecycle-{event['type']}")
        self._own_task(task)
        if release_notice is not None and not release_notice.done():
            await asyncio.wait((task, release_notice), return_when=asyncio.FIRST_COMPLETED)
            if not release_notice.done():
                task.result()

    async def _unarchive_session(
        self, session_id: str, user_id: str | None = None, *, returning: OwnedTaskContext | None = None,
        wait_for_lifecycle: bool = True, release_notice: asyncio.Future[None] | None = None,
    ) -> Session:
        lock = self._session_lifecycle_lock(session_id)
        if not wait_for_lifecycle and lock.locked():
            raise SessionAdmissionBusy("Original session lifecycle is busy")
        async with lock:
            if self.session_deleting(session_id):
                raise SessionAdmissionClosed("Session is being deleted")
            if returning is not None:
                self._require_task_return(returning, session_id, user_id)
            current = self.sessions.get(session_id)
            if returning is not None and current is not None:
                if current._archive_reservation is not None or current._forking:
                    raise SessionAdmissionBusy("Original session is busy (forking or archiving)")
                if current.user_id != user_id or current._admission_closed or self.session_owner_failed(session_id):
                    raise SessionAdmissionClosed("Original session admission is unavailable")
                return current
            session = await self._restore_archived_session(session_id, user_id, returning=returning)
        await self._publish_lifecycle({
            "type": "session_unarchived", "session_id": session.id, "name": session.name,
            "session": session_info(session).model_dump(mode="json"), "dispatch_state": self._session_dispatch_snapshot(session),
        }, user_id=session.user_id, returning=returning is not None, release_notice=release_notice)
        return session

    async def _restore_archived_session(
        self, session_id: str, user_id: str | None = None, *, returning: OwnedTaskContext | None = None,
    ) -> Session:
        """Flip archived=0 in the DB and reload the row into memory.

        Refuses unknown / non-archived ids with ValueError — and an id that
        belongs to another account is one of those, indistinguishably, which is
        what keeps a 404 from confirming it exists.
        """
        if self.db is None:
            raise ValueError("DB not initialized")
        await self.db.require_application_session_writable(session_id)
        rows = await self.db.load_archived_sessions(user_id=user_id, session_id=session_id)
        match = rows[0] if rows else None
        if match is None:
            raise SessionAdmissionClosed(f"Archived session {session_id} not found")
        await self.db.verify_ticket_acceptance_limits(session_id)
        message_state = match["message_count"], match["next_message_seq"], match["duplicate_seqs"]
        if returning is not None:
            await self.require_active_agent(match["agent_id"])
            binding = returning.return_binding
            if not await self.db.request_in_model_history(session_id, binding.reference.request_id, binding.captured_after_seq):
                raise SessionAdmissionClosed("The calling ticket has handed off its model context")
            origin = binding.context.origin
            if origin.session_id == session_id:
                batch = await self.db.ticket_batch_state(session_id, origin.request_id)
                if batch is not None and (batch not in TICKET_BATCH_RETURN_STATES
                        or await self.db.handoff_human_executions(session_id, origin.request_id) is None):
                    raise SessionAdmissionClosed("The calling ticket batch is paused or its human authorization has changed")
            # Recheck the process-local owner after the storage reads as well.
            self._require_task_return(returning, session_id, user_id)
        await self.db.update_session_field(session_id, archived=False)
        # Reload into the in-memory map so writes (sendMessage etc.)
        # immediately route to this session.
        session = _session_from_row(match)
        paused, revision, order = self._archived_dispatch.pop(session_id, (False, 0, 0))
        session._dispatch_paused = paused
        session._dispatch_revision, session._dispatch_order = revision + 1, order
        session.restore_message_state(*message_state)
        self.sessions[session.id] = session
        self.reopen_application_session_work(session.id)
        from .owner_runtime import ReadContext

        await self._session_owner(session).call_runtime(ReadContext())
        return session

    def _require_task_return(self, owned: OwnedTaskContext, session_id: str, user_id: str | None) -> None:
        if (self._admission_closed or self.session_deleting(session_id) or self.session_owner_failed(session_id)
                or receive_return(owned, session_id=session_id, user_id=user_id,
                                  work_id=owned.return_binding.work_id) is None):
            raise SessionAdmissionClosed("Original task return admission is unavailable")

    async def _task_return_eligibility(
        self, owned: OwnedTaskContext | None, *, session_id: str, work_id: str | None,
    ) -> tuple[str | None, KnowledgeContext] | ReturnRefused:
        """Check without restoring membership or reserving execution resources."""
        if owned is None:
            return ReturnRefused("binding_unavailable")
        binding = owned.return_binding
        reference = binding.reference
        if reference.session_id != session_id or binding.work_id != work_id:
            return ReturnRefused("binding_unavailable")
        context = receive_return(owned, session_id=session_id, user_id=reference.user_id, work_id=work_id)
        if self._admission_closed or self.session_deleting(session_id) or context is None:
            return ReturnRefused("admission_closed")
        if self.session_owner_failed(session_id):
            return ReturnRefused("destination_unavailable", SessionOwnerUnavailable("Destination owner is unavailable"))
        row = await self.input_database().get_session_binding(session_id)
        if row is None or row["user_id"] != reference.user_id:
            return ReturnRefused("destination_unavailable")
        if (context.origin.session_id == session_id
                and await self.input_database().ticket_input_has_joined_batch(session_id, context.origin.request_id)):
            return ReturnRefused("admission_closed", SessionAdmissionClosed("The original input joined a ticket batch"))
        if not await self.input_database().request_in_model_history(session_id, reference.request_id, binding.captured_after_seq):
            return ReturnRefused("admission_closed", SessionAdmissionClosed("The calling ticket has handed off its model context"))
        await self.require_active_agent(row["agent_id"])
        return row["agent_id"], context

    async def _restore_task_return(
        self, owned: OwnedTaskContext | None, *, session_id: str, work_id: str | None,
        wait_for_lifecycle: bool = True,
    ) -> tuple[Session, KnowledgeContext] | None:
        """Restore only the fixed, still-authorized caller of independent work."""
        if owned is None:
            return None
        binding = owned.return_binding
        reference = binding.reference
        try:
            eligibility = await self._task_return_eligibility(owned, session_id=session_id, work_id=work_id)
            if isinstance(eligibility, ReturnRefused):
                return None
            agent_id, _ = eligibility
            session = await self._run_agent_operation(agent_id, lambda: self._unarchive_session(
                reference.session_id, reference.user_id, returning=owned, wait_for_lifecycle=wait_for_lifecycle,
            ))
        except SessionAdmissionBusy:
            raise
        except (SessionAdmissionClosed, ApplicationDeleting):
            return None
        context = receive_return(owned, session_id=session.id, user_id=session.user_id, work_id=binding.work_id)
        return (session, context) if context is not None else None

    async def delete_session(
        self, session_id: str, *, only_empty_application: str | None = None, expected_session: Session | None = None,
    ) -> bool:
        session = self.sessions.get(session_id)
        if only_empty_application is not None:
            if session is None:
                return False
            conditional = asyncio.create_task(
                self._delete_empty_application_session(session, only_empty_application),
                name=f"delete-empty-session-{session_id}",
            )
            self._own_task(conditional)
            return await asyncio.shield(conditional)
        if session is not None and (session._archive_reservation is not None or session._forking):
            raise self._session_deletion_busy()
        async with self._session_lifecycle_lock(session_id):
            session = self.sessions.get(session_id)
            if expected_session is not None and session is not expected_session:
                return False
            audience: str | None
            if session is not None:
                if session._forking or session._archive_reservation is not None:
                    raise self._session_deletion_busy()
                audience, task = session.user_id, session._deletion_task
            elif archived := self._archived_deletions.get(session_id):
                audience, task = archived
            else:
                binding = await self.db.get_session_binding(session_id) if self.db is not None else None
                if binding is None or not binding["archived"]:
                    return False
                audience, task = binding["user_id"], None
            if task is None or task.done():
                if audience is None:
                    raise ValueError("The session has no attachment owner")
                self._close_independent_session_work(session_id, permanent=True)
                task = asyncio.create_task(self._delete_captured_session(session_id, session, audience),
                                           name=f"delete-session-{session_id}")
                if session is None:
                    self._archived_deletions[session_id] = (audience, task)
                else:
                    session._deletion_task = task
                self._own_task(task)
        return await asyncio.shield(task)

    @staticmethod
    def _session_deletion_busy() -> ForkError:
        return ForkError("A fork or archive is still using this session; retry deletion when it completes",
                         reason="session_lifecycle_busy", status_code=409)

    async def _delete_empty_application_session(self, session: Session, app_id: str) -> bool:
        async with self._retirement_operation(session.agent_id):
            owner = self._session_owners.get(id(session)) or self._session_owner(session)
            if not await self._close_original_execution(owner, CloseExecution("deleted", only_empty_application=app_id)):
                return False
            # A successful proof can outlive the live registry entry. Its original
            # Session still holds the result of an overlapping explicit deletion.
            task = session._deletion_task
            if task is not None and (not task.done() or self.sessions.get(session.id) is not session):
                return await asyncio.shield(task)
            return await self.delete_session(session.id, expected_session=session)

    async def _delete_captured_session(self, session_id: str, session: Session | None, audience: str) -> bool:
        if session is None:
            return await self._finish_session_deletion(session_id, session, audience)
        async with self._retirement_operation(session.agent_id):
            owner = self._session_owners.get(id(session)) or self._session_owner(session)
            if not await self._close_original_execution(owner, CloseExecution("deleted")):
                raise SessionOwnerUnavailable("Original session did not confirm closure")
            await self.await_session_execution(session_id, timeout=_BACKEND_RETIRE_TIMEOUT)
            await self._join_session_owner_commands(session)
            if session.app_id is None and self.db is not None:
                session.app_id = await self.db.retain_application_session_owner(session_id)
            return await self._finish_session_deletion(session_id, session, audience)

    async def _finish_session_deletion(self, session_id: str, session: Session | None, audience: str) -> bool:
        from ..bg_tasks import bg_task_manager
        from ..delegations import delegation_manager
        from ..research import research_manager

        work: list[Awaitable[None]] = []
        if bg_task_manager._db is self.db:
            work.append(bg_task_manager.retire_session(session_id))
        if delegation_manager.db is self.db:
            work.append(delegation_manager.retire_session(session_id, permanent=True))
        if research_manager.db is self.db:
            work.append(research_manager.retire_session(session_id))
        results = await asyncio.gather(*work, return_exceptions=True)
        errors = [result for result in results if isinstance(result, BaseException)]
        if len(errors) == 1:
            raise errors[0]
        if errors:
            raise BaseExceptionGroup("Original session work retirement failed", errors)
        # Materialize descendant ID references before removing the parent row:
        # the ancestor chain cannot resolve a deleted parent afterward.
        await self._blit_attachments_to_descendant_forks(session_id)
        if self.db is not None and delegation_manager.db is self.db:
            async with delegation_manager.seal_session_deletion(session_id):
                await self.db.delete_session(session_id)
        elif self.db is not None:
            await self.db.delete_session(session_id)
        if session is not None and self.sessions.get(session_id) is session:
            self.sessions.pop(session_id)
            self._session_removed(session)
        self._archived_dispatch.pop(session_id, None)
        self._archived_deletions.pop(session_id, None)
        await self._broadcast({"type": "session_deleted", "session_id": session_id}, user_id=audience)
        # Native transcripts and already authorized downloads may still use
        # the original attachment/spill paths. Row deletion is not proof that
        # these references have ended, so it does not authorize file removal.
        return True
