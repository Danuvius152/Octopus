"""Agent-to-agent delegation manager (agent-collaboration.md).

The shape mirrors `server/bg_tasks.py`: an asynchronous, fire-and-forget
operation on the parent agent's behalf, whose terminal result is
delivered back to the parent session as a *new* injected user message
(routed through `SessionManager.admit_return`, the same path bg-task
results use). That parent-injection is what gives the parent agent a
fresh turn to react to the child's reply.

For agent collaboration, the operation is "spawn a child Session under
another agent and complete a work round there". A delegation is **not** a new
persistence concept — it is a normal `Session` row with
``origin='delegation'`` and ``parent_session_id`` set. The delegation id
*is* the child session id; we don't invent a parallel id space.

What this module owns:

  - The registry of live and finished rounds, with the latest round saved
    on its existing child-session row for explicit continuation after restart.
  - The original request's completion consumer, which selects the round's
    result before reading its committed reply. Broadcasts only forward
    questions; they do not select a second business result.
  - The cycle and depth guards that walk the parent chain.
  - The agent-name lookup (case-insensitive, ambiguity-rejecting).
  - The injection formatter: ``[agent-reply|agent-error:<name>
    delegation=<id>]`` plus the body, fed through
    ``SessionManager.admit_return`` under the original return identity.

What this module does NOT own:

  - The ``ask_agent`` MCP server (Phase 2). The server is a thin
    stdio shim that POSTs to the FastAPI routes which call into here.
  - The caller-aware ``ask`` server (Phase 3) — that's a one-line
    change in the existing question handler.
  - Frontend rendering (Phase 4).

Restart restores receipts and closes old work identities. It neither runs
the child nor replays a reply; the original parent must explicitly follow up.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import Callable, Coroutine
from contextlib import AsyncExitStack
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

if TYPE_CHECKING:
    from .database import Database
    from .db.sessions import SessionRelation
    from .session_manager import SessionManager
    from .sessions.base import Session

from .aio import complete_cleanup
from .bg_tasks import bg_task_manager
from .db.applications import ApplicationDeleting
from .db.sessions import DelegationWorkPending, TicketReviewAdmissionClosed
from .diagnostics import exception_diagnostic, safe_diagnostic
from .knowledge import (
    AdmittedRequest,
    OwnedTaskContext,
    capture,
    decode_owned,
    derive,
    encode,
)
from .mcp_identity import McpScope, close_work, require_live_scope, work_is_open
from .monitor import Event as _MonEvent
from .monitor import record as _mon_record
from .question_resolution import UNKNOWN_SOURCE, AnswerSource, QuestionAnswerInvalid
from .research.manager import research_manager
from .service_health import ServiceUnavailable
from .sessions.base import TICKET_BATCH_ORIGINS
from .sessions.publication import WorkPublications
from .sessions.runtime_types import (
    AdmissionIntegrityError,
    SessionAdmissionBusy,
    SessionAdmissionClosed,
    SessionMailboxFull,
    SessionOwnerUnavailable,
)
from .sessions.submission import (
    RequestCompletion,
    ReturnBody,
    ReturnRefused,
    ReturnSkipped,
    ReturnTarget,
    ReturnUnconfirmed,
    SubmissionReceipt,
)
from .submission_models import SubmissionConflict

logger = logging.getLogger(__name__)
_T = TypeVar("_T")


# Maximum agent-delegation hops allowed in a single caller chain
# (agent-collaboration.md §5.9). The user is hop 0; each delegation
# (origin='delegation' session) is one hop. Allows root → A → B → C
# (three delegated agents under the human) but rejects deeper. A small
# constant on purpose — agent fan-out is meant to be shallow.
DEPTH_CAP = 3

# Both submission paths send the same round and reply contract to the model.
_DELEGATION_PROTOCOL = (
    "**Delegation protocol — read before responding:**\n"
    "- This delegation round returns one reply when its original work completes. "
    "A round may include multiple executions as owned descendants return; collect "
    "their necessary results before completing the work. Return admission records "
    "handoff, not proof that the receiving model understood the material.\n"
    "- Complete the requested work and put its core evidence and result in a "
    "self-contained final reply. A progress-only preamble is not a completed task.\n"
    "- Reply text comes from the committed result: with a valid final-answer "
    "reference, only the selected final answer is returned; without a final-answer "
    "reference, the existing associated text-fragment policy applies. An explicit "
    "empty final answer stays empty; an invalid reference does not borrow other "
    "text. Do not assume all commentary or tool output will reach the caller.\n"
    "- Tools and background work may help, but necessary results must be collected "
    "and included before this round completes. Independent late results do not "
    "automatically resend an old round's reply. If essential work cannot return "
    "within this round, state the limitation and the concrete follow-up needed "
    "from the caller.\n"
    "- The conversation remains open; human follow-ups do not resend this round's "
    "reply. Only an explicit caller follow-up starts a new delegation round."
)

TerminalDisposition = Literal[
    "accepted", "duplicate", "steered", "closed", "unbound", "unavailable", "not_replayed", "stopped", "unknown",
]
DelegationPurpose = Literal["consultation", "ticket_review"]


class DelegationError(Exception):
    """Surface-level error for the REST layer. Carries an HTTP status so
    the routes can translate uniformly (404 for resolution failures,
    409 for guard violations)."""

    def __init__(self, message: str, *, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


class _PersistedRound(BaseModel):
    """The current round storage contract, validated before restoring identity."""

    model_config = ConfigDict(strict=True, extra="forbid")

    work_id: str = Field(min_length=1)
    parent_work_id: str | None
    target_agent_name: str
    request: str
    created_at: str
    state: Literal["running", "completed", "failed", "cancelled", "interrupted"]
    finished_at: str | None
    error: str | None
    child_work_ids: list[str] = Field(default_factory=list)
    candidate_request_id: str | None = None
    terminal_disposition: TerminalDisposition | None
    terminal_delivery_error: str | None
    knowledge_context: str | None = None
    review_ticket_id: str | None = Field(default=None, min_length=1)

    @model_validator(mode="before")
    @classmethod
    def retire_legacy_relay_markers(cls, value):
        # Existing snapshots have these two markers. Read them only to retire
        # their representation; they never prove consumption or a final result.
        if isinstance(value, dict) and "candidate_request_id" not in value:
            return {key: field for key, field in value.items()
                    if key not in {"awaiting_sub_relay", "deferred_request_id"}}
        return value


@dataclass
class _QuestionDelivery:
    """One live question's relay; discarded when that question closes."""

    delivery_id: str
    disposition: Literal["stopped", "unknown"] | None = None
    error: str | None = None
    receipt: SubmissionReceipt | None = None
    injecting: bool = False
    notice_recorded: bool = False
    command: asyncio.Task[Any] | None = field(default=None, repr=False)


@dataclass
class _CommandRecording:
    """Diagnostic responsibilities held by an existing delegation command."""

    checks: list[Callable[[], bool]] = field(default_factory=list)
    parent_session_id: str | None = None
    parent_work_id: str | None = None
    release_notice: asyncio.Future[None] | None = None
    write_failed: bool = False
    return_sessions: set[str] = field(default_factory=set)
    question_return: asyncio.Future[bool] | None = None

    def finish_admission(self) -> None:
        """The original round now owns the remaining terminal receipt."""
        self.parent_session_id = self.parent_work_id = None


@dataclass
class DelegationRunState:
    """Live record of one delegation. Lifetime: from
    ``start_delegation`` to ``state != "running"`` plus a retention
    window so ``list_agent_tasks`` can show recently-finished items.
    """

    # The id is the child session id — see module docstring.
    delegation_id: str
    parent_session_id: str
    target_agent_id: str
    target_agent_name: str
    request: str
    # One immutable round in the reusable child conversation. Follow-up makes
    # a new record, so an in-flight cancellation retains its original target.
    work_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    parent_work_id: str | None = None
    owned_context: OwnedTaskContext | None = field(default=None, repr=False)
    review_ticket_id: str | None = None
    created_at: str = field(
        default_factory=lambda: datetime.now(UTC).isoformat()
    )
    state: str = "running"  # running | completed | failed | cancelled | interrupted
    finished_at: str | None = None
    error: str | None = None
    _terminal_disposition: TerminalDisposition | None = None
    terminal_delivery_error: str | None = None
    _metric_recorded: bool = False
    _terminal_injecting: bool = False
    # References only: submission/execution/terminal own the consumption fact.
    # Follow-up cannot replace a historical child work still owed to this work.
    _child_work_ids: set[str] = field(default_factory=set)
    _candidate_request_id: str | None = None
    _persisted_state: str | None = field(default=None, repr=False)
    _save_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    _question_deliveries: dict[str, _QuestionDelivery] = field(default_factory=dict, repr=False)
    _terminal_command: asyncio.Task[Any] | None = field(default=None, repr=False)
    _retirement_command: asyncio.Task[DelegationRunState] | None = field(default=None, repr=False)
    _selected_cancellation: tuple[str, str] | None = field(default=None, repr=False)
    _retiring_with_parent: bool = False
    # These writers have different targets: the round CAS needs the parent
    # relationship, while question notices need only the original child.
    _round_recording_closed: bool = False
    _question_notices_closed: bool = False
    # Confirmed row deletion is narrower than a writer closed by replacement.
    _relationship_deleted: bool = False

    def to_public_dict(self) -> dict[str, Any]:
        """API-shape for ``GET /sessions/{sid}/delegations`` and the
        ``ask_agent`` tool's return value."""
        return {
            "delegation_id": self.delegation_id,
            "sub_session_id": self.delegation_id,
            "parent_session_id": self.parent_session_id,
            "target_agent_id": self.target_agent_id,
            "target_agent_name": self.target_agent_name,
            "request": self.request,
            "work_id": self.work_id,
            "state": self.state,
            "created_at": self.created_at,
            "finished_at": self.finished_at,
            "error": self.error,
            "terminal_disposition": self._terminal_disposition,
            "terminal_delivery_error": self.terminal_delivery_error,
            "recording_state": "committed",
        }


