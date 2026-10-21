"""The admission receipt: waiting observes work; it does not own execution."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Literal

from ..models import TurnOutcome


@dataclass(frozen=True)
class RequestCompletion:
    input_request_id: str
    request_id: str
    state: Literal["completed", "recording_failed", "withdrawn", "expired", "deleted", "shutdown", "unavailable"]
    outcome: TurnOutcome | None = None
    recording_state: Literal["committed", "failed", "unconfirmed", "none"] = "none"
    reason: str | None = None
    notification_state: Literal["delivered", "failed", "unavailable"] = "delivered"


@dataclass(frozen=True)
class SubmissionReceipt:
    disposition: Literal["accepted", "duplicate", "closed", "steered"]
    request_id: str
    _completion: asyncio.Future[RequestCompletion] = field(repr=False, compare=False)

    @classmethod
    def closed(cls, request_id: str, reason: Literal["shutdown", "withdrawn"]) -> SubmissionReceipt:
        """A refusal has no admitted operation or consumer handoff."""
        completion = asyncio.get_running_loop().create_future()
        completion.set_result(RequestCompletion(request_id, request_id, reason, reason=reason))
        return cls("closed", request_id, completion)

    @property
    def accepted(self) -> bool:
        return self.disposition in {"accepted", "duplicate", "steered"}

    async def wait(self) -> RequestCompletion:
        return await asyncio.shield(self._completion)
