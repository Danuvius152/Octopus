"""Original request and resource observations; no cancellation authority."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from ..harness.run import RetirementObservation

EvidenceState = Literal["pending", "proven", "failed", "unavailable"]


def future_state(future: asyncio.Future[None]) -> EvidenceState:
    if not future.done():
        return "pending"
    return "failed" if future.cancelled() or future.exception() is not None else "proven"


@dataclass(frozen=True)
class AttemptResource:
    request_id: str
    attempt_id: str
    work_id: str | None
    run_lease_id: str | None
    _retirement: RetirementObservation | None = field(repr=False, compare=False)

    @property
    def state(self) -> EvidenceState:
        return self._retirement.state if self._retirement is not None else "unavailable"

    async def wait(self) -> EvidenceState:
        if self._retirement is None:
            return "unavailable"
        await self._retirement.wait()
        return self.state


@dataclass
class RequestObservation:
    request_id: str
    work_id: str | None
    _driver: asyncio.Future[None] = field(repr=False)
    _attempts: list[AttemptResource] = field(default_factory=list, repr=False)

    @property
    def driver_state(self) -> EvidenceState:
        return future_state(self._driver)

    @property
    def attempts(self) -> tuple[AttemptResource, ...]:
        return tuple(self._attempts)

    async def wait_driver(self) -> None:
        await asyncio.shield(self._driver)

    def follow(self, original: RequestObservation) -> None:
        """An inline input observes its actual carrier, including cleanup."""
        self._attempts = original._attempts
        self.follow_driver(original._driver)

    def follow_driver(self, driver: asyncio.Future[None]) -> None:

        def completed(done: asyncio.Future[None]) -> None:
            if self._driver.done():
                return
            if done.cancelled():
                self._driver.cancel()
            elif (error := done.exception()) is not None:
                self._driver.set_exception(error)
            else:
                self._driver.set_result(None)

        driver.add_done_callback(completed)

    async def wait_resources(self) -> tuple[EvidenceState, ...]:
        # Once the original driver ends, it cannot attach another attempt.
        await self.wait_driver()
        return tuple(await asyncio.gather(*(attempt.wait() for attempt in self.attempts)))
