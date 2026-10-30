"""Metadata-only client response windows, never server decoder timings.

See docs/implemented/codex-performance-monitor.md. Usage can be written after a tool
finishes: the last generated item, not the usage timestamp, ends the window.
"""
from __future__ import annotations

import json
import math
import re
from datetime import datetime
from typing import Any

from .events import HarnessEvent

_STRING = re.compile(r'"(?:[^"\\]|\\.)*"')
_PRIMITIVE = re.compile(r'(?:true|false|null|-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)')
_TOKENS = dict.fromkeys(("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens", "total_tokens"))
_PAYLOAD = {**dict.fromkeys(("type", "role", "thread_id", "turn_id", "response_id", "model", "effort", "service_tier", "time_to_first_token_ms")),
            "usage": _TOKENS, "info": {"last_token_usage": _TOKENS, "total_token_usage": _TOKENS}}
_FIELDS = {"type": None, "timestamp": None, "payload": _PAYLOAD}
_DECODER = json.JSONDecoder()
_INPUTS = {"function_call_output", "custom_tool_call_output", "local_shell_call_output", "tool_search_output", "agent_message"}


def _skip(text: str, position: int) -> int:
    """Skip opaque JSON without decoding strings, arguments or message bodies."""
    stack: list[str] = []
    while position < len(text):
        char = text[position]
        if char == '"':
            match = _STRING.match(text, position)
            if match is None:
                raise ValueError("incomplete string")
            position = match.end()
        elif char in "[{":
            stack.append("}" if char == "{" else "]")
            if len(stack) > 128:
                raise ValueError("metadata nesting")
            position += 1
        elif char in "]}":
            if not stack or stack.pop() != char:
                raise ValueError("invalid container")
            position += 1
        elif stack:
            position += 1
        else:
            match = _PRIMITIVE.match(text, position)
            if match is None:
                raise ValueError("invalid value")
            position = match.end()
        if not stack:
            return position
    raise ValueError("incomplete value")


def _project(text: str, fields: dict, position: int = 0) -> tuple[dict[str, Any], int]:
    while position < len(text) and text[position].isspace():
        position += 1
    if position >= len(text) or text[position] != "{":
        raise ValueError("expected object")
    position += 1
    result: dict[str, Any] = {}
    seen: set[str] = set()
    while position < len(text):
        while position < len(text) and text[position].isspace():
            position += 1
        if text[position:position + 1] == "}":
            return result, position + 1
        key, position = _DECODER.raw_decode(text, position)
        if not isinstance(key, str) or key in seen:
            raise ValueError("invalid metadata key")
        seen.add(key)
        while position < len(text) and text[position].isspace():
            position += 1
        if text[position:position + 1] != ":":
            raise ValueError("expected colon")
        position += 1
        while position < len(text) and text[position].isspace():
            position += 1
        if key not in fields:
            position = _skip(text, position)
        elif isinstance(fields[key], dict) and text[position:position + 1] == "{":
            result[key], position = _project(text, fields[key], position)
        elif text[position:position + 1] not in ("{", "["):
            result[key], position = _DECODER.raw_decode(text, position)
        else:
            position = _skip(text, position)
        while position < len(text) and text[position].isspace():
            position += 1
        if text[position:position + 1] == "}":
            return result, position + 1
        if text[position:position + 1] != ",":
            raise ValueError("expected comma")
        position += 1
    raise ValueError("incomplete object")


