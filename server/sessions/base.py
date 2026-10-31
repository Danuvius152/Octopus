from __future__ import annotations

import asyncio
import json
import logging
import uuid
import weakref
from collections import deque
from collections.abc import Awaitable, Callable, Coroutine
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from ..config import settings
from ..database import Database
from ..harness import (
    HarnessCredential,
    HarnessEvent,
    HarnessRun,
    get_harness,
)
from ..models import (
    MessageContent,
    SessionStatus,
)
from ..oauth_errors import RefreshErrorCode
from .submission import (
    RequestCompletion,
    ReturnAdmission,
    ReturnBody,
    ReturnRefused,
    ReturnSkipped,
    ReturnTarget,
    SubmissionReceipt,
)

if TYPE_CHECKING:
    from ..knowledge import KnowledgeContext, OwnedTaskContext
    from .owner import SessionOwner
    from .owner_questions import QuestionNotification
    from .owner_runtime import ReleaseArchive, ReserveFork
    from .runtime_types import _Settlement

logger = logging.getLogger(__name__)

# How often streamed token deltas are flushed to the client as one WS frame
# (inline-steering.md §4 S1). 50ms is ~20 updates/second: below the rate at
# which a reader perceives stepping, and ~10x fewer frames and renders than
# forwarding every token individually.
_DELTA_FLUSH_SECONDS = 0.05

# How many sub-agent cards a session keeps live state for. They're UI state
# (the transcript holds the durable record), so this only stops a session that
# fans out all day from growing a map forever.
_MAX_SUBAGENTS_PER_SESSION = 64

# Holding a finished CLI process makes the next turn ~1.5s faster and keeps its
# prompt cache warm, but costs ~255MB of RSS for as long as it's held
# (inline-steering.md §7). Two bounds keep that honest: a process is dropped
# after this long without a turn, and only this many are held at once (the
# least-recently-used goes first). A dropped process is never a broken
# session — the next turn spawns and `--resume`s, which is what every turn did
# before reuse existed.
_HELD_PROCESS_IDLE_SECONDS = 600.0
_MAX_HELD_PROCESSES = 4
_REAPER_INTERVAL_SECONDS = 30.0
# How long to wait for one held process to die before giving up on it.
_HELD_STOP_TIMEOUT = 2.0
# The complete stdin/TERM/KILL retirement, including late spawn compensation.
_BACKEND_RETIRE_TIMEOUT = 8.0
# How many un-written steers a single turn will hold. It's a person typing;
# beyond this the message queues for the next turn instead of being refused.
_MAX_PENDING_STEERS = 8


