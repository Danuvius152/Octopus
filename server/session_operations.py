"""Submitted session operations, separate from the effective execution context."""

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

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


class CompactResult(BaseModel):
    """Observed history effects, independent of the request's business status."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    effect: Literal["applied", "not_applied", "unknown"]
    reason: str | None = None
    connection_generation: str | None = None
    thread_id: str | None = None
    turn_id: str | None = None
    item_ids: list[str] = Field(default_factory=list)


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
    return {**{key: item for key, item in value.items() if key not in {"content", "attachment_ids", "wiki_mode"}},
            "operation": operation.model_dump()}
