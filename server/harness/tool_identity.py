"""Capture tool provenance once; projections never infer a control target."""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Iterable
from typing import Any

from ..models import RuntimeMetadata, ToolReceipt
from .events import HarnessEvent

_SPAWN_TOOLS = {"Task", "Agent", "subagent:spawn_agent"}


def _text_blocks(content: Any) -> Iterable[str]:
    if isinstance(content, str):
        try:
            value = json.loads(content)
        except (ValueError, TypeError):
            yield content
        else:
            if isinstance(value, (list, dict)):
                yield from _text_blocks(value)
            else:
                yield content
    elif isinstance(content, list):
        for block in content:
            yield from _text_blocks(block)
    elif isinstance(content, dict):
        if content.get("type") == "text" and isinstance(content.get("text"), str):
            yield content["text"]
        elif isinstance(content.get("content"), list):
            yield from _text_blocks(content["content"])


def decode_receipt(tool_name: str | None, content: Any, is_error: bool) -> ToolReceipt | None:
    if is_error:
        return None
    receipts = []
    for text in _text_blocks(content):
        if tool_name == "mcp__bg__run":
            match = re.match(r"Started bg task `([A-Za-z0-9-]+)`", text)
            if match:
                receipts.append(ToolReceipt(kind="bg", task_id=match[1]))
        elif tool_name == "mcp__ask_agent__ask":
            match = re.match(r"(?:Started|Continued) delegation `([A-Za-z0-9-]+)`[^\n]*?\. work_id=([A-Za-z0-9-]+)\.", text)
            if match and match[2] != "unknown":
                receipts.append(ToolReceipt(kind="delegation", delegation_id=match[1], work_id=match[2]))
    return receipts[0] if len(receipts) == 1 else None


class ToolIdentityTracker:
    """One actual HarnessRun's captured calls and native child provenance."""

    def __init__(self) -> None:
        self._attempt_id: str | None = None
        self._calls: dict[str, tuple[RuntimeMetadata, str | None]] = {}
        self._children: dict[str, RuntimeMetadata] = {}
        # One claim per actual spawn ID; None retains evidence of ambiguity.
        # Ordinary calls need no process-history entries.
        self._spawn_claims: dict[str, RuntimeMetadata | None] = {}

    def _resolve_result(self, event: HarnessEvent) -> None:
        captured = self._calls.pop(event.tool_use_id or "", None)
        if captured:
            identity, name = captured
            event.runtime_metadata = identity.model_copy(update={
                "receipt": decode_receipt(name, event.content, event.is_error)
            }).model_dump()

    def _resolve_child(self, event: HarnessEvent) -> None:
        if event.type != "subagent" or event.subagent is None:
            return
        update = event.subagent
        child_identity = self._children.get(update.task_id)
        if child_identity is None:
            captured_spawn = self._spawn_claims.get(update.tool_use_id or "")
            if captured_spawn and captured_spawn.attempt_id == self._attempt_id:
                child_identity = captured_spawn
                self._children[update.task_id] = child_identity
        if child_identity:
            update.tool_call_id = child_identity.tool_call_id

    def normalize_idle(self, event: HarnessEvent) -> None:
        """Idle output can finish captured work, never create a claimed call."""
        if event.identity_checked:
            return
        event.identity_checked = True
        if event.type in ("tool_use", "question_request") and event.tool_use_id:
            if event.tool_name in _SPAWN_TOOLS or event.tool_use_id in self._spawn_claims:
                self._spawn_claims[event.tool_use_id] = None
            # An unsolicited reuse cannot claim a result belonging to an old
            # pending call merely because the provider repeats its raw ID.
            self._calls.pop(event.tool_use_id, None)
        elif event.type == "tool_result":
            self._resolve_result(event)
        self._resolve_child(event)

    def normalize(self, event: HarnessEvent, request_id: str, attempt_id: str) -> None:
        if event.identity_checked:
            return
        event.identity_checked = True
        if attempt_id != self._attempt_id:
            self._attempt_id = attempt_id
            self._calls.clear()
        raw = event.tool_use_id
        if raw and event.type in ("tool_use", "question_request"):
            if event.type == "tool_use" or raw not in self._calls:
                self._calls[raw] = (RuntimeMetadata(request_id=request_id, attempt_id=attempt_id,
                                                    tool_call_id=uuid.uuid4().hex), event.tool_name)
                if raw in self._spawn_claims:
                    self._spawn_claims[raw] = None
                elif event.tool_name in _SPAWN_TOOLS:
                    self._spawn_claims[raw] = self._calls[raw][0]
            event.runtime_metadata = self._calls[raw][0].model_dump()
        elif raw and event.type == "tool_result":
            self._resolve_result(event)
        self._resolve_child(event)