class CodexResponseMetrics:
    """One verified native turn; callers own file and subprocess identity."""

    def __init__(self, thread_id: str, turn_id: str, started: float) -> None:
        self.thread_id, self.turn_id, self.started = thread_id, turn_id, started
        self.model: str | None = None
        self.effort: str | None = None
        self.tier: str | None = None
        self._active = False
        self._start: float | None = None
        self._end: float | None = None
        self._next_start: float | None = None
        self._pending: tuple[dict, str | None, float] | None = None
        self._seen: set[str] = set()
        self._modern = False
        self._last_signature: tuple | None = None

    def feed_line(self, line: bytes, now: float) -> list[HarnessEvent]:
        try:
            obj, _ = _project(line.decode("utf-8"), _FIELDS)
            stamp = datetime.fromisoformat(obj["timestamp"].replace("Z", "+00:00"))
            if stamp.tzinfo is None:
                return []
            ts = stamp.timestamp()
            if not math.isfinite(ts) or ts < self.started or ts > now + 2:
                return []
            payload = obj.get("payload")
            if not isinstance(payload, dict):
                return []
            if payload.get("thread_id", self.thread_id) != self.thread_id or payload.get("turn_id", self.turn_id) != self.turn_id:
                return []
            return self._feed(obj.get("type"), payload, ts)
        except (ValueError, KeyError, TypeError, UnicodeError, RecursionError, OverflowError):
            # A broken metadata record invalidates its window, not the turn.
            self._start = None
            return []

    def missing_boundary(self) -> None:
        """An oversized or unavailable record may hide an input boundary."""
        self._start = self._end = self._next_start = None

    def _reset(self, start: float | None) -> None:
        self._start, self._end, self._next_start, self._pending = start, None, None, None

    def _measurement(self, usage: dict, response_id: str | None, ts: float) -> list[HarnessEvent]:
        output = usage.get("output_tokens")
        if type(output) is not int or output < 0:
            self._reset(self._next_start or ts)
            return []
        end = self._end
        duration = (end - self._start) * 1000 if end is not None and self._start is not None and end > self._start else None
        signature = (end, tuple(sorted(usage.items())))
        if response_id is None and self._last_signature is not None and (
            signature == self._last_signature
            or (end is None and signature[1] == self._last_signature[1])
        ):
            self._reset(self._next_start or ts)
            return []
        self._last_signature = signature
        raw: dict[str, Any] = {
            "kind": "codex_response", "model": self.model or "unknown",
            "native_thread_id": self.thread_id, "native_turn_id": self.turn_id,
            "response_id": response_id or f"legacy:{end or ts:.6f}",
            "completed_at": end or ts, "output_tokens": output,
            "effort": self.effort, "service_tier": self.tier,
            "timing_source": "client_response_window", "timing_status": "measured" if duration else "missing_boundary",
        }
        for source, target in (("reasoning_output_tokens", "reasoning_tokens"), ("input_tokens", "input_tokens"), ("cached_input_tokens", "cached_input_tokens")):
            value = usage.get(source)
            raw[target] = value if type(value) is int and value >= 0 else None
        if raw["reasoning_tokens"] is not None and raw["reasoning_tokens"] > output:
            duration = None
            raw["timing_status"] = "invalid_usage"
        event = HarnessEvent(type="model_measurement", duration_ms=round(duration, 3) if duration else None, raw=raw)
        self._reset(self._next_start or ts)
        return [event]

    def _flush(self) -> list[HarnessEvent]:
        if self._pending is None:
            return []
        usage, identifier, ts = self._pending
        return self._measurement(usage, identifier, ts)

    def _feed(self, record_type: Any, payload: dict, ts: float) -> list[HarnessEvent]:
        kind = payload.get("type")
        if record_type == "event_msg" and kind == "task_started":
            if payload.get("turn_id") == self.turn_id:
                self._active = True
                self._reset(ts)
            return []
        if not self._active:
            return []
        if record_type == "turn_context":
            if payload.get("turn_id") != self.turn_id:
                return []
            model = payload.get("model")
            if isinstance(model, str) and 0 < len(model) <= 200:
                self.model = model
            for field, attribute in (("effort", "effort"), ("service_tier", "tier")):
                value = payload.get(field)
                if isinstance(value, str) and len(value) <= 100:
                    setattr(self, attribute, value)
            return []
        if record_type == "event_msg" and kind == "task_complete":
            events = self._flush()
            self._active = False
            ttft = payload.get("time_to_first_token_ms")
            if isinstance(ttft, (int, float)) and not isinstance(ttft, bool) and math.isfinite(ttft) and ttft >= 0:
                events.append(HarnessEvent(type="model_measurement", duration_ms=ttft, raw={
                    "kind": "codex_turn_latency", "model": self.model or "unknown", "completed_at": ts,
                    "native_thread_id": self.thread_id, "native_turn_id": self.turn_id,
                }))
            return events
        if record_type == "response_item":
            generated = kind == "reasoning" or (kind == "message" and payload.get("role") == "assistant") or (isinstance(kind, str) and kind.endswith("_call"))
            incoming = kind in _INPUTS or (kind == "message" and payload.get("role") in {"user", "system", "developer"})
            if generated:
                # Usage may precede items, or arrive after a tool result.
                # Close the previous response before its successor's items.
                events = self._flush() if self._next_start is not None else []
                if self._next_start is not None:
                    # Missing usage crossed a response boundary: assigning
                    # the next usage to this combined span would be a guess.
                    self.missing_boundary()
                self._end = ts
                return events
            elif incoming:
                events = self._flush()
                if self._end is None:
                    self._start = ts
                else:
                    self._next_start = ts
                return events
            return []
        if record_type == "token_usage_record":
            self._modern = True
            identifier = payload.get("response_id")
            if not isinstance(identifier, str) or not identifier or len(identifier) > 300 or identifier in self._seen:
                return []
            self._seen.add(identifier)
            usage = payload.get("usage")
        elif record_type == "event_msg" and kind == "token_count" and not self._modern:
            identifier = None
            info = payload.get("info")
            usage = info.get("last_token_usage") if isinstance(info, dict) else None
        else:
            return []
        if not isinstance(usage, dict):
            return []
        if (identifier is None and self._end is None and self._last_signature is not None
                and tuple(sorted(usage.items())) == self._last_signature[1]):
            # Legacy token_count also repeats the previous response on
            # rate-limit/context updates; it has no fresh response identity.
            return []
        if self._pending is not None:
            previous, previous_id, previous_ts = self._pending
            if previous_id is None and previous == usage:
                # Upgrade a legacy mirror to its canonical modern identity.
                self._pending = (usage, identifier, previous_ts)
                return []
            # Distinct usage without an intervening input is ambiguous.
            # Retain counts with missing timing rather than combine spans.
            self.missing_boundary()
            events = self._flush()
            self.missing_boundary()
        else:
            events = []
        self._pending = (usage, identifier, ts)
        return events
