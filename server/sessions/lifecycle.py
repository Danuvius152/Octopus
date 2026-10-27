"""A session's life: create, archive, unarchive, import, delete, evict.

One slice of `SessionManager`, which was a single 4,017-line class with 84
methods (docs/plans/polish-2026-09.md §3 A1). Split by responsibility; the
methods are unchanged and still reached through one `session_manager` object,
so no call site moved.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

from ..config import settings
from ..credential_bindings import validate_credential_binding
from ..harness import get_harness
from ..knowledge import KnowledgeContext, OwnedTaskContext, receive_return
from ..models import MessageContent, SessionDetail
from ..users import owner_is_available
from ..workspace import confine
from .base import (
    _BACKEND_RETIRE_TIMEOUT,
    Session,
    SessionManagerBase,
    _session_from_row,
    resolve_working_dir,
)
from .owner import SessionOwnerUnavailable
from .owner_runtime import AutoArchive, CloseExecution, ReleaseArchive
from .presentation import session_info

# The window an archived open returns, matching the live one in
# `routers/sessions.MESSAGE_WINDOW`. Declared here rather than imported to keep
# the slice free of a router import; the read-only manage view pages back
# through the same `…/messages` cursor.
_ARCHIVED_MESSAGE_WINDOW = 200


class LifecycleMixin(SessionManagerBase):

    def _session_lifecycle_lock(self, session_id: str) -> asyncio.Lock:
        lock = self._session_lifecycle_locks.get(session_id)
        if lock is None:
            lock = asyncio.Lock()
            self._session_lifecycle_locks[session_id] = lock
        return lock

    async def require_active_agent(self, agent_id: str | None) -> dict[str, Any] | None:
        if agent_id in self._closed_agents:
            raise ValueError("Agent admission is closed")
        agent = await self.db.get_agent(agent_id) if self.db and agent_id else None
        if self.db and (agent is None or agent["archived"]):
            raise ValueError("Agent not found or archived")
        if self.db and agent is not None and not await owner_is_available(self.db, agent.get("user_id")):
            raise ValueError("Account is unavailable")
        if agent_id in self._closed_agents:
            raise ValueError("Agent admission is closed")
        return agent

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

        async with self._agent_lifecycle_lock:
            agent = await self.db.get_agent(agent_id)
            if agent is None:
                raise AgentError("Agent not found")
            if agent["is_system"]:
                raise AgentError("The Default Agent cannot be archived")
            task = self._agent_archive_tasks.get(agent_id)
            if task is None or task.done():
                self._closed_agents.add(agent_id)

                async def finish() -> None:
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

        async with self._agent_lifecycle_lock:
            agent = await self.db.get_agent(agent_id, user_id=user_id)
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
        *, persist: bool = True,
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
        if persist:
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

        Schedules are owned by the *Agent* now, not the session, so there
        is nothing to repoint (agent-refactor.md §5.2).

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
        accepted = await owner.call_runtime(CloseExecution("archived", reservation, only_request_id))
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
            _, successor = await self._archive_original(old, replace=replace, only_request_id=only_request_id)
            return successor

    async def _archive_original(
        self, session: Session, *, replace: bool = False, successor: Session | None = None,
        only_request_id: str | None = None, automatic: bool = False,
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
        async with lifecycle_lock:
            if self.sessions.get(session.id) is not session:
                return False, None
            archived = await self._archive_reserved_session(
                session, only_request_id=only_request_id, automatic=automatic,
            )
        # Subscriber I/O cannot hold a lifecycle lock: a result subscriber may
        # itself need to restore this same session.
        if archived:
            return True, await self._finish_session_archive(session, replace=replace, successor=successor, dispatch_state=archived)
        return False, None

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
                try:
                    accepted = not owner.failed and await owner.call_runtime(CloseExecution("archived", reservation, only_request_id))
                except SessionOwnerUnavailable:
                    if not owner.failed:
                        raise
                    accepted = False
                if owner.failed:
                    # Manager now owns the ended writer's original state and
                    # resources. New admission is already closed; no command
                    # or successor writer is needed to reserve this archive.
                    await self._failed_owner_retirement(owner)
                    if not owner.runtime.can_close_request(only_request_id):
                        return None
                    if session._archive_reservation not in {None, reservation}:
                        raise ValueError(f"Session {session.id} is already being archived")
                    session._archive_reservation = reservation
                elif not accepted:
                    return None
            await self.await_session_execution(session.id, timeout=_BACKEND_RETIRE_TIMEOUT)
            await self._join_session_owner_commands(session)
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

    async def _finish_session_archive(
        self, old: Session, *, replace: bool, dispatch_state: dict[str, Any], successor: Session | None = None,
    ) -> Session | None:
        """The archive effect is confirmed; publish or create its successor."""

        if not replace and successor is None:
            await self._broadcast(
                {
                    "type": "session_archived",
                    "old_session_id": old.id,
                    "new_session_id": None,
                    "name": old.name,
                    "dispatch_state": dispatch_state,
                }, user_id=old.user_id,
            )
            return None

        # New session inherits agent / name / working_dir / credential_id /
        # origin but starts with no claude_session_id (fresh conversation).
        new = successor
        if new is None:
            # Replacement belongs to this already registered lifecycle command.
            new = await self._create_session(
                agent_id=old.agent_id, name=old.name, working_dir=old.working_dir,
                credential_id=old.credential_id, origin=old.origin, backend=old.backend,
            )
            new.model, new.reasoning_effort, new.service_tier = old.model, old.reasoning_effort, old.service_tier
            if self.db:
                await self.db.update_session_field(new.id, model=new.model,
                    reasoning_effort=new.reasoning_effort, service_tier=new.service_tier)

        # Schedules are agent-owned, so ownership needs no repoint.
        # But a schedule created from this session (origin_session_id == old.id)
        # should follow the live successor thread, otherwise its runs fall back
        # to throwaway sessions. Move them onto `new`, then re-register the live
        # jobs so the next fire targets the successor, not the archived
        # session.
        if self.db:
            repointed = await self.db.repoint_schedules_origin(old.id, new.id)
            if self._schedule_runner is not None:
                for row in repointed:
                    await self._schedule_runner.reschedule(row)

        await self._broadcast(
            {
                "type": "session_archived",
                "old_session_id": old.id,
                "new_session_id": new.id,
                "name": new.name,
                "model": new.model,
                "reasoning_effort": new.reasoning_effort,
                "service_tier": new.service_tier,
                "dispatch_state": dispatch_state,
            }, user_id=old.user_id,
        )
        return new

    async def auto_archive_scheduled_session(self, session_id: str) -> bool:
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
            or session.origin not in self._AUTO_ARCHIVE_ELIGIBLE
        ):
            return False
        task = asyncio.create_task(self._auto_archive_idle_session(session), name=f"auto-archive-{session.id}")
        self._own_task(task)
        return await asyncio.shield(task)

    async def _auto_archive_idle_session(self, session: Session) -> bool:
        archived, _ = await self._archive_original(session, automatic=True)
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
                await self._retire_archived_session(session, reservations[session.id])
            # A storage failure in any captured request keeps every session
            # available for verification, instead of partially evicting them.
            for session in sessions:
                self.sessions.pop(session.id, None)
                self._session_removed(session)
                await self._broadcast(
                    {"type": "session_archived", "old_session_id": session.id,
                     "new_session_id": None, "name": session.name},
                    user_id=session.user_id,
                )
            return [session.id for session in sessions]
        finally:
            for session in sessions:
                if self.sessions.get(session.id) is session:
                    owner = self._session_owners.get(id(session))
                    if owner is not None and not owner.failed:
                        await owner.call_runtime(ReleaseArchive(reservations[session.id]))

    async def list_archived_sessions(
        self, user_id: str | None = None
    ) -> list[dict[str, Any]]:
        """Return SessionInfo-shaped dicts for every archived DB row.

        Pulled lazily from the DB (archived sessions aren't kept in the
        in-memory `self.sessions` map), so the owner filter is the database's
        job here rather than `list_sessions`'. Caller turns them into Pydantic
        models for the response.
        """
        if self.db is None:
            return []
        rows = await self.db.load_sessions(include_archived=True, user_id=user_id)
        out: list[dict[str, Any]] = []
        for row in rows:
            if not row["archived"]:
                continue
            count, _next_seq, duplicates = await self.db.message_sequence_state(row["id"])
            out.append(session_info(row, count, archived=True,
                                    transcript_integrity="duplicate_seq" if duplicates else "ok").model_dump())
        return out

    async def load_archived_session_detail(
        self, session_id: str, user_id: str | None = None
    ) -> SessionDetail | None:
        """Read full message history for an archived session straight
        from the DB. Returns None if the id isn't an archived row — or isn't
        one of `user_id`'s, which a caller must not be able to tell apart.
        """
        if self.db is None:
            return None
        rows = await self.db.load_sessions(include_archived=True, user_id=user_id)
        match = next(
            (r for r in rows if r["id"] == session_id and r["archived"]), None
        )
        if match is None:
            return None
        # Windowed like a live open, and counted separately: `len(messages)`
        # stopped being the message count once a window is all we fetch
        # (polish-2026-09.md §4 B2).
        messages_raw = await self.db.load_messages(
            session_id, limit=_ARCHIVED_MESSAGE_WINDOW, newest_first=True
        )
        messages = [MessageContent(**m) for m in messages_raw]
        total, _next_seq, duplicates = await self.db.message_sequence_state(session_id)
        if duplicates:
            messages = []
        oldest = messages[0].seq if messages else None
        return SessionDetail(
            **session_info(match, total, archived=True, transcript_integrity="duplicate_seq" if duplicates else "ok").model_dump(),
            messages=messages,
            pending_questions=[],
            oldest_loaded_seq=oldest,
            has_more_messages=await self.db.has_messages_before(session_id, oldest),
            next_message_seq=max((m.seq for m in messages if m.seq is not None), default=-1) + 1,
        )

    async def unarchive_session(
        self, session_id: str, user_id: str | None = None,
    ) -> Session:
        binding = await self.db.get_session_binding(session_id) if self.db else None
        if binding is None or (user_id is not None and binding["user_id"] != user_id):
            raise ValueError(f"Archived session {session_id} not found")
        return await self._run_agent_operation(
            binding["agent_id"], lambda: self._unarchive_session(session_id, user_id),
        )

    async def _unarchive_session(
        self, session_id: str, user_id: str | None = None, *, returning: OwnedTaskContext | None = None,
    ) -> Session:
        async with self._session_lifecycle_lock(session_id):
            if returning is not None:
                self._require_task_return(returning, session_id, user_id)
            current = self.sessions.get(session_id)
            if returning is not None and current is not None:
                if current.user_id != user_id or current._admission_closed or self.session_owner_failed(session_id):
                    raise ValueError("Original session admission is unavailable")
                return current
            session = await self._restore_archived_session(session_id, user_id, returning=returning)
        await self._broadcast({"type": "session_unarchived", "session_id": session.id, "name": session.name,
                               "session": session_info(session).model_dump(mode="json"),
                               "dispatch_state": self._session_dispatch_snapshot(session)})
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
        rows = await self.db.load_sessions(include_archived=True, user_id=user_id)
        match = next(
            (r for r in rows if r["id"] == session_id and r["archived"]), None
        )
        if match is None:
            raise ValueError(f"Archived session {session_id} not found")
        message_state = await self.db.message_sequence_state(session_id)
        if returning is not None:
            await self.require_active_agent(match["agent_id"])
            # Recheck after waiting for lifecycle admission and storage reads,
            # before the operation that changes archived membership begins.
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
        return session

    def _require_task_return(self, owned: OwnedTaskContext, session_id: str, user_id: str | None) -> None:
        if (self._admission_closed or self.session_owner_failed(session_id)
                or receive_return(owned, session_id=session_id, user_id=user_id,
                                  work_id=owned.return_binding.work_id) is None):
            raise ValueError("Original task return admission is unavailable")

    async def resolve_task_return(
        self, owned: OwnedTaskContext | None, *, session_id: str, work_id: str | None,
    ) -> tuple[Session, KnowledgeContext] | None:
        """Restore only the fixed, still-authorized caller of independent work."""
        if owned is None or self.db is None or self._admission_closed:
            return None
        binding = owned.return_binding
        reference = binding.reference
        if reference.session_id != session_id or binding.work_id != work_id:
            return None
        context = receive_return(owned, session_id=reference.session_id,
                                 user_id=reference.user_id, work_id=binding.work_id)
        if context is None or self.session_owner_failed(reference.session_id):
            return None
        row = await self.db.get_session_binding(reference.session_id)
        if row is None or row["user_id"] != reference.user_id:
            return None
        try:
            session = await self._run_agent_operation(row["agent_id"], lambda: self._unarchive_session(
                reference.session_id, reference.user_id, returning=owned,
            ))
        except ValueError:
            return None
        context = receive_return(owned, session_id=session.id, user_id=session.user_id, work_id=binding.work_id)
        return (session, context) if context is not None else None

    async def delete_session(self, session_id: str) -> bool:
        session = self.sessions.get(session_id)
        if session is None:
            return False
        task = session._deletion_task
        if task is None or task.done():
            task = asyncio.create_task(self._delete_live_session(session), name=f"delete-session-{session_id}")
            session._deletion_task = task
            self._own_task(task)
        return await asyncio.shield(task)

    async def _delete_live_session(self, session: Session) -> bool:
        async with self._retirement_operation(session.agent_id):
            return await self._finish_session_deletion(session)

    async def _finish_session_deletion(self, session: Session) -> bool:
        session_id = session.id
        if session.user_id is None:
            raise ValueError("The session has no attachment owner")
        owner = self._session_owners.get(id(session)) or self._session_owner(session)
        await owner.call_runtime(CloseExecution("deleted"))
        await self.await_session_execution(session_id, timeout=_BACKEND_RETIRE_TIMEOUT)
        await self._join_session_owner_commands(session)
        # Materialize descendant ID references before removing the parent row:
        # the ancestor chain cannot resolve a deleted parent afterward.
        await self._blit_attachments_to_descendant_forks(session_id)
        if self.db:
            await self.db.delete_session(session_id)
        if self.sessions.get(session_id) is session:
            self.sessions.pop(session_id)
            self._session_removed(session)
        # Native transcripts and already authorized downloads may still use
        # the original attachment/spill paths. Row deletion is not proof that
        # these references have ended, so it does not authorize file removal.
        return True
