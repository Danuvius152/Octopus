"""Resolve product references within one owned, read-only transcript snapshot.

There is no native history client here. Optional uncollected history and
unbound spawn observations are unavailable; malformed saved records fail the
query rather than pretending that necessary output was never collected.
"""

from __future__ import annotations

import json
from typing import Any

from ..models import MessageContent, NativeDetailPage, RuntimeMetadata, SubagentRun
from .projection import RecordProjection
from .transaction import Transaction

_MESSAGE = RecordProjection(
    "seq", "role", "type", "content", "tool_name", "tool_input", "tool_use_id",
    "runtime_metadata", "is_error", ("session_id", "session_id_ref"), "cost", "attachments", "created_at",
)
_SCOPE = ("request_id", "attempt_id", "native.root_thread_id", "native.root_turn_id")
_TARGET = (*_SCOPE, "native.thread_id", "native.turn_id")


class NativeDetailError(ValueError):
    pass


def _message(row: Any) -> MessageContent:
    values = _MESSAGE.read(row)
    for key in ("content", "tool_input", "runtime_metadata", "attachments"):
        values[key] = json.loads(values[key]) if values[key] is not None else ([] if key == "attachments" else None)
    return MessageContent.model_validate(values)


def _scope(identity: RuntimeMetadata) -> tuple[str, ...]:
    assert identity.native is not None
    return identity.request_id, identity.attempt_id, identity.native.root_thread_id, identity.native.root_turn_id


def _where(paths: tuple[str, ...]) -> str:
    return " AND ".join(f"json_extract(runtime_metadata,'$.{path}')=?" for path in paths)


async def read_native_details(
    tx: Transaction, session_id: str, user_id: str | None, anchor_seq: int,
    *, after_seq: int | None, limit: int,
) -> NativeDetailPage:
    if await tx.fetch_one("SELECT id FROM sessions WHERE id=? AND user_id IS ?", (session_id, user_id)) is None:
        raise NativeDetailError("not_found")
    if await tx.fetch_one("SELECT seq FROM messages WHERE session_id=? GROUP BY seq HAVING COUNT(*)>1 LIMIT 1",
                          (session_id,)) is not None:
        raise NativeDetailError("duplicate_message_sequences")
    row = await tx.fetch_one(f"SELECT {_MESSAGE} FROM messages WHERE session_id=? AND seq=?", (session_id, anchor_seq))
    if row is None:
        raise NativeDetailError("not_found")
    anchor = _message(row)
    identity = anchor.runtime_metadata
    if identity is None or identity.native is None:
        raise NativeDetailError("not_found")
    source = identity.native
    scope = _scope(identity)
    if not all(scope):
        raise NativeDetailError("invalid_native_identity")
    card = anchor.tool_input or {}
    if anchor.type == "native_subagent":
        thread = card.get("native_thread_id")
        card_id = card.get("card_id")
        if not isinstance(thread, str) or not thread or not isinstance(card_id, str) or not card_id:
            raise NativeDetailError("not_found")
        cursor = await tx.execute(
            f"SELECT {_MESSAGE} FROM messages WHERE session_id=? AND type='native_subagent' AND {_where(_SCOPE)} "
            "AND json_extract(tool_input,'$.card_id')=? ORDER BY seq", (session_id, *scope, card_id),
        )
        observations = [_message(record) for record in await cursor.fetchall()]
    else:
        if anchor.type not in {"text", "thinking", "tool_use", "tool_result"}:
            raise NativeDetailError("not_found")
        thread = source.thread_id
        if not thread or not source.turn_id:
            raise NativeDetailError("invalid_native_identity")
        cursor = await tx.execute(
            f"SELECT {_MESSAGE} FROM messages WHERE session_id=? AND type='native_subagent' AND {_where(_SCOPE)} "
            "AND json_extract(tool_input,'$.native_thread_id')=? AND json_extract(tool_input,'$.native_turn_id')=? ORDER BY seq",
            (session_id, *scope, thread, source.turn_id),
        )
        observations = [_message(record) for record in await cursor.fetchall()]
    if thread == source.root_thread_id:
        raise NativeDetailError("not_found")
    child_data: dict[str, Any] = dict(task_id=thread, native_thread_id=thread, status="unknown", runtime_metadata=identity)
    turns = set()
    if anchor.type != "native_subagent":
        turns.add(source.turn_id)
    for observation in observations:
        patch = observation.tool_input or {}
        if patch.get("native_thread_id") != thread:
            raise NativeDetailError("conflicting_native_identity")
        if patch.get("native_turn_id"):
            native = observation.runtime_metadata.native if observation.runtime_metadata else None
            if native is None or (native.thread_id, native.turn_id) != (thread, patch["native_turn_id"]):
                raise NativeDetailError("conflicting_native_identity")
            turns.add(patch["native_turn_id"])
        child_data.update({key: value for key, value in patch.items() if value is not None and value != ""})
        child_data["runtime_metadata"] = observation.runtime_metadata
    if len(turns) > 1:
        raise NativeDetailError("conflicting_native_identity")
    child = SubagentRun.model_validate(child_data)
    turn = next(iter(turns), None)
    if not turn:
        if after_seq is not None:
            raise NativeDetailError("invalid_detail_cursor")
        return NativeDetailPage(anchor_seq=anchor_seq, child=child, availability="not_collected")
    child.native_turn_id = turn
    target = (session_id, *scope, thread, turn)
    condition = f"session_id=? AND {_where(_TARGET)} AND type!='native_subagent'"
    if after_seq is not None:
        if await tx.fetch_one(f"SELECT seq FROM messages WHERE {condition} AND seq=?", (*target, after_seq)) is None:
            raise NativeDetailError("invalid_detail_cursor")
    available = await tx.fetch_one(f"SELECT seq FROM messages WHERE {condition} LIMIT 1", target) is not None
    cursor = await tx.execute(
        f"SELECT {_MESSAGE} FROM messages WHERE {condition} AND seq>? ORDER BY seq LIMIT ?",
        (*target, -1 if after_seq is None else after_seq, limit + 1),
    )
    rows = await cursor.fetchall()
    messages = [_message(record) for record in rows[:limit]]
    return NativeDetailPage(anchor_seq=anchor_seq, child=child, availability="available" if available else "not_collected",
        messages=messages, next_after_seq=messages[-1].seq if messages else None, has_more=len(rows) > limit)
