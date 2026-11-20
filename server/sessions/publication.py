"""Bounded, ordered subscriber I/O for one session owner."""

from __future__ import annotations

import asyncio
import json
import logging
from collections import deque
from collections.abc import Awaitable, Callable
from copy import deepcopy
from dataclasses import dataclass
from functools import partial
from typing import Any

from ..config import settings
from ..diagnostics import exception_diagnostic

_MAX_PUBLICATIONS = 256
_MAX_PUBLICATION_BYTES = 1024 * 1024
logger = logging.getLogger(__name__)


@dataclass(eq=False)
class Publication:
    event: dict[str, Any]
    audience: str | None
    marker: object | None
    delivered: bool = False
    error: BaseException | None = None


def _event_bytes(event: dict[str, Any]) -> int:
    # ASCII JSON accounts for escaped surrogate code points too, without
    # constructing a second full serialized frame beside the captured event.
    return sum(len(part) for part in json.JSONEncoder(default=vars).iterencode(event))


class SessionPublisher:
    """Deliver captured events; only the owner consumes completion facts.

    In-flight events and completed facts share one budget. A subscriber may
    submit another command while this worker awaits it: the owner can accept
    that command without asking this worker to consume its own later event.
    """

    def __init__(
        self, broadcast: Callable[..., Awaitable[None]], wake_owner: Callable[[], None],
        format_error: Callable[[object | None, BaseException], str],
    ) -> None:
        self._broadcast = broadcast
        self._wake_owner = wake_owner
        self._format_error = format_error
        self._entries: deque[tuple[Publication, int]] = deque()
        self._bytes = 0
        self._wake = asyncio.Event()
        self._sealed = False
        self.task = asyncio.create_task(self._deliver(), name="session-publication")

    def can_offer(self, event: dict[str, Any]) -> bool:
        if self._sealed:
            return False
        if not self._entries:
            # One legal large frame may own an otherwise empty buffer. The
            # publisher does not narrow the pre-existing event-size contract.
            return True
        return (len(self._entries) < _MAX_PUBLICATIONS
                and self._bytes + _event_bytes(event) <= _MAX_PUBLICATION_BYTES)

    def offer(
        self, event: dict[str, Any], audience: str | None, *, marker: object | None = None,
    ) -> Publication | None:
        if not self.can_offer(event):
            return None
        captured = deepcopy(event)
        size = _event_bytes(captured)
        publication = Publication(captured, audience, marker)
        self._entries.append((publication, size))
        self._bytes += size
        self._wake.set()
        return publication

    def take_completed(self) -> tuple[Publication, ...]:
        completed = []
        while self._entries:
            publication, size = self._entries[0]
            if not publication.delivered and publication.error is None:
                break
            self._entries.popleft()
            self._bytes -= size
            completed.append(publication)
        return tuple(completed)

    @property
    def idle(self) -> bool:
        return not self._entries

    def in_callback(self) -> bool:
        return asyncio.current_task() is self.task

    async def close(self) -> None:
        """Seal and join actual subscriber I/O, independently of owner uptake.

        Callers within a subscriber use in_callback() to leave the join to
        the external Manager drain rather than wait for their own return.
        """
        self._sealed = True
        self._wake.set()
        await asyncio.shield(self.task)

    async def _deliver(self) -> None:
        while True:
            publication = next((
                publication for publication, _size in self._entries
                if not publication.delivered and publication.error is None
            ), None)
            if publication is None:
                if self._sealed:
                    return
                self._wake.clear()
                await self._wake.wait()
                continue
            try:
                await self._broadcast(publication.event, user_id=publication.audience,
                                      format_error=partial(self._format_error, publication.marker))
            except (asyncio.CancelledError, Exception) as error:
                publication.error = error
            else:
                publication.delivered = True
            self._wake_owner()


class WorkPublication:
    """The notification handoff for one original work."""

    def __init__(self, label: str) -> None:
        self.label = label
        self._released: asyncio.Future[None] = asyncio.get_running_loop().create_future()

    def release(self) -> None:
        if not self._released.done():
            self._released.set_result(None)


class WorkPublications:
    """Track ordered notifications that may outlive their resource writer."""

    def __init__(self) -> None:
        self._tails: dict[WorkPublication, asyncio.Task[BaseException | None]] = {}

    @property
    def pending(self) -> set[asyncio.Task[BaseException | None]]:
        # Each last publication joins every predecessor for the same source.
        return set(self._tails.values())

    def create_scope(self, label: str) -> WorkPublication:
        return WorkPublication(label)

    def _finish(self, source: WorkPublication, done: asyncio.Task[BaseException | None]) -> None:
        if self._tails[source] is done:
            del self._tails[source]
        if not done.cancelled():
            done.exception()

    async def publish(
        self, source: WorkPublication, send: Callable[[], Awaitable[None]], *, required: bool = True,
    ) -> None:
        tail = self._tails.get(source)
        secrets = (settings.auth_token,)
        label, released = source.label, source._released
        sending = withdrawn = False

        async def deliver() -> BaseException | None:
            nonlocal sending
            if tail is not None:
                await asyncio.wait({tail})
            if withdrawn:
                return None
            sending = True
            caller = asyncio.current_task()
            cancellations = caller.cancelling() if caller is not None else 0
            try:
                await send()
            except (asyncio.CancelledError, Exception) as error:
                if isinstance(error, asyncio.CancelledError) and caller is not None and caller.cancelling() > cancellations:
                    raise
                if required and not released.done():
                    # Keep subscriber cancellation as a delivery result so
                    # it remains distinguishable from cancellation of this task.
                    return error
                logger.error("Work publication failed key=%s\n%s", label,
                             exception_diagnostic(error, secrets=secrets))
            return None

        task = asyncio.create_task(deliver(), name=f"work-publication-{label}")
        self._tails[source] = task
        task.add_done_callback(partial(self._finish, source))
        # Pending registration owns the scope. Only the completion signals
        # belong to the waiter, whose traceback can outlive the send.
        del source

        milestones: set[asyncio.Future[Any]] = {task, released}
        try:
            await asyncio.wait(milestones, return_when=asyncio.FIRST_COMPLETED)
        except asyncio.CancelledError:
            if not released.done():
                withdrawn = True
                if sending:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
            raise
        # A required send that failed before release keeps its original error,
        # even if the producer and the handoff are both ready when it resumes.
        if task.done() and (not task.cancelled() or not released.done()):
            if (error := task.result()) is not None:
                raise error
