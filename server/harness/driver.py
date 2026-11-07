"""Protocol operations for an owned HarnessRun.

Drivers own protocol state, never processes, leases or product conclusions.
The profile remains the authority for capabilities; the run supplies checked
I/O and owns cancellation/retirement even when its caller has gone away.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ..session_operations import CompactResult
from .profile import ParseOutput, RuntimeProfile, TurnContext

if TYPE_CHECKING:
    from .events import NativeQuestionRef
    from .run import HarnessRun


@dataclass(frozen=True)
class ActiveTurnRef:
    connection_generation: str
    attempt_id: str
    thread_id: str
    turn_id: str


class SteerNotAccepted(RuntimeError):
    """Proof that this input was not sent or was explicitly refused."""

    def __init__(self, message: str, *, submitted: bool = False) -> None:
        super().__init__(message)
        self.submitted = submitted


class RunDriver(ABC):
    """Per-run protocol state. No input operation implies provider acceptance."""

    input_in_argv: bool
    multiplexed = False
    # Some JSONL runtimes also report irreversible history changes while
    # retiring. Their finite reader must outlive Stop without adopting RPC.
    retains_retirement_output = False
    stdout_line_limit_bytes: int | None = None

    def __init__(self, profile: RuntimeProfile) -> None:
        self.profile = profile
        self.parser = profile.new_event_parser()

    def build_argv(self, context: TurnContext) -> tuple[list[str], dict[str, Any]]:
        return self.profile.build_turn_argv(context)

    def continue_configuration(self, previous: RunDriver) -> None:
        """Carry native-resolved defaults across an Owner-authorized recovery.

        Argv protocols already receive the request's captured configuration.
        Native protocols may resolve additional defaults during preparation.
        This operation grants no input or retry authority.
        """
        return None

    async def begin_input(self, io: HarnessRun, resume_id: str | None) -> None:
        """Register original input facts before fallible command preparation."""
        return None

    async def prepare(self, io: HarnessRun, context: TurnContext, argv: list[str], kwargs: dict[str, Any]) -> None:
        if context.resume_id and self.profile.prepare_resume is not None:
            await self.profile.prepare_resume(context, argv, kwargs)

    @abstractmethod
    async def start_input(self, io: HarnessRun, context: TurnContext) -> None: ...

    async def start_compact(self, io: HarnessRun, context: TurnContext) -> None:
        raise RuntimeError(f"{self.profile.backend} does not support native compaction")

    @property
    def compaction_available(self) -> bool:
        return self.profile.can_compact

    @property
    def compact_result(self) -> CompactResult:
        return CompactResult(effect="not_applied", reason="No native compaction was sent")

    async def send_input(self, io: HarnessRun, text: str, *, input_id: str | None = None) -> str:
        raise RuntimeError(f"{self.profile.backend} does not take input on stdin")

    @property
    def active_turn(self) -> ActiveTurnRef | None:
        return None

    @property
    def observed_root(self) -> ActiveTurnRef | None:
        """Original accepted identity for recording; grants no execution right."""
        return None

    @property
    def native_terminal_status(self) -> str | None:
        return None

    @property
    def steering_open(self) -> bool:
        return True

    async def steer(self, io: HarnessRun, text: str, *, input_id: str | None,
                    target: ActiveTurnRef | None) -> str:
        return await io.send_input(text, input_id=input_id)

    def begin_turn(self) -> None:
        self.parser.begin_turn()

    def receive(self, obj: dict[str, Any]) -> ParseOutput:
        """Consume one frame without waiting on I/O or product recording."""
        return self.parser.parse(obj)

    async def interrupt(self, io: HarnessRun) -> None:
        """Existing JSONL transports retire by EOF and process-group signals."""
        return None

    def connection_closed(self) -> None:
        """Release protocol waiters when the owned reader ends."""
        return None

    async def finish_observations(self) -> None:
        """Join already observed evidence before closing this stream."""
        return None

    async def close_observations(self, *, retired: bool) -> None:
        """Retirement closes supplementary readers even when process cleanup fails."""
        return None

    def retirement_events(self, reason: str, *, reconcile: bool = False) -> list[Any]:
        return []

    def release_output(self) -> None:
        """The original consumer has finished its finite collection."""
        return None

    def question_open(self, ref: NativeQuestionRef) -> bool:
        return False

    async def answer_question(self, io: HarnessRun, ref: NativeQuestionRef,
                              answers: list[dict[str, Any]] | None) -> None:
        raise RuntimeError("This protocol has no native question response")

    def question_sent(self, ref: NativeQuestionRef) -> None:
        return None


class ArgvDriver(RunDriver):
    """Existing exec protocol: spawning can submit input; stdin must get EOF."""

    input_in_argv = True

    async def start_input(self, io: HarnessRun, context: TurnContext) -> None:
        io.close_stdin()
