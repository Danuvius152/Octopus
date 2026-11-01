"""Session-owned result association, with consumer-specific text projections."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError

from .models import FinalAnswerRef, TurnOutcome


@dataclass(frozen=True)
class SessionResult:
    input_request_id: str
    request_id: str
    outcome: TurnOutcome | None = None
    attempt_id: str | None = None
    fragments: tuple[str, ...] = ()
    cost: float | None = None
    missing: str | None = None
    message_seqs: tuple[int, ...] = ()

    def reply_text(self) -> str:
        return _reply_text(self.fragments)


def _reply_text(fragments: tuple[str, ...] | list[str]) -> str:
    return "\n\n".join(fragment for fragment in fragments if fragment).strip()


def referenced_fragments(answer: FinalAnswerRef, request_id: str, attempt_id: str | None,
                         rows: list[dict[str, Any]], terminal_seq: int | None) -> tuple[str, ...] | None:
    """Both live and committed readers validate the terminal's exact selection."""
    seqs = answer.message_seqs
    if (not attempt_id or not answer.thread_id or not answer.turn_id or terminal_seq is None
            or seqs != sorted(set(seqs)) or any(seq < 0 or seq >= terminal_seq for seq in seqs)):
        return None
    selected = {row.get("seq"): row for row in rows}
    fragments = []
    for seq in seqs:
        row = selected.get(seq)
        if row is None or row.get("role") != "assistant" or row.get("type") != "text" or not isinstance(row.get("content"), str):
            return None
        metadata = row.get("runtime_metadata") or {}
        native = metadata.get("native") or {}
        if (metadata.get("request_id") != request_id or metadata.get("attempt_id") != attempt_id
                or native.get("thread_id") != answer.thread_id or native.get("root_thread_id") != answer.thread_id
                or native.get("turn_id") != answer.turn_id or native.get("root_turn_id") != answer.turn_id
                or not native.get("item_id") or native.get("phase") != "final_answer"):
            return None
        fragments.append(row["content"])
    return tuple(fragments)


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
    if outcome.final_answer is not None:
        selected = referenced_fragments(outcome.final_answer, request_id, attempt_id, rows, terminal.get("seq"))
        return SessionResult(input_request_id, request_id, outcome, attempt_id, selected or (), terminal.get("cost"),
                             missing="answer_reference_invalid" if selected is None else None,
                             message_seqs=tuple(outcome.final_answer.message_seqs) if selected is not None else ())
    selected_rows = [row for row in rows
        if row["role"] == "assistant" and row["type"] == "text" and isinstance(row["content"], str)
        and (row.get("tool_input") or {}).get("attempt_context", {}).get("request_id") == request_id
        and (not attempt_id or (row.get("tool_input") or {}).get("attempt_context", {}).get("attempt_id") == attempt_id)]
    return SessionResult(input_request_id, request_id, outcome, attempt_id,
                         tuple(row["content"] for row in selected_rows), terminal.get("cost"),
                         message_seqs=tuple(row["seq"] for row in selected_rows if isinstance(row.get("seq"), int)))


@dataclass
class LiveReply:
    """The live consumer's original request/attempt, without a database reread."""

    request_id: str
    attempt_id: str | None = None
    fragments: list[str] = field(default_factory=list)
    missing: str | None = None
    _native_rows: list[dict[str, Any]] = field(default_factory=list, repr=False)
    _terminal: bool = False

    def observe(self, event: dict[str, Any]) -> bool | None:
        """None ignores another request; True asks the consumer to emit reset."""
        if event.get("request_id") != self.request_id or self._terminal:
            return None
        reset = False
        attempt = event.get("attempt_id")
        if attempt and attempt != self.attempt_id:
            reset = self.attempt_id is not None
            self.attempt_id = attempt
            self.fragments.clear()
            self._native_rows.clear()
            self.missing = None
        kind = event.get("type")
        if event.get("retrying") and kind in {"recovery", "result", "error"}:
            self.fragments.clear()
            reset |= kind == "recovery"
        elif kind == "assistant_text":
            native = (event.get("runtime_metadata") or {}).get("native")
            if (not native or native.get("thread_id") == native.get("root_thread_id")
                    and native.get("turn_id") == native.get("root_turn_id") and native.get("phase") == "final_answer"):
                if native:
                    self._native_rows.append({"seq": event.get("seq"), "role": "assistant", "type": "text",
                        "content": event.get("content"), "runtime_metadata": event["runtime_metadata"]})
                if event.get("content"):
                    self.fragments.append(event["content"])
        elif kind == "result" and event.get("terminal"):
            self._terminal = True
            try:
                outcome = TurnOutcome.model_validate(event.get("content"))
            except ValidationError:
                self.missing = "terminal_invalid"
            else:
                if outcome.request_id != self.request_id:
                    self.missing = "terminal_invalid"
                elif outcome.final_answer is not None:
                    selected = referenced_fragments(outcome.final_answer, self.request_id, self.attempt_id,
                                                    self._native_rows, event.get("seq"))
                    if selected is None:
                        self.missing = "answer_reference_invalid"
                    else:
                        replacement = [fragment for fragment in selected if fragment]
                        reset |= replacement != self.fragments
                        self.fragments = replacement
            if self.missing:
                reset |= bool(self.fragments)
                self.fragments.clear()
            self._native_rows.clear()
        return reset

    def text(self) -> str:
        return _reply_text(self.fragments)
