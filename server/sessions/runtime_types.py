"""Concrete execution evidence and command errors, without manager dependencies."""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from ..harness import Harness, HarnessEvent, HarnessRun
from ..models import MessageContent, TurnOutcome

if TYPE_CHECKING:
    from .owner_questions import QuestionNotification


@dataclass
class _Settlement:
    request_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    work_id: str | None = None
    attempt_id: str | None = None
    protocol_issues: dict[str, int] = field(default_factory=dict)
    protocol_locations: list[dict[str, Any]] = field(default_factory=list)
    started_at: float | None = None
    # Per-logical-turn retry budgets, also captured in the terminal outcome.
    recovery: dict[str, int] = field(default_factory=lambda: {
        "transient": 0, "idle": 0, "stale_session": 0, "premature_exit": 0,
    })
    cost: float | None = None
    outcome: TurnOutcome | None = None
    recording_state: Literal["selected", "committed", "failed"] = "selected"
    stored: MessageContent | None = None
    seq: int | None = None
    event: dict[str, Any] | None = None
    # Original notification resources can outlive the question attempt/record.
    question_prefix: tuple[QuestionNotification, ...] = ()
    interruption_cause: Literal["user", "shutdown"] | None = None

    def request_interruption(self, cause: Literal["user", "shutdown"]) -> None:
        if self.outcome is None and self.interruption_cause is None:
            self.interruption_cause = cause

    def interruption(self) -> tuple[str, str]:
        if self.interruption_cause == "shutdown":
            return "(interrupted by service shutdown)", "service_shutdown"
        return "(interrupted by user)", "interrupted"


@dataclass
class _Attempt:
    """Owner-held evidence from one Harness invocation in a logical turn."""

    backend: HarnessRun
    # The prompt THIS invocation ran — the original on the first pass, and
    # whatever the retry decided on later ones.
    prompt: str
    # True when the process was already holding this conversation, so the
    # prompt goes in as the next message rather than a fresh invocation.
    reused: bool
    attempt_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    result: HarnessEvent | None = None
    saw_tool_use: bool = False
    # Observed on this attempt, rather than inherited from an earlier request.
    conversation_id: str | None = None
    # Error notifications are diagnostic until the final result is known.
    saw_error_event: bool = False
    error_code: str | None = None
    error_text: str = ""
    preparation_failed: bool = False
    # Streaming-text coalescing (inline-steering.md §4 S1). The CLI emits one
    # delta per token; forwarding each as its own WS frame would be a frame
    # per token and a React render per token. We batch them into at most one
    # frame every _DELTA_FLUSH_SECONDS, which is still far below the threshold
    # where a human sees stepping, and flush before any non-delta event so
    # ordering with tool calls and the final block is exact.
    delta_buf: list[str] = field(default_factory=list)
    delta_last_flush: float = 0.0
    # Per-turn watchdog state (turn-safety.md §3): the watchdog stops a turn
    # that goes silent (idle) or runs too long (overall) so it can never hang
    # forever the way the deep-research wedge did.
    watchdog: dict[str, Any] = field(
        default_factory=lambda: {"last": time.monotonic(), "tripped": None}
    )

    def touch(self, timestamp: float | None = None) -> None:
        """A late native observation cannot grant a fresh idle window."""
        now = time.monotonic() if timestamp is None else timestamp
        self.watchdog["last"] = max(self.watchdog["last"], now)

    @property
    def timeout(self) -> tuple[str, int] | None:
        """`(reason, limit)` when the watchdog stopped this attempt."""
        return self.watchdog["tripped"]

    @property
    def failed(self) -> bool:
        """A turn that errored, or ended without ever saying it was done."""
        return self.result is None or self.result.is_error

    @property
    def failure_code(self) -> str | None:
        return (self.result.error_code if self.result else None) or self.error_code

@dataclass
class _Turn:
    """What survives across the CLI invocations of one logical turn.

    The retry budgets and captured continuation survive individual attempts.
    """

    # Original prepared input; only unsubmitted attempts can replay it.
    prompt: str
    # The prompt the next attempt will run: `prompt`, "continue", or a
    # history-replay wrapper.
    current_prompt: str
    harness: Harness
    # The effective credential id (session override, else the agent's). Used
    # to flag the right row needs_reconnect on a mid-turn 401
    # (harness-credential-reauth.md §4). None = host-default CLI auth.
    cred_id: str | None
    settlement: _Settlement
    started_at: float = field(default_factory=time.monotonic)
    # A conversation observed during this logical turn, usable only by a
    # protocol that supports continuation. It is not proof of safe replay.
    continuation_id: str | None = None


@dataclass
class _Decision:
    """What one finished CLI invocation means: stop, or run it again.

    `events` are the frames the user sees before either happens — the
    explanation for whichever it is. Returning this rather than yielding in
    place is what lets each classifier be an ordinary method instead of a
    branch inside an async generator.
    """

    retry: bool
    events: list[dict[str, Any]] = field(default_factory=list)
    # The prompt the retry runs; None keeps whatever the turn already had.
    prompt: str | None = None
    delay: float = 0.0

    @classmethod
    def stop(cls, *events: dict[str, Any]) -> _Decision:
        return cls(retry=False, events=list(events))

    @classmethod
    def again(
        cls, prompt: str, *events: dict[str, Any], delay: float = 0.0
    ) -> _Decision:
        return cls(retry=True, events=list(events), prompt=prompt, delay=delay)


class QuotaExceeded(Exception):
    """Too many turns at once for one account (multi-tenancy.md §8).

    Its own type rather than a ValueError because the router answers it with
    429 — "try again shortly" — and that is a different thing from the 400 a
    malformed request gets.
    """

    code = "quota_exceeded"


class SessionOwnerUnavailable(RuntimeError):
    """The original owner ended; its commands must not be replayed."""


class SessionMailboxFull(ValueError):
    """This command was refused before taking ownership of any effects."""

    code = "mailbox_full"


class AdmissionIntegrityError(ValueError):
    """A permanent integrity refusal before this input takes effect."""


class SessionAdmissionBusy(ValueError):
    """A lifecycle reservation refused input before it could take effect."""


class SessionAdmissionClosed(ValueError):
    """The original account, agent or session cannot admit new work."""
