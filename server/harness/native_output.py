"""Observed native work identities, independent of the bounded card cache.

This is a projection of thread/turn/item facts, never a scheduler or a test
for parent consumption. Bodies stay in the existing transcript.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from typing import Any

from ..models import NativeSource, RuntimeMetadata
from .events import HarnessEvent, SubagentUpdate


def recorded_file_change_paths(message: dict[str, Any]) -> list[str]:
    """Read saved native patch facts, including a completion without a start.

    The wire payload stays in its original tool record. Consumers never infer
    paths from model text or mistake an arbitrary tool's JSON output for a patch.
    """
    native = (message.get("runtime_metadata") or {}).get("native")
    if not native or message.get("tool_name") != "fileChange":
        return []
    if message.get("type") == "tool_use":
        params = message.get("tool_input")
    elif message.get("type") == "tool_result" and isinstance(message.get("content"), str):
        try:
            params = json.loads(message["content"])
        except (ValueError, TypeError):
            return []
    else:
        return []
    if not isinstance(params, dict):
        return []
    item = params.get("item")
    if (not isinstance(item, dict) or item.get("type") != "fileChange"
            or (params.get("threadId"), params.get("turnId"), item.get("id"))
            != (native.get("thread_id"), native.get("turn_id"), native.get("item_id"))):
        return []
    changes = item.get("changes")
    if not isinstance(changes, list):
        return []
    paths = []
    for change in changes:
        if not isinstance(change, dict):
            continue
        if isinstance(change.get("path"), str) and change["path"]:
            paths.append(change["path"])
        kind = change.get("kind")
        if isinstance(kind, dict) and kind.get("type") == "update" and isinstance(kind.get("move_path"), str) and kind["move_path"]:
            paths.append(kind["move_path"])
    return paths


@dataclass
class _Work:
    update: SubagentUpdate
    source: NativeSource


class NativeOutput:
    def __init__(self, request_id: str, attempt_id: str, thread_id: str, turn_id: str) -> None:
        self.request_id, self.attempt_id = request_id, attempt_id
        self.thread_id, self.turn_id = thread_id, turn_id
        self.parents: dict[str, str | None] = {}
        self.names: dict[str, str] = {}
        self._work: dict[tuple[str, str], _Work] = {}
        self._pending: dict[str, _Work] = {}
        self._current: dict[str, str] = {}
        self._waits: dict[tuple[str, str, str], dict[str, _Work]] = {}

    def source(self, thread: str, turn: str, item: str | None = None, phase: str | None = None) -> NativeSource:
        return NativeSource(thread_id=thread, turn_id=turn, item_id=item,
            root_thread_id=self.thread_id, root_turn_id=self.turn_id,
            parent_thread_id=self.parents.get(thread), phase=phase)

    def metadata(self, source: NativeSource) -> dict[str, Any]:
        return RuntimeMetadata(request_id=self.request_id, attempt_id=self.attempt_id, native=source).model_dump()

    def accepts(self, thread: Any, turn: dict[str, Any]) -> bool:
        if thread != self.thread_id and turn.get("rootTurnId") not in {None, self.turn_id}:
            return False
        if thread == self.thread_id or thread in self.parents:
            return True
        if isinstance(thread, str) and turn.get("rootTurnId") == self.turn_id:
            self.parents[thread] = None
            return True
        return False

    def _event(self, work: _Work) -> HarnessEvent:
        metadata = self.metadata(work.source)
        return HarnessEvent(type="subagent", subagent=replace(work.update, runtime_metadata=metadata),
            runtime_metadata=metadata, identity_checked=True)

    def observe(self, method: str, params: dict[str, Any]) -> list[HarnessEvent]:
        thread = params["threadId"]
        if not isinstance(thread, str):
            raise ValueError("Native output has no thread identity")
        turn = params.get("turn") or {}
        item = params.get("item") or {}
        events: list[HarnessEvent] = []
        observation = (thread, params.get("turnId", ""), item.get("id", ""))
        if item.get("type") == "collabAgentToolCall" and item.get("tool") == "wait":
            if method == "item/started":
                self._waits[observation] = {
                    child: work for child in item.get("receiverThreadIds") or []
                    if (work := self._work.get((child, self._current.get(child, ""))) or self._pending.get(child)) is not None}
            elif method == "item/completed":
                captured = self._waits.pop(observation, {})
                for observed_child, work in captured.items():
                    state = (item.get("agentsStates") or {}).get(observed_child) or {}
                    status = {"completed": "completed", "errored": "failed", "interrupted": "interrupted"}.get(state.get("status"))
                    if status is not None and work.update.status == "running":
                        work.update = replace(work.update, status=status)
                        events.append(self._event(work))
        if method == "item/completed" and item.get("type") in {"subAgentActivity", "collabAgentToolCall"}:
            source = self.source(thread, params["turnId"], item["id"])
            if item["type"] == "subAgentActivity":
                receivers = [item.get("agentThreadId")]
                starts = item.get("kind") == "started"
            else:
                receivers = item.get("receiverThreadIds") or []
                starts = item.get("tool") in {"spawnAgent", "spawn_agent"}
            for child in receivers:
                if not isinstance(child, str) or not child or child == self.thread_id:
                    continue
                if starts:
                    self.parents[child] = thread
                else:
                    self.parents.setdefault(child, None)
                if item.get("agentPath"):
                    self.names[child] = item["agentPath"]
                current = self._work.get((child, self._current.get(child, "")))
                # Wait completion and parent-side 'completed' activities have
                # no exact child turn. They never finish a newer work round.
                if not starts:
                    # QueueOnly messages and TriggerTurn follow-ups share the
                    # same interacted notification. Only an observed child
                    # turn creates another work round; keep the interaction
                    # itself in the original complete tool item.
                    observed = current or self._pending.get(child)
                    if observed is not None and item.get("agentPath"):
                        observed.update = replace(observed.update, name=self.names.get(child, observed.update.name))
                        events.append(self._event(observed))
                    continue
                if current is not None:
                    current.update = replace(current.update, name=self.names.get(child, current.update.name),
                                             parent_thread_id=self.parents.get(child))
                    current.source = current.source.model_copy(update={"parent_thread_id": self.parents.get(child)})
                    events.append(self._event(current))
                    continue
                if child in self._pending:
                    continue
                card = json.dumps([self.request_id, self.attempt_id, child, thread, source.turn_id, source.item_id])
                update = SubagentUpdate(task_id=child, card_id=card, native_thread_id=child,
                    parent_thread_id=self.parents.get(child), name=self.names.get(child, ""), prompt=item.get("prompt") or "")
                pending = self._pending[child] = _Work(update, source)
                events.append(self._event(pending))
        if thread != self.thread_id and method in {"turn/started", "turn/completed"}:
            identifier = turn.get("id")
            if not isinstance(identifier, str) or not identifier:
                raise ValueError("Native child turn has no identity")
            key = thread, identifier
            work = self._work.get(key)
            if work is None:
                pending = self._pending.pop(thread, None)
                source = self.source(thread, identifier)
                update = SubagentUpdate(task_id=thread, card_id=json.dumps([self.request_id, self.attempt_id, thread, identifier]),
                    native_thread_id=thread, native_turn_id=identifier, parent_thread_id=self.parents.get(thread),
                    name=self.names.get(thread, ""))
                if pending is not None:
                    update = replace(pending.update, native_turn_id=identifier,
                        status="running" if pending.update.status in {"running", "unknown"} else pending.update.status)
                work = self._work[key] = _Work(update, source)
                self._current[thread] = identifier
            if method == "turn/completed":
                status = {"completed": "completed", "failed": "failed", "interrupted": "interrupted"}.get(turn.get("status"))
                if status is None:
                    raise ValueError("Unknown native child terminal status")
                if work.update.status == status:
                    return events
                work.update = replace(work.update, status=status)
            events.append(self._event(work))
        if len(self._work) + len(self._pending) + len(self._waits) > 4096:
            raise ValueError("Native observation identity capacity exceeded")
        return events

    def retire(self, reason: str, *, reconcile: bool = False) -> list[HarnessEvent]:
        events = []
        for work in [*self._work.values(), *self._pending.values()]:
            if work.update.status in {"running", "unknown"}:
                work.update = replace(work.update, status="interrupted", stop_reason=reason)
            elif not reconcile:
                continue
            # Failure may have withheld a real terminal after updating this
            # same bounded projection. Reconcile its final known observation;
            # completed work must neither stay running nor become interrupted.
            events.append(self._event(work))
        return events
