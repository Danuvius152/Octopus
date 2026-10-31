"""Session-owned result association, with consumer-specific text projections."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError

from .models import TurnOutcome


@dataclass(frozen=True)
class SessionResult:
    input_request_id: str
    request_id: str
    outcome: TurnOutcome | None = None
    attempt_id: str | None = None
    fragments: tuple[str, ...] = ()
    cost: float | None = None
    missing: str | None = None


def committed_result(input_request_id: str, request_id: str, rows: list[dict[str, Any]]) -> SessionResult:
    terminal = next((row for row in rows if row["type"] == "result"), None)
    if terminal is None:
        return SessionResult(input_request_id, request_id, missing="terminal_missing")
    try:
        outcome = TurnOutcome.model_validate(terminal["content"])
    except ValidationError:
        return SessionResult(input_request_id, request_id, missing="terminal_invalid")
    if outcome.request_id != request_id:
        return SessionResult(input_request_id, request_id, missing="terminal_invalid")
    attempt_id = (terminal.get("tool_input") or {}).get("attempt_context", {}).get("attempt_id")
    fragments = tuple(row["content"] for row in rows
        if row["role"] == "assistant" and row["type"] == "text" and isinstance(row["content"], str)
        and (row.get("tool_input") or {}).get("attempt_context", {}).get("request_id") == request_id
        and (not attempt_id or (row.get("tool_input") or {}).get("attempt_context", {}).get("attempt_id") == attempt_id))
    return SessionResult(input_request_id, request_id, outcome, attempt_id, fragments, terminal.get("cost"))


@dataclass
class LiveReply:
    """The live consumer's original request/attempt, without a database reread."""

    request_id: str
    attempt_id: str | None = None
    fragments: list[str] = field(default_factory=list)

    def observe(self, event: dict[str, Any]) -> bool | None:
        """None ignores another request; True asks the consumer to emit reset."""
        if event.get("request_id") != self.request_id:
            return None
        reset = False
        attempt = event.get("attempt_id")
        if attempt and attempt != self.attempt_id:
            reset = self.attempt_id is not None
            self.attempt_id = attempt
            self.fragments.clear()
        kind = event.get("type")
        if event.get("retrying") and kind in {"recovery", "result", "error"}:
            self.fragments.clear()
            reset |= kind == "recovery"
        elif kind == "assistant_text" and event.get("content"):
            self.fragments.append(event["content"])
        return reset

    def text(self) -> str:
        return "\n\n".join(self.fragments).strip()
