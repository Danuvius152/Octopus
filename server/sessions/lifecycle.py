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
from datetime import UTC, datetime
from typing import Any

from ..attachments import delete_session_attachments
from ..large_prompts import delete_session_large_prompts
from ..models import MessageContent, SessionDetail, SessionStatus
from ..workspace import confine
from .base import (
    _BACKEND_RETIRE_TIMEOUT,
    Session,
    SessionManagerBase,
    _session_fork_kwargs,
    fork_info_fields,
    resolve_working_dir,
)

# The window an archived open returns, matching the live one in
# `routers/sessions.MESSAGE_WINDOW`. Declared here rather than imported to keep
# the slice free of a router import; the read-only manage view pages back
# through the same `…/messages` cursor.
_ARCHIVED_MESSAGE_WINDOW = 200


class LifecycleMixin(SessionManagerBase):

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
        self,
        agent_id: str | None,
        name: str | None = None,
        working_dir: str | None = None,
        credential_id: str | None = None,
        origin: str = "user",
        backend: str = "claude-code",
        parent_session_id: str | None = None,
        delegation_request: str | None = None,
        app_id: str | None = None,
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
            # each of them having to remember. Before accounts exist there is
            # no owner, and `adopt_orphan_rows` settles that at upgrade.
            user_id=owner_id,
        )
        self.sessions[sid] = session
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
                parent_session_id=session.parent_session_id,
                delegation_request=session.delegation_request,
                app_id=session.app_id,
                user_id=session.user_id,
            )
        return session

    async def import_session(
        self,
        name: str,
        working_dir: str | None = None,
        claude_session_id: str | None = None,
        credential_id: str | None = None,
        messages: list[MessageContent] | None = None,
        agent_id: str | None = None,
        origin: str = "user",
        backend: str = "claude-code",
    ) -> Session:
        agent = await self.db.get_agent(agent_id) if (self.db and agent_id) else None
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
        self.sessions[sid] = session
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
            if self.db:
                await self.db.flush()
        return session

    async def archive_session(
        self, session_id: str, *, replace: bool = True
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
        """
        old = self.sessions.get(session_id)
        if old is None:
            raise ValueError(f"Session {session_id} not found")

        task = asyncio.create_task(self._archive_live_session(old, replace=replace),
                                   name=f"archive-session-{session_id}")
        self._own_task(task)
        return await asyncio.shield(task)

    async def _retire_archived_session(self, session: Session, reservation: object) -> None:
        """Close explicit admission and finish captured work before hiding it."""
        async with session._dispatch_lock:
            if self.sessions.get(session.id) is not session:
                raise ValueError(f"Session {session.id} not found")
            if session._admission_closed or session._archive_reservation is not None:
                raise ValueError(f"Session {session.id} is already being archived")
            if session._recording_blocked is not None:
                raise ValueError("Result recording must be verified before archiving the session")
            session._archive_reservation = reservation
            session._admission_closed = True
            session._dispatch_paused = True
            session._steer_open = False
            self._withdraw_requests(session, [*session._pending_queue, *session._steer_queue], reason="withdrawn")
            session._pending_queue.clear()
            session._steer_queue.clear()
            current = session._current_request
            if current is not None and not current.started:
                self._withdraw_requests(session, [current], reason="withdrawn")
            backend = session._backend
            if backend is not None:
                self._retire_backend(session, backend)
            tasks = {task for task in (session._inner_task, session._active_task)
                     if task is not None and task is not asyncio.current_task() and not task.done()}
            for task in tasks:
                task.cancel()
        await self._close_session_questions(session, reason="archived")
        if tasks:
            _, pending = await asyncio.wait(tasks, timeout=_BACKEND_RETIRE_TIMEOUT)
            if pending:
                raise ValueError("Session execution retirement remains unconfirmed")
        await self._await_backend_retirement(session)
        if session._recording_blocked is not None:
            raise ValueError("Result recording must be verified before archiving the session")

    async def _archive_live_session(self, old: Session, *, replace: bool) -> Session | None:
        reservation = object()
        try:
            await self._retire_archived_session(old, reservation)
            return await self._finish_session_archive(old, replace=replace)
        finally:
            if old._archive_reservation is reservation:
                old._archive_reservation = None
                if self.sessions.get(old.id) is old:
                    old._admission_closed = False

    async def _finish_session_archive(self, old: Session, *, replace: bool) -> Session | None:
        session_id = old.id

        # Mark the DB row archived; drop it from the in-memory dict so
        # subsequent list/get calls don't surface it.
        if self.db:
            await self.db.update_session_field(session_id, archived=True)
        self.sessions.pop(session_id, None)

        if not replace:
            await self._broadcast(
                {
                    "type": "session_archived",
                    "old_session_id": old.id,
                    "new_session_id": None,
                    "name": old.name,
                }, user_id=old.user_id,
            )
            return None

        # New session inherits agent / name / working_dir / credential_id /
        # origin but starts with no claude_session_id (fresh conversation).
        agent_id = old.agent_id
        if agent_id is None and self.db:
            sys_agent = await self.db.get_system_agent()
            agent_id = sys_agent["id"] if sys_agent else None
        new = await self.create_session(
            agent_id=agent_id,
            name=old.name,
            working_dir=old.working_dir,
            credential_id=old.credential_id,
            origin=old.origin,
            backend=old.backend,
        )
        new.model = old.model
        new.reasoning_effort = old.reasoning_effort
        new.service_tier = old.service_tier
        if self.db:
            await self.db.update_session_field(
                new.id, model=new.model, reasoning_effort=new.reasoning_effort,
                service_tier=new.service_tier,
            )

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
            }, user_id=old.user_id,
        )
        return new

    async def auto_archive_scheduled_session(self, session_id: str) -> bool:
        """Hide a finished transient session (schedule or delegation
        child) once it goes idle. agent-refactor.md §5.6 + agent-
        collaboration.md §5.2.

        Unlike `archive_session`, no replacement thread is created — the
        next fire (schedule) or delegation request materializes its own
        fresh session under the agent. No-op if the session is gone,
        not auto-archivable by origin, or still running. Returns True
        if it archived.

        The function name is kept (rather than renamed) because it's
        referenced from main.py / scheduler.py; the behaviour
        generalises while the call sites stay stable.
        """
        session = self.sessions.get(session_id)
        if (
            session is None
            or session.origin not in self._AUTO_ARCHIVE_ELIGIBLE
        ):
            return False
        def idle() -> bool:
            return (
                self.sessions.get(session_id) is session
                and not session._dispatch_paused
                and session._recording_blocked is None
                and not session._pending_queue
                and session._current_request is None
                and not (session._active_task and not session._active_task.done())
            )

        reservation = object()
        async with session._dispatch_lock:
            if not idle() or session._archive_reservation is not None:
                return False
            session._archive_reservation = reservation
            revision = session._dispatch_revision

        async def restore_live_row() -> None:
            if self.db and self.sessions.get(session_id) is session:
                await self.db.update_session_field(session_id, archived=False)

        try:
            if self.db:
                await self.db.update_session_field(session_id, archived=True)
            async with session._dispatch_lock:
                archived = idle() and session._dispatch_revision == revision
                if archived:
                    self.sessions.pop(session_id)
            if not archived:
                await restore_live_row()
                return False
        except BaseException:
            await asyncio.shield(restore_live_row())
            raise
        finally:
            if session._archive_reservation is reservation:
                session._archive_reservation = None
        await self._broadcast(
            {
                "type": "session_archived",
                "old_session_id": session_id,
                "new_session_id": None,
                "name": session.name,
            }, user_id=session.user_id,
        )
        return True

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
        if any(session._recording_blocked is not None for session in sessions):
            raise ValueError("Result recording must be verified before archiving the agent")
        reservations = {session.id: object() for session in sessions}
        try:
            for session in sessions:
                await self._retire_archived_session(session, reservations[session.id])
            # A storage failure in any captured request keeps every session
            # available for verification, instead of partially evicting them.
            for session in sessions:
                self.sessions.pop(session.id, None)
                await self._broadcast(
                    {"type": "session_archived", "old_session_id": session.id,
                     "new_session_id": None, "name": session.name},
                    user_id=session.user_id,
                )
            return [session.id for session in sessions]
        finally:
            for session in sessions:
                if session._archive_reservation is reservations[session.id]:
                    session._archive_reservation = None
                    if self.sessions.get(session.id) is session:
                        session._admission_closed = False

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
            count = await self.db.count_messages(row["id"])
            out.append(
                {
                    "id": row["id"],
                    "name": row["name"],
                    "working_dir": row["working_dir"],
                    "status": SessionStatus.idle.value,
                    "created_at": row["created_at"],
                    "message_count": count,
                    "claude_session_id": row["claude_session_id"],
                    "credential_id": row.get("credential_id"),
                    "agent_id": row.get("agent_id"),
                    "origin": row.get("origin") or "user",
                    "backend": row.get("backend") or "claude-code",
                    "model": row.get("model"),
                    "reasoning_effort": row.get("reasoning_effort"),
                    "service_tier": row.get("service_tier"),
                    "parent_session_id": row.get("parent_session_id"),
                    "delegation_request": row.get("delegation_request"),
                    "archived": True,
                    **fork_info_fields(
                        backend=row.get("backend") or "claude-code",
                        forked_from_session_id=row.get("forked_from_session_id"),
                        fork_after_seq=row.get("fork_after_seq"),
                        fork_metadata=row.get("fork_metadata"),
                        fork_revert_record=row.get("fork_revert_record"),
                    ),
                }
            )
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
        total = await self.db.count_messages(session_id)
        oldest = messages[0].seq if messages else None
        return SessionDetail(
            id=match["id"],
            name=match["name"],
            working_dir=match["working_dir"],
            status=SessionStatus.idle,
            created_at=match["created_at"],
            message_count=total,
            claude_session_id=match["claude_session_id"],
            credential_id=match.get("credential_id"),
            agent_id=match.get("agent_id"),
            origin=match.get("origin") or "user",
            backend=match.get("backend") or "claude-code",
            model=match.get("model"),
            reasoning_effort=match.get("reasoning_effort"),
            service_tier=match.get("service_tier"),
            parent_session_id=match.get("parent_session_id"),
            delegation_request=match.get("delegation_request"),
            **fork_info_fields(
                backend=match.get("backend") or "claude-code",
                forked_from_session_id=match.get("forked_from_session_id"),
                fork_after_seq=match.get("fork_after_seq"),
                fork_metadata=match.get("fork_metadata"),
                fork_revert_record=match.get("fork_revert_record"),
            ),
            archived=True,
            messages=messages,
            pending_queue=[],
            pending_questions=[],
            oldest_loaded_seq=oldest,
            has_more_messages=bool(oldest),
            next_message_seq=total,
        )

    async def unarchive_session(
        self, session_id: str, user_id: str | None = None
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
        await self.db.update_session_field(session_id, archived=False)
        # Reload into the in-memory map so writes (sendMessage etc.)
        # immediately route to this session.
        session = Session(
            id=match["id"],
            name=match["name"],
            working_dir=match["working_dir"],
            created_at=match["created_at"],
            claude_session_id=match["claude_session_id"],
            credential_id=match.get("credential_id"),
            agent_id=match.get("agent_id"),
            origin=match.get("origin") or "user",
            backend=match.get("backend") or "claude-code",
            model=match.get("model"),
            reasoning_effort=match.get("reasoning_effort"),
            service_tier=match.get("service_tier"),
            user_id=match.get("user_id"),
            # Preserve the delegation chain fields when unarchiving —
            # without these, an unarchived delegation child would lose
            # its parent_session_id pointer and the "Delegated from"
            # banner / cycle walk would break.
            parent_session_id=match.get("parent_session_id"),
            delegation_request=match.get("delegation_request"),
            **_session_fork_kwargs(match),
        )
        session._message_count = await self.db.count_messages(session.id)
        self.sessions[session.id] = session
        await self._broadcast(
            {
                "type": "session_unarchived",
                "session_id": session.id,
                "name": session.name,
            }
        )
        return session

    async def delete_session(self, session_id: str) -> bool:
        session = self.sessions.get(session_id)
        if session is None:
            return False
        task = session._deletion_task
        if task is None or task.done():
            session._admission_closed = True
            session._dispatch_paused = True
            task = asyncio.create_task(self._delete_live_session(session), name=f"delete-session-{session_id}")
            session._deletion_task = task
            self._own_task(task)
        return await asyncio.shield(task)

    async def _delete_live_session(self, session: Session) -> bool:
        session_id = session.id
        session._admission_closed = True
        session._dispatch_paused = True
        self._withdraw_requests(session, [*session._pending_queue, *session._steer_queue], reason="deleted")
        if session._current_request is not None and not session._current_request.started:
            self._withdraw_requests(session, [session._current_request], reason="deleted")
        session._pending_queue.clear()
        session._steer_queue.clear()
        await self._close_session_questions(session, reason="deleted")
        if session._inner_task and not session._inner_task.done():
            session._inner_task.cancel()
        if session._active_task and not session._active_task.done():
            session._active_task.cancel()
        if session._backend:
            self._retire_backend(session, session._backend)
        tasks = [task for task in (session._inner_task, session._active_task) if task is not None]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await self._await_backend_retirement(session)
        if self.sessions.get(session_id) is session:
            self.sessions.pop(session_id)
        # Blit attachment files into descendant forks BEFORE removing this
        # session's dir, so the read-time fallback stays valid (§5.5). Done
        # before the DB delete so the descendant message rows are still
        # readable for their attachment references.
        await self._blit_attachments_to_descendant_forks(session_id)
        if self.db:
            await self.db.delete_session(session_id)
        # Best-effort: wipe any uploaded files for this session. We do
        # this after the DB delete so the FK cascade has already removed
        # message rows pointing at them — if rmtree fails, the session
        # row is still gone, which is the user-visible expectation.
        delete_session_attachments(session_id)
        delete_session_large_prompts(session_id)
        return True
