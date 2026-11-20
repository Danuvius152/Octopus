"""Submitted session operations, separate from the effective execution context."""

from dataclasses import dataclass
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, TypeAdapter

OperationKind = Literal["message", "compact"]


class MessageOperation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["message"]
    content: str
    attachment_ids: list[str]
    wiki_mode: Literal["auto", "read", "maintain"] | None = None


class CompactOperation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["compact"]


SessionOperation = Annotated[MessageOperation | CompactOperation, Field(discriminator="kind")]
_operation = TypeAdapter(SessionOperation)


class NativeCompactCommand(BaseModel):
    """Original Claude command facts; opaque provider fields retain their shape."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    input_uuid: str = Field(min_length=1)
    local_command_run: dict[str, Any] | None = None
    local_command_outcome: dict[str, Any] | None = None


class CompactResult(BaseModel):
    """Observed history effects, independent of the request's business status."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    effect: Literal["applied", "not_applied", "unknown"]
    reason: str | None = None
    connection_generation: str | None = None
    thread_id: str | None = None
    turn_id: str | None = None
    item_ids: list[str] = Field(default_factory=list)
    native_command: NativeCompactCommand | None = None


class HandoffScopeClause(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    text: Annotated[str, StringConstraints(min_length=1)]
    scope: Literal["batch", "next"]
    start: int | None = Field(default=None, ge=0, strict=True, description="Zero-based Unicode character offset in message")
    hold: bool = Field(default=False, strict=True, description="Quote naming an operation to defer under an applicable restriction")


HandoffHumanScope = dict[str, tuple[HandoffScopeClause, ...]]
_handoff_scope = TypeAdapter(HandoffHumanScope)


@dataclass(frozen=True)
class SavedHandoffSelection:
    clauses: tuple[HandoffScopeClause, ...]
    next_expired: bool


def handoff_human_scope(payload: dict[str, Any]) -> HandoffHumanScope:
    """An empty selection closes the update with the ticket that received it."""
    decisions: HandoffHumanScope = dict.fromkeys(payload.get("answered_progress_request_ids", ()), ())
    decisions.update(_handoff_scope.validate_python(payload.get("handoff_human_scope", {})))
    if any(not request_id.strip() or any(not clause.text.strip() for clause in clauses)
           for request_id, clauses in decisions.items()):
        raise ValueError("Ticket handoff has an empty human input ID or scope clause")
    return decisions


def decode_payload(value: dict[str, Any]) -> dict[str, Any]:
    """Normalize legacy flat messages only at the retained-input boundary.

    A present operation must validate as such; a malformed or unknown kind
    must never fall back to treating arbitrary data as a user message.
    """
    if "operation" in value:
        operation = _operation.validate_python(value["operation"])
        if operation.kind == "compact" and any(value.get(key) for key in (
                "content", "attachment_ids", "wiki_mode", "attachments", "knowledge", "trusted_request", "recovery_of_client_id")):
            raise ValueError("Compaction cannot carry message input or recovery context")
    else:
        operation = MessageOperation(
            kind="message", content=value["content"], attachment_ids=value["attachment_ids"], wiki_mode=value.get("wiki_mode"),
        )
    if "ticket_batch_candidate_for" in value or "finish_ticket_batch_for" in value:
        batch = value.get("ticket_batch_candidate_for")
        if (value.get("source") != "human" or operation.kind != "message"
                or not isinstance(batch, str) or not batch.strip() or value.get("finish_ticket_batch_for", batch) != batch):
            raise ValueError("The ticket acceptance limit has no valid original human binding")
    if "finish_ticket_batch_instruction_id" in value:
        instruction_id = value["finish_ticket_batch_instruction_id"]
        if "finish_ticket_batch_for" not in value or not isinstance(instruction_id, str) or not instruction_id.strip():
            raise ValueError("The adopted ticket input has no valid instruction binding")
    if value.get("source") == "handoff":
        if (operation.kind != "message" or operation.attachment_ids or value.get("recovery_of_client_id")
                or value.get("handoff_backend") not in {"codex", "claude-code"}
                or not all(isinstance(value.get(key), str) and value[key] for key in (
                    "delivery_id", "handoff_from", "previous_native_id", "knowledge", "trusted_request"))):
            raise ValueError("Ticket handoff has no complete original binding")
        fresh = value.get("fresh_native_id")
        if fresh is not None and (not isinstance(fresh, str) or not fresh or fresh == value["previous_native_id"]):
            raise ValueError("Ticket handoff has no distinct fresh native history")
        progress = value.get("answered_progress_request_ids", [])
        if (not isinstance(progress, list)
                or any(not isinstance(request_id, str) or not request_id.strip() for request_id in progress)
                or len(set(progress)) != len(progress)):
            raise ValueError("Ticket handoff contains invalid answered progress input IDs")
        handoff_human_scope(value)
    return {**{key: item for key, item in value.items() if key not in {"content", "attachment_ids", "wiki_mode"}},
            "operation": operation.model_dump()}
