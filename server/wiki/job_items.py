"""The versioned JSON boundary for job items and their original executions.

Items stay in the existing jobs column. Original execution references retain
their identity across retries; retired item encodings are rejected.
"""

import copy
import json
from typing import Any, Literal, TypedDict

ItemState = Literal["pending", "verifying", "unknown", "done", "attention", "failed", "excluded"]
ExecutionPhase = Literal["prepared", "accepted", "rejected", "unknown", "settled"]


class ExecutionRef(TypedDict, total=False):
    attempt: int
    session_id: str
    request_id: str
    work_id: str
    phase: ExecutionPhase
    recording_state: str
    disposition: str
    terminal: dict[str, Any]


class JobItem(TypedDict, total=False):
    version: int
    status: ItemState
    doc: str
    path: str
    revision: str
    worker_request: ExecutionRef
    previous_worker_requests: list[ExecutionRef]


RETIRED_FIELDS = ("legacy_evidence", "recording_failure", "recording_recovery", "execution_unknown",
                  "index_recovery", "cancelled_before_admission")
ITEM_STATES = {"pending", "verifying", "unknown", "done", "attention", "failed", "excluded"}
EXECUTION_PHASES = {"prepared", "accepted", "rejected", "unknown", "settled"}


def normalize(value):
    from .store import WikiError

    if not isinstance(value, dict) or value.get("doc") is not None and not isinstance(value["doc"], str):
        raise WikiError("Wiki job membership is malformed; durable evidence retained", 503)
    item = copy.deepcopy(value)
    if type(item.get("version")) is not int or item["version"] != 1:
        raise WikiError("Wiki job item version is unsupported; durable evidence retained", 503)
    if (item.get("status") not in ITEM_STATES
            or any(key in item for key in RETIRED_FIELDS)
            or not isinstance(item.get("previous_worker_requests", []), list)
            or any(not isinstance(ref, dict) for ref in item.get("previous_worker_requests", []))
            or "worker_request" in item and not isinstance(item["worker_request"], dict)):
        raise WikiError("Wiki job membership is malformed; durable evidence retained", 503)
    for reference in [*item.get("previous_worker_requests", []), *([item["worker_request"]] if "worker_request" in item else [])]:
        if reference.get("phase") not in EXECUTION_PHASES:
            raise WikiError("Wiki job execution is malformed; durable evidence retained", 503)
    return item


def parse(raw):
    """Strict JSON array syntax, shared with the finite storage preparation."""
    from .store import WikiError

    def unique_object(pairs):
        value = dict(pairs)
        if len(value) != len(pairs):
            raise ValueError("duplicate job item key")
        return value

    def invalid_constant(value):
        raise ValueError("nonstandard JSON constant")

    try:
        items = json.loads(raw, object_pairs_hook=unique_object, parse_constant=invalid_constant) if isinstance(raw, str) else raw
        if not isinstance(items, list):
            raise ValueError("invalid membership")
        return items
    except (ValueError, TypeError, RecursionError) as error:
        raise WikiError("Wiki job membership is malformed; durable evidence retained", 503) from error


def decode(raw):
    from .store import WikiError

    try:
        return [normalize(item) for item in parse(raw)]
    except (ValueError, TypeError, RecursionError) as error:
        raise WikiError("Wiki job membership is malformed; durable evidence retained", 503) from error


def encode(items):
    """Stamp framework-created items; persisted input must use the strict reader."""
    from .store import WikiError

    if not isinstance(items, list):
        raise WikiError("Wiki job membership is malformed; durable evidence retained", 503)
    current = [item | {"version": item.get("version", 1)} if isinstance(item, dict) else item for item in items]
    return json.dumps(decode(current), ensure_ascii=False, allow_nan=False)


def unresolved(item):
    reference = item.get("worker_request")
    return isinstance(reference, dict) and reference.get("phase") not in {"settled", "rejected"}


def references(items):
    """All batches, including retained original attempts, keep their own scope."""
    result = {}
    for item in items:
        for reference in [*item.get("previous_worker_requests", ()), item.get("worker_request")]:
            if isinstance(reference, dict):
                key = tuple(reference.get(field) for field in ("attempt", "session_id", "request_id"))
                result[key] = reference
    return list(result.values())


def sql_state(value="value"):
    return f"json_extract({value},'$.status')"


def sql_pending_request(value="value"):
    return f"""(CASE WHEN json_extract({value},'$.worker_request.phase') NOT IN ('settled','rejected')
        THEN json_extract({value},'$.worker_request.request_id') END)"""
