"""The admission receipt: waiting observes work; it does not own execution."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Literal

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


def receipt_for_input(row: dict[str, Any], future: asyncio.Future[RequestCompletion] | None = None) -> SubmissionReceipt:
    """Observe durable admission without creating an Owner or dispatching work."""
    request_id = row["request_id"]
    if future is None:
        terminal = row["terminal"] if row["delivery_state"] != "unknown" else None
        future = asyncio.get_running_loop().create_future()
        future.set_result(RequestCompletion(
            request_id, row["execution_owner"] or request_id,
            "completed" if terminal is not None else "withdrawn" if row["execution_state"] == "withdrawn" else "unavailable",
            outcome=TurnOutcome(**terminal) if terminal is not None else None,
            recording_state="committed" if terminal is not None else "none", reason=row["reason_code"],
            notification_state="unavailable",
        ))
    return SubmissionReceipt("duplicate" if row["receipt"] and row["receipt"]["accepted"] else "closed", request_id, future)
