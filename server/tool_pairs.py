"""Exact runtime pairing and ordered native transcript consolidation."""

from __future__ import annotations

from typing import Any


def pair_tool_records(rows: list[dict[str, Any]], *, native: bool = False) -> dict[int, int]:
    pending: dict[str, int] = {}
    pairs: dict[int, int] = {}
    for index, row in enumerate(rows):
        identity = row.get("runtime_metadata") or {}
        call_id = identity.get("tool_call_id")
        key = f"canonical:{call_id}" if call_id else None
        if key is None and native and row.get("tool_use_id"):
            key = f"native:{row['tool_use_id']}"
        if native and row.get("role") == "user" and row.get("type") == "text":
            pending.clear()
        if not key:
            continue
        if row.get("type") == "tool_use":
            pending[key] = index
        elif row.get("type") == "tool_result":
            start = pending.pop(key, None)
            if start is not None:
                pairs[start] = index
    return pairs
