"""Install the original Session owner and supervise its actual lifetime."""

from __future__ import annotations

import asyncio

from ..config import settings
from ..diagnostics import exception_diagnostic
from ..service_health import ServiceHealth
from .base import _BACKEND_RETIRE_TIMEOUT, Session, SessionManagerBase, logger
from .owner import SessionOwner, SessionOwnerUnavailable
from .owner_runtime import ReleaseArchive, ReserveFork


class OwnershipMixin(SessionManagerBase):
    def drain_blockers(self) -> tuple[str, ...]:
        """Derive current responsibilities without creating or waking owners."""
        blockers = [f"session {owner.session.id}: {reason}"
                    for owner in self._session_owners.values() for reason in owner.drain_blockers()]
        if any(not task.done() for task in self._owned_tasks):
            blockers.append("owned operation")
        if (self._agent_lifecycle_lock.locked() or any(self._agent_operations.values())
                or any(not task.done() for task in self._agent_archive_tasks.values())):
            blockers.append("agent lifecycle")
        if self.db is None or self.db.configuration_lock.locked():
            blockers.append("configuration")
        if any(not task.done() for task in self._credential_refreshes.values()):
            blockers.append("credential refresh")
        for session in self.sessions.values():
            if id(session) not in self._session_owners and (
                session._pending_queue or session._steer_queue or session._forking
                or session._archive_reservation is not None or session._run.retirements
            ):
                blockers.append(f"session {session.id}: retained lifecycle")
        return tuple(blockers)

    def session_owner_failed(self, session_id: str) -> bool:
        """Read the original owner's permanent failure without installing one."""
        session = self.sessions.get(session_id)
        owner = self._session_owners.get(id(session)) if session is not None else None
        return (owner is not None and owner.failed) or any(
            original.session.id == session_id and original.failed
            for original in self._session_owners.values()
        )



    def _session_owner(self, session: Session) -> SessionOwner:
        if session._archive_reservation is not None:
            raise SessionOwnerUnavailable("Session archive is in progress; retry this delivery later")
        owner = self._session_owners.get(id(session))
        if owner is not None:
            if owner.session is not session:
                raise SessionOwnerUnavailable("The original Session owner has not finished cleanup")
            owner.require_available()
            return owner
        owner = SessionOwner(self, session)
        self._session_owners[id(session)] = owner
        owner.task.add_done_callback(lambda task: self._owner_ended(owner, task))
        return owner

    def _require_session_writer(self, session: Session) -> None:
        owner = self._session_owners.get(id(session))
        if owner is not None and owner.failed:
            raise SessionOwnerUnavailable("Session owner failed; retained input requires review")
        if owner is not None and asyncio.current_task() is not owner.task:
            raise RuntimeError("Only the original Session owner may change live runtime state")

    def _owner_ended(self, owner: SessionOwner, task: asyncio.Task[None]) -> None:
        if not owner.failed:
            if self._session_owners.get(id(owner.session)) is owner:
                self._session_owners.pop(id(owner.session))
            return
        session = owner.session
        session._admission_closed = True
        session._dispatch_paused = True
        session._steer_open = False
        error = SessionOwnerUnavailable("Session owner failed; retained input requires review")
        owner.runtime.end_completions()
        for question in session._questions.values():
            if question.state == "pending":
                question.state = "closed"
            question.ready.set()
            if question.timer is not None:
                question.timer.cancel()
        cause: BaseException = error if task.cancelled() else task.exception() or error
        diagnostic_backend = next((
            resource.backend for resource in session._run.retirements.values()
            if resource.task.done() and not resource.task.cancelled() and resource.task.exception() is cause
        ), None)
        execution = owner.runtime.current
        if diagnostic_backend is not None:
            detail = diagnostic_backend.format_exception(cause)
        elif execution is not None:
            detail = execution.format_exception(cause)
        elif session._run.backend is not None:
            detail = session._run.backend.format_exception(cause)
        else:
            detail = exception_diagnostic(cause, secrets=(settings.auth_token,))
        logger.error("Session owner failed session=%s error=%s\n%s", session.id, type(cause).__name__, detail)
        owner.resource_cleanup = asyncio.create_task(self._retire_failed_owner(owner), name=f"owner-resources-{session.id}")
        self._own_task(owner.resource_cleanup)
        owner.cleanup = asyncio.create_task(self._cleanup_failed_owner(owner), name=f"owner-cleanup-{session.id}")
        self._own_task(owner.cleanup)

    def _session_removed(self, session: Session) -> None:
        owner = self._session_owners.get(id(session))
        if owner is not None:
            owner.seal()

    async def _join_session_owner_commands(self, session: Session) -> None:
        owner = self._session_owners.get(id(session))
        if owner is not None:
            async with asyncio.timeout(_BACKEND_RETIRE_TIMEOUT):
                retirement = await self._failed_owner_retirement(owner)
                if retirement is not None:
                    await asyncio.shield(retirement)
                else:
                    try:
                        await owner.wait_commands()
                    except SessionOwnerUnavailable:
                        retirement = await self._failed_owner_retirement(owner)
                        if retirement is None:
                            raise
                        await asyncio.shield(retirement)

    async def _failed_owner_retirement(self, owner: SessionOwner) -> asyncio.Task[None] | None:
        if not owner.failed:
            return None
        # Include the original task's callback handoff. No successor writer
        # or publisher join is needed to confirm these original resources.
        await asyncio.gather(owner.task, return_exceptions=True)
        if owner.resource_cleanup is None:
            raise RuntimeError("Original Session resource cleanup has not been registered")
        return owner.resource_cleanup

    async def _retire_failed_owner(self, owner: SessionOwner) -> None:
        try:
            async with asyncio.timeout(_BACKEND_RETIRE_TIMEOUT):
                await owner.runtime.cleanup()
        except BaseException as error:
            health = self.db.health if self.db else ServiceHealth()
            health.fail(error)

    async def _release_session_reservation(
        self, owner: SessionOwner, command: ReleaseArchive | ReserveFork,
    ) -> None:
        """Release the original lifecycle claim across writer handoff."""
        try:
            if not owner.failed:
                await owner.call_runtime(command)
                return
        except SessionOwnerUnavailable:
            if not owner.failed:
                raise
        await self._failed_owner_retirement(owner)
        session = owner.session
        if isinstance(command, ReleaseArchive):
            if session._archive_reservation is command.reservation:
                session._archive_reservation = None
        else:
            assert not command.reserve
            session._forking = False
        # Failure admission stays closed; releasing the original claim does
        # not create a writer or discard any unconfirmed resource.

    async def _cleanup_failed_owner(self, owner: SessionOwner) -> None:
        # Resource uncertainty already has the fatal deadline above. Slow
        # subscribers instead remain owned by external publication drain;
        # they must not turn confirmed retirement into a resource failure.
        assert owner.resource_cleanup is not None
        await asyncio.shield(owner.resource_cleanup)
        try:
            operation = owner.runtime._auto_archive_operation
            if operation is not None:
                await asyncio.shield(operation)
        finally:
            await owner.publisher.close()

    async def close_session_owners(self) -> None:
        """Join original publishers after other producers have drained."""
        if any(owner.publisher.in_callback() for owner in self._session_owners.values()):
            raise RuntimeError("Owner publication drain must run outside its subscribers")
        async with asyncio.timeout(_BACKEND_RETIRE_TIMEOUT):
            await self.join_mcp_question_calls()
            while True:
                owners = tuple(self._session_owners.values())
                for owner in owners:
                    if owner.failed:
                        # Join the original task's callback handoff too: its
                        # cleanup is registered by Manager, not by the owner.
                        await asyncio.gather(owner.task, return_exceptions=True)
                        if owner.cleanup is not None:
                            await asyncio.shield(owner.cleanup)
                if (tuple(self._session_owners.values()) != owners
                        or any(owner.failed and (owner.cleanup is None or not owner.cleanup.done())
                               for owner in owners)):
                    continue
                live = tuple(owner for owner in owners if not owner.failed)
                if all(owner.idle for owner in live):
                    # No callback remains which can derive another command.
                    # Seal all owners in this event-loop turn, then join them.
                    for owner in live:
                        owner.seal()
                    await asyncio.gather(*(owner.close() for owner in live))
                    return
                waiting = await asyncio.gather(
                    *(owner.wait_idle() for owner in live), return_exceptions=True,
                )
                for result in waiting:
                    if isinstance(result, BaseException) and not isinstance(result, SessionOwnerUnavailable):
                        raise result
                # An owner may fail while these waits are in flight. Re-enter
                # the same loop to join its original callback and cleanup.
