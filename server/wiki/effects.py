"""Execute a request's finite effects through the original application owner."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING

from ..aio import complete_cleanup
from ..applications import ApplicationError
from .caller import WikiCaller
from .store import WikiError

if TYPE_CHECKING:
    from ..applications import ApplicationManager


@dataclass(frozen=True)
class WikiEffects:
    user_id: str | None
    caller: WikiCaller
    applications: ApplicationManager | None = None

    async def run[T](self, operation: Callable[[], Awaitable[T]]) -> T:
        if self.caller.app_id is None:
            return await operation()
        if self.applications is None:
            raise WikiError("The original application effect owner is unavailable", 503)
        try:
            return await self.applications.run_effect(self.caller.app_id, self.user_id, operation)
        except ApplicationError as error:
            raise WikiError(error.message, error.status_code) from error

    async def run_sync(self, function, *args, **kwargs):
        async def finish():
            return await complete_cleanup(asyncio.to_thread(partial(function, *args, **kwargs)))

        return await self.run(finish)