class ForkError(Exception):
    """A fork request was rejected for a reason the route maps to a status
    code (session-rewind.md §5.1). `reason` is a stable machine token;
    `status_code` is the HTTP status the route should return."""

    def __init__(self, message: str, *, reason: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.reason = reason
        self.status_code = status_code


def resolve_working_dir(working_dir: str | None) -> str:
    """Freeze a session's working directory to an ABSOLUTE path at creation.

    A session's conversation + memory live under a path Claude derives from
    its working directory (``projects/<cwd-slug>/``). If we stored the dir
    relative (``.``, ``Octopus``), the slug would be re-resolved against the
    *server process's* cwd on every turn — so the storage location would
    depend on where/how the server happens to be launched. That ambient
    coupling silently relocates (and orphans) a session's history whenever
    the server's cwd differs: a manual launch from another dir, an edited
    systemd unit, or a cloud deployment with a different pwd.

    Resolving to absolute once, here, removes ``os.getcwd()`` from the
    equation forever after: the slug becomes a pure function of session-owned
    data. Relative input is interpreted against the server cwd this one time
    (the natural meaning of a path the caller typed), then frozen.
    """
    raw = working_dir or settings.default_working_dir
    return str(Path(raw).expanduser().resolve())


def _session_fork_kwargs(row: dict[str, Any]) -> dict[str, Any]:
    """Extract the six persisted fork columns from a `load_sessions` row dict
    into Session(**…) kwargs. Centralised so every Session-from-row site stays
    in lock-step (session-rewind.md §4)."""
    return {
        "forked_from_session_id": row.get("forked_from_session_id"),
        "fork_after_seq": row.get("fork_after_seq"),
        "fork_needs_replay": bool(row.get("fork_needs_replay")),
        "fork_metadata": row.get("fork_metadata"),
        "fork_revert_record": row.get("fork_revert_record"),
        "fork_status": row.get("fork_status"),
    }


def _session_from_row(row: dict[str, Any]) -> Session:
    """Restore persisted settings; execution state always starts fresh.

    Empty optional settings use the same None normalization on startup and
    unarchive. Sequence recovery and publication remain owned by the caller.
    """
    return Session(
        id=row["id"],
        name=row["name"],
        working_dir=row["working_dir"],
        created_at=row["created_at"],
        claude_session_id=row["claude_session_id"],
        credential_id=row.get("credential_id"),
        agent_id=row.get("agent_id"),
        origin=row.get("origin") or "user",
        backend=row.get("backend") or "claude-code",
        model=row.get("model") or None,
        reasoning_effort=row.get("reasoning_effort") or None,
        service_tier=row.get("service_tier") or None,
        user_id=row.get("user_id") or None,
        parent_session_id=row.get("parent_session_id"),
        delegation_request=row.get("delegation_request"),
        app_id=row.get("app_id"),
        **_session_fork_kwargs(row),
    )


def fork_info_fields(
    *,
    backend: str,
    forked_from_session_id: str | None,
    fork_after_seq: int | None,
    fork_metadata: str | None,
    fork_revert_record: str | None,
) -> dict[str, Any]:
    """The fork-related fields exposed on `SessionInfo`
    (session-rewind.md §4). `can_fork` comes from the harness profile;
    `fork_prefilled_prompt` is read out of the ephemeral `fork_metadata` blob
    (while non-null); `fork_revert_record` is the durable revert outcome.
    fork_status / fork_needs_replay / the raw blob stay server-internal."""
    try:
        can_fork = get_harness(backend).profile.can_fork
    except Exception:
        can_fork = False
    prefilled: str | None = None
    full_copy = False
    if fork_metadata:
        try:
            meta = json.loads(fork_metadata)
            prefilled = meta.get("prefilled_prompt")
            full_copy = bool(meta.get("full_copy"))
        except (json.JSONDecodeError, AttributeError):
            prefilled = None
    revert: dict[str, Any] | None = None
    if fork_revert_record:
        try:
            revert = json.loads(fork_revert_record)
        except json.JSONDecodeError:
            revert = None
    return {
        "can_fork": can_fork,
        "forked_from_session_id": forked_from_session_id,
        "fork_after_seq": fork_after_seq,
        "fork_prefilled_prompt": prefilled,
        "fork_revert_record": revert,
        # A /fork copy-dir duplicate vs a /rewind branch — the UI renders the
        # fork banner / sidebar badge differently (session-fork.md).
        "fork_is_full_copy": full_copy,
    }


@dataclass
class RecordedInput:
    """One fixed request input retained through uncertain inline delivery."""

    seq: int | None
    message: MessageContent
    git_head: str | None = None
    git_status_clean: bool | None = None
    confirmed: bool = False


@dataclass
class QueuedPrompt:
    """A user turn waiting to run.

    Carries both the raw prompt text and any attachments the user
    uploaded with it. Admission validates every session-owned file; execution
    validates again after queueing and resolves the absolute paths. A missing
    required file fails the complete input before any model starts.
    """

    prompt: str
    attachment_ids: list[str]
    wiki_mode: str | None = None
    trusted_request: str | None = None
    knowledge_context: KnowledgeContext | None = None
    source: str = "human"
    priority: bool = False
    request_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    delivery_id: str | None = None
    started: bool = False
    work_id: str | None = None
    order: int = 0
    question_session_id: str | None = None
    question_id: str | None = None
    recorded_input: RecordedInput | None = None
    queued_event: dict[str, Any] | None = field(default=None, repr=False)

    @property
    def summary(self) -> str:
        return self.prompt[:1000]


@dataclass
class PendingInput:
    """Owner's queue projection; execution reloads the durable input by ID."""

    request_id: str
    source: str
    summary: str
    work_id: str | None
    priority: bool
    order: int
    queued_event: dict[str, Any] | None = field(default=None, repr=False)

    @property
    def started(self) -> bool:
        return False


@dataclass
class PendingQuestion:
    """One question's form, answer delivery and publication lifetime.

    Answered records remain readable by late polls until their attempt ends.
    A closed record stays owned while its closure is still being published.
    """

    question_id: str
    questions: list[dict[str, Any]]
    deadline: str | None = None
    expires_at: float | None = None
    request_id: str | None = None
    attempt_id: str | None = None
    work_id: str | None = None
    state: Literal["pending", "answered", "closed"] = "pending"
    answer: str | None = None
    ready: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    timer: asyncio.Task[None] | None = field(default=None, repr=False)
    request_publication: QuestionNotification | None = field(default=None, repr=False)
    publication: QuestionNotification | None = field(default=None, repr=False)
    retirement_requested: bool = False


@dataclass
class BackendRetirement:
    """One original resource and the bounded native cards its owner ended."""

    backend: HarnessRun
    task: asyncio.Task[None]
    notifications: deque[dict[str, Any]] = field(default_factory=deque)
    audience: str | None = None


@dataclass
class ActiveRun:
    """The session's execution resources, retained for their actual lifetimes.

    Dispatch, result selection, question admission and backend retirement end
    separately. Replacing a request never drops a held or retiring handle.
    Identities come from the existing request, settlement, attempt and backend.
    """

    request: QueuedPrompt | None = None
    # Selected before backend initialization and retained for terminal identity.
    settlement: _Settlement | None = None
    backend: HarnessRun | None = field(default=None, repr=False)
    # The reaper's clock only applies while a finished process is held.
    held_since: float | None = None
    retirements: dict[int, BackendRetirement] = field(default_factory=dict, repr=False)
    # Question admission closes before the settlement loses its last attempt.
    question_attempt_id: str | None = None
    question_watchdog: dict[str, Any] | None = field(default=None, repr=False)
    knowledge_context: KnowledgeContext | None = None
    wiki_task_id: str | None = None
    wiki_mode: str | None = None

    @property
    def work_id(self) -> str | None:
        return self.settlement.work_id if self.settlement is not None else None


@dataclass
class Session:
    id: str
    name: str
    working_dir: str
    status: SessionStatus = SessionStatus.idle
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    claude_session_id: str | None = None
    credential_id: str | None = None
    # Owning agent (agent-refactor.md). agent_id is required for any session
    # created post-refactor; left optional on the dataclass so legacy
    # in-memory construction paths don't break mid-migration.
    agent_id: str | None = None
    # Who created this session: 'user' | 'schedule' | 'delegation' | 'fork' |
    # 'application' | 'app'. Scheduler fires auto-archive on idle (§5.6); user
    # sessions
    # persist. 'delegation' sessions auto-archive on idle too — they're a
    # transient child spawned by an agent-to-agent ask_agent call
    # (agent-collaboration.md §5.2).
    origin: str = "user"
    # Which AI backend drives this session ('claude-code' | 'codex').
    backend: str = "claude-code"
    # Per-session model (`/model`). None = inherit the agent's, whose None
    # means the backend's own default. Same shape as `backend` above.
    model: str | None = None
    reasoning_effort: str | None = None
    service_tier: str | None = None
    # Who owns this conversation (multi-tenancy.md §5). Denormalised from the
    # agent on purpose: `agent_id` is nullable, and the WebSocket fan-out has
    # to route by owner on every frame.
    user_id: str | None = None
    # Agent-to-agent: parent session that spawned this delegation, or None
    # for every non-delegation session. Used by the delegation listener to
    # route replies/questions/errors back to the parent and by guards to
    # walk the caller chain (cycle + depth). (agent-collaboration.md §4.1)
    parent_session_id: str | None = None
    # The original delegation prompt, kept verbatim for UI display on
    # delegation sessions. NULL elsewhere.
    delegation_request: str | None = None
    # The application that owns this session — its build session, or a
    # conversation the running app is holding with an agent (origin='app').
    # NULL on every ordinary session. (app-agent-access.md §3)
    app_id: str | None = None
    # Live sub-agent runs, keyed by the tool call that spawned them
    # (native-subagents.md §4). Broadcast-only state: the durable record is
    # the Task / collab tool call already in the transcript, so this exists
    # to keep a card alive across a browser reload, not across a restart.
    _subagents: dict[str, Any] = field(default_factory=dict, repr=False)
    # Session tree-rewind / fork (session-rewind.md §4). All NULL/False
    # on non-fork sessions. fork_metadata / fork_revert_record hold raw JSON
    # strings (parsed lazily); fork_status drives crash recovery.
    forked_from_session_id: str | None = None
    fork_after_seq: int | None = None
    fork_needs_replay: bool = False
    fork_metadata: str | None = None
    fork_revert_record: str | None = None
    fork_status: str | None = None
    # Stored rows and reserved sequence numbers are deliberately separate.
    _message_count: int = field(default=0, repr=False)
    _next_message_seq: int = field(default=0, repr=False)
    _transcript_integrity: Literal["ok", "duplicate_seq"] = field(default="ok", repr=False)

    def restore_message_state(self, count: int, next_seq: int, duplicates: int) -> None:
        self._message_count = count
        fork_floor = self.fork_after_seq + 1 if self.fork_after_seq is not None else 0
        self._next_message_seq = max(self._next_message_seq, next_seq, fork_floor)
        self._transcript_integrity = "duplicate_seq" if duplicates else "ok"
    # Set True for the lifetime of a fork-create saga against this session as
    # the PARENT (session-rewind.md §5.4). start_message() refuses while
    # set; the original owner installs and releases this lifecycle reservation.
    _forking: bool = field(default=False, repr=False)
    _run: ActiveRun = field(default_factory=ActiveRun, repr=False)
    _deletion_task: asyncio.Task | None = field(default=None, repr=False)
    _command_receipts: dict[str, tuple[str, dict[str, Any]]] = field(default_factory=dict, repr=False)
    _questions: dict[str, PendingQuestion] = field(default_factory=dict, repr=False)
    _pending_queue: list[PendingInput] = field(default_factory=list, repr=False)
    _dispatch_paused: bool = field(default=False, repr=False)
    _admission_closed: bool = field(default=False, repr=False)
    _archive_reservation: object | None = field(default=None, repr=False)
    _dispatch_revision: int = field(default=0, repr=False)
    _dispatch_order: int = field(default=0, repr=False)
    _delivery_receipts: dict[str, SubmissionReceipt] = field(default_factory=dict, repr=False)
    _request_completions: dict[str, asyncio.Future[RequestCompletion]] = field(default_factory=dict, repr=False)
    _inline_requests: dict[str, list[QueuedPrompt]] = field(default_factory=dict, repr=False)
    _delivery_notice_ids: set[str] = field(default_factory=set, repr=False)
    # Steers: messages typed while THIS turn is running, to be handed to the
    # CLI mid-turn rather than queued behind the turn (inline-steering.md §8).
    # The owner captures one prepared/write operation at a time and receives
    # its result independently of the backend's stdout progress.
    _steer_queue: list[PendingInput] = field(default_factory=list, repr=False)
    # Set while a turn can still accept a steer: from the moment the stream
    # starts after the initial input completes until `result` is seen. Only
    # the owner accepts steers or changes this window.
    _steer_open: bool = field(default=False, repr=False)


class SessionManagerBase:
    """Shared state and the broadcast bus every other slice stands on.

    Holds the session map, the subscriber list, the injected collaborators and
    the tuning constants. A mixin inherits this rather than declaring the
    attributes it borrows, so the type checker knows what it may touch — the
    lesson from the database split, where a method-only partition silently
    dropped the class constants and `Database` ended up with no `initialize`.
    """

    # ---------------------------------------------------------------- contracts
    #
    # What one slice calls on another. Declared here rather than left implicit
    # because the split makes the dependency real: `turns` in particular reaches
    # into credentials, processes, events, outcomes and questions, which is the
    # honest shape of driving a turn and worth being able to see in one place.
    #
    # Every one is overridden by the mixin that owns it — the mixins precede
    # this class in the MRO — so these bodies never run. They exist so the type
    # checker can verify the calls, which is the check that caught the database
    # split dropping its class constants.

    def _session_owner(self, session: Session) -> SessionOwner:
        raise NotImplementedError  # provided by a mixin

    def session_owner_failed(self, session_id: str) -> bool:
        raise NotImplementedError  # provided by a mixin

    def _require_session_writer(self, session: Session) -> None:
        raise NotImplementedError  # provided by a mixin

    def _session_removed(self, session: Session) -> None:
        raise NotImplementedError  # provided by a mixin

    async def _join_session_owner_commands(self, session: Session) -> None:
        raise NotImplementedError  # provided by a mixin

    async def close_session_owners(self) -> None:
        raise NotImplementedError  # provided by a mixin

    async def _blit_attachments_to_descendant_forks(self, session_id: str) -> None:
        raise NotImplementedError  # provided by a mixin

    async def _fork_attachment_source(self, session_id: str, attachment_id: str, *, user_id: str) -> Path | None:
        raise NotImplementedError  # provided by a mixin

    async def require_active_agent(self, agent_id: str | None) -> dict[str, Any] | None:
        raise NotImplementedError

    def agent_operation(self, agent_id: str | None) -> AbstractAsyncContextManager[None]:
        raise NotImplementedError

    async def _run_agent_operation[T](
        self, agent_id: str | None, command: Callable[[], Awaitable[T]],
    ) -> T:
        raise NotImplementedError

    def _own_task(self, task: asyncio.Task[Any]) -> None:
        raise NotImplementedError

    def session_is_executing(self, session: Session) -> bool:
        raise NotImplementedError

    async def await_session_execution(self, session_id: str, *, timeout: float | None = None) -> None:
        raise NotImplementedError


    async def _retire_backend(self, session: Session, backend: HarnessRun, *, reason: str = "process_ended") -> asyncio.Task[None]:
        raise NotImplementedError

    async def _await_backend_retirement(self, session: Session, *, timeout: float | None = None) -> None:
        raise NotImplementedError

    async def cancel_request(self, session_id: str, request_id: str) -> bool:
        raise NotImplementedError


    async def drain_turns(self, timeout: float = 5.0) -> int:
        raise NotImplementedError  # provided by a mixin

    def dispatch_snapshot(self, session_id: str) -> dict[str, Any]:
        raise NotImplementedError  # provided by a mixin

    def has_pending_question(self, session_id: str, question_id: str) -> bool:
        raise NotImplementedError  # provided by a mixin

    def get_pending_questions(self, session_id: str) -> tuple[PendingQuestion, ...]:
        raise NotImplementedError  # provided by a mixin

    async def resume_dispatch(self, session_id: str, *, resume_queue: bool = True) -> bool:
        raise NotImplementedError  # provided by a mixin

    async def _record_native_question(
        self, session: Session, question_id: str, questions: list[dict[str, Any]], *, attempt_id: str,
    ) -> PendingQuestion | None:
        raise NotImplementedError

    async def _clear_fork_first_turn_state(self, session: Session) -> None:
        raise NotImplementedError  # provided by a mixin

    async def _enforce_held_cap(self, *, keep_session_id: str | None = None) -> int:
        raise NotImplementedError  # provided by a mixin

    @staticmethod
    def _event_to_message_content(event: HarnessEvent) -> MessageContent | None:
        raise NotImplementedError  # provided by a mixin

    @staticmethod
    def _event_to_ws_message(session_id: str, event: HarnessEvent) -> dict[str, Any] | None:
        raise NotImplementedError  # provided by a mixin

    @staticmethod
    def _tag_persisted(
        event: dict[str, Any], seq: int | None, msg: MessageContent
    ) -> dict[str, Any]:
        raise NotImplementedError  # provided by a mixin

    def _flush_text_deltas(
        self, session_id: str, buf: list[str]
    ) -> dict[str, Any] | None:
        raise NotImplementedError  # provided by a mixin

    def _forget_backend(self, session: Session, expected: HarnessRun | None = None, *, reason: str = "process_ended") -> list[dict[str, Any]]:
        raise NotImplementedError  # provided by a mixin

    async def _load_agent(self, session: Session) -> dict[str, Any] | None:
        raise NotImplementedError  # provided by a mixin

    def _make_run(
        self,
        session: Session,
        agent: dict[str, Any] | None = None,
        connectors: list[tuple[Any, Any]] | None = None,
    ) -> HarnessRun:
        raise NotImplementedError  # provided by a mixin

    async def _mark_needs_reconnect(
        self, credential_id: str, code: RefreshErrorCode
    ) -> None:
        raise NotImplementedError  # provided by a mixin

    async def drain_credential_refreshes(self) -> None:
        raise NotImplementedError  # provided by a mixin

    async def _persist_message(
        self,
        session: Session,
        msg: MessageContent,
        *,
        git_head: str | None = None,
        git_status_clean: bool | None = None,
    ) -> int | None:
        raise NotImplementedError  # provided by a mixin

    @staticmethod
    def _record_subagent(session: Session, update: Any) -> None:
        raise NotImplementedError  # provided by a mixin

    async def _recover_incomplete_forks(self) -> None:
        raise NotImplementedError  # provided by a mixin

    async def _resolve_credential(
        self, session: Session, agent: dict[str, Any] | None, harness
    ) -> HarnessCredential | None:
        raise NotImplementedError  # provided by a mixin




    async def _surface_auth_expiry(
        self, session: Session, *, cred_id: str | None, backend: str
    ) -> dict[str, Any]:
        raise NotImplementedError  # provided by a mixin

    async def _surface_stale_session(
        self, session: Session, *, backend: str, replayed: int = 0, omitted: int = 0
    ) -> dict[str, Any]:
        raise NotImplementedError  # provided by a mixin

    async def _surface_transient_exhausted(
        self, session: Session, *, backend: str, attempts: int
    ) -> dict[str, Any]:
        raise NotImplementedError  # provided by a mixin

    async def _surface_transient_retry(
        self, session: Session, *, attempt: int, max_attempts: int, delay: float
    ) -> dict[str, Any]:
        raise NotImplementedError  # provided by a mixin

    async def _recovery_frame(
        self, session: Session, *, code: str, message: str,
        phase: Literal["diagnostic", "retrying", "recovered"] = "retrying",
        attempt: int | None = None, maximum: int | None = None,
        delay: float | None = None,
    ) -> dict[str, Any]:
        raise NotImplementedError  # provided by a mixin

    async def _surface_turn_timeout(
        self, session: Session, *, reason: str, limit: int, backend: str, retries: int = 0
    ) -> dict[str, Any]:
        raise NotImplementedError  # provided by a mixin

    async def _surface_idle_retry(
        self, session: Session, *, limit: int, attempt: int, max_attempts: int
    ) -> dict[str, Any]:
        raise NotImplementedError  # provided by a mixin

    async def auto_archive_scheduled_session(self, session_id: str) -> bool:
        raise NotImplementedError  # provided by a mixin

    async def _restore_task_return(
        self, owned: OwnedTaskContext | None, *, session_id: str, work_id: str | None,
    ) -> tuple[Session, KnowledgeContext] | None:
        raise NotImplementedError  # provided by lifecycle

    async def _task_return_eligibility(
        self, owned: OwnedTaskContext | None, *, session_id: str, work_id: str | None,
    ) -> tuple[str | None, KnowledgeContext] | ReturnRefused:
        raise NotImplementedError  # provided by lifecycle

    async def admit_return(
        self, target: ReturnTarget, *, prepare: Callable[[], Awaitable[ReturnBody | ReturnSkipped]],
    ) -> ReturnAdmission:
        raise NotImplementedError  # provided by turns

    def _session_dispatch_snapshot(self, session: Session) -> dict[str, Any]:
        raise NotImplementedError  # provided by turns

    async def _archive_original(
        self, session: Session, *, replace: bool = False, successor: Session | None = None,
        only_request_id: str | None = None, automatic: bool = False,
    ) -> tuple[bool, Session | None]:
        raise NotImplementedError  # provided by lifecycle

    async def _release_session_reservation(self, owner: SessionOwner, command: ReleaseArchive | ReserveFork) -> None:
        raise NotImplementedError  # provided by a mixin

    async def _failed_owner_retirement(self, owner: SessionOwner) -> asyncio.Task[None] | None:
        raise NotImplementedError  # provided by ownership

    async def interrupt(self, session_id: str) -> bool:
        raise NotImplementedError  # provided by a mixin

    async def resolve_credential_by_id(
        self,
        cred_id: str | None,
        *,
        backend: str,
        context: str = "",
        require_auth: bool = True,
        user_id: str | None = None,
    ) -> HarnessCredential | None:
        raise NotImplementedError  # provided by a mixin

    _DURABLE_FORK_META_KEYS = ("full_copy", "duplicated_from")
    _AUTO_ARCHIVE_ORIGINS = ("schedule",)
    _AUTO_ARCHIVE_ELIGIBLE = ("schedule", "delegation")
    _MAX_RECOVERY_ATTEMPTS = 1
    _MAX_TRANSIENT_RETRIES = 2
    _TRANSIENT_RETRY_BASE_DELAY = 1.0  # seconds; doubles each attempt
    _OAUTH_REFRESH_LEEWAY_SEC = 300
    _BACKEND_DISPLAY = {"claude-code": "Claude Code", "codex": "Codex"}
    AUTO_ANSWER_TEXT = (
        "No human is available to answer this question right now. "
        "Proceed with the task autonomously and try hard to finish it without "
        "asking again. Make the most reasonable choice and continue.\n\n"
        "Only stop and leave a clear note describing what you would have done "
        "if the next action is genuinely risky or irreversible — for example: "
        "destroying data, force-pushing or rewriting shared git history, "
        "deploying to production, modifying billing/payments, sending "
        "messages or emails to external recipients, or running commands that "
        "affect shared infrastructure. For everything else (ambiguous design "
        "choices, formatting, library picks, small refactors), pick the most "
        "reasonable option and keep going."
    )

    def __init__(self) -> None:
        self.sessions: dict[str, Session] = {}
        self._session_owners: dict[int, SessionOwner] = {}
        # Serialize archive/restoration without retaining historical sessions.
        self._session_lifecycle_locks: weakref.WeakValueDictionary[str, asyncio.Lock] = weakref.WeakValueDictionary()
        # Carry Stop and dispatch ordering to the next live owner in this
        # service generation. No input or execution is retained here.
        self._archived_dispatch: dict[str, tuple[bool, int, int]] = {}
        self._dispatch_generation = uuid.uuid4().hex
        self._retired_dispatch_generations: frozenset[str] = frozenset()
        self._admission_closed = False
        self._input_state: Literal["uninitialized", "initializing", "ready", "failed"] = "uninitialized"
        self._owned_tasks: set[asyncio.Task[Any]] = set()
        # Resolution owns refresh through provider completion and conditional writeback.
        self._credential_refreshes: dict[tuple[str | None, str], asyncio.Task[HarnessCredential | None]] = {}
        self._credential_refresh_closed = False
        # Short configuration operations share a lock, independently of turn
        # execution. Read/validate/write and turn snapshots must agree.
        self.configuration_lock = asyncio.Lock()
        self._agent_lifecycle_lock = asyncio.Lock()
        self._wiki_dispatch_locks: dict[str | None, asyncio.Lock] = {}
        self._closed_agents: set[str] = set()
        self._agent_operations: dict[str | None, set[asyncio.Future[None]]] = {}
        self._agent_archive_tasks: dict[str, asyncio.Task[None]] = {}
        # key -> (callback, subscriber user_id or None for internal)
        self._broadcast_callbacks: dict[str, tuple[Callable, str | None]] = {}
        # Business observers receive the same completion value as the receipt;
        # subscriber delivery is an independent fact in that value.
        self._completion_callbacks: dict[str, Callable[[str, RequestCompletion], Coroutine[Any, Any, None]]] = {}
        self.db: Database | None = None
        # Background task that drops idle held CLI processes.
        self._reaper_task: asyncio.Task[None] | None = None
        # Wired in by main.py once the manager is constructed. Kept as
        # an opaque object — we only call `.fire(event)` on it — so the
        # session manager doesn't take a hard dependency on the
        # notifiers package's import surface.
        self._notifier_manager: Any = None
        # Likewise wired in by main.py — the ScheduleRunner. Used to
        # re-register jobs when archiving a session repoints the schedules
        # anchored to it. Opaque: we only call `.reschedule(row)`.
        self._schedule_runner: Any = None

    def set_notifier_manager(self, mgr: Any) -> None:
        self._notifier_manager = mgr

    def set_schedule_runner(self, runner: Any) -> None:
        self._schedule_runner = runner

    async def initialize(self, db: Database, *, previous_writers_retired: bool = False) -> None:
        if self._input_state == "initializing":
            raise RuntimeError("SessionManager initialization is already in progress")
        self._input_state = "initializing"
        self._admission_closed = False
        try:
            await self._initialize_storage(db, previous_writers_retired=previous_writers_retired)
        except BaseException:
            self._input_state = "failed"
            raise
        self._input_state = "ready"

    def input_database(self) -> Database:
        """Only completed initialization/recovery authorizes input operations."""
        from ..submission_models import InputAdmissionNotReady

        if self._input_state != "ready":
            raise InputAdmissionNotReady(self._input_state)
        if self.db is None:
            raise InputAdmissionNotReady("failed")
        self.db.health.require_healthy()
        return self.db

    async def _initialize_storage(self, db: Database, *, previous_writers_retired: bool) -> None:
        await self.drain_credential_refreshes()
        self._credential_refresh_closed = False
        if self._session_owners:
            await self.close_session_owners()
            self._session_owners.clear()
        if self.db is not db:
            # Lifespan binds a new runner after initialization. Retirement
            # must never query a runner that still owns an earlier database.
            self._schedule_runner = None
        self.configuration_lock = asyncio.Lock()
        self._agent_lifecycle_lock = asyncio.Lock()
        self._closed_agents.clear()
        self._wiki_dispatch_locks.clear()
        self._agent_operations.clear()
        self._agent_archive_tasks.clear()
        self._archived_dispatch.clear()
        self.db = db
        self._dispatch_generation = uuid.uuid4().hex
        self._retired_dispatch_generations = frozenset()
        previous_generations = await db.recover_client_submissions()
        # Only the verified startup boundary can prove retirement of these
        # pre-existing generations. Current work always retains its Owner and
        # exact resources; later unknown inputs cannot inherit this proof.
        if previous_writers_retired:
            self._retired_dispatch_generations = previous_generations
        # Turns in flight unwind before this connection does (see
        # `drain_turns`). Registered here rather than in the lifespan so a test
        # that builds its own manager gets it too — which is where the hang it
        # prevents actually showed up.
        db.add_close_hook(self.close_session_owners)
        db.add_close_hook(self.drain_turns)
        db.add_close_hook(self.drain_credential_refreshes)
        rows = await db.load_sessions()
        for row in rows:
            session = _session_from_row(row)
            from ..db.applications import ApplicationDeleting

            try:
                await db.require_application_session_writable(session.id)
            except ApplicationDeleting:
                session._admission_closed = True
            session.restore_message_state(*await db.message_sequence_state(session.id))
            self.sessions[session.id] = session
        logger.info("Loaded %d sessions from database", len(rows))
        # Sweep forks left mid-saga by a crash (session-rewind.md §5.6.7).
        await self._recover_incomplete_forks()

    def on_broadcast(
        self, key: str, callback: Callable, user_id: str | None = None
    ) -> None:
        """Subscribe to the bus.

        `user_id=None` means *internal*: the delegation manager, the
        application manager and the notifier all need every frame, because
        their job is to react to other people's sessions on the server's
        behalf. A browser connection passes its account and sees only that
        account's frames (multi-tenancy.md §7).
        """
        self._broadcast_callbacks[key] = (callback, user_id)

    def remove_broadcast(self, key: str) -> None:
        self._broadcast_callbacks.pop(key, None)

    def on_request_complete(self, key: str, callback: Callable[[str, RequestCompletion], Coroutine[Any, Any, None]]) -> None:
        self._completion_callbacks[key] = callback

    def remove_request_complete(self, key: str) -> None:
        self._completion_callbacks.pop(key, None)

    def _audience(self, message: dict, user_id: str | None) -> str | None:
        """Whose frame this is, or None for one that belongs to the install.

        Told explicitly when the caller knows (an agent row carries its owner),
        otherwise derived from the session the frame is about. A frame with
        neither is a system event — `schedules_changed` has no session and no
        owner — and goes to everyone.
        """
        if user_id is not None:
            return user_id
        sid = message.get("session_id") or message.get("old_session_id")
        if isinstance(sid, str):
            session = self.sessions.get(sid)
            if session is not None:
                return session.user_id
        return None

    async def _broadcast(self, message: dict, *, user_id: str | None = None) -> None:
        """Deliver to the subscribers entitled to see it.

        Before accounts this was a fan-out to every callback, which with
        accounts would stream one person's assistant text into another
        person's browser — the quietest possible way to get multi-tenancy
        wrong, since nothing errors and both tabs look plausible.
        """
        audience = self._audience(message, user_id)
        sid = message.get("session_id") or message.get("old_session_id")
        for cb, subscriber in list(self._broadcast_callbacks.values()):
            if subscriber is not None and audience is None and isinstance(sid, str):
                # An await in an earlier callback can cross account adoption.
                # Unknown session ownership never becomes install-wide access.
                audience = self._audience(message, user_id)
                if audience is None and self.db and await self.db.count_users() > 0:
                    audience = await self.db.get_session_owner(sid)
                    if audience is None:
                        continue
            if subscriber is not None and audience is not None and subscriber != audience:
                continue
            try:
                await cb(message)
            except Exception:
                logger.exception("Broadcast callback error")

    def list_sessions(self, user_id: str | None = None) -> list[Session]:
        """Every live session, or only one account's (multi-tenancy.md §5).

        `user_id=None` means "no scoping asked for", which is what the
        background paths want — the scheduler firing everyone's jobs, the
        delegation manager walking a chain, the recovery sweeps at boot. A
        *request* must always pass one, and `deps.Ctx` is what makes that the
        path of least resistance rather than a thing to remember.
        """
        rows = list(self.sessions.values())
        if user_id is None:
            return rows
        return [s for s in rows if s.user_id == user_id]

    async def session_belongs_to(
        self, session_id: str, user_id: str | None
    ) -> bool:
        """Does this account own the session — live or archived?

        For the routes that deliberately serve an archived session: an
        attachment's thumbnail has to keep rendering after the conversation is
        put away, so they cannot resolve ownership through the in-memory map
        alone. `user_id=None` is the pre-accounts install, where nobody owns
        anything and everybody who got past `verify_token` is the operator.
        """
        if user_id is None:
            return True
        if self.get_session(session_id, user_id) is not None:
            return True
        rows = await self.input_database().load_sessions(include_archived=True, user_id=user_id)
        return any(row["id"] == session_id for row in rows)

    def get_session(
        self, session_id: str, user_id: str | None = None
    ) -> Session | None:
        """One session, or None — including when it exists but belongs to
        someone else, which a caller must not be able to tell apart from
        "no such session"."""
        session = self.sessions.get(session_id)
        if session is None:
            return None
        if user_id is not None and session.user_id != user_id:
            return None
        return session

    async def _fire_session_idle_notification(self, session: Session) -> None:
        """Notify async targets that this session just went fully idle.

        Best-effort: any failure inside a notifier is logged by the
        manager. Skipped if no manager is wired (tests, etc.).
        """
        if self._notifier_manager is None or session._dispatch_paused or session._pending_queue:
            return
        try:
            from ..notifiers import NotifierEvent

            await self._notifier_manager.fire(
                NotifierEvent(
                    type="session_idle",
                    title=session.name or "Session idle",
                    message=(
                        f"Session '{session.name}' finished its work and is idle."
                    ),
                    session_id=session.id,
                    session_name=session.name,
                ),
                session.user_id,
            )
        except Exception:
            logger.exception(
                "notifier_manager.fire raised for session %s", session.id
            )




def _guess_mime(filename: str) -> str:
    """Lightweight MIME guess for replayed attachments.

    Mirrors the upload-time logic in `server.attachments._detect_mime`,
    but we don't have the client's declared MIME at replay so we always
    derive from the filename extension.
    """
    import mimetypes

    guess, _ = mimetypes.guess_type(filename)
    return guess or "application/octet-stream"


def _split_tool_list(raw: str | None) -> list[str] | None:
    """Parse an agent's newline-separated tool/MCP name list.

    Empty / whitespace-only → None (meaning "no restriction" for allow,
    "nothing extra" for deny). Order preserved, blanks dropped.
    """
    if not raw:
        return None
    items = [line.strip() for line in raw.splitlines() if line.strip()]
    return items or None


def _augment_prompt_with_attachments(prompt: str, paths: list[str]) -> str:
    """Prepend an `<attachments>` block listing absolute paths.

    The agent (Claude Code, Codex, anything with a Read tool) sees the
    paths in its input and can open them on demand. Format kept terse
    and obvious — one path per line so the model doesn't have to parse
    anything clever.
    """
    if not paths:
        return prompt
    lines = ["<attachments>"]
    lines.extend(f"- {p}" for p in paths)
    lines.append("</attachments>")
    lines.append("")
    lines.append(prompt)
    return "\n".join(lines)


# Singleton
