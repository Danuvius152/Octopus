"""Durable browser inputs; startup classifies them without replaying work."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from itertools import groupby
from typing import TYPE_CHECKING, Any, Literal

from ..session_operations import (
    SavedHandoffSelection,
    TicketPhase,
    decode_payload,
    handoff_human_scope,
)
from ..submission_models import InputRecoveryUnavailable, NativeInputEvidence
from .applications import require_application_writable
from .base import DatabaseBase
from .schedules import (
    OneShotFire,
    ScheduleChangedError,
    ScheduleOriginChanged,
    schedule_delivery_id,
    stored_run_at,
)
from .transaction import Transaction
from .users import owner_is_available

if TYPE_CHECKING:
    from ..knowledge import AdmittedRequest

_COLUMNS = ("session_id", "client_id", "payload_sha256", "request_id", "receipt", "payload", "execution_state",
            "execution_owner", "execution_generation", "reason_code", "state_version", "created_at",
            "updated_at", "recovery_of", "dismissed", "execution_boot", "process_group", "delivery_state", "native_inputs")

_SELECT = (
    f"SELECT {','.join('i.' + column for column in _COLUMNS)},successor.client_id,m.content "
    "FROM client_submissions i LEFT JOIN messages m ON m.session_id=i.session_id "
    "AND m.terminal_request_id=COALESCE(i.execution_owner,i.request_id) "
    "AND (i.execution_owner IS NULL OR i.execution_owner=i.request_id OR i.delivery_state<>'none') "
    "LEFT JOIN client_submissions successor ON successor.session_id=i.session_id AND successor.recovery_of=i.client_id "
)

_SOURCE_INPUT = ("i.client_id=? AND json_extract(i.payload,'$.source')=? "
                 "AND json_extract(i.payload,'$.delivery_id')=?")
_ONE_SHOT_FIELDS = ("agent_id", "name", "prompt", "recurrence_label",
                    "run_at", "knowledge_context", "enabled")

# Only unresolved steer acceptance needs a notice. Accepted evidence stays on
# the original input without requiring the user to dismiss a success notice.
_NATIVE_STEER_NOTICE = (
    "(json_extract(i.receipt,'$.disposition')='steered' AND EXISTS(SELECT 1 FROM json_each(i.native_inputs) "
    "WHERE json_extract(value,'$.kind')='steer' AND json_extract(value,'$.state') IN ('not_sent','unknown'))"
    ")"
)

# Legacy message submissions stored the body without an operation object.
HUMAN_MESSAGE = ("json_extract(i.payload,'$.source')='human' AND "
                 "(json_type(i.payload,'$.operation') IS NULL OR json_extract(i.payload,'$.operation.kind')='message')")
_ADOPTED_BATCH = ("COALESCE(json_extract(i.payload,'$.ticket_batch_update_for'),"
                  "json_extract(i.payload,'$.finish_ticket_batch_for'))")
INDEPENDENT_HUMAN_INPUT = (f"{HUMAN_MESSAGE} AND json_extract(i.receipt,'$.disposition') IS NOT 'steered' "
                           f"AND {_ADOPTED_BATCH} IS NULL")
_BATCH_NOT_REPLACED = (
    "NOT EXISTS (SELECT 1 FROM client_submissions i WHERE i.session_id=client_submissions.session_id "
    f"AND i.rowid>client_submissions.rowid AND {INDEPENDENT_HUMAN_INPUT})"
)
_QUEUED_BATCH_CANDIDATE_FOR = (
    "CASE WHEN i.execution_state='pending' AND json_extract(i.payload,'$.ticket_batch') IS NULL "
    "THEN json_extract(i.payload,'$.ticket_batch_candidate_for') END"
)
# Future candidates are classified in their own turn. They still block
# ordinary handoff, result admission, and close until their scope is known.
_BATCH_MAY_AMEND = (
    "NOT EXISTS (SELECT 1 FROM client_submissions i WHERE i.session_id=client_submissions.session_id "
    f"AND i.rowid>client_submissions.rowid AND {INDEPENDENT_HUMAN_INPUT} "
    f"AND {_QUEUED_BATCH_CANDIDATE_FOR} IS NOT client_submissions.request_id)"
)

_EXECUTION_INPUT_COLUMNS = "i.request_id,i.payload,i.receipt,i.delivery_state,m.seq,m.content,m.tool_input,i.native_inputs"
_EXECUTION_INPUT_MESSAGE = """
    LEFT JOIN messages m ON m.session_id=i.session_id AND m.role='user' AND m.type='text'
      AND CASE WHEN json_valid(m.tool_input) THEN json_extract(m.tool_input,'$.request_context.request_id')=i.request_id ELSE 0 END
"""

# Limit proof ends at the selected instruction. A later uncertain input can
# pause a return without invalidating an already committed acceptance limit.
TICKET_ACCEPTANCE_EVIDENCE = f"""
    WITH acceptance AS (
        SELECT session_id,request_id AS batch_id,NULL AS adopted_id,
               json_extract(payload,'$.ticket_batch_limit_request_id') AS instruction_id, 0 AS is_update
          FROM client_submissions
         WHERE (?1 IS NULL OR session_id=?1) AND (?2 IS NULL OR request_id=?2)
           AND (json_extract(payload,'$.ticket_batch')='finishing' OR json_type(payload,'$.ticket_batch_limit_request_id') IS NOT NULL)
        UNION ALL
        SELECT session_id,json_extract(payload,'$.finish_ticket_batch_for'),request_id,
               COALESCE(json_extract(payload,'$.finish_ticket_batch_instruction_id'),request_id), 0
          FROM client_submissions
         WHERE (?1 IS NULL OR session_id=?1) AND (?2 IS NULL OR json_extract(payload,'$.finish_ticket_batch_for')=?2)
           AND (json_type(payload,'$.finish_ticket_batch_for') IS NOT NULL
             OR json_type(payload,'$.finish_ticket_batch_instruction_id') IS NOT NULL)
        UNION ALL
        SELECT session_id,json_extract(payload,'$.ticket_batch_update_for'),request_id,request_id,1
          FROM client_submissions
         WHERE (?1 IS NULL OR session_id=?1) AND (?2 IS NULL OR json_extract(payload,'$.ticket_batch_update_for')=?2)
           AND json_type(payload,'$.ticket_batch_update_for') IS NOT NULL
    )
    SELECT a.session_id,s.user_id,a.batch_id,a.adopted_id,b.payload,b.receipt,b.execution_owner,
           instruction.request_id,c.request_id,c.payload,c.delivery_state,a.is_update,{_EXECUTION_INPUT_COLUMNS}
      FROM acceptance a JOIN sessions s ON s.id=a.session_id
      LEFT JOIN client_submissions b ON b.session_id=a.session_id AND b.request_id=a.batch_id
      LEFT JOIN client_submissions latest ON latest.session_id=b.session_id
        AND latest.request_id=json_extract(b.payload,'$.ticket_batch_limit_request_id')
      LEFT JOIN client_submissions instruction ON instruction.session_id=b.session_id
        AND instruction.request_id=a.instruction_id AND (a.is_update=1 OR instruction.rowid<=latest.rowid)
      LEFT JOIN client_submissions c ON c.session_id=instruction.session_id AND c.request_id=instruction.execution_owner
        AND c.execution_owner=c.request_id AND c.rowid BETWEEN b.rowid AND instruction.rowid
        AND (a.adopted_id IS NULL OR c.request_id=a.adopted_id)
      LEFT JOIN client_submissions i ON i.session_id=c.session_id AND i.execution_owner=c.request_id AND i.rowid<=instruction.rowid
      {_EXECUTION_INPUT_MESSAGE}
     ORDER BY a.session_id,a.batch_id,a.adopted_id,m.seq,i.rowid
