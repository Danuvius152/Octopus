"""Converting a HarnessEvent into what gets persisted and what gets broadcast.

One slice of `SessionManager`, which was a single 4,017-line class with 84
methods (docs/post-mortems/2026-09-25-polish-pass.md). Split by responsibility; the
methods are unchanged and still reached through one `session_manager` object,
so no call site moved.
"""

from __future__ import annotations

from typing import Any

from ..harness import HarnessEvent
from ..models import MessageContent, MessageRole, RuntimeMetadata
from .base import (
    _MAX_SUBAGENTS_PER_SESSION,
    Session,
    SessionManagerBase,
)


class EventsMixin(SessionManagerBase):

    @staticmethod
    def _event_to_message_content(event: HarnessEvent) -> MessageContent | None:
        if event.type == "text":
            if event.content is None or not event.content.strip() and not (event.runtime_metadata or {}).get("native"):
                return None
            return MessageContent(
                role=MessageRole.assistant, type="text", content=event.content,
                tool_input=event.tool_input,
                runtime_metadata=RuntimeMetadata.model_validate(event.runtime_metadata) if event.runtime_metadata else None,
            )
        if event.type == "thinking":
            # Persist thinking as a typed message; the UI can choose to hide
            # it. Don't filter at the persistence layer.
            return MessageContent(
                role=MessageRole.assistant,
                type="thinking",
                content=event.content,
                tool_input=event.tool_input,
                runtime_metadata=RuntimeMetadata.model_validate(event.runtime_metadata) if event.runtime_metadata else None,
            )
        if event.type == "subagent" and event.subagent is not None and event.subagent.card_id:
            from dataclasses import asdict
            details = asdict(event.subagent)
            details.pop("summary", None)
            details.pop("prompt", None)
            details.pop("runtime_metadata", None)  # The typed message field owns this source.
            return MessageContent(role=MessageRole.system, type="native_subagent", tool_input=details,
                runtime_metadata=RuntimeMetadata.model_validate(event.runtime_metadata))
        if event.type == "tool_use":
            return MessageContent(
                role=MessageRole.assistant,
                type="tool_use",
                tool_name=event.tool_name,
                tool_input=event.tool_input,
                tool_use_id=event.tool_use_id,
                runtime_metadata=RuntimeMetadata.model_validate(event.runtime_metadata) if event.runtime_metadata else None,
            )
        if event.type == "tool_result":
            return MessageContent(
                role=MessageRole.tool,
                type="tool_result",
                content=event.content,
                tool_name=event.tool_name,
                tool_input=event.tool_input,
                tool_use_id=event.tool_use_id,
                runtime_metadata=RuntimeMetadata.model_validate(event.runtime_metadata) if event.runtime_metadata else None,
                is_error=event.is_error,
            )
        if event.type == "question_request":
            return MessageContent(
                role=MessageRole.assistant,
                type="question_request",
                tool_name="AskUserQuestion",
                tool_input=event.tool_input,
                tool_use_id=event.tool_use_id,
                runtime_metadata=RuntimeMetadata.model_validate(event.runtime_metadata) if event.runtime_metadata else None,
            )
        if event.type == "result":
            return MessageContent(
                role=MessageRole.system,
                type="result",
                content=event.content,
                is_error=event.is_error,
                session_id=event.session_id,
                cost=event.cost,
            )
        return None

    @staticmethod
    def _record_subagent(session: Session, update: Any) -> None:
        """Merge one sub-agent observation into the session's live map.

        Every harness reports sub-agents in partial shapes — a status patch
        with no name, a summary with no counters — so each observation is
        merged onto the last (`SubagentUpdate.merged_with`). Bounded, oldest
        first: a long session can spawn many, and this is UI state, not
        history.
        """
        key = update.card_id or update.tool_call_id
        if not key:
            return
        session._subagents[key] = update.merged_with(session._subagents.get(key))
        while len(session._subagents) > _MAX_SUBAGENTS_PER_SESSION:
            session._subagents.pop(next(iter(session._subagents)))

    def _flush_text_deltas(
        self, session_id: str, buf: list[str], metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Drain the coalescing buffer into one `assistant_delta` frame.

        Empties `buf` in place and returns None when there was nothing to send,
        so callers can flush unconditionally. The frame shape comes from
        `_event_to_ws_message` rather than being rebuilt here — one definition
        of the wire format, not two.
        """
        if not buf:
            return None
        text = "".join(buf)
        buf.clear()
        return self._event_to_ws_message(
            session_id, HarnessEvent(type="text_delta", content=text, runtime_metadata=metadata)
        )

    @staticmethod
    def _event_to_ws_message(session_id: str, event: HarnessEvent) -> dict[str, Any] | None:
        if event.type == "text_delta":
            # Broadcast-only (never persisted — `_event_to_message_content`
            # has no branch for it). The client appends these to a scratch
            # buffer and drops the buffer when the real `assistant_text`
            # lands, so losing one costs a flicker, not a message.
            if not event.content:
                return None
            return {
                "type": "assistant_delta",
                "session_id": session_id,
                "content": event.content,
                **({"runtime_metadata": event.runtime_metadata} if event.runtime_metadata else {}),
            }
        if event.type == "text":
            if event.content is None or not event.content.strip() and not (event.runtime_metadata or {}).get("native"):
                return None
            return {
                "type": "assistant_text",
                "session_id": session_id,
                "content": event.content,
                **({"tool_input": event.tool_input} if event.tool_input else {}),
                **({"runtime_metadata": event.runtime_metadata} if event.runtime_metadata else {}),
            }
        if event.type == "thinking":
            # We persist thinking but don't broadcast it by default — the
            # UI doesn't render it today.
            return None
        if event.type == "tool_use":
            return {
                "type": "tool_use",
                "session_id": session_id,
                "tool": event.tool_name,
                "input": event.tool_input,
                "tool_use_id": event.tool_use_id,
            }
        if event.type == "tool_result":
            return {
                "type": "tool_result",
                "session_id": session_id,
                "tool_use_id": event.tool_use_id,
                "output": event.content,
                "is_error": event.is_error,
                **({"tool_input": event.tool_input} if event.tool_input else {}),
            }
        if event.type == "subagent" and event.subagent is not None:
            u = event.subagent
            return {
                "type": "subagent",
                "session_id": session_id,
                "task_id": u.task_id,
                "tool_use_id": u.tool_use_id,
                "tool_call_id": u.tool_call_id,
                "card_id": u.card_id,
                "native_thread_id": u.native_thread_id,
                "native_turn_id": u.native_turn_id,
                "parent_thread_id": u.parent_thread_id,
                "runtime_metadata": event.runtime_metadata,
                "status": u.status,
                "stop_reason": u.stop_reason,
                "name": u.name,
                "description": u.description,
                "prompt": u.prompt,
                "summary": u.summary,
                "tokens": u.tokens,
                "tool_uses": u.tool_uses,
                "duration_ms": u.duration_ms,
            }
        if event.type == "question_request":
            return {
                "type": "question_request",
                "session_id": session_id,
                "question_id": event.tool_use_id,
                "questions": (event.tool_input or {}).get("questions") or [],
            }
        if event.type == "result":
            return {
                "type": "result",
                "session_id": session_id,
                "claude_session_id": event.session_id,
                "cost": event.cost,
                "turns": event.num_turns,
                "duration_ms": event.duration_ms,
                "is_error": event.is_error,
                "content": event.content,
            }
        return None
