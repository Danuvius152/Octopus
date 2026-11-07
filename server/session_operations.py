"""Submitted session operations, separate from the effective execution context."""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

OperationKind = Literal["message"]


class MessageOperation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["message"]
    content: str
    attachment_ids: list[str]
    wiki_mode: Literal["auto", "read", "maintain"] | None = None


def decode_payload(value: dict[str, Any]) -> dict[str, Any]:
    """Normalize legacy flat messages only at the retained-input boundary.

    A present operation must validate as such; a malformed or unknown kind
    must never fall back to treating arbitrary data as a user message.
    """
    if "operation" in value:
        operation = MessageOperation.model_validate(value["operation"])
    else:
        operation = MessageOperation(
            kind="message", content=value["content"], attachment_ids=value["attachment_ids"], wiki_mode=value.get("wiki_mode"),
        )
    return {**{key: item for key, item in value.items() if key not in {"content", "attachment_ids", "wiki_mode"}},
            "operation": operation.model_dump()}
