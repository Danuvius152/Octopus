"""Affirmative native storage evidence, independent of business results."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from .codex_items import NATIVE_LINE_BYTES


def _records(path: Path) -> Iterator[dict[str, Any]]:
    with path.open("rb") as handle:
        while raw := handle.readline(NATIVE_LINE_BYTES + 1):
            if len(raw) > NATIVE_LINE_BYTES or not raw.endswith(b"\n"):
                raise ValueError("Incomplete native input history")
            yield json.loads(raw)


def codex_input_installed(path: str, thread_id: str, turn_id: str, text: str) -> bool:
    try:
        records = _records(Path(path))
        first = next(records)
        if first.get("type") != "session_meta" or first.get("payload", {}).get("id") != thread_id:
            return False
        installed = False
        for row in records:
            payload = row.get("payload", {})
            if row.get("type") == "compacted" or (
                    row.get("type") == "event_msg" and payload.get("type") == "thread_rolled_back"):
                installed = False
            elif row.get("type") == "response_item" and payload.get("type") == "message":
                metadata = payload.get("internal_chat_message_metadata_passthrough") or {}
                if (payload.get("role") == "user" and metadata.get("turn_id") == turn_id
                        and payload.get("content") == [{"type": "input_text", "text": text}]):
                    installed = True
        return installed
    except (OSError, ValueError, StopIteration, TypeError, AttributeError):
        return False


def claude_input_installed(path: Path, session_id: str, input_id: str, text: str) -> bool:
    try:
        return any(row.get("type") == "user" and row.get("uuid") == input_id
            and row.get("sessionId") == session_id and row.get("isSidechain") is False
            and row.get("message") == {"role": "user", "content": text}
            for row in _records(path))
    except (OSError, ValueError, TypeError, AttributeError):
        return False
