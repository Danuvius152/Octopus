"""The versioned JSON boundary for job items and their original executions.

Items stay in the existing jobs column. Unknown historical fields and replaced
flags remain evidence, never a second authority for dispatch or settlement.
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
    legacy_evidence: dict[str, Any]


LEGACY_FIELDS = ("recording_failure", "recording_recovery", "execution_unknown",
                 "index_recovery", "cancelled_before_admission")
ITEM_STATES = {"pending", "verifying", "unknown", "done", "attention", "failed", "excluded"}
EXECUTION_PHASES = {"prepared", "accepted", "rejected", "unknown", "settled"}


def normalize(value):
    from .store import WikiError

    if not isinstance(value, dict) or value.get("doc") is not None and not isinstance(value["doc"], str):
        raise WikiError("Wiki job membership is malformed; durable evidence retained", 503)
    item = copy.deepcopy(value)
    if item.get("version", 1) != 1:
        raise WikiError("Wiki job item version is unsupported; durable evidence retained", 503)
    if "version" not in item and item.get("status") not in ITEM_STATES:
        item["legacy_evidence"] = item.get("legacy_evidence", {}) | {"status": item.get("status")}
        item["status"] = "unknown"
    if (item.get("status") not in ITEM_STATES
            or not isinstance(item.get("legacy_evidence", {}), dict)
            or not isinstance(item.get("previous_worker_requests", []), list)
            or any(not isinstance(ref, dict) for ref in item.get("previous_worker_requests", []))
            or "worker_request" in item and not isinstance(item["worker_request"], dict)):
        raise WikiError("Wiki job membership is malformed; durable evidence retained", 503)
    legacy = {key: item.pop(key) for key in LEGACY_FIELDS if key in item}
    if legacy:
        item["legacy_evidence"] = item.get("legacy_evidence", {}) | legacy
        failure = legacy.get("recording_failure")
        if isinstance(failure, dict) and failure:
            prior = item.get("worker_request", {})
            old = failure | {"phase": "unknown"}
            if prior and any(prior.get(key) != failure.get(key) for key in ("attempt", "session_id", "request_id")):
                item.setdefault("previous_worker_requests", []).append(old)
            else:
                item["worker_request"] = prior | old
        if legacy.get("recording_failure"):
            item["status"] = "attention"
        if legacy.get("execution_unknown"):
            item["status"] = "unknown"
        elif legacy.get("recording_recovery") or legacy.get("index_recovery"):
            item["status"] = "verifying"
        if failure and not isinstance(failure, dict):
            item["status"] = "unknown"
        if legacy.get("cancelled_before_admission") and not item.get("worker_request"):
            item["worker_request"] = legacy["cancelled_before_admission"] | {
                "phase": "rejected", "disposition": "not_admitted",
            }
    for reference in [*item.get("previous_worker_requests", []), *([item["worker_request"]] if "worker_request" in item else [])]:
        reference.setdefault("phase", "unknown")
        if reference["phase"] not in EXECUTION_PHASES:
            raise WikiError("Wiki job execution is malformed; durable evidence retained", 503)
    item["version"] = 1
    return item


def decode(raw):
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
        return [normalize(item) for item in items]
    except (ValueError, TypeError, RecursionError) as error:
        raise WikiError("Wiki job membership is malformed; durable evidence retained", 503) from error


def encode(items):
    return json.dumps(decode(items), ensure_ascii=False, allow_nan=False)


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


def _sql_truth(value, path):
    """Match Python truth for legacy JSON flags, including strings/containers."""
    kind, content = f"json_type({value},'{path}')", f"json_extract({value},'{path}')"
    return f"""(CASE {kind} WHEN 'true' THEN 1 WHEN 'integer' THEN {content}<>0
        WHEN 'real' THEN {content}<>0 WHEN 'text' THEN length({content})>0
        WHEN 'array' THEN json_array_length({content})>0
        WHEN 'object' THEN json({content})<>'{{}}' ELSE 0 END)"""


def sql_state(value="value"):
    """Notification projections read old rows without a normalization write."""
    failure = _sql_truth(value, "$.recording_failure")
    return f"""(CASE
        WHEN {failure} AND json_type({value},'$.recording_failure')<>'object' THEN 'unknown'
        WHEN {_sql_truth(value, '$.execution_unknown')} THEN 'unknown'
        WHEN {_sql_truth(value, '$.recording_recovery')} OR {_sql_truth(value, '$.index_recovery')} THEN 'verifying'
        WHEN {failure} THEN 'attention'
        WHEN json_type({value},'$.version') IS NULL
          AND coalesce(json_extract({value},'$.status'),'') NOT IN ({','.join(repr(state) for state in sorted(ITEM_STATES))})
          THEN 'unknown'
        ELSE json_extract({value},'$.status') END)"""


def sql_pending_request(value="value"):
    matches = " AND ".join(f"json_extract({value},'$.worker_request.{key}') IS json_extract({value},'$.recording_failure.{key}')"
                           for key in ("attempt", "session_id", "request_id"))
    return f"""(CASE WHEN json_type({value},'$.recording_failure')='object'
        AND {_sql_truth(value, '$.recording_failure')}
        AND (json_type({value},'$.worker_request') IS NULL
             OR json(json_extract({value},'$.worker_request'))='{{}}' OR ({matches}))
        THEN json_extract({value},'$.recording_failure.request_id')
        WHEN coalesce(json_extract({value},'$.worker_request.phase'),'unknown') NOT IN ('settled','rejected')
        THEN json_extract({value},'$.worker_request.request_id') END)"""
