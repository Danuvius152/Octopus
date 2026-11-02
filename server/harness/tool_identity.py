"""Capture tool provenance once; projections never infer a control target."""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from ..models import RuntimeMetadata, ToolReceipt
from .events import HarnessEvent

_SPAWN_TOOLS = {"Task", "Agent", "subagent:spawn_agent"}
_CHILD_OBSERVATION_TOOLS = {"subagent:wait", "subagent:wait_agent", "subagent:close_agent"}


@dataclass
class _ChildRun:
    identity: RuntimeMetadata
    status: str
    ambiguous: bool = False
    completed_turns: set[str] = field(default_factory=set)


@dataclass
class _CapturedCall:
    identity: RuntimeMetadata
    name: str | None
    observed_children: dict[str, RuntimeMetadata | None] = field(default_factory=dict)
    result_seen: bool = False


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
        self._calls: dict[str | tuple[str, ...], _CapturedCall] = {}
        self._children: dict[str, _ChildRun] = {}
        # One claim per actual spawn ID; None retains evidence of ambiguity.
        # Ordinary calls need no process-history entries.
        self._spawn_claims: dict[str | tuple[str, ...], RuntimeMetadata | None] = {}

    def _resolve_result(self, event: HarnessEvent) -> None:
        raw_id = self._key(event)
        captured = self._calls.get(raw_id)
        if captured and not captured.result_seen:
            captured.result_seen = True
            identity, name = captured.identity, captured.name
            if not captured.observed_children:
                self._calls.pop(raw_id, None)
            event.runtime_metadata = identity.model_copy(update={
                "receipt": decode_receipt(name, event.content, event.is_error)
            }).model_dump()
            raw = event.raw or {}
            if (name in {"subagent:send_input", "subagent:resume_agent"}
                    and not event.is_error and raw.get("status") == "completed"):
                receivers, states = raw.get("receiver_thread_ids"), raw.get("agents_states", {})
                if not isinstance(receivers, list) or not isinstance(states, dict):
                    return
                for child_id in receivers:
                    if not isinstance(child_id, str) or not child_id:
                        continue
                    state = states.get(child_id, {})
                    status = state.get("status") if isinstance(state, dict) else None
                    if (isinstance(state, dict) and (status is None or isinstance(status, str)
                            and status in {"", "pending_init", "running", "completed"})):
                        self._start_child(child_id, identity)

    def _start_child(self, child_id: str, identity: RuntimeMetadata) -> None:
        previous = self._children.get(child_id)
        ambiguous = bool(previous and (previous.ambiguous or previous.status in {"running", "unknown"}))
        self._children[child_id] = _ChildRun(
            identity, "unknown" if ambiguous else "running", ambiguous,
            set(previous.completed_turns) if previous else set(),
        )

    def _resolve_child(self, event: HarnessEvent) -> None:
        if event.type != "subagent" or event.subagent is None:
            return
        update = event.subagent
        raw_id = (event.raw or {}).get("id")
        observed_call = self._calls.get(raw_id) if isinstance(raw_id, str) else None
        if observed_call is not None and update.task_id in observed_call.observed_children:
            observed_identity = observed_call.observed_children[update.task_id]
            if observed_call.result_seen:
                observed_call.observed_children.pop(update.task_id)
                if not observed_call.observed_children:
                    assert isinstance(raw_id, str)
                    self._calls.pop(raw_id, None)
            current = self._children.get(update.task_id)
            if observed_identity is None or current is None or current.identity != observed_identity:
                # This wait observed a previous round (or an unbound target).
                # Its readable tool result remains in the transcript, but it
                # cannot settle the child execution that started afterwards.
                update.tool_call_id = None
                return
        child = self._children.get(update.task_id)
        if child is None:
            captured_spawn = self._spawn_claims.get(update.tool_use_id or "")
            if captured_spawn and captured_spawn.attempt_id == self._attempt_id:
                child = self._children[update.task_id] = _ChildRun(captured_spawn, update.status)
        if child is not None:
            update.tool_call_id = child.identity.tool_call_id
            if child.ambiguous:
                update.status = "unknown"
                update.stop_reason = "overlapping_execution"
                update.summary = ""
            elif child.status in {"completed", "failed", "cancelled", "interrupted"}:
                if update.status != child.status:
                    update.summary = ""
                    update.stop_reason = None
                update.status = child.status
            else:
                child.status = update.status

    def normalize_observations(
        self, events: list[HarnessEvent], request_id: str, attempt_id: str,
    ) -> list[HarnessEvent]:
        """Accept exact adapter calls; no stdout item-ID or timing guesses.

        A child thread identifies the helper, while each accepted follow-up
        gets its own tool identity. Overlapping child turns cannot be assigned
        to a particular follow-up from completion metadata alone.
        """
        output: list[HarnessEvent] = []
        aliases: dict[str, RuntimeMetadata] = {}
        for event in events:
            raw = event.raw or {}
            kind, tool = raw.get("native_kind"), raw.get("native_tool")
            if raw.get("native_observation") is not True:
                continue
            if event.type == "tool_use":
                child_id = (event.tool_input or {}).get("native_thread_id")
                known = self._children.get(child_id) if isinstance(child_id, str) else None
                if tool == "spawn_agent" and known is not None:
                    if known.identity.attempt_id == attempt_id:
                        aliases[event.tool_use_id or ""] = known.identity
                    continue
                self.normalize(event, request_id, attempt_id)
                captured = RuntimeMetadata.model_validate(event.runtime_metadata)
                if tool in {"spawn_agent", "followup_task"} and isinstance(child_id, str):
                    self._start_child(child_id, captured)
            elif event.type == "tool_result":
                if event.tool_use_id in aliases:
                    continue
                self.normalize(event, request_id, attempt_id)
            elif event.type == "subagent" and event.subagent is not None:
                update = event.subagent
                child = self._children.get(update.task_id)
                if child is None or child.identity.attempt_id != attempt_id:
                    continue
                if kind == "completed":
                    turn = raw.get("native_child_turn_id")
                    if not isinstance(turn, str) or turn in child.completed_turns:
                        continue
                    child.completed_turns.add(turn)
                    if child.ambiguous or child.status in {"completed", "interrupted", "cancelled", "failed"}:
                        continue
                elif kind == "started" and update.tool_use_id in aliases:
                    # Stdout already established this exact child; a second
                    # source must not turn a completed helper back to running.
                    continue
                elif kind == "interacted" and child.ambiguous:
                    update.status = "unknown"
                    update.stop_reason = "overlapping_execution"
                elif kind == "interrupted":
                    if child.status in {"completed", "failed", "cancelled", "interrupted"}:
                        continue
                    update.stop_reason = "native_interrupted"
                update.tool_call_id = child.identity.tool_call_id
                event.identity_checked = True
                child.status = update.status
            else:
                continue
            event.raw = None
            output.append(event)
        return output

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
        raw = self._key(event)
        native = RuntimeMetadata.model_validate(event.runtime_metadata).native if event.runtime_metadata else None
        if raw and event.type in ("tool_use", "question_request"):
            if event.type == "tool_use" or raw not in self._calls:
                observations = {}
                if event.tool_name in _CHILD_OBSERVATION_TOOLS:
                    for child_id in (event.tool_input or {}).get("agents", []):
                        if isinstance(child_id, str):
                            child = self._children.get(child_id)
                            observations[child_id] = child.identity if child else None
                self._calls[raw] = _CapturedCall(
                    RuntimeMetadata(request_id=request_id, attempt_id=attempt_id, tool_call_id=uuid.uuid4().hex, native=native),
                    event.tool_name, observations,
                )
                if raw in self._spawn_claims:
                    self._spawn_claims[raw] = None
                elif event.tool_name in _SPAWN_TOOLS:
                    self._spawn_claims[raw] = self._calls[raw].identity
            event.runtime_metadata = self._calls[raw].identity.model_dump()
        elif raw and event.type == "tool_result":
            if native is not None and raw not in self._calls:
                self._calls[raw] = _CapturedCall(RuntimeMetadata(request_id=request_id, attempt_id=attempt_id,
                    tool_call_id=uuid.uuid4().hex, native=native), event.tool_name)
            self._resolve_result(event)
        self._resolve_child(event)

    @staticmethod
    def _key(event: HarnessEvent) -> str | tuple[str, ...]:
        metadata = event.runtime_metadata or {}
        source = metadata.get("native")
        if source and event.tool_use_id:
            return (metadata["request_id"], metadata["attempt_id"], source["thread_id"], source["turn_id"], source["item_id"])
        return event.tool_use_id or ""