"""


def _submission(row) -> dict[str, Any]:
    result = dict(zip((*_COLUMNS, "recovered_by", "terminal"), row, strict=True))
    for key in ("payload", "receipt", "terminal", "native_inputs"):
        result[key] = json.loads(result[key]) if result[key] is not None else None
    if result["payload"] is not None:
        result["payload"] = decode_payload(result["payload"])
    result["native_inputs"] = [NativeInputEvidence.model_validate(item).model_dump() for item in result["native_inputs"]]
    return result


def _input_with_message(row) -> dict[str, Any]:
    return {
        "request_id": row[0], "payload": decode_payload(json.loads(row[1])) if row[1] is not None else None,
        "receipt": json.loads(row[2]) if row[2] is not None else None, "delivery_state": row[3],
        "seq": row[4], "content": json.loads(row[5]) if row[5] is not None else None,
        "tool_input": json.loads(row[6]) if row[6] is not None else None,
        "native_inputs": [NativeInputEvidence.model_validate(item).model_dump() for item in json.loads(row[7])],
    }


def _execution_snapshot(original: dict[str, Any], owner: str | None, rows) -> dict[str, Any]:
    inputs = [_input_with_message(row) for row in rows]
    request_id = original["request_id"]
    primary = [item for item in inputs if item["request_id"] == request_id]
    return {"original": original, "owner": owner, "primary": primary[0] if len(primary) == 1 else None,
            "inputs": [item for item in inputs if item["request_id"] != request_id]}


def verify_ticket_acceptance_evidence(rows) -> None:
    """Check the carrier and confirmed message for each batch amendment."""
    from ..sessions.client_submissions import (
        confirmed_execution_from_inputs,
        saved_context,
    )

    try:
        for (session_id, root_id, adopted_id), members in groupby(rows, key=lambda row: (row[0], row[2], row[3])):
            group = list(members)
            (_, user_id, _, _, raw_root, raw_receipt, execution_owner, instruction_id,
             carrier_id, raw_carrier, delivery, is_update) = group[0][:12]
            root, receipt = decode_payload(json.loads(raw_root)), json.loads(raw_receipt)
            origin = saved_context(session_id, {"request_id": root_id, "payload": root})
            states = {"registered", "finishing", "stopped"} if is_update else {"finishing", "stopped"}
            if (root.get("source") != "human" or root["operation"]["kind"] != "message"
                    or root.get("ticket_batch") not in states or execution_owner != root_id
                    or receipt.get("accepted") is not True or receipt.get("disposition") != "accepted"
                    or origin is None or origin.origin.user_id != user_id):
                raise ValueError("Invalid original ticket batch")
            if carrier_id is None:
                raise ValueError("Missing carrier for ticket acceptance")
            original = {"request_id": carrier_id, "execution_owner": carrier_id, "delivery_state": delivery,
                        "payload": decode_payload(json.loads(raw_carrier))}
            snapshot = _execution_snapshot(original, user_id, (row[12:] for row in group))
            execution = confirmed_execution_from_inputs(session_id, snapshot, require_terminal=False)
            if execution is None:
                raise ValueError("Unconfirmed ticket acceptance input")
            if execution.original.origin != origin.origin and (execution.primary is None or execution.adopted_for != root_id):
                raise ValueError("The acceptance carrier belongs to a different request")
            if adopted_id is not None and (adopted_id == root_id or execution.adopted_for != root_id
                                          or execution.primary is None):
                raise ValueError("The adopted input has no distinct human carrier")
            if instruction_id == carrier_id:
                matched = execution.primary is not None and (is_update or not execution.progress_only)
            else:
                matched = any(edit.reference.request_id == instruction_id for _, edit in execution.steers)
            if not matched:
                raise ValueError("Unconfirmed ticket acceptance instruction")
    except (AttributeError, KeyError, ValueError, TypeError):
        raise RuntimeError("Primary database contains an invalid ticket acceptance limit") from None


async def _withdraw_unsent_handoff(tx: Transaction, session_id: str, request_id: str, reason: str) -> str | None:
    """Seal the first writer and restore the previous history in one commit."""
    row = await tx.fetch_one(
        "SELECT json_extract(i.payload,'$.previous_native_id'),s.claude_session_id,i.native_inputs "
        "FROM client_submissions i JOIN sessions s ON s.id=i.session_id WHERE i.session_id=? AND i.request_id=? "
        "AND json_extract(i.payload,'$.source')='handoff' AND json_extract(i.payload,'$.handoff_backend')=s.backend "
        "AND json_extract(i.payload,'$.fresh_native_id') IS NULL AND s.fork_needs_replay=0 "
        "AND i.delivery_state='none' AND i.execution_state IN ('pending','running') "
        "AND (i.execution_state='pending' OR s.backend='claude-code' OR json_array_length(i.native_inputs)>0) "
        "AND NOT EXISTS(SELECT 1 FROM json_each(i.native_inputs) "
        "WHERE json_extract(value,'$.state') NOT IN ('not_sent','rejected'))",
        (session_id, request_id),
    )
    if row is None:
        return None
    previous, current, raw_evidence = row
    if not isinstance(previous, str) or not previous:
        return None
    message = await tx.fetch_one(
        "SELECT seq FROM messages WHERE session_id=? AND role='user' AND type='text' "
        "AND CASE WHEN json_valid(tool_input) THEN json_extract(tool_input,'$.request_context.source')='handoff' "
        "AND json_extract(tool_input,'$.request_context.request_id')=? ELSE 0 END",
        (session_id, request_id),
    )
    if current != previous:
        if message is None or (current is not None and not any(
                item.get("thread_id") == current for item in json.loads(raw_evidence))):
            return None
        await tx.execute("UPDATE sessions SET claude_session_id=?,context_state=NULL WHERE id=?", (previous, session_id))
    if message is not None:
        await tx.execute(
            "UPDATE messages SET tool_input=json_set(tool_input,'$.handoff_withdrawn',json('true')) "
            "WHERE session_id=? AND seq=?", (session_id, message[0]),
        )
    await tx.execute(
        "UPDATE client_submissions SET execution_state='withdrawn',reason_code=?,"
        "state_version=state_version+1,updated_at=? WHERE session_id=? AND request_id=?",
        (reason, datetime.now(UTC).isoformat(), session_id, request_id),
    )
    return previous


async def _ticket_batch_stage(tx: Transaction, session_id: str, origin_id: str,
                              opening_request_id: str | None) -> tuple[str, TicketPhase] | None:
    from ..sessions.client_submissions import saved_context

    rows = await tx.fetch_all(
        "SELECT i.request_id,i.payload,i.delivery_state,i.execution_state FROM client_submissions i "
        "WHERE i.session_id=:session AND json_extract(i.payload,'$.source')='handoff' "
        "AND i.rowid>(SELECT b.rowid FROM client_submissions b WHERE b.session_id=:session AND b.request_id=:batch) "
        "AND (:opening IS NULL OR i.request_id=:opening) ORDER BY i.rowid DESC",
        {"session": session_id, "batch": origin_id, "opening": opening_request_id},
    )
    for request_id, raw, delivery, execution in rows:
        payload = decode_payload(json.loads(raw))
        if not payload.get("fresh_native_id") and delivery != "unknown" and execution != "unknown":
            continue
        context = saved_context(session_id, {"request_id": request_id, "payload": payload})
        if context is None:
            return None
        if context.origin.session_id == session_id and context.origin.request_id == origin_id:
            if not payload.get("fresh_native_id"):
                return None
            return request_id, "final_acceptance" if payload.get("handoff_phase") == "final_acceptance" else "ticket"
    if opening_request_id is not None:
        return None
    return origin_id, "ticket"


async def ticket_review_stage(tx: Transaction, caller: AdmittedRequest) -> str | None:
    """Resolve the ticket from the captured caller and committed Owner facts."""
    active, origin = caller.reference, caller.context.origin
    if (active.session_id, active.user_id) != (origin.session_id, origin.user_id):
        return None
    original = await tx.fetch_one(
        "SELECT 1 FROM client_submissions WHERE session_id=? AND request_id=? "
        "AND json_extract(payload,'$.ticket_batch') IN ('registered','finishing') "
        f"AND {_BATCH_NOT_REPLACED}", (origin.session_id, origin.request_id),
    )
    if original is None:
        return None
    request = await tx.fetch_one(
        "SELECT json_extract(payload,'$.source') FROM client_submissions WHERE session_id=? AND request_id=? "
        "AND execution_state='running' AND execution_owner=request_id",
        (active.session_id, active.request_id),
    )
    if request is None:
        return None
    stage = await _ticket_batch_stage(tx, origin.session_id, origin.request_id,
                                     active.request_id if request[0] == "handoff" else None)
    return stage[0] if stage is not None and stage[1] == "ticket" else None


class ClientSubmissionsMixin(DatabaseBase):
    async def verify_ticket_acceptance_limits(self, session_id: str | None = None) -> None:
        async with self.transaction(write=False) as tx:
            verify_ticket_acceptance_evidence(await tx.fetch_all(TICKET_ACCEPTANCE_EVIDENCE, (session_id, None)))

    async def register_ticket_batch(self, session_id: str, origin_id: str) -> bool:
        async with self.transaction(write=True) as tx:
            updated = await tx.execute(
                "UPDATE client_submissions AS i SET payload=json_set(payload,'$.ticket_batch','registered') "
                f"WHERE session_id=? AND request_id=? AND {INDEPENDENT_HUMAN_INPUT} "
                "AND COALESCE(json_extract(payload,'$.ticket_batch'),'registered')='registered'",
                (session_id, origin_id),
            )
            return updated.rowcount == 1

    async def ticket_batch_state(self, session_id: str, origin_id: str) -> str | None:
        async with self.transaction(write=False) as tx:
            row = await tx.fetch_one(
                "SELECT json_extract(payload,'$.ticket_batch') FROM client_submissions i "
                f"WHERE session_id=? AND request_id=? AND {INDEPENDENT_HUMAN_INPUT}",
                (session_id, origin_id),
            )
            return row[0] if row else None

    async def ticket_batch_stage(self, session_id: str, origin_id: str, *,
                                 opening_request_id: str | None = None) -> tuple[str, TicketPhase] | None:
        """Follow the accepted boundary even if its native history had to be replayed."""
        async with self.transaction(write=False) as tx:
            return await _ticket_batch_stage(tx, session_id, origin_id, opening_request_id)

    async def ticket_review_stage(self, caller: AdmittedRequest) -> str | None:
        async with self.transaction(write=False) as tx:
            return await ticket_review_stage(tx, caller)

    async def finish_ticket_batch(self, session_id: str, origin_id: str, state: Literal["registered", "finishing"]) -> bool:
        """Close a batch from a verified stage, after any human supplement settles."""
        async with self.transaction(write=True) as tx:
            updated = await tx.execute(
                "UPDATE client_submissions SET payload=json_set(payload,'$.ticket_batch','stopped') "
                "WHERE session_id=? AND request_id=? AND json_extract(payload,'$.ticket_batch')=? "
                f"AND {_BATCH_NOT_REPLACED}", (session_id, origin_id, state),
            )
            return updated.rowcount == 1

    async def stop_ticket_batch(self, session_id: str, origin_id: str) -> bool:
        """Check the original batch is still current in the same write transaction."""
        async with self.transaction(write=True) as tx:
            updated = await tx.execute(
                "UPDATE client_submissions SET payload=json_set(payload,'$.ticket_batch','stopped') "
                "WHERE session_id=? AND request_id=? AND json_extract(payload,'$.ticket_batch') IN ('finishing','registered') "
                f"AND ({_BATCH_NOT_REPLACED} OR request_id=(SELECT CASE "
                "WHEN i.execution_state IN ('pending','running') AND json_extract(i.payload,'$.ticket_batch') IS NULL "
                "THEN json_extract(i.payload,'$.ticket_batch_candidate_for') END FROM client_submissions i "
                f"WHERE i.session_id=client_submissions.session_id AND {INDEPENDENT_HUMAN_INPUT} "
                "ORDER BY i.rowid DESC LIMIT 1))",
                (session_id, origin_id),
            )
            return updated.rowcount == 1

    async def ticket_input_has_joined_batch(self, session_id: str, request_id: str) -> bool:
        """Work captured before adoption cannot return as independent work afterward."""
        async with self.transaction(write=False) as tx:
            row = await tx.fetch_one(
                "SELECT 1 FROM client_submissions i WHERE session_id=? AND request_id=? "
                f"AND {_ADOPTED_BATCH} IS NOT NULL", (session_id, request_id),
            )
            return row is not None

    async def adopt_ticket_batch_update(self, session_id: str, origin_id: str, request_id: str, *, progress_only: bool) -> str:
        patch: dict[str, str | bool] = {"ticket_batch_update_for": origin_id}
        if progress_only:
            patch["ticket_batch_progress_only"] = True
        async with self.transaction(write=True) as tx:
            adopted = await tx.execute(
                "UPDATE client_submissions SET payload=json_patch(payload,?) "
                "WHERE session_id=? AND request_id=? AND execution_state='running' AND execution_owner=request_id "
                "AND json_extract(payload,'$.ticket_batch_candidate_for')=? "
                "AND json_extract(payload,'$.ticket_batch') IS NULL "
                "AND json_extract(payload,'$.finish_ticket_batch_for') IS NULL "
                "AND (json_extract(payload,'$.ticket_batch_update_for') IS NULL "
                "OR COALESCE(json_extract(payload,'$.ticket_batch_progress_only'),0)=?)",
                (json.dumps(patch), session_id, request_id, origin_id, progress_only),
            )
            original = await tx.fetch_one(
                "SELECT json_extract(payload,'$.ticket_batch') FROM client_submissions WHERE session_id=? AND request_id=? "
                f"AND json_extract(payload,'$.ticket_batch') IN ('registered','finishing') AND {_BATCH_MAY_AMEND}",
                (session_id, origin_id),
            )
            if adopted.rowcount != 1 or original is None:
                raise ValueError("The original ticket batch cannot adopt this human update")
            verify_ticket_acceptance_evidence(await tx.fetch_all(TICKET_ACCEPTANCE_EVIDENCE, (session_id, origin_id)))
            return original[0]

    async def limit_ticket_batch(self, session_id: str, origin_id: str, carrier_id: str, instruction_id: str,
                                 *, adopt_input: bool) -> None:
        """Commit the narrower state and its original human input in one write."""
        async with self.transaction(write=True) as tx:
            if adopt_input:
                adopted = await tx.execute(
                    "UPDATE client_submissions SET payload=json_set(payload,'$.finish_ticket_batch_for',?,"
                    "'$.finish_ticket_batch_instruction_id',?) "
                    "WHERE session_id=? AND request_id=? AND execution_state='running' AND execution_owner=request_id "
                    "AND json_extract(payload,'$.ticket_batch_candidate_for')=? "
                    "AND json_extract(payload,'$.ticket_batch') IS NULL "
                    "AND (json_extract(payload,'$.finish_ticket_batch_for') IS NULL "
                    "OR json_extract(payload,'$.finish_ticket_batch_for')=?)",
                    (origin_id, instruction_id, session_id, carrier_id, origin_id, origin_id),
                )
                if adopted.rowcount != 1:
                    raise ValueError("This human input cannot narrow the original ticket batch")
            updated = await tx.execute(
                "UPDATE client_submissions SET payload=json_set(payload,'$.ticket_batch','finishing',"
                "'$.ticket_batch_limit_request_id',?) WHERE session_id=? AND request_id=? "
                "AND json_extract(payload,'$.ticket_batch') IN ('registered','finishing') "
                f"AND {_BATCH_MAY_AMEND}", (instruction_id, session_id, origin_id),
            )
            if updated.rowcount != 1:
                raise ValueError("The original ticket batch is no longer current")
            verify_ticket_acceptance_evidence(await tx.fetch_all(TICKET_ACCEPTANCE_EVIDENCE, (session_id, origin_id)))

    async def conflicting_handoff(self, session_id: str, batch_request_id: str,
                                  parent_request_id: str, native_id: str | None) -> str | None:
        """Block replay within the batch or native history after the Owner checks writer retirement."""
        async with self.transaction(write=False) as tx:
            row = await tx.fetch_one(
                """
                SELECT json_extract(i.payload,'$.delivery_id') FROM client_submissions i
                LEFT JOIN client_submissions origin ON origin.session_id=i.session_id AND origin.request_id=:batch_id
                    AND json_extract(origin.payload,'$.ticket_batch')='registered'
                WHERE i.session_id=:session_id AND json_extract(i.payload,'$.source')='handoff'
                    AND NOT (i.execution_state='withdrawn' AND i.delivery_state='none'
                        AND json_extract(i.payload,'$.fresh_native_id') IS NULL
                        AND NOT EXISTS (SELECT 1 FROM json_each(i.native_inputs)
                            WHERE json_extract(value,'$.state') IN ('unknown','accepted')))
                    AND (json_extract(i.payload,'$.handoff_from')=:parent_id
                        OR (i.execution_state IN ('pending','running') AND json_extract(i.payload,'$.fresh_native_id') IS NULL)
                        OR ((i.delivery_state='unknown' OR (i.execution_state='unknown'
                            AND json_extract(i.payload,'$.fresh_native_id') IS NULL))
                            AND NOT (origin.rowid IS NOT NULL AND i.rowid<origin.rowid AND :native_id IS NOT NULL
                                AND json_extract(i.payload,'$.previous_native_id') IS NOT :native_id
                                AND json_extract(i.payload,'$.fresh_native_id') IS NOT :native_id
                                AND NOT EXISTS (SELECT 1 FROM json_each(i.native_inputs)
                                    WHERE json_extract(value,'$.thread_id')=:native_id)
                                AND EXISTS (SELECT 1 FROM messages m WHERE m.session_id=i.session_id
                                    AND m.terminal_request_id=i.request_id)))
                        OR (json_extract(i.payload,'$.previous_native_id')=:native_id
                            AND json_extract(i.payload,'$.fresh_native_id') IS NOT NULL))
                ORDER BY i.rowid DESC LIMIT 1
                """, {"batch_id": batch_request_id, "session_id": session_id, "parent_id": parent_request_id, "native_id": native_id},
            )
            return row[0] if row else None

    async def handoff_human_executions(self, session_id: str, origin_id: str, *,
                                       before_handoff: str | None = None,
                                       amending: str | None = None) -> list[str] | None:
        """Locate confirmed human updates from the batch origin to its active or replayed boundary."""
        async with self.transaction(write=False) as tx:
            origin = await tx.fetch_one("SELECT rowid FROM client_submissions i WHERE session_id=? AND request_id=? "
                                       f"AND {INDEPENDENT_HUMAN_INPUT}", (session_id, origin_id))
            if origin is None:
                return None
            cutoff = None
            if before_handoff is not None:
                boundary = await tx.fetch_one(
                    "SELECT rowid FROM client_submissions WHERE session_id=? AND request_id=? "
                    "AND json_extract(payload,'$.fresh_native_id') IS NOT NULL",
                    (session_id, before_handoff),
                )
                if boundary is None or boundary[0] <= origin[0]:
                    return None
                cutoff = boundary[0]
            later = await tx.fetch_all(
                "SELECT request_id,json_extract(receipt,'$.disposition'),execution_owner,delivery_state,execution_state,"
                f"{_ADOPTED_BATCH},json_extract(payload,'$.ticket_batch_candidate_for') "
                f"FROM client_submissions i WHERE session_id=:session AND rowid>:origin AND {HUMAN_MESSAGE} "
                "AND (:cutoff IS NULL OR rowid<:cutoff) "
                f"AND (:amending IS NULL OR ({_QUEUED_BATCH_CANDIDATE_FOR}) IS NOT :batch) ORDER BY rowid",
                {"session": session_id, "origin": origin[0], "cutoff": cutoff, "amending": amending, "batch": origin_id})
            carriers = {origin_id: None}
            for request_id, disposition, owner, delivery, state, adopted_for, candidate_for in later:
                if adopted_for == origin_id or (request_id == amending and candidate_for == origin_id and state == "running"):
                    carriers[request_id] = None
                    continue
                if disposition != "steered":
                    return None
                if state == "withdrawn" and delivery == "none":
                    continue
                # An unaccepted steer can requeue and run as its own human input.
                if delivery != "confirmed" or not owner or owner == request_id:
                    return None
                carriers[owner] = None
            return list(carriers)

    async def saved_handoff_human_scope(self, session_id: str, origin_id: str, *, advancing: bool = False,
                                       through_handoff: str | None = None) -> dict[str, SavedHandoffSelection]:
        """Read confirmed selections and expiry evidence for the Owner to check against the source."""
        async with self.transaction(write=False) as tx:
            rows = await tx.fetch_all(
                "SELECT i.payload FROM client_submissions i "
                "WHERE i.session_id=:session AND json_extract(i.payload,'$.source')='handoff' "
                "AND i.rowid>(SELECT o.rowid FROM client_submissions o WHERE o.session_id=i.session_id AND o.request_id=:origin) "
                "AND (:through IS NULL OR i.rowid<=(SELECT b.rowid FROM client_submissions b "
                "WHERE b.session_id=i.session_id AND b.request_id=:through)) "
                "AND json_extract(i.payload,'$.fresh_native_id') IS NOT NULL ORDER BY i.rowid",
                {"session": session_id, "origin": origin_id, "through": through_handoff})
            decisions: dict[str, SavedHandoffSelection] = {}
            for index, (payload,) in enumerate(rows):
                next_expired = advancing or index < len(rows) - 1
                for request_id, clauses in handoff_human_scope(decode_payload(json.loads(payload))).items():
                    decisions[request_id] = SavedHandoffSelection(clauses, next_expired)
            return decisions

    async def record_input_history(self, session_id: str, request_id: str, attempt_id: str,
                                   generation: str, backend: str, native_id: str | None,
                                   may_have_run: bool) -> None:
        """Commit original input and replay consumption together, without an outcome."""
        from ..fork_helpers import consumed_fork_metadata
        async with self.transaction(write=True) as tx:
            row = await tx.fetch_one(
                "SELECT i.execution_generation,i.execution_owner,i.native_inputs,"
                "s.backend,s.claude_session_id,s.fork_needs_replay,s.fork_metadata,"
                "json_extract(i.payload,'$.previous_native_id'),json_extract(i.payload,'$.fresh_native_id'),i.execution_state "
                "FROM client_submissions i JOIN sessions s ON s.id=i.session_id WHERE i.session_id=? AND i.request_id=?",
                (session_id, request_id))
            if row is None or row[0] != generation or row[1] != request_id or row[3] != backend:
                raise ValueError("Input history lost its original execution binding")
            handoff = row[7] is not None
            if handoff and row[9] == "withdrawn":
                if native_id is None and not may_have_run:
                    return  # Original retirement repeats the already committed unsent evidence.
                raise ValueError("The original ticket handoff was withdrawn before sending")
            if (not row[5] and not handoff) or (handoff and row[8] is not None):
                return
            if native_id is not None:
                if handoff and native_id == row[7]:
                    raise ValueError("Ticket handoff reused its original native history")
                if not may_have_run or row[4] is not None and row[4] != native_id:
                    raise ValueError("Input history identity changed")
                entries = [NativeInputEvidence.model_validate(item) for item in json.loads(row[2])]
                if backend == "codex" and not any(item.attempt_id == attempt_id and item.thread_id == native_id
                        and item.kind == "start" and item.state == "accepted" for item in entries):
                    raise ValueError("Installed input has no matching native start")
                if handoff:
                    await tx.execute("UPDATE sessions SET claude_session_id=? WHERE id=?", (native_id, session_id))
                    await tx.execute("UPDATE client_submissions SET payload=json_set(payload,'$.fresh_native_id',?) "
                                     "WHERE session_id=? AND request_id=?", (native_id, session_id, request_id))
                else:
                    await tx.execute("UPDATE sessions SET claude_session_id=?,fork_needs_replay=0,fork_metadata=? WHERE id=?",
                        (native_id, consumed_fork_metadata(row[6]), session_id))
            delivery = "confirmed" if native_id else "unknown" if may_have_run else "none"
            await tx.execute(
                "UPDATE client_submissions SET delivery_state=?,reason_code=?,state_version=state_version+1,updated_at=? "
                "WHERE session_id=? AND request_id=?",
                (delivery, "input_history_unconfirmed" if delivery == "unknown" else None,
                 datetime.now(UTC).isoformat(), session_id, request_id))

    async def record_native_input(self, session_id: str, request_id: str, evidence: NativeInputEvidence,
                                  *, process_group: int | None = None) -> None:
        """Commit protocol evidence on its original submission before business I/O.

        A later provider terminal does not settle an unknown input. Old records
        have no native facts; local write/drain confirmation is never upgraded.
        """
        async with self.transaction(write=True) as tx:
            row = await tx.fetch_one(
                "SELECT native_inputs,execution_state,EXISTS(SELECT 1 FROM messages m "
                "WHERE m.session_id=i.session_id AND m.terminal_request_id=COALESCE(i.execution_owner,i.request_id)),payload "
                "FROM client_submissions i WHERE session_id=? AND request_id=?",
                (session_id, request_id),
            )
            if row is None:
                raise ValueError("Native input has no original submission")
            if evidence.input_id != request_id:
                raise ValueError("Native input identity does not match its submission")
            payload = decode_payload(json.loads(row[3]))
            operation = payload["operation"]
            if (operation["kind"] == "compact") != (evidence.kind == "compact"):
                raise ValueError("Native evidence kind does not match its original operation")
            entries = [NativeInputEvidence.model_validate(item) for item in json.loads(row[0])]
            previous = next((item for item in entries if item.attempt_id == evidence.attempt_id), None)
            if row[1] == "pending" and (evidence.state != "not_sent" or entries and previous is None and any(
                    item.state not in {"not_sent", "rejected"} or item.kind != "steer" for item in entries)):
                raise ValueError("A pending submission only accepts its initial unsent native input")
            if row[1] not in {"pending", "running"} and (
                    previous is None or previous.state not in {"unknown", "accepted", "rejected"}
                    or evidence.state not in {"accepted", "rejected"}):
                raise ValueError("A closed submission only accepts proof of its existing native input")
            if evidence.thread_id is not None:
                if evidence.thread_id == payload.get("previous_native_id"):
                    raise ValueError("Ticket handoff reused its original native thread")
                # Persist history association with input evidence, before a
                # business frame can outrun the Owner's session_started event.
                updated = await tx.execute(
                    "UPDATE sessions SET claude_session_id=? WHERE id=? AND backend='codex' "
                    "AND (claude_session_id IS NULL OR claude_session_id=?)",
                    (evidence.thread_id, session_id, evidence.thread_id),
                )
                if updated.rowcount != 1:
                    raise ValueError("Native history no longer belongs to this session")
            if process_group is not None:
                await tx.execute("UPDATE client_submissions SET process_group=? WHERE session_id=? AND request_id=?",
                                 (process_group, session_id, request_id))
            if previous is not None:
                if previous.acknowledged and not evidence.acknowledged:
                    raise ValueError("Native compaction acknowledgment cannot be lost")
                transitions = {"not_sent": {"not_sent", "unknown", "rejected"}, "unknown": {"unknown", "accepted", "rejected"},
                               "accepted": {"accepted"}, "rejected": {"rejected"}}
                if evidence.state not in transitions[previous.state]:
                    raise ValueError("Native input evidence cannot lose a known fact")
                for key in ("input_id", "rpc_id", "thread_id", "turn_id", "acceptance_source", "kind",
                            "connection_generation", "target_turn_id"):
                    known = getattr(previous, key)
                    if known is not None and known != getattr(evidence, key):
                        raise ValueError(f"Native input {key} changed within its attempt")
                entries = [evidence if item.attempt_id == evidence.attempt_id else item for item in entries]
            else:
                entries.append(evidence)
            if payload.get("source") == "handoff" and evidence.kind == "start" and evidence.state == "accepted":
                await tx.execute("UPDATE client_submissions SET payload=json_set(payload,'$.fresh_native_id',?) "
                                 "WHERE session_id=? AND request_id=? AND json_extract(payload,'$.fresh_native_id') IS NULL",
                                 (evidence.thread_id, session_id, request_id))
            uncertain = any(item.state == "unknown" for item in entries)
            delivery = "unknown" if uncertain else "confirmed" if any(item.state == "accepted" for item in entries) else "none"
            # Observation can settle input uncertainty after Stop. It cannot
            # reopen execution or replace the already committed outcome.
            state = row[1]
            if state != "running" and row[2] and not uncertain:
                state = "done" if delivery == "confirmed" else "withdrawn"
            await tx.execute(
                "UPDATE client_submissions SET native_inputs=?,delivery_state=?,execution_state=?,reason_code=?,state_version=state_version+1,updated_at=? "
                "WHERE session_id=? AND request_id=?",
                (json.dumps([item.model_dump() for item in entries]), delivery, state,
                 "input_acceptance_unknown" if uncertain else None, datetime.now(UTC).isoformat(), session_id, request_id),
            )

    async def requeue_unaccepted_input(self, session_id: str, request_id: str) -> bool:
        """Return the same input only after its original writer proves rejection."""
        async with self.transaction(write=True) as tx:
            row = await tx.fetch_one("SELECT native_inputs,execution_state FROM client_submissions WHERE session_id=? AND request_id=?",
                                     (session_id, request_id))
            if row is None:
                raise ValueError("The original native input no longer exists")
            if row[1] != "running" or any(
                    item["state"] not in {"not_sent", "rejected"} for item in json.loads(row[0])):
                return False
            await tx.execute(
                "UPDATE client_submissions SET execution_state='pending',execution_owner=NULL,delivery_state='none',reason_code=NULL,"
                "receipt=json_set(receipt,'$.disposition','accepted'),state_version=state_version+1,updated_at=? "
                "WHERE session_id=? AND request_id=?", (datetime.now(UTC).isoformat(), session_id, request_id))
            return True

    async def get_source_submission(self, source: str, delivery_id: str, *, session_id: str | None = None) -> dict[str, Any] | None:
        """Resolve the committed source identity, including its original target."""
        async with self.transaction(write=False) as tx:
            row = await tx.fetch_one(
                _SELECT + "WHERE " + _SOURCE_INPUT
                + (" AND i.session_id=?" if session_id is not None else ""),
                (f"{source}:{delivery_id}", source, delivery_id, *((session_id,) if session_id is not None else ())),
            )
            return _submission(row) if row else None

    async def get_submission_for_request(self, session_id: str, request_id: str) -> dict[str, Any] | None:
        async with self.transaction(write=False) as tx:
            row = await tx.fetch_one(
                _SELECT + "WHERE i.session_id=? AND i.request_id=?",
                (session_id, request_id),
            )
            return _submission(row) if row else None

    async def execution_context_inputs(self, session_id: str, request_id: str) -> dict[str, Any] | None:
        """Read one original execution and its exact inline inputs together.

        The execution owner fixes membership; transcript position only orders
        those members. Invalid or missing member evidence stays visible to the
        caller rather than being filtered into an apparently unchanged request.
        """
        async with self.transaction(write=False) as tx:
            original = await tx.fetch_one(
                _SELECT + "WHERE i.session_id=? AND i.request_id=?", (session_id, request_id),
            )
            owner = await tx.fetch_one("SELECT user_id FROM sessions WHERE id=?", (session_id,))
            if original is None or owner is None:
                return None
            rows = await tx.fetch_all(
                f"SELECT {_EXECUTION_INPUT_COLUMNS} FROM client_submissions i {_EXECUTION_INPUT_MESSAGE} "
                "WHERE i.session_id=? AND i.execution_owner=? ORDER BY m.seq,i.rowid",
                (session_id, request_id),
            )
            return _execution_snapshot(_submission(original), owner[0], rows)

    async def get_client_submission(self, session_id: str, client_id: str) -> dict[str, Any] | None:
        async with self.transaction(write=False) as tx:
            return await self._get_client_submission(tx, session_id, client_id)

    async def _get_client_submission(self, tx: Transaction, session_id: str, client_id: str) -> dict[str, Any] | None:
        cursor = await tx.execute(
            _SELECT + "WHERE i.session_id=? AND i.client_id=?",
            (session_id, client_id),
        )
        row = await cursor.fetchone()
        return _submission(row) if row else None

    async def admit_client_submission(self, session_id: str, client_id: str, payload_sha256: str,
                                      request_id: str, *, payload: dict, receipt: dict,
                                      recovery_of: str | None = None,
                                      schedule: OneShotFire | None = None, new_session: dict | None = None,
                                      superseded_handoff: tuple[str, str] | None = None) -> tuple[bool, dict[str, Any]]:
        """Commit the complete input and its acceptance in the same operation.

        The SessionOwner checks live admission before entering this transaction.
        No dispatch or source acknowledgment precedes its commit.
        """
        async with self.transaction(write=True) as tx:
            now = datetime.now(UTC).isoformat()
            existing = await self._get_client_submission(tx, session_id, client_id)
            if existing is not None:
                return False, existing
            if payload.get("work_id") is not None and payload.get("source") == "delegation":
                # Share the original round's durable seal, even if admission
                # checked its process-local lease before waiting for this writer.
                work = await tx.fetch_one("SELECT delegation_state FROM sessions WHERE id=? AND origin='delegation'", (session_id,))
                if work is not None and work[0] is not None:
                    current = json.loads(work[0])
                    if current["work_id"] != payload["work_id"] or current["state"] != "running":
                        raise ValueError("Delegation work is closed")
            await require_application_writable(tx, session_id)
            if schedule is not None:
                # The fire consumes exactly the definition it authorized. No
                # session or input can survive a missing or concurrently edited
                # one-shot. Compare after acquiring SQLite write ownership.
                definition = schedule.definition
                run_at = definition.get("run_at")
                if not definition["enabled"] or not run_at:
                    raise ValueError("Only an enabled one-shot can be consumed at admission")
                current = await tx.fetch_one(
                    f"SELECT origin_session_id,{','.join(_ONE_SHOT_FIELDS)} FROM schedules WHERE id=?", (definition["id"],),
                )
                if current is None:
                    raise ScheduleChangedError("Schedule disappeared before its fire was admitted")
                current_origin, *current_definition = current
                if tuple(current_definition) != tuple(definition.get(field) for field in _ONE_SHOT_FIELDS):
                    raise ScheduleChangedError("Schedule changed before its fire was admitted")
                if stored_run_at(run_at) > datetime.now(UTC):
                    raise ScheduleChangedError("The original one-shot time is not due")
                if current_origin != definition.get("origin_session_id"):
                    raise ScheduleOriginChanged("The one-shot was rebound before its fire was admitted")
                delivery_id = schedule_delivery_id(definition["id"], run_at)
                if await tx.fetch_one(
                    "SELECT 1 FROM client_submissions i WHERE " + _SOURCE_INPUT,
                    (f"schedule:{delivery_id}", "schedule", delivery_id),
                ) is not None:
                    raise ScheduleChangedError("The original one-shot was already admitted")
            if new_session is not None:
                from .sessions import insert_session

                await insert_session(tx, new_session)
            if recovery_of:
                original = await self._get_client_submission(tx, session_id, recovery_of)
                if (original is None or original["payload"] is None
                        or original["payload"].get("source") == "handoff"
                        or original["payload"]["operation"]["kind"] != "message" or decode_payload(payload)["operation"]["kind"] != "message"
                        or original["execution_state"] != "withdrawn" or original["dismissed"]
                        or original["terminal"] is not None and not (original["native_inputs"] and original["delivery_state"] == "none")):
                    raise InputRecoveryUnavailable("This input is not available for recovery. Review its current status.")
                if original["recovered_by"]:
                    raise InputRecoveryUnavailable("This input has already been resubmitted. Review the new request.")
            restored_handoff_history = None
            if superseded_handoff is not None:
                handoff_id, batch_id = superseded_handoff
                restored_handoff_history = await _withdraw_unsent_handoff(tx, session_id, handoff_id, "withdrawn")
                if restored_handoff_history is not None:
                    payload = {**payload, "ticket_batch_candidate_for": batch_id}
            cursor = await tx.execute(
                "INSERT INTO client_submissions "
                "(session_id,client_id,payload_sha256,request_id,payload,receipt,execution_state,created_at,updated_at,recovery_of) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (session_id, client_id, payload_sha256, request_id,
                 json.dumps(payload, ensure_ascii=False), json.dumps(receipt, ensure_ascii=False),
                 "pending", now, now, recovery_of),
            )
            created = cursor.rowcount == 1
            if created and decode_payload(payload)["operation"]["kind"] == "message":
                await tx.execute(
                    "UPDATE applications SET status='building',error=NULL,updated_at=? "
                    "WHERE session_id=? AND deletion IS NULL AND EXISTS ("
                    "SELECT 1 FROM sessions s WHERE s.id=applications.session_id "
                    "AND s.user_id IS applications.user_id AND (s.app_id IS NULL OR s.app_id=applications.id))",
                    (now, session_id),
                )
            row = await self._get_client_submission(tx, session_id, client_id)
            assert row is not None
            if restored_handoff_history is not None:
                row["restored_handoff_history"] = restored_handoff_history
            if schedule is not None:
                await tx.execute("DELETE FROM schedules WHERE id=?", (schedule.definition["id"],))
                # Use the last synchronous boundary before COMMIT so a newer
                # registration or relationship phase can still refuse this fire.
                await tx.finish()
                schedule.validate_admission()
            return created, row

    async def claim_client_submission(self, session_id: str, request_id: str, owner: str, generation: str) -> bool:
        """Reserve the original input identity; this is not a runtime ACK."""
        async with self.transaction(write=True) as tx:
            from ..execution_identity import boot_identity
            cursor = await tx.execute(
                "SELECT execution_state,execution_generation FROM client_submissions WHERE session_id=? AND request_id=?",
                (session_id, request_id),
            )
            row = await cursor.fetchone()
            if row is None:
                return False
            if row[0] != "pending" and not (row[0] == "running" and row[1] == generation):
                return False
            await tx.execute(
                "UPDATE client_submissions SET execution_state='running',reason_code=NULL,execution_owner=?,execution_generation=?,execution_boot=?,"
                "receipt=CASE WHEN request_id=? AND json_extract(receipt,'$.disposition')='steered' "
                "THEN json_set(receipt,'$.disposition','accepted') ELSE receipt END,"
                "state_version=state_version+1,updated_at=? WHERE session_id=? AND request_id=?",
                (owner, generation, boot_identity(), owner, datetime.now(UTC).isoformat(), session_id, request_id),
            )
            return True

    async def set_steer_delivery(self, session_id: str, request_id: str, *, confirmed: bool) -> bool:
        """Record local frame write/drain, independently of provider processing."""
        async with self.transaction(write=True) as tx:
            if not confirmed:
                owner = await tx.fetch_one("SELECT user_id FROM sessions WHERE id=?", (session_id,))
                if owner is None or not await owner_is_available(tx, owner[0]):
                    return False
            reason = None if confirmed else "steer_delivery_unknown"
            await tx.execute(
                "UPDATE client_submissions SET reason_code=?,delivery_state=?,state_version=state_version+1,updated_at=? "
                "WHERE session_id=? AND request_id=? AND execution_state='running'",
                (reason, "confirmed" if confirmed else "unknown", datetime.now(UTC).isoformat(), session_id, request_id),
            )
            return True

    async def note_submission_process(self, session_id: str, owner: str, group: int) -> None:
        async with self.transaction(write=True) as tx:
            await tx.execute(
                "UPDATE client_submissions SET process_group=? WHERE session_id=? AND execution_owner=? AND execution_state='running'",
                (group, session_id, owner),
            )

    async def uncertain_submission_writers(self, session_id: str) -> list[dict[str, Any]]:
        async with self.transaction(write=False) as tx:
            cursor = await tx.execute(
                _SELECT + "WHERE i.session_id=? AND i.payload IS NOT NULL "
                "AND i.execution_state='unknown' AND i.execution_generation IS NOT NULL", (session_id,),
            )
            return [_submission(row) for row in await cursor.fetchall()]

    async def complete_client_submission(self, session_id: str, request_id: str, *, owner: str,
                                         state: str, reason: str | None = None) -> None:
        async with self.transaction(write=True) as tx:
            await tx.execute(
                "UPDATE client_submissions SET execution_state=CASE WHEN delivery_state='unknown' THEN 'unknown' ELSE ? END,"
                "execution_owner=?,reason_code=CASE WHEN delivery_state='unknown' THEN reason_code ELSE ? END,"
                "state_version=state_version+1,updated_at=? WHERE session_id=? AND request_id=? "
                "AND execution_state IN ('pending','running')",
                (state, owner, reason, datetime.now(UTC).isoformat(), session_id, request_id),
            )

    async def withdraw_unsent_handoff(self, session_id: str, request_id: str, reason: str) -> str | None:
        async with self.transaction(write=True) as tx:
            return await _withdraw_unsent_handoff(tx, session_id, request_id, reason)

    async def recover_client_submissions(self) -> frozenset[str]:
        """Classify inputs and return the pre-existing uncertain generations.

        This snapshot is not retirement evidence. The startup caller needs
        verified installation ownership before treating its writers as gone.
        """
        async with self.transaction(write=True) as tx:
            generations = await tx.fetch_all(
                "SELECT DISTINCT execution_generation FROM client_submissions WHERE payload IS NOT NULL "
                "AND execution_state IN ('running','unknown') AND execution_generation IS NOT NULL",
            )
            for session_id, request_id in await tx.fetch_all(
                "SELECT session_id, request_id FROM client_submissions WHERE execution_state='running' "
                "AND json_extract(payload,'$.source')='handoff'",
            ):
                await _withdraw_unsent_handoff(tx, session_id, request_id, "service_restart_before_start")
            # Handoffs retire the history boundary together with the input.
            # Other historical rows still need explicit native evidence.
            await tx.execute(
                "UPDATE client_submissions SET execution_state='withdrawn',reason_code='service_restart_before_start',"
                "state_version=state_version+1,updated_at=? WHERE execution_state='running' AND delivery_state='none' "
                "AND json_extract(payload,'$.source') IS NOT 'handoff' "
                "AND json_array_length(native_inputs)>0 AND NOT EXISTS ("
                "SELECT 1 FROM json_each(native_inputs) WHERE json_extract(value,'$.state') NOT IN ('not_sent','rejected'))",
                (datetime.now(UTC).isoformat(),),
            )
            # Classify each newly interrupted writer once, before changing it
            # to unknown. Old unknown rows cannot invalidate a newer sample on
            # every subsequent restart. Ordinary messages may auto-compact too.
            from ..context_models import ContextState, invalidate_context

            for session_id, raw in await tx.fetch_all(
                "SELECT id,context_state FROM sessions WHERE context_state IS NOT NULL AND EXISTS ("
                "SELECT 1 FROM client_submissions i WHERE i.session_id=sessions.id AND i.execution_state='running')",
            ):
                state = ContextState.model_validate_json(raw)
                stale = invalidate_context(state, state.binding_target, "operation_unconfirmed")
                await tx.execute("UPDATE sessions SET context_state=? WHERE id=?", (stale.model_dump_json(), session_id))
            await tx.execute(
                "UPDATE client_submissions SET reason_code=CASE WHEN delivery_state='unknown' THEN reason_code "
                "WHEN execution_state='pending' "
                "THEN 'service_restart_before_start' ELSE 'service_restart_execution_unknown' END, "
                "execution_state=CASE WHEN execution_state='pending' THEN 'withdrawn' ELSE 'unknown' END, "
                "state_version=state_version+1,updated_at=? WHERE execution_state IN ('pending','running')",
                (datetime.now(UTC).isoformat(),),
            )
            return frozenset(row[0] for row in generations)

    async def list_recoverable_submissions(self, session_id: str, *, before: int | None = None,
                                            limit: int = 20) -> tuple[list[dict[str, Any]], int | None]:
        async with self.transaction(write=False) as tx:
            cursor = await tx.execute(
                _SELECT.replace("SELECT ", "SELECT i.rowid,", 1) +
                "WHERE i.session_id=? AND i.payload IS NOT NULL AND ((i.execution_state IN ('withdrawn','unknown') "
                "AND (i.delivery_state='unknown' OR m.id IS NULL OR i.delivery_state='none' AND json_array_length(i.native_inputs)>0)) "
                f"OR {_NATIVE_STEER_NOTICE}) "
                "AND i.dismissed=0 AND successor.client_id IS NULL AND (? IS NULL OR i.rowid<?) ORDER BY i.rowid DESC LIMIT ?",
                (session_id, before, before, limit + 1),
            )
            rows = list(await cursor.fetchall())
            return [_submission(row[1:]) for row in rows[:limit]], rows[limit - 1][0] if len(rows) > limit else None

    async def dismiss_client_submission(self, session_id: str, client_id: str) -> bool:
        async with self.transaction(write=True) as tx:
            cursor = await tx.execute(
                "UPDATE client_submissions AS i SET dismissed=1,state_version=state_version+1,updated_at=? "
                f"WHERE session_id=? AND client_id=? AND (execution_state IN ('withdrawn','unknown') OR {_NATIVE_STEER_NOTICE})",
                (datetime.now(UTC).isoformat(), session_id, client_id),
            )
            return cursor.rowcount == 1