class DelegationManager:
    """In-process owner of delegation state.

    Wired in by ``main.py`` lifespan after ``SessionManager.initialize``:
    we register a broadcast listener so the per-session WS events the
    SessionManager already produces become our event source. No extra
    subprocess, no extra polling.
    """

    BROADCAST_KEY = "delegation_manager"

    def __init__(self) -> None:
        # Keyed by delegation_id (== child session id). Records stay
        # around after their terminal state so list_agent_tasks can
        # render recently-finished entries; a future LRU/retention
        # policy can trim this, but at single-user scale a flat dict
        # is fine.
        self._records: dict[str, DelegationRunState] = {}
        self._rounds: dict[str, DelegationRunState] = {}
        self._commands: dict[asyncio.Task[Any], _CommandRecording] = {}
        self._publications = WorkPublications()
        self._closed_parents: set[str] = set()
        self._completion_handoffs: dict[asyncio.Future[Any], tuple[str, Callable[[asyncio.Future[Any]], None]]] = {}
        self.session_mgr: SessionManager | None = None
        self.db: Database | None = None
        self._closing = False

    # ------------------------------------------------------------ wiring

    def bind(self, session_mgr: SessionManager, db: Database) -> None:
        """Subscribe to the session manager's broadcast bus. Idempotent
        on repeat calls (last writer wins on the key)."""
        self.session_mgr = session_mgr
        self.db = db
        self._closing = False
        self._closed_parents.clear()
        session_mgr.on_broadcast(self.BROADCAST_KEY, self._on_broadcast, required=True)
        db.add_close_hook(self._before_database_close)
        db.add_session_delete_hook(self._session_deleted)

    def _session_deleted(self, session_id: str) -> None:
        # Only an acknowledged relationship deletion closes these writers.
        # Owner failure, archive and admission closure do not prove deletion.
        for rec in self._rounds.values():
            if session_id in (rec.delegation_id, rec.parent_session_id):
                rec._round_recording_closed = True
                rec._relationship_deleted = True
            if session_id == rec.delegation_id:
                rec._question_notices_closed = True
        self._retire_delivery_failures()

    def _snapshot(self, rec: DelegationRunState) -> str:
        snapshot = {
            "work_id": rec.work_id, "parent_work_id": rec.parent_work_id,
            "target_agent_name": rec.target_agent_name, "request": rec.request,
            "created_at": rec.created_at, "state": rec.state,
            "finished_at": rec.finished_at, "error": rec.error,
            "child_work_ids": sorted(rec._child_work_ids),
            "candidate_request_id": rec._candidate_request_id,
            "terminal_disposition": rec._terminal_disposition,
            "terminal_delivery_error": rec.terminal_delivery_error,
            "knowledge_context": encode(rec.owned_context) if rec.owned_context is not None else None,
        }
        if rec.review_ticket_id is not None:
            snapshot["review_ticket_id"] = rec.review_ticket_id
        return json.dumps(snapshot, sort_keys=True, separators=(",", ":"))

    def _start_command(
        self, operation: Coroutine[Any, Any, _T], *, parent_session_id: str | None = None,
        parent_work_id: str | None = None,
        return_session_id: str | None = None, question_return: bool = False,
    ) -> asyncio.Task[_T]:
        """Register before scheduling so drain sees completion-created commands."""
        if self._closing:
            operation.close()
            raise DelegationError("Delegation admission is closed", status_code=503)
        assert self.session_mgr is not None
        task = asyncio.create_task(operation, name="delegation-command")
        recording = _CommandRecording(parent_session_id=parent_session_id, parent_work_id=parent_work_id)
        if parent_session_id is not None:
            recording.release_notice = asyncio.get_running_loop().create_future()
        if question_return:
            recording.question_return = asyncio.get_running_loop().create_future()
        if return_session_id is not None:
            recording.return_sessions.add(return_session_id)
        self._commands[task] = recording
        self.session_mgr._own_task(task)

        def finished(done: asyncio.Task[Any]) -> None:
            if recording.question_return is not None and not recording.question_return.done():
                recording.question_return.set_result(False)
            # A failed/cancelled cleanup still owns its resources. Ordinary
            # domain refusals have completed their confirmed local rollback.
            if done not in self._commands or not all(check() for check in recording.checks):
                return
            if recording.write_failed or (not done.cancelled() and (
                done.exception() is None or isinstance(done.exception(), DelegationError)
            )):
                self._forget_command(done)

        task.add_done_callback(finished)
        return task

    def _forget_command(self, task: asyncio.Task[Any]) -> None:
        self._commands.pop(task, None)

    def _retire_delivery_failures(self) -> None:
        # Existing reentry can hand an unfinished diagnostic to its new owned
        # command. Nothing starts or polls here; the old error remains logged.
        for task, recording in tuple(self._commands.items()):
            if recording.write_failed and task.done() and all(check() for check in recording.checks):
                self._forget_command(task)

    def _delivery_recording_failed(self, task: asyncio.Task[Any] | None) -> None:
        if task is not None and task in self._commands:
            self._commands[task].write_failed = True

    async def _command(self, operation: Coroutine[Any, Any, _T], *, parent_session_id: str | None = None,
                       parent_work_id: str | None = None, return_session_id: str | None = None) -> _T:
        """Own the complete operation; an HTTP waiter only owns its wait."""
        task = self._start_command(operation, parent_session_id=parent_session_id, parent_work_id=parent_work_id,
                                   return_session_id=return_session_id)
        await asyncio.wait({task})
        return task.result()

    def _watch_completion(self, session_id: str, work_id: str | None, receipt: SubmissionReceipt) -> None:
        """Observe the admitted request without making drain await live work."""
        rec = self._records.get(session_id)
        if not receipt.accepted or rec is None or rec.work_id != work_id:
            return

        def completed(future: asyncio.Future[Any]) -> None:
            if self._completion_handoffs.pop(future, None) is None:
                return
            self._start_command(self._complete_request(rec, receipt), return_session_id=rec.parent_session_id)

        if receipt._completion in self._completion_handoffs:
            return
        self._completion_handoffs[receipt._completion] = (rec.parent_session_id, completed)
        receipt._completion.add_done_callback(completed)

    async def _complete_request(self, rec: DelegationRunState, receipt: SubmissionReceipt) -> None:
        """Classify one exact receipt before any asynchronous reply-text read."""
        completion = await receipt.wait()
        if completion.input_request_id != receipt.request_id:
            raise RuntimeError("Delegation completion changed the original input identity")
        if await self._save_round(rec, running_only=True, completion=completion):
            if rec.state != "running":
                await self._inject_terminal(rec, request_id=completion.request_id)

    async def _missing_relationship(
        self, rec: DelegationRunState, user_id: str | None,
    ) -> DelegationError | None:
        """Interpret an acknowledged CAS miss against the current relation."""
        assert self.db is not None
        async with self.db.session_relation_snapshot() as read:
            rows = await read((rec.parent_session_id, rec.delegation_id))
        parent, child = rows.get(rec.parent_session_id), rows.get(rec.delegation_id)
        if parent is None or child is None:
            rec._relationship_deleted = True
        if child is None:
            rec._question_notices_closed = True
        if parent is None or parent.user_id != user_id:
            rec._round_recording_closed = True
            return DelegationError("Parent session is gone", status_code=404)
        if child is None:
            rec._round_recording_closed = True
            return DelegationError("Child session is gone; start a fresh delegation", status_code=409)
        if (child.parent_session_id != rec.parent_session_id
                or child.user_id != user_id or child.origin != "delegation"
                or child.agent_id != rec.target_agent_id):
            rec._round_recording_closed = True
            return DelegationError("Child delegation relationship changed; start a fresh delegation", status_code=409)
        target = await self.db.get_agent(rec.target_agent_id)
        if target is None or target["user_id"] != user_id:
            rec._round_recording_closed = True
            return DelegationError("Target agent is gone", status_code=404)
        return None

    async def _commit_round(
        self, rec: DelegationRunState, proposed: DelegationRunState,
        user_id: str | None,
        *, parent_change: tuple[DelegationRunState, DelegationRunState] | None = None,
        review_binding: tuple[AdmittedRequest, str] | None = None,
        confirm_selected_write: bool = False,
    ) -> str:
        """Commit the captured full value while the record lock is held.

        A storage exception belongs to the database's fail-stop contract. Only
        an acknowledged CAS miss permits reads to explain a normal conflict.
        The caller publishes the returned baseline without another await.
        """
        assert self.db is not None
        expected, snapshot = rec._persisted_state, self._snapshot(proposed)
        saved = await self.db.compare_and_set_delegation_state(
            rec.delegation_id, expected, snapshot,
            parent_session_id=rec.parent_session_id, user_id=user_id,
            target_agent_id=rec.target_agent_id,
            parent_expected_state=parent_change[0]._persisted_state if parent_change else None,
            parent_new_state=self._snapshot(parent_change[1]) if parent_change else None,
            seal_request_id=proposed._candidate_request_id if proposed.state == "completed"
                and (rec.state != "completed" or rec.work_id != proposed.work_id) else None,
            review_binding=review_binding,
        )
        if not saved:
            missing = await self._missing_relationship(rec, user_id)
            if missing is not None:
                raise missing
            actual = await self.db.load_delegation_state(
                rec.delegation_id, parent_session_id=rec.parent_session_id, user_id=user_id,
            )
            if confirm_selected_write and actual == snapshot:
                return snapshot
            if actual is not None and json.loads(actual).get("work_id") != rec.work_id:
                raise DelegationError("Delegation already has a new round", status_code=409)
            raise DelegationError("Delegation state changed concurrently", status_code=409)
        return snapshot

    async def _commit_admission(self, previous: DelegationRunState, proposed: DelegationRunState,
                                parent_session: Session, scope: McpScope | None) -> str:
        """One parent-work ordering point for child responsibility and seal."""
        assert self.session_mgr is not None
        if self.session_mgr.session_deleting(proposed.delegation_id):
            raise DelegationError("Child session is being deleted", status_code=409)
        user_id = parent_session.user_id
        if proposed.review_ticket_id is not None:
            assert proposed.owned_context is not None
            caller = proposed.owned_context.return_binding
            current = self._capture_caller(parent_session, scope, proposed.request, caller.reference.request_id)
            if current.context != caller.context:
                raise DelegationError("The review caller's authorization changed; retry from the current input", status_code=409)
            try:
                return await self._commit_round(previous, proposed, user_id,
                                                review_binding=(caller, proposed.review_ticket_id))
            except TicketReviewAdmissionClosed as error:
                raise DelegationError(str(error), status_code=409) from error
        if proposed.parent_work_id is None:
            return await self._commit_round(previous, proposed, user_id)
        parent = self._records.get(proposed.parent_session_id)
        if parent is None or parent.work_id != proposed.parent_work_id:
            raise DelegationError("Original parent work is unavailable", status_code=409)
        async with parent._save_lock:
            if (self._records.get(parent.delegation_id) is not parent or parent.state != "running"
                    or not work_is_open(parent.work_id)):
                raise DelegationError("Original parent work is closed", status_code=409)
            if self.session_mgr.session_deleting(proposed.delegation_id):
                raise DelegationError("Child session is being deleted", status_code=409)
            changed = replace(parent, _child_work_ids=parent._child_work_ids | {proposed.work_id})
            snapshot = await self._commit_round(previous, proposed, user_id, parent_change=(parent, changed))
            parent._child_work_ids = changed._child_work_ids
            parent._persisted_state = self._snapshot(changed)
            return snapshot

    async def _withdraw_child_responsibility(self, child: DelegationRunState) -> None:
        parent = self._records.get(child.parent_session_id)
        if parent is not None and parent.work_id == child.parent_work_id:
            if await self._save_round(parent, running_only=True, withdraw_child=child.work_id):
                if parent.state == "completed":
                    await self._inject_terminal(parent, request_id=parent._candidate_request_id)

    async def _save_round(
        self, rec: DelegationRunState, *, running_only: bool = False,
        completion: RequestCompletion | None = None, confirm_selected_write: bool = False, **changes: Any,
    ) -> bool:
        """Publish a change only after its complete CAS has committed."""
        assert self.db is not None and self.session_mgr is not None
        parent = self.session_mgr.get_session(rec.parent_session_id)
        user_id = parent.user_id if parent else await self.db.get_session_owner(rec.parent_session_id)
        async with rec._save_lock:
            if self._records.get(rec.delegation_id) is not rec:
                # A follow-up can still restore this record after a confirmed
                # pre-admission refusal. That command owns writer retirement.
                return False
            if running_only and rec.state != "running":
                return False
            withdrawn = changes.pop("withdraw_child", None)
            if withdrawn is not None:
                changes["_child_work_ids"] = rec._child_work_ids - {withdrawn}
            candidate = rec._candidate_request_id if withdrawn is not None else None
            if completion is not None and completion.operation == "message":
                outcome = completion.outcome if completion.recording_state == "committed" else None
                if outcome is not None and outcome.status == "success":
                    candidate = completion.request_id
                else:
                    changes = {
                        "state": "interrupted" if outcome is None or outcome.code == "service_shutdown" else "failed",
                        "error": (outcome.message or "child session reported an error result" if outcome is not None else
                                  "Child has no committed outcome; inspect the original conversation before retrying"),
                        "finished_at": datetime.now(UTC).isoformat(),
                    }
            if candidate is not None:
                remaining, ready = await self.db.delegation_work_consumption(
                    rec.delegation_id, rec.work_id, changes.get("_child_work_ids", rec._child_work_ids), candidate)
                changes.update(_child_work_ids=remaining, _candidate_request_id=candidate)
                if ready:
                    changes.update(state="completed", finished_at=datetime.now(UTC).isoformat(), error=None)
            proposed = replace(rec, **changes)
            try:
                snapshot = await self._commit_round(rec, proposed, user_id, confirm_selected_write=confirm_selected_write)
            except DelegationWorkPending:
                # An input admitted while the writer was awaited invalidates
                # this seal. Its own original completion will wake the owner.
                for key in ("state", "finished_at", "error"):
                    changes.pop(key, None)
                proposed = replace(rec, **changes)
                snapshot = await self._commit_round(rec, proposed, user_id)
            for name, value in changes.items():
                setattr(rec, name, value)
            rec._persisted_state = snapshot
        return True

    async def confirm_recording(self, *, return_session_id: str | None = None) -> None:
        """Drain commands and their handoffs, optionally for one return destination."""
        deadline = asyncio.get_running_loop().time() + 5.0
        while True:
            self._retire_delivery_failures()
            # Future callbacks run on the next loop turn. A completed receipt
            # already owns its handoff even when that callback has not run.
            for future, (parent_session_id, completed) in tuple(self._completion_handoffs.items()):
                if future.done() and return_session_id in {None, parent_session_id}:
                    completed(future)
            commands = {task: recording for task, recording in self._commands.items()
                        if return_session_id is None or return_session_id in recording.return_sessions}
            if return_session_id is not None:
                commands = {task: recording for task, recording in commands.items() if not (
                    recording.question_return is not None and recording.question_return.done()
                    and recording.question_return.result()
                )}
            publications = self._publications.pending if return_session_id is None else ()
            if not commands and not publications:
                return
            milestones: set[asyncio.Future[Any]] = {
                recording.question_return if return_session_id is not None and recording.question_return is not None else task
                for task, recording in commands.items()
            }
            milestones.update(publications)
            remaining = max(0.0, deadline - asyncio.get_running_loop().time())
            _, pending = await asyncio.wait(milestones, timeout=remaining)
            if pending:
                raise RuntimeError("Delegation commands remain unfinished")
            for task in commands:
                recording = self._commands.get(task)
                if recording is None:
                    continue  # Its captured diagnostic moved to an owned reentry.
                if return_session_id is not None and recording.question_return is not None and recording.question_return.result():
                    continue  # This command now owns only the surviving child's notice.
                if not all(check() for check in recording.checks):
                    raise RuntimeError("Delegation command cleanup failed") from (
                        task.exception() if not task.cancelled() else None
                    )
                if recording.write_failed:
                    self._forget_command(task)
                    continue
                if task.cancelled():
                    raise RuntimeError("Delegation command was cancelled before cleanup")
                if task.exception() is not None and not isinstance(task.exception(), DelegationError):
                    raise RuntimeError("Delegation command cleanup failed") from task.exception()
                self._forget_command(task)

    async def _before_database_close(self) -> None:
        assert self.session_mgr is not None
        # Drivers can create a final round writer while retiring. Confirm
        # recording after those producers end, before the DB close hook exits.
        await self.session_mgr.drain_turns()
        await self.confirm_recording()

    async def restore(self) -> None:
        """Restore future round receipts; never resume work or replay replies."""
        assert self.db is not None
        try:
            rows = [(row, _PersistedRound.model_validate_json(row["delegation_state"]))
                    for row in await self.db.load_delegation_states()]
        except ValidationError:
            # Invalid input has acquired no work or cleanup resources. Keep
            # request-bearing validation diagnostics out of operational errors.
            raise RuntimeError("Invalid persisted delegation state") from None
        await self._command(self._restore(rows))

    async def _restore(self, rows: list[tuple[dict[str, Any], _PersistedRound]]) -> None:
        assert self.db is not None
        for row, data in rows:
            saved = row["delegation_state"]
            rec = DelegationRunState(
                delegation_id=row["id"], parent_session_id=row["parent_session_id"],
                target_agent_id=row["agent_id"], target_agent_name=data.target_agent_name,
                request=data.request, work_id=data.work_id,
                parent_work_id=data.parent_work_id, created_at=data.created_at,
                state=data.state, finished_at=data.finished_at, error=data.error,
                _persisted_state=saved, _metric_recorded=True,
                _terminal_disposition=data.terminal_disposition or "not_replayed",
                terminal_delivery_error=data.terminal_delivery_error,
                _child_work_ids=set(data.child_work_ids), _candidate_request_id=data.candidate_request_id,
                owned_context=decode_owned(data.knowledge_context),
                review_ticket_id=data.review_ticket_id,
            )
            changes: dict[str, Any] = {}
            if rec.state == "running":
                # A committed final round snapshot is the seal. A successful
                # latest request cannot reconstruct lost consumption ordering.
                changes.update(state="interrupted", error="interrupted by service restart")
                changes["finished_at"] = datetime.now(UTC).isoformat()
            close_work(rec.work_id)
            self._register_round(rec)
            if data.state == "running":
                await self._save_round(rec, **changes)

    def _register_round(self, rec: DelegationRunState) -> None:
        # The parent can close while the child's admission transaction commits.
        if rec.parent_session_id in self._closed_parents or not work_is_open(rec.parent_work_id):
            close_work(rec.work_id)
        self._records[rec.delegation_id] = rec
        self._rounds[rec.work_id] = rec

    def drain_blockers(self) -> tuple[str, ...]:
        if (self._commands or self._completion_handoffs or self._publications.pending
                or any(rec.state in {"pending", "queued", "running"} or rec._terminal_injecting
                       or rec._save_lock.locked() for rec in self._rounds.values())):
            return ("command, original work, question relay or terminal recording",)
        return ()

    def begin_shutdown(self) -> None:
        self._closing = True

    def shutdown(self) -> None:
        self.begin_shutdown()
        if self.session_mgr is not None:
            self.session_mgr.remove_broadcast(self.BROADCAST_KEY)

    # --------------------------------------------------------- public API

    async def start_delegation(
        self, *, parent_session_id: str, agent_name: str, request: str,
        files: list[str] | None = None, scope: McpScope | None = None,
        purpose: DelegationPurpose = "consultation", caller_request_id: str | None = None,
    ) -> DelegationRunState:
        if self.session_mgr is None or self.db is None:
            raise DelegationError("DelegationManager not bound", status_code=500)
        return await self._command(self._start_delegation(
            parent_session_id=parent_session_id, agent_name=agent_name,
            request=request, files=files, scope=scope, purpose=purpose, caller_request_id=caller_request_id,
        ), parent_session_id=parent_session_id, parent_work_id=scope.work_id if scope else None)

    async def _start_delegation(
        self,
        *,
        parent_session_id: str,
        agent_name: str,
        request: str,
        files: list[str] | None,
        scope: McpScope | None,
        purpose: DelegationPurpose,
        caller_request_id: str | None,
    ) -> DelegationRunState:
        """Create a child session under the named target agent and kick
        off its first turn. Returns immediately with the record — the
        reply arrives later via injection into the parent session
        (agent-collaboration.md §5.1)."""
        if self.session_mgr is None or self.db is None:
            raise DelegationError(
                "DelegationManager not bound", status_code=500
            )
        if not request or not request.strip():
            raise DelegationError(
                "request must be a non-empty string", status_code=400
            )

        command = asyncio.current_task()
        assert command is not None
        admission = self._commands[command]
        parent = self.session_mgr.get_session(parent_session_id)
        if parent is None:
            raise DelegationError(
                f"Parent session {parent_session_id!r} not found",
                status_code=404,
            )
        caller = self._capture_caller(parent, scope, request, caller_request_id)
        try:
            await self.db.require_application_session_writable(parent_session_id)
        except ApplicationDeleting as error:
            raise DelegationError(str(error), status_code=409) from error
        owned_context = derive(caller, request)
        parent_work_id = caller.work_id
        self._require_caller(scope, parent_session_id, parent_work_id)
        review_ticket_id = None
        if purpose == "ticket_review":
            self._require_review_scope(parent, scope, caller_request_id)
            review_ticket_id = await self.db.ticket_review_stage(caller)
            if review_ticket_id is None:
                raise DelegationError("Formal review requires an active, confirmed ticket", status_code=409)

        target = await self._resolve_target_agent(agent_name, parent.user_id)
        self._require_caller(scope, parent_session_id, parent_work_id)
        if target is None:
            raise DelegationError(
                f"No agent named {agent_name!r}", status_code=404
            )
        if parent.agent_id and target["id"] == parent.agent_id:
            raise DelegationError(
                "Cannot delegate to yourself — pick a different agent",
                status_code=409,
            )

        await self._check_chain(parent, target_agent_id=target["id"])

        # Parent name is informational only (used in the child's first
        # message and in the injection prefix). A missing parent agent
        # falls back to a generic placeholder rather than 500ing.
        parent_agent = (
            await self.db.get_agent(parent.agent_id, parent.user_id)
            if parent.agent_id
            else None
        )
        parent_name = (parent_agent or {}).get("name") or "another agent"
        self._require_caller(scope, parent_session_id, parent_work_id)

        composed = self._compose_initial_prompt(
            parent_name=parent_name,
            parent_session_id=parent.id,
            request=request,
            files=files or [],
            working_dir=parent.working_dir,
        )
        child_backend = (target.get("backend") or "claude-code")
        child_name = f"{target['name']} ← {parent_name}"
        child = await self.session_mgr.create_session(
            agent_id=target["id"],
            name=child_name,
            working_dir=parent.working_dir,
            origin="delegation",
            backend=child_backend,
            parent_session_id=parent.id,
            delegation_request=request,
        )
        try:
            self._require_caller(scope, parent_session_id, parent_work_id)
        except DelegationError:
            await self.session_mgr.auto_archive_scheduled_session(child.id, expected_session=child,
                                                                release_notice=admission.release_notice)
            raise

        rec = DelegationRunState(
            delegation_id=child.id,
            parent_session_id=parent.id,
            target_agent_id=target["id"],
            target_agent_name=target["name"],
            request=request,
            parent_work_id=parent_work_id,
            owned_context=owned_context,
            review_ticket_id=review_ticket_id,
        )

        async def settle_initial_terminal_return() -> None:
            await self._inject_terminal(rec)
            if rec._terminal_injecting:
                # A concurrent cancellation may own this observation;
                # setup still owns the subsequent responsibility exit.
                observer = rec._terminal_command
                assert observer is not None and observer is not asyncio.current_task()
                await asyncio.wait({observer})
                observer.result()
            # An unknown handoff still depends on the original receipt or consumption.
            if rec._terminal_disposition != "unknown":
                await self._withdraw_child_responsibility(rec)

        async def abandon_setup(
            error: str = "Delegation setup ended before child request admission",
            *, return_terminal: bool = False,
        ) -> None:
            """Finish any original return before withdrawing the exact parent obligation."""
            assert self.session_mgr is not None
            if rec.state != "running" or self.session_mgr.work_has_resources(rec.work_id):
                return
            finished_at = datetime.now(UTC).isoformat()
            try:
                saved = await self._save_round(
                    rec, running_only=True, state="cancelled",
                    error=error, finished_at=finished_at,
                )
            except DelegationError:
                # Only the acknowledged absence of the original relationship
                # permits retiring this unadmitted round without another write.
                if not rec._relationship_deleted:
                    raise
                async with rec._save_lock:
                    if (self._records.get(child.id) is not rec or rec.state != "running"
                            or self.session_mgr.work_has_resources(rec.work_id)):
                        return
                    rec.state = "cancelled"
                    rec.error = "Delegation relationship deleted before child admission"
                    rec.finished_at = finished_at
                    rec._round_recording_closed = True
                    rec._question_notices_closed = True
                    self._records.pop(child.id, None)
                    self._rounds.pop(rec.work_id, None)
                    saved = False
            else:
                if not saved:
                    return
            close_work(rec.work_id)
            observe_return = saved and return_terminal
            if not observe_return:
                # Proven refusal has no return to admit. Release the exact
                # parent responsibility before archival can fail independently.
                await self._withdraw_child_responsibility(rec)
            try:
                await self.session_mgr.auto_archive_scheduled_session(child.id, expected_session=child,
                                                                    release_notice=admission.release_notice)
            except ServiceUnavailable:
                raise
            except Exception as archive_failure:
                # The original archive task still reports its failure. The
                # committed child terminal still belongs to the parent.
                if not observe_return:
                    raise
                try:
                    await settle_initial_terminal_return()
                except ServiceUnavailable:
                    raise
                except Exception as return_failure:
                    raise ExceptionGroup(
                        "Initial delegation archive and terminal return failed", [archive_failure, return_failure],
                    ) from None
                finally:
                    # A later terminal write can settle its own diagnostic.
                    # This command still owns the archive failure.
                    task = asyncio.current_task()
                    assert task is not None
                    self._commands[task].write_failed = False
                raise
            if observe_return:
                admission.finish_admission()
                await settle_initial_terminal_return()

        # Commit before publishing, then register before child admission:
        # a fast child can synchronously broadcast its complete result there.
        try:
            async with rec._save_lock:
                rec._persisted_state = await self._commit_admission(rec, rec, parent, scope)
                self._register_round(rec)
        except DelegationError:
            # A confirmed refusal owns no admitted work. Retire the empty
            # conversation allocated before waiting for the parent's seal.
            await self.session_mgr.auto_archive_scheduled_session(child.id, expected_session=child,
                                                                release_notice=admission.release_notice)
            raise
        try:
            if self.session_mgr.get_session(parent.id) is not parent:
                raise DelegationError("Parent session is no longer live", status_code=404)
            self._require_caller(scope, parent_session_id, parent_work_id)
        except DelegationError:
            await abandon_setup()
            raise

        try:
            receipt = await self.session_mgr.start_message(
                child.id, composed, source="delegation", work_id=rec.work_id,
                knowledge_context=owned_context.execution,
            )
        except (SessionAdmissionClosed, SessionAdmissionBusy, SessionMailboxFull, AdmissionIntegrityError) as exc:
            await abandon_setup()
            raise DelegationError(str(exc), status_code=409) from exc
        if not receipt.accepted:
            await abandon_setup("Child request suppressed: admission is closed", return_terminal=True)
        else:
            self._watch_completion(child.id, rec.work_id, receipt)

        return rec

    async def cancel_delegation(
        self, delegation_id: str, *, reason: str | None = None,
        work_id: str | None = None, scope: McpScope | None = None,
    ) -> DelegationRunState:
        if self.session_mgr is None:
            raise DelegationError("DelegationManager not bound", status_code=500)
        rec = self._rounds.get(work_id) if work_id else self._records.get(delegation_id)
        if rec is None or rec.delegation_id != delegation_id:
            raise DelegationError(f"No delegation {delegation_id!r}", status_code=404)
        self._require_caller(scope, rec.parent_session_id, scope.work_id if scope else None, allow_closed=True)
        return await self._retire_round(rec, reason=reason)

    async def _retire_round(self, rec: DelegationRunState, *, reason: str | None) -> DelegationRunState:
        assert self.session_mgr is not None
        task = asyncio.create_task(self._retire_rounds([rec], reason=reason or "cancelled by caller"),
                                   name=f"delegation-retirement-{rec.work_id}")
        self.session_mgr._own_task(task)
        await asyncio.shield(task)
        return rec

    def _close_rounds(self, roots: list[DelegationRunState], *, deleted_parent: str | None = None) -> list[DelegationRunState]:
        """Revoke the captured forest and release its lifecycle notifications before waiting on any writer."""
        children: dict[str, list[DelegationRunState]] = {}
        for item in self._rounds.values():
            if item.parent_work_id is not None:
                children.setdefault(item.parent_work_id, []).append(item)
        branch = list(roots)
        owned = {item.work_id for item in roots}
        for item in branch:
            close_work(item.work_id)
            for child in children.get(item.work_id, ()):
                if child.work_id not in owned:
                    owned.add(child.work_id)
                    branch.append(child)
            if deleted_parent is not None:
                item._retiring_with_parent = True
                self._publications.release(item.work_id)
        for recording in self._commands.values():
            if recording.parent_work_id in owned or (deleted_parent is not None and recording.parent_session_id == deleted_parent):
                notice = recording.release_notice
                if notice is not None and not notice.done():
                    notice.set_result(None)
        return branch

    async def _retire_rounds(
        self, roots: list[DelegationRunState], *, reason: str, deleted_parent: str | None = None,
    ) -> None:
        joined: set[asyncio.Task[Any]] = set()
        while True:
            if deleted_parent is not None:
                roots = [rec for rec in self._rounds.values() if rec.parent_session_id == deleted_parent]
            branch = self._close_rounds(roots, deleted_parent=deleted_parent)
            owned = {item.work_id for item in branch}
            pending = {task for task, recording in self._commands.items() if recording.parent_work_id in owned
                       or (deleted_parent is not None and recording.parent_session_id == deleted_parent)} - joined
            if not pending:
                break
            joined.update(pending)
            await self._join_admissions(pending)

        commands = []
        child_commands: dict[str, list[asyncio.Task[DelegationRunState]]] = {}
        root_ids = {item.work_id for item in roots}
        for item in reversed(branch):
            task = self._start_round_retirement(
                item, children=child_commands.get(item.work_id, []),
                reason=reason if item.work_id in root_ids else f"parent delegation cancelled ({reason})",
                retry_completed=(self._round_terminal_pending(item) if deleted_parent is not None else item.work_id in root_ids),
            )
            if item.parent_work_id is not None:
                child_commands.setdefault(item.parent_work_id, []).append(task)
            commands.append(task)
        if commands:
            await asyncio.wait(commands)
            for task in reversed(commands):
                task.result()

    def _round_terminal_pending(self, rec: DelegationRunState) -> bool:
        task = rec._terminal_command
        if task is None:
            return False
        recording = self._commands.get(task)
        return not task.done() or recording is not None

    def _start_round_retirement(
        self, rec: DelegationRunState, *, children: list[asyncio.Task[DelegationRunState]], reason: str,
        retry_completed: bool,
    ) -> asyncio.Task[DelegationRunState]:
        previous = rec._retirement_command
        if previous is not None and (not previous.done() or (
            not retry_completed and not previous.cancelled() and previous.exception() is None
        )):
            return previous
        task = self._start_command(self._cancel_delegation(rec, children, reason=reason),
                                   return_session_id=rec.parent_session_id)
        rec._retirement_command = task
        if previous is not None:
            recording = self._commands.get(previous)
            # The replacement owns this round's cleanup. A failed disposition
            # write still belongs to the exact terminal recording.
            if recording is not None and not recording.write_failed:
                self._forget_command(previous)
        return task

    async def _cancel_delegation(
        self, rec: DelegationRunState, children: list[asyncio.Task[DelegationRunState]], *, reason: str,
    ) -> DelegationRunState:
        """Own one round's cancellation, returning after its branch settles."""
        assert self.session_mgr is not None
        refusal = None
        try:
            if rec.state == "running":
                if rec._selected_cancellation is None:
                    rec._selected_cancellation = (reason, datetime.now(UTC).isoformat())
                error, finished_at = rec._selected_cancellation
                await self._save_round(rec, running_only=True, state="cancelled", error=error, finished_at=finished_at,
                                       confirm_selected_write=True)
                rec._selected_cancellation = None
        except DelegationError as exc:
            if not (rec._retiring_with_parent and rec._relationship_deleted):
                refusal = exc
        # Completed answers keep their state while their residual resources stop.
        affected = await self.session_mgr.close_work(rec.work_id)
        await bg_task_manager.cancel_work(rec.work_id)
        await research_manager.cancel_work(rec.work_id)
        await self.session_mgr.finish_work_cancellation([affected])
        await research_manager.settle_work_returns(rec.work_id)
        if children:
            await asyncio.wait(set(children))
            for child in children:
                try:
                    child.result()
                except DelegationError as exc:
                    refusal = refusal or exc
        try:
            await self._settle_retired_round_terminal(rec)
        except DelegationError as exc:
            refusal = refusal or exc
        if refusal is not None:
            raise refusal
        return rec

    async def _settle_retired_round_terminal(self, rec: DelegationRunState) -> None:
        retry_terminal = False
        original = rec._terminal_command
        if rec._retiring_with_parent and original is not None and original is not asyncio.current_task():
            if not original.done():
                await asyncio.wait({original})
            recording = self._commands.get(original)
            retry_terminal = rec._terminal_disposition is not None and recording is not None and recording.write_failed
            if recording is not None and not retry_terminal:
                original.result()
        if rec.state == "cancelled" or retry_terminal:
            await self._inject_terminal(rec)

    async def follow_up_delegation(
        self, *, parent_session_id: str, delegation_id: str, request: str,
        scope: McpScope | None = None,
        purpose: DelegationPurpose | None = None, caller_request_id: str | None = None,
    ) -> DelegationRunState:
        """Continue a terminal round in its existing child conversation."""
        if self.session_mgr is None or self.db is None:
            raise DelegationError("DelegationManager not bound", status_code=500)
        return await self._command(self._follow_up_delegation(
            parent_session_id=parent_session_id, delegation_id=delegation_id,
            request=request, scope=scope, purpose=purpose, caller_request_id=caller_request_id,
        ), parent_session_id=parent_session_id, parent_work_id=scope.work_id if scope else None)

    async def _follow_up_delegation(
        self, *, parent_session_id: str, delegation_id: str, request: str,
        scope: McpScope | None, purpose: DelegationPurpose | None, caller_request_id: str | None,
    ) -> DelegationRunState:
        assert self.session_mgr is not None and self.db is not None
        if not request or not request.strip():
            raise DelegationError("request must be a non-empty string", status_code=400)
        parent = self.session_mgr.get_session(parent_session_id)
        if parent is None:
            raise DelegationError("Parent session is no longer live", status_code=404)
        caller = self._capture_caller(parent, scope, request, caller_request_id)
        try:
            await self.db.require_application_session_writable(parent_session_id)
        except ApplicationDeleting as error:
            raise DelegationError(str(error), status_code=409) from error
        owned_context = derive(caller, request)
        parent_work_id = caller.work_id
        self._require_caller(scope, parent_session_id, parent_work_id)
        previous = self._records.get(delegation_id)
        if previous is None or previous.parent_session_id != parent_session_id:
            raise DelegationError(f"No delegation {delegation_id!r} owned by this session", status_code=404)
        formal = previous.review_ticket_id is not None
        if purpose is not None and (purpose == "ticket_review") != formal:
            raise DelegationError("A delegation's purpose cannot change; start a fresh delegation", status_code=409)
        if formal:
            self._require_review_scope(parent, scope, caller_request_id)

        # The old round's lock also coordinates every continuation. A winning
        # CAS publishes the new round before another contender can enter.
        async with previous._save_lock:
            if self._records.get(delegation_id) is not previous:
                raise DelegationError("Delegation already has a new round", status_code=409)
            if previous.state == "running":
                raise DelegationError(f"Delegation {delegation_id!r} is still running; wait for its reply before following up", status_code=409)
            self._require_caller(scope, parent_session_id, parent_work_id)
            rec = replace(
                previous, work_id=uuid.uuid4().hex, parent_work_id=parent_work_id,
                state="running",
                error=None, finished_at=None, _terminal_disposition=None, terminal_delivery_error=None,
                _metric_recorded=False, _terminal_injecting=False,
                _question_deliveries={},
                _terminal_command=None, _retirement_command=None, _selected_cancellation=None, _retiring_with_parent=False,
                _round_recording_closed=False, _question_notices_closed=False,
                _relationship_deleted=False,
                _child_work_ids=set(), request=request, _candidate_request_id=None,
                owned_context=owned_context, created_at=datetime.now(UTC).isoformat(),
            )
            rec._persisted_state = await self._commit_admission(previous, rec, parent, scope)
            self._register_round(rec)

        child = self.session_mgr.get_session(delegation_id)
        reopening = child is None
        command = asyncio.current_task()
        assert command is not None
        recording = self._commands[command]
        restored_previous = False
        admission: Literal["not_submitted", "unknown", "refused", "accepted"] = "not_submitted"

        async def rollback_setup() -> None:
            nonlocal restored_previous
            assert self.session_mgr is not None
            # Only a confirmed business refusal before admission rolls back.
            # Neither a lost waiter nor an unknown storage error takes this path.
            if admission not in {"not_submitted", "refused"} or self.session_mgr.has_pending_or_active_work(delegation_id, rec.work_id):
                return
            async with rec._save_lock:
                if self._records.get(delegation_id) is not rec or rec.state != "running":
                    return
                try:
                    previous._persisted_state = await self._commit_round(rec, previous, parent.user_id)
                except DelegationError:
                    if not rec._relationship_deleted:
                        raise
                    # No submission was made (or its original receipt refused
                    # it), and the original relation's deletion is confirmed.
                    # There is no remaining row to restore. Unknown acceptance
                    # and a replaced relation never enter this local exit.
                    if self.session_mgr.has_pending_or_active_work(delegation_id, rec.work_id):
                        return
                    self._records.pop(delegation_id, None)
                    rec.state = "cancelled"
                    rec.error = "Delegation relationship deleted before child admission"
                    rec.finished_at = datetime.now(UTC).isoformat()
                else:
                    self._records[delegation_id] = previous
                    restored_previous = True
                close_work(rec.work_id)
                self._rounds.pop(rec.work_id, None)
                rec._round_recording_closed = True
                rec._question_notices_closed = True
            await self._withdraw_child_responsibility(rec)
            if reopening and child is not None:
                await self.session_mgr.auto_archive_scheduled_session(delegation_id, expected_session=child,
                                                                    release_notice=recording.release_notice)

        try:
            if reopening:
                try:
                    child = await self.session_mgr.unarchive_session(delegation_id, release_notice=recording.release_notice)
                except ValueError as exc:
                    raise DelegationError(f"Child session {delegation_id!r} is gone and can't be reused; start a fresh delegation", status_code=409) from exc
            parent_agent = await self.db.get_agent(parent.agent_id) if parent.agent_id else None
            parent_name = (parent_agent or {}).get("name") or "another agent"
            composed = (
                f"Agent **{parent_name}** has a follow-up for you in the "
                f"same line of work — your previous reply is above in "
                f"this transcript.\n\n{_DELEGATION_PROTOCOL}\n\nTheir new request follows.\n\n"
                f"---\n{request.strip()}"
            )
            if self.session_mgr.get_session(parent_session_id) is not parent:
                raise DelegationError("Parent session is gone", status_code=404)
            if self.session_mgr.get_session(delegation_id) is not child:
                raise DelegationError("Child session is gone; start a fresh delegation", status_code=409)
            if self.session_mgr.session_deleting(delegation_id):
                raise DelegationError("Child session is being deleted", status_code=409)
            self._require_caller(scope, parent_session_id, parent_work_id)
            if rec.state != "running" or not work_is_open(rec.work_id) or self._records.get(delegation_id) is not rec:
                return rec
            try:
                admission = "unknown"
                receipt = await self.session_mgr.start_message(
                    delegation_id, composed, source="delegation", work_id=rec.work_id,
                    knowledge_context=owned_context.execution,
                )
            except (SessionAdmissionClosed, SessionAdmissionBusy, SessionMailboxFull, AdmissionIntegrityError) as exc:
                admission = "refused"
                raise DelegationError(str(exc), status_code=409) from exc
            except ValueError as exc:
                raise DelegationError(str(exc), status_code=409) from exc
            admission = "accepted" if receipt.accepted else "refused"
            if not receipt.accepted:
                await self._save_round(
                    rec, running_only=True, state="cancelled",
                    error="Child request suppressed: admission is closed",
                    finished_at=datetime.now(UTC).isoformat(),
                )
                recording.finish_admission()
                await self._inject_terminal(rec)
            else:
                self._watch_completion(delegation_id, rec.work_id, receipt)
        except DelegationError:
            await rollback_setup()
            raise
        finally:
            # Another command may stage a new round while rollback awaits
            # archival. Only this command's confirmed rollback is decisive.
            if not restored_previous:
                previous._round_recording_closed = True
                previous._question_notices_closed = True
                self._retire_delivery_failures()
        return rec

    def list_delegations(
        self, parent_session_id: str, *, limit: int = 25
    ) -> list[DelegationRunState]:
        """Recent delegations spawned by a parent session, newest first."""
        rows = [
            r
            for r in self._rounds.values()
            if r.parent_session_id == parent_session_id
        ]
        rows.sort(key=lambda r: r.created_at, reverse=True)
        return rows[:limit]

    def get_delegation(self, delegation_id: str) -> DelegationRunState | None:
        return self._records.get(delegation_id)

    def has_active_delegation_for_parent(self, parent_session_id: str) -> bool:
        """True if `parent_session_id` has any still-running delegation
        (session-rewind.md §5.4). The fork live-work check uses this to
        refuse forking a parent that's mid-delegation — a fork is only allowed
        against a quiescent parent."""
        return any(
            r.parent_session_id == parent_session_id and r.state == "running"
            for r in self._records.values()
        )

    # ----------------------------------------------------------- internals

    def close_session(self, session_id: str, *, permanent: bool = False) -> None:
        self._closed_parents.add(session_id)
        if permanent:
            self._close_rounds([rec for rec in self._rounds.values() if rec.parent_session_id == session_id],
                               deleted_parent=session_id)

    def reopen_session(self, session_id: str) -> None:
        self._closed_parents.discard(session_id)

    async def retire_session(self, session_id: str, *, permanent: bool = False) -> None:
        """Join startup, then retire exact rounds, including completed rounds' BG."""
        self.close_session(session_id)
        if permanent:
            await self._retire_rounds([], reason="Parent session deleted", deleted_parent=session_id)
            await self.confirm_recording(return_session_id=session_id)
            return
        await self._join_admissions({task for task, recording in self._commands.items()
                                     if recording.parent_session_id == session_id})
        rounds = [rec for rec in self._rounds.values() if rec.parent_session_id == session_id]
        for rec in rounds:
            await self.cancel_delegation(rec.delegation_id, work_id=rec.work_id, reason="Application writer retired")

    async def _join_admissions(self, tasks: set[asyncio.Task[Any]]) -> None:
        if tasks:
            await asyncio.wait(tasks)
        for task in tasks:
            recording = self._commands.get(task)
            if recording is not None and recording.parent_session_id is not None:
                try:
                    task.result()
                except DelegationError:
                    pass

    @staticmethod
    def _capture_caller(parent: Session, scope: McpScope | None, request: str,
                        expected_request_id: str | None) -> AdmittedRequest:
        try:
            caller = capture(parent, scope, prompt=request)
        except ValueError as exc:
            raise DelegationError(str(exc), status_code=409) from exc
        if expected_request_id is not None and caller.reference.request_id != expected_request_id:
            raise DelegationError("The original requesting turn is no longer active", status_code=409)
        return caller

    @staticmethod
    def _require_review_scope(parent: Session, scope: McpScope | None, caller_request_id: str | None) -> None:
        if scope is None or parent.origin not in TICKET_BATCH_ORIGINS or scope.work_id is not None:
            raise DelegationError("Formal review requires a live model turn in the original ticket chat", status_code=409)
        if caller_request_id is None:
            raise DelegationError("Formal review requires the original requesting turn ID", status_code=409)

    def _require_caller(self, scope: McpScope | None, session_id: str, work_id: str | None, *, allow_closed: bool = False) -> None:
        if not allow_closed and session_id in self._closed_parents:
            raise DelegationError("Caller session admission is closed", status_code=409)
        try:
            require_live_scope(scope, session_id)
        except ValueError as exc:
            raise DelegationError(str(exc), status_code=409) from exc
        if not work_is_open(work_id):
            raise DelegationError("Caller work is closed", status_code=409)
        caller = self._rounds.get(work_id) if work_id is not None else None
        if not allow_closed and caller is not None and caller.state != "running":
            raise DelegationError("Original delegation work is finalized; start a new follow-up to add delegated work", status_code=409)

    async def _resolve_target_agent(
        self, name: str, user_id: str | None = None
    ) -> dict[str, Any] | None:
        """Case-insensitive name lookup over this account's non-archived agents.

        Scoped to the delegating session's owner (multi-tenancy.md §7). An
        unscoped lookup made a *name* enough to start a turn inside somebody
        else's account — running in their workspace, on their credential, and
        reporting back into this one.

        Multiple matches → DelegationError(409). One match → the row.
        Zero matches → None (caller turns this into a 404)."""
        assert self.db is not None  # bound at lifespan
        wanted = (name or "").strip().lower()
        if not wanted:
            return None
        agents = await self.db.load_agents(user_id=user_id)
        matches = [a for a in agents if (a.get("name") or "").lower() == wanted]
        if not matches:
            return None
        if len(matches) > 1:
            names = ", ".join(repr(a.get("name")) for a in matches)
            raise DelegationError(
                f"Ambiguous agent name {name!r} (matches: {names})",
                status_code=409,
            )
        return matches[0]

    async def _check_chain(self, parent: Session, *, target_agent_id: str) -> None:
        """Walk the parent chain upward from the given parent session.

        Enforces two rules (agent-collaboration.md §5.9):
        - Cycle: the target agent must not already appear in the chain.
        - Depth: the new delegation (counted as +1) plus all existing
          ``origin='delegation'`` hops in the chain must not exceed
          ``DEPTH_CAP``.

        Self-delegation (target == parent.agent_id) is rejected by the
        caller; this method still catches it transitively via the cycle
        check, but the dedicated error message at the call site is
        clearer for the user.

        Fail-closed semantics: a corrupted parent_session_id chain
        (loop in the session-id pointers, or a chain longer than the
        safety cap, or a non-null ``parent_session_id`` whose target
        the DB also can't find) is rejected as a 409 rather than
        silently treated as a valid (short) chain. The cycle guard is
        what stands between "Vera asks Octo" and an infinite
        delegation tower — it must never be skipped.

        An archived ancestor still contributes its agent and origin. Resolve
        each live miss by ID, using one read snapshot for persisted relations.
        """
        assert self.session_mgr is not None
        assert self.db is not None
        chain_agent_ids: set[str] = set()
        visited_session_ids: set[str] = set()
        existing_hops = 0
        safety_cap = 64
        sid: str | None = parent.id
        async with AsyncExitStack() as snapshot:
            read = None
            for _ in range(safety_cap):
                if sid is None:
                    break
                if sid in visited_session_ids:
                    raise DelegationError(
                        "Caller chain has a session-id cycle (corrupted parent_session_id pointers)",
                        status_code=409,
                    )
                visited_session_ids.add(sid)
                relation: Session | SessionRelation | None = self.session_mgr.get_session(sid)
                if relation is None:
                    if read is None:
                        read = await snapshot.enter_async_context(self.db.session_relation_snapshot())
                    relation = (await read((sid,))).get(sid)
                if relation is None:
                    raise DelegationError(
                        "Caller chain references a session that "
                        "exists in neither memory nor the database; "
                        "refuse rather than guess",
                        status_code=409,
                    )
                if relation.user_id != parent.user_id:
                    raise DelegationError("Caller chain crosses account ownership", status_code=409)
                if relation.agent_id:
                    chain_agent_ids.add(relation.agent_id)
                if relation.origin == "delegation":
                    existing_hops += 1
                sid = relation.parent_session_id
            else:
                raise DelegationError(
                    f"Caller chain exceeds {safety_cap} hops; refuse "
                    "as a fail-closed guard against pointer corruption",
                    status_code=409,
                )

        if target_agent_id in chain_agent_ids:
            raise DelegationError(
                "Cycle rejected: target agent already in the caller chain",
                status_code=409,
            )
        # +1 because the new delegation we're about to create is the
        # next hop. The cap is "no more than N delegation hops in any
        # one caller chain" (root user is not counted; only delegations).
        if existing_hops + 1 > DEPTH_CAP:
            raise DelegationError(
                f"Delegation depth would exceed {DEPTH_CAP} hops",
                status_code=409,
            )

    def _compose_initial_prompt(
        self,
        *,
        parent_name: str,
        parent_session_id: str,
        request: str,
        files: list[str],
        working_dir: str,
    ) -> str:
        """The child's first user message. Names the caller, ships the
        request verbatim, and optionally lists files the parent flagged
        as relevant.

        File paths are resolved against ``working_dir`` (plan §5.7):
        absolute paths pass through unchanged; relative ones get joined
        to the working dir. We also check existence and clearly flag
        missing entries — better the child sees ``(not found)`` than a
        misleading absolute path that doesn't exist on disk.

        We do NOT include any of the parent's transcript — that's a
        deliberate scope/privacy boundary (plan §2).

        The original request's completion supplies one reply for this round.
        The conversation stays open, but human follow-ups do not resend that
        reply. An explicit parent follow-up creates a new round."""
        from pathlib import Path

        lines = [
            f"You were asked by agent **{parent_name}** "
            f"(session `{parent_session_id}`) to handle a task on their behalf.",
            "",
            _DELEGATION_PROTOCOL,
            "",
            f"The request from **{parent_name}** follows.",
            "",
            "---",
            request.strip(),
        ]
        if files:
            lines.append("")
            lines.append(
                "The caller flagged these files as relevant "
                f"(paths resolved against `{working_dir}`):"
            )
            base = Path(working_dir)
            for raw in files:
                resolved = (
                    Path(raw) if Path(raw).is_absolute() else base / raw
                )
                try:
                    resolved = resolved.resolve()
                    # Unlike exists(), stat keeps symlink loops and other
                    # resolution failures distinct from a missing reference.
                    resolved.stat()
                except FileNotFoundError:
                    lines.append(f"- {resolved}  (not found)")
                except (OSError, RuntimeError):
                    # Path resolution failed (e.g. permission error
                    # walking a symlink); surface the raw form so the
                    # child still sees what the parent meant.
                    lines.append(f"- {raw}  (could not resolve)")
                else:
                    lines.append(f"- {resolved}")
        return "\n".join(lines)

    # ----------------------------------------------- broadcast → injection

    async def _on_broadcast(self, msg: dict[str, Any]) -> None:
        if msg.get("type") in {"question_answer", "question_closed"}:
            rec = self._records.get(msg.get("session_id", ""))
            if rec is not None:
                question_id = msg.get("question_id", "")
                delivery = rec._question_deliveries.get(question_id)
                if delivery is not None and self._question_recorded(rec, delivery):
                    rec._question_deliveries.pop(question_id)
            return
        if msg.get("type") != "question_request":
            return
        rec = self._records.get(msg.get("session_id", ""))
        if rec is None or (rec.state != "running" and msg.get("question_id") not in rec._question_deliveries):
            return
        command = self._start_command(self._handle_broadcast(rec, msg), return_session_id=rec.parent_session_id,
                                      question_return=True)
        await asyncio.wait({command})
        command.result()

    async def _handle_broadcast(self, rec: DelegationRunState, msg: dict[str, Any]) -> None:
        """Route live questions; business finality comes from the receipt."""
        sid = msg.get("session_id")
        if not sid:
            return
        if self._records.get(sid) is not rec or (rec.state != "running" and msg.get("question_id") not in rec._question_deliveries):
            return

        if "work_id" in msg and msg["work_id"] != rec.work_id:
            return
        if msg.get("type") == "question_request":
            await self._inject_question(rec, msg)

    async def _inject_question(
        self, rec: DelegationRunState, msg: dict[str, Any]
    ) -> None:
        """Bubble a child's `question_request` up to the parent session
        as an injected `[agent-question:…]` turn. The pending question
        itself stays on the child's session (the existing UI path can
        still answer it manually); the parent's
        `answer_agent_question(delegation_id, choice)` tool drains that
        same queue on success — first to drain wins.
        """
        assert self.session_mgr is not None
        question_id = msg.get("question_id") or ""
        delivery = rec._question_deliveries.setdefault(
            question_id, _QuestionDelivery(f"agent-question:{rec.work_id}:{question_id}"),
        )
        if delivery.injecting:
            return
        delivery.injecting = True
        command = asyncio.current_task()
        if command in self._commands and delivery.command is not command:
            delivery.command = command
            self._commands[command].return_sessions.add(rec.parent_session_id)
            self._commands[command].checks.append(
                lambda: delivery.command is not command or self._question_recorded(rec, delivery),
            )
            self._retire_delivery_failures()
        try:
            if delivery.disposition == "stopped":
                await self._retain_question_delivery(rec, question_id, delivery)
                return
            if delivery.receipt is not None:
                await self._acknowledge_question(rec, question_id, delivery, delivery.receipt)
                return

            async def prepare() -> ReturnBody | ReturnSkipped:
                assert self.session_mgr is not None
                if delivery.disposition is not None:
                    return ReturnSkipped("Previously unconfirmed question admission is observation only")
                if (rec.state != "running" or self._records.get(rec.delegation_id) is not rec
                        or not self.session_mgr.has_pending_question(rec.delegation_id, question_id)):
                    return ReturnSkipped("The original round or question no longer needs a return")
                body = self._render_question_body(msg.get("questions") or [])
                prompt = (
                    f"[agent-question:{rec.target_agent_name} delegation={rec.delegation_id} "
                    f"question_id={question_id}]\n{body}\n\n"
                    f"Decide: answer them by calling `mcp__ask_agent__answer(delegation_id="
                    f"\"{rec.delegation_id}\", question_id=\"{question_id}\", answers=…)` "
                    "with one answer per question (selected labels or free text; keyed by question ID/zero-based index, "
                    "or in order). `choice=…` is supported only for a single-question batch. If you don't "
                    "know, use your own `mcp__ask__user` to ask the user and forward their answer; or cancel via "
                    "`mcp__ask_agent__cancel`."
                )
                return ReturnBody(prompt, question_session_id=rec.delegation_id, question_id=question_id)

            while True:
                try:
                    result = await self.session_mgr.admit_return(
                        ReturnTarget(rec.parent_session_id, "delegation", delivery.delivery_id,
                                     rec.parent_work_id, rec.owned_context),
                        prepare=prepare, wait_for_lifecycle=msg.get("answer_target") != "parent",
                    )
                except AdmissionIntegrityError as error:
                    if delivery.disposition == "unknown":
                        raise
                    self._remember_question_delivery(delivery, "stopped", error)
                    await self._retain_question_delivery(rec, question_id, delivery)
                    return
                except ServiceUnavailable:
                    raise
                except SubmissionConflict as error:
                    self._remember_question_delivery(delivery, "stopped", error)
                    await self._retain_question_delivery(rec, question_id, delivery)
                    raise
                except (Exception, asyncio.CancelledError) as error:
                    self._remember_question_delivery(delivery, "unknown", error)
                    await complete_cleanup(self._retain_question_delivery(rec, question_id, delivery))
                    raise
                if isinstance(result, SubmissionReceipt):
                    await self._acknowledge_question(rec, question_id, delivery, result)
                    return
                if delivery.disposition == "unknown":
                    await self._retain_question_delivery(rec, question_id, delivery)
                    return  # Absence or a later refusal does not settle the original handoff.
                if isinstance(result, ReturnUnconfirmed):
                    self._remember_question_delivery(delivery, "unknown", result.cause)
                    await self._retain_question_delivery(rec, question_id, delivery)
                    return
                assert isinstance(result, ReturnRefused)
                if result.reason in {"capacity", "busy"}:
                    if msg.get("answer_target") == "parent":
                        self._remember_question_delivery(delivery, "stopped",
                            result.cause or "The parent cannot accept this unattended decision request")
                        await self._retain_question_delivery(rec, question_id, delivery)
                        return
                    await asyncio.sleep(1)
                    continue
                if result.reason == "source_withdrawn":
                    return
                self._remember_question_delivery(
                    delivery, "stopped", result.cause or "The original parent request is closed or unavailable",
                )
                await self._retain_question_delivery(rec, question_id, delivery)
                return
        finally:
            delivery.injecting = False
            if (not self.session_mgr.has_pending_question(rec.delegation_id, question_id)
                    and self._question_recorded(rec, delivery)):
                if rec._question_deliveries.get(question_id) is delivery:
                    rec._question_deliveries.pop(question_id)

    @staticmethod
    def _remember_question_delivery(
        delivery: _QuestionDelivery, disposition: Literal["stopped", "unknown"], error: BaseException | str,
    ) -> None:
        if delivery.disposition is None:
            delivery.disposition = disposition
            delivery.error = safe_diagnostic(f"{type(error).__name__}: {error}" if isinstance(error, BaseException) else error)
        if isinstance(error, BaseException):
            logger.warning("Delegation question return diagnostic delivery=%s disposition=%s\n%s",
                           delivery.delivery_id, delivery.disposition, exception_diagnostic(error))

    async def _retain_question_delivery(
        self, rec: DelegationRunState, question_id: str, delivery: _QuestionDelivery,
    ) -> None:
        assert self.session_mgr is not None
        self._handoff_question_notice(delivery)
        if delivery.notice_recorded or rec._question_notices_closed:
            return
        label = "Return admission unconfirmed" if delivery.disposition == "unknown" else "Automatic return stopped"
        child = self.session_mgr.get_session(rec.delegation_id)
        pending = child._questions.get(question_id) if child else None
        consequence = ("This unattended decision request will close as needing input; no automatic return retry will start."
                       if pending is not None and pending.answer_target == "parent" else
                       "This return will not be resubmitted automatically. The original question remains answerable here while pending.")
        try:
            recorded = await self.session_mgr.retain_result_notice(
                rec.delegation_id, f"agent-question:{rec.work_id}:{question_id}",
                f"Question {question_id}: {label}.\n{delivery.error}\n" + consequence,
            )
            if not recorded:
                raise SessionOwnerUnavailable("The original child question delivery notice could not be recorded")
        except (Exception, asyncio.CancelledError):
            self._delivery_recording_failed(delivery.command)
            raise
        delivery.notice_recorded = True

    @staticmethod
    def _question_recorded(rec: DelegationRunState, delivery: _QuestionDelivery) -> bool:
        return rec._question_notices_closed or delivery.disposition is None or delivery.notice_recorded

    def _handoff_question_notice(self, delivery: _QuestionDelivery) -> None:
        if delivery.command is not None and (recording := self._commands.get(delivery.command)) is not None:
            handoff = recording.question_return
            if handoff is not None and not handoff.done():
                handoff.set_result(True)

    async def _acknowledge_question(
        self, rec: DelegationRunState, question_id: str, delivery: _QuestionDelivery, receipt: SubmissionReceipt,
    ) -> None:
        delivery.receipt = receipt
        if receipt.accepted:
            self._watch_completion(rec.parent_session_id, rec.parent_work_id, receipt)
        self._handoff_question_notice(delivery)
        if delivery.disposition is not None:
            await self._retain_question_delivery(rec, question_id, delivery)
        delivery.disposition = None
        delivery.error = None
        if receipt.accepted:
            assert self.session_mgr is not None
            child = self.session_mgr.get_session(rec.delegation_id)
            pending = child._questions.get(question_id) if child else None
            if child is not None and pending is not None and pending.answer_target == "parent":
                from .sessions.owner_questions import AttachQuestionParent
                await self.session_mgr._question_owner(child, internal=True).call_question(
                    AttachQuestionParent(question_id, rec.parent_session_id, receipt))

    @staticmethod
    def _render_question_body(questions: list[dict[str, Any]]) -> str:
        """Render the AskUserQuestion payload into a human-readable
        block the parent's model can reason over. We render every
        question in the batch, but answering currently applies the
        parent's `choice` to the FIRST question only — see
        `answer_pending_question`."""
        if not questions:
            return "(no question text — child sent an empty payload)"
        lines: list[str] = []
        for i, q in enumerate(questions, start=1):
            qtext = q.get("question") or "(no question text)"
            header = q.get("header")
            multi = bool(q.get("multiSelect"))
            options = q.get("options") or []
            lines.append(f"Question {i}: {qtext}")
            if header:
                lines.append(f"  (header: {header})")
            for opt in options:
                label = (opt or {}).get("label") or "?"
                desc = (opt or {}).get("description")
                if desc:
                    lines.append(f"  - {label}: {desc}")
                else:
                    lines.append(f"  - {label}")
            mode = "multi-select" if multi else "single-choice"
            lines.append(f"  ({mode}; pass the chosen label as `choice`.)")
        return "\n".join(lines)

    async def answer_pending_question(
        self, delegation_id: str, choice: str | None = None, *,
        answers: list[dict[str, Any]] | dict[str, dict[str, Any]] | None = None,
        question_id: str | None = None,
        source: AnswerSource = UNKNOWN_SOURCE,
    ) -> dict[str, Any]:
        """Answer a complete batch; legacy choice is limited to one question."""
        if self.session_mgr is None:
            raise DelegationError(
                "DelegationManager not bound", status_code=500
            )
        rec = self._records.get(delegation_id)
        if rec is None:
            raise DelegationError(
                f"No delegation {delegation_id!r}", status_code=404
            )
        if rec.state != "running":
            raise DelegationError(
                f"Delegation {delegation_id!r} is {rec.state} — no "
                f"pending question to answer",
                status_code=409,
            )
        child = self.session_mgr.get_session(rec.delegation_id)
        if child is None:
            raise DelegationError(
                "Child session no longer alive", status_code=404
            )
        pending_questions = self.session_mgr.get_pending_questions(child.id)
        if not pending_questions:
            raise DelegationError(
                "No pending question on the child session",
                status_code=409,
            )
        question_id = question_id or pending_questions[0].question_id
        pending = next((item for item in pending_questions if item.question_id == question_id), None)
        if pending is None:
            raise DelegationError("The specified question is no longer pending", status_code=409)
        questions = pending.questions
        if answers is None:
            if len(questions) != 1 or not (choice or "").strip():
                raise DelegationError("Provide answers for every question; choice supports a single question only", status_code=400)
            assert choice is not None
            answers = [{"selected": [choice.strip()]}]
        elif choice is not None:
            raise DelegationError("Provide either choice or answers", status_code=400)
        if isinstance(answers, dict):
            indices = [str(index) for index in range(len(questions))]
            ids = [str(question.get("id") or index) for index, question in enumerate(questions)]
            keys = indices if set(answers) == set(indices) else ids
            if len(set(keys)) != len(questions) or set(answers) != set(keys):
                raise DelegationError("Answers must identify every question by ID or zero-based index", status_code=400)
            answers = [answers[key] for key in keys]
        normalized = []
        # Keep this entry's historical whitespace conversion; acceptance is
        # decided once by the original question Owner against its own form.
        for answer in answers:
            if isinstance(answer, dict):
                text = answer.get("text")
                normalized.append({"selected": [] if answer.get("selected") is None else answer["selected"],
                                   "text": (text.strip() or None) if isinstance(text, str) else text})
            else:
                normalized.append(answer)
        source = replace(source, delegation_id=rec.delegation_id, delegation_work_id=rec.work_id,
                         parent_session_id=rec.parent_session_id)
        try:
            ok = await self.session_mgr.answer_question(rec.delegation_id, question_id, normalized, source=source)
        except QuestionAnswerInvalid as error:
            raise DelegationError(str(error), status_code=400) from error
        if not ok:
            # Race: the human UI answered between our get and our set.
            raise DelegationError(
                "Question already answered by another path",
                status_code=409,
            )
        return {
            "delegation_id": delegation_id,
            "question_id": question_id,
            "choice": choice,
            "ok": True,
        }

    async def _inject_terminal(self, rec: DelegationRunState, *, request_id: str | None = None) -> None:
        """Relay one committed terminal using its stable original work identity."""
        assert self.session_mgr is not None
        if not rec._metric_recorded and rec.state in {"completed", "failed", "cancelled", "interrupted"}:
            rec._metric_recorded = True
            _mon_record(_MonEvent(
                kind="delegation", session_id=rec.parent_session_id,
                ok=rec.state == "completed", error_code=None if rec.state == "completed" else rec.state,
                detail={"delegation_id": rec.delegation_id, "state": rec.state},
            ))
        if rec._terminal_injecting:
            return
        if rec.state not in {"completed", "failed", "cancelled", "interrupted"}:
            return
        rec._terminal_injecting = True
        command = asyncio.current_task()
        if command in self._commands and rec._terminal_command is not command:
            rec._terminal_command = command
            self._commands[command].return_sessions.add(rec.parent_session_id)
            self._commands[command].checks.append(
                lambda: rec._terminal_command is not command or rec._round_recording_closed
                or rec._terminal_disposition is None or self._snapshot(rec) == rec._persisted_state,
            )
            self._retire_delivery_failures()
        try:
            if rec._terminal_disposition is not None:
                await self._persist_terminal_delivery(rec)
                if rec._terminal_disposition != "unknown":
                    return

            async def prepare() -> ReturnBody | ReturnSkipped:
                if rec._terminal_disposition is not None:
                    return ReturnSkipped("Previously unconfirmed terminal admission is observation only")
                delivery_failure: tuple[str, str] | None = None
                if rec.state == "completed":
                    assert self.db is not None and request_id is not None
                    result = await self.db.read_session_result(rec.delegation_id, request_id)
                    if result.operation != "message":
                        delivery_failure = ("reply_unavailable", "The child terminal is not a message reply.")
                    elif result.outcome is None or result.missing is not None:
                        if result.missing not in {"session_missing", "answer_reference_invalid"}:
                            raise RuntimeError("The committed delegation terminal is missing from its retained conversation")
                        delivery_failure = (
                            "reply_unavailable",
                            f"The child completed, but its recorded reply is unavailable ({result.missing}).",
                        )
                    else:
                        body = result.reply_text() if result.outcome.final_answer is not None else "".join(result.fragments).strip()
                        if not body:
                            body = "(child session ended without producing any text)"
                        return ReturnBody(
                            f"[agent-reply:{rec.target_agent_name} "
                            f"delegation={rec.delegation_id}]\n{body}"
                        )
                else:
                    delivery_failure = (rec.error or rec.state, f"(child session ended in state {rec.state!r})")
                assert delivery_failure is not None
                reason, detail = delivery_failure
                return ReturnBody(
                    f"[agent-error:{rec.target_agent_name} "
                    f"delegation={rec.delegation_id} reason={reason}]\n"
                    f"{detail}"
                )

            while True:
                try:
                    result = await self.session_mgr.admit_return(
                        ReturnTarget(rec.parent_session_id, "delegation", f"agent-terminal:{rec.work_id}",
                                     rec.parent_work_id, rec.owned_context),
                        prepare=prepare,
                    )
                except AdmissionIntegrityError as error:
                    if rec._terminal_disposition == "unknown":
                        raise
                    await self._stop_terminal_delivery(rec, "stopped", error)
                    return
                except ServiceUnavailable:
                    raise
                except SubmissionConflict as error:
                    if rec._terminal_disposition != "unknown":
                        await self._stop_terminal_delivery(rec, "stopped", error)
                    raise
                except (Exception, asyncio.CancelledError) as error:
                    if rec._terminal_disposition is None:
                        rec._terminal_disposition = "unknown"
                        rec.terminal_delivery_error = self._terminal_diagnostic(rec, error)
                        await complete_cleanup(self._persist_terminal_delivery(rec))
                    raise
                if isinstance(result, SubmissionReceipt):
                    await self._acknowledge_terminal(rec, result)
                    return
                if rec._terminal_disposition == "unknown":
                    return  # A later refusal cannot settle an earlier uncertain handoff.
                if isinstance(result, ReturnUnconfirmed):
                    rec._terminal_disposition = "unknown"
                    rec.terminal_delivery_error = self._terminal_diagnostic(rec, result.cause)
                    await self._persist_terminal_delivery(rec)
                    return
                assert isinstance(result, ReturnRefused)
                if result.reason in {"capacity", "busy"}:
                    await asyncio.sleep(1)
                    continue
                disposition: TerminalDisposition = (
                    "unbound" if result.reason == "binding_unavailable" else
                    "closed" if result.reason == "admission_closed" else "stopped"
                )
                await self._stop_terminal_delivery(
                    rec, disposition, result.cause or "The original parent request contract is missing, closed or unavailable",
                )
                if result.reason == "binding_unavailable":
                    try:
                        await self.session_mgr.retain_result_notice(
                            rec.parent_session_id, f"agent-terminal:{rec.work_id}",
                            f"Delegation {rec.delegation_id}: the result is retained in the child conversation. "
                            "The original request is unavailable, so no automatic reply was started.",
                        )
                    except ServiceUnavailable:
                        raise
                    except Exception:
                        logger.exception("Delegation %s auxiliary terminal notice failed", rec.delegation_id)
                return
        finally:
            rec._terminal_injecting = False

    async def _acknowledge_terminal(self, rec: DelegationRunState, receipt: SubmissionReceipt) -> None:
        if receipt.accepted:
            self._watch_completion(rec.parent_session_id, rec.parent_work_id, receipt)
        rec._terminal_disposition = receipt.disposition
        rec.terminal_delivery_error = None if receipt.accepted else "Session admission is closed"
        await self._persist_terminal_delivery(rec)

    async def _persist_terminal_delivery(self, rec: DelegationRunState) -> None:
        assert self.session_mgr is not None
        if rec._round_recording_closed or self._snapshot(rec) == rec._persisted_state:
            return
        try:
            saved = await self._save_round(rec, confirm_selected_write=True)
        except (Exception, asyncio.CancelledError):
            if not rec._round_recording_closed and self._snapshot(rec) != rec._persisted_state:
                self._delivery_recording_failed(rec._terminal_command)
            raise
        if saved:
            manager = self.session_mgr
            event = {
                "type": "delegation_delivery", "session_id": rec.parent_session_id,
                "delegation": rec.to_public_dict(),
            }
            await self._publications.publish(
                rec.work_id, lambda: manager._broadcast(event, wrap_required_cancellation=True),
                retiring=(rec._retiring_with_parent or manager.session_deleting(rec.parent_session_id)
                          or not work_is_open(rec.parent_work_id)),
            )

    async def _stop_terminal_delivery(
        self, rec: DelegationRunState, disposition: TerminalDisposition, error: BaseException | str,
    ) -> None:
        rec._terminal_disposition = disposition
        rec.terminal_delivery_error = self._terminal_diagnostic(rec, error)
        await self._persist_terminal_delivery(rec)

    @staticmethod
    def _terminal_diagnostic(rec: DelegationRunState, error: BaseException | str) -> str | None:
        if isinstance(error, BaseException):
            logger.warning("Delegation terminal return diagnostic work=%s session=%s\n%s",
                           rec.work_id, rec.parent_session_id, exception_diagnostic(error))
            return safe_diagnostic(f"{type(error).__name__}: {error}")
        return safe_diagnostic(error)

# Module-level singleton (mirrors the session_manager / bg_tasks
# pattern). Wired in main.py's lifespan with .bind(...).
delegation_manager = DelegationManager()
