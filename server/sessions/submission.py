"""The admission receipt: waiting observes work; it does not own execution."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, Literal

from ..models import TurnOutcome
from .observation import RequestObservation

if TYPE_CHECKING:
    from ..knowledge import OwnedTaskContext
    from .base import Session


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
    execution: RequestObservation | None = field(default=None, repr=False, compare=False)

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


@dataclass(frozen=True)
class ReturnTarget:
    """An independent source's captured identity, never a new caller binding."""

    session_id: str
    source: Literal["bg", "research", "delegation"]
    delivery_id: str
    work_id: str | None
    owned: OwnedTaskContext | None


@dataclass(frozen=True)
class ReturnBody:
    prompt: str
    question_session_id: str | None = None
    question_id: str | None = None

    def __post_init__(self) -> None:
        if (self.question_session_id is None) != (self.question_id is None):
            raise ValueError("Return question identity must include both session and question")


@dataclass(frozen=True)
class ReturnSkipped:
    """The source no longer needs a new input; earlier admission still wins."""

    reason: str


@dataclass(frozen=True)
class ReturnRefused:
    """This call admitted no new input; it says nothing about an earlier call."""

    reason: Literal["source_withdrawn", "binding_unavailable", "destination_unavailable", "admission_closed", "capacity", "busy"]
    cause: Exception | None = None


@dataclass(frozen=True)
class ReturnUnconfirmed:
    """Admission may have happened; only the original receipt can confirm it."""

    cause: Exception


ReturnAdmission = SubmissionReceipt | ReturnRefused | ReturnUnconfirmed


def receipt_for_input(row: dict[str, Any], future: asyncio.Future[RequestCompletion] | None = None,
                      execution: RequestObservation | None = None) -> SubmissionReceipt:
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
    return SubmissionReceipt("duplicate" if row["receipt"] and row["receipt"]["accepted"] else "closed", request_id, future, execution)


def observed_input_receipt(row: dict[str, Any], session: Session | None) -> SubmissionReceipt:
    """Keep the original observed completion after durable identity checks.

    The saved row proves admission; only the original process can also know
    its exact notification/retirement outcome. Reconstruct only without that
    observation, never replacing a known completion with an inference.
    """
    if session is not None:
        original = session._delivery_receipts.get((row["payload"] or {}).get("delivery_id"))
        if original is not None and original.request_id == row["request_id"]:
            return replace(original, disposition="duplicate")
    future = session._request_completions.get(row["request_id"]) if session is not None else None
    return receipt_for_input(row, future, session._request_observations.get(row["request_id"]) if session else None)
