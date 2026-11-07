"""Durable browser inputs; startup classifies them without replaying work."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from ..session_operations import decode_payload
from ..submission_models import InputRecoveryUnavailable, NativeInputEvidence
from .applications import require_application_writable
from .base import DatabaseBase
from .schedules import ScheduleChangedError
from .transaction import Transaction
from .users import owner_is_available

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

# Only unresolved steer acceptance needs a notice. Accepted evidence stays on
# the original input without requiring the user to dismiss a success notice.
_NATIVE_STEER_NOTICE = (
    "(json_extract(i.receipt,'$.disposition')='steered' AND EXISTS(SELECT 1 FROM json_each(i.native_inputs) "
    "WHERE json_extract(value,'$.kind')='steer' AND json_extract(value,'$.state') IN ('not_sent','unknown'))"
    ")"
)


def _submission(row) -> dict[str, Any]:
    result = dict(zip((*_COLUMNS, "recovered_by", "terminal"), row, strict=True))
    for key in ("payload", "receipt", "terminal", "native_inputs"):
        result[key] = json.loads(result[key]) if result[key] is not None else None
    if result["payload"] is not None:
        result["payload"] = decode_payload(result["payload"])
    result["native_inputs"] = [NativeInputEvidence.model_validate(item).model_dump() for item in result["native_inputs"]]
    return result


class ClientSubmissionsMixin(DatabaseBase):
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
            operation = decode_payload(json.loads(row[3]))["operation"]
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
                _SELECT + "WHERE i.client_id=? AND json_extract(i.payload,'$.source')=? "
                "AND json_extract(i.payload,'$.delivery_id')=? "
                + ("AND i.session_id=?" if session_id is not None else ""),
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
                "SELECT i.request_id,i.payload,i.receipt,i.delivery_state,m.seq,m.content,m.tool_input "
                "FROM client_submissions i LEFT JOIN messages m ON m.session_id=i.session_id "
                "AND m.role='user' AND m.type='text' AND CASE WHEN json_valid(m.tool_input) THEN "
                "json_extract(m.tool_input,'$.request_context.request_id')=i.request_id ELSE 0 END "
                "WHERE i.session_id=? AND i.execution_owner=? AND i.request_id<>? ORDER BY m.seq,i.rowid",
                (session_id, request_id, request_id),
            )
            return {"original": _submission(original), "owner": owner[0], "inputs": [
                {"request_id": row[0], "payload": decode_payload(json.loads(row[1])) if row[1] is not None else None,
                 "receipt": json.loads(row[2]) if row[2] is not None else None, "delivery_state": row[3],
                 "seq": row[4], "content": json.loads(row[5]) if row[5] is not None else None,
                 "tool_input": json.loads(row[6]) if row[6] is not None else None}
                for row in rows
            ]}

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
                                      schedule: dict | None = None, new_session: dict | None = None) -> tuple[bool, dict[str, Any]]:
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
                # one-shot; a later callback can try the current definition.
                fields = ("agent_id", "origin_session_id", "prompt", "run_at", "knowledge_context", "enabled")
                current = await tx.fetch_one(
                    f"SELECT {','.join(fields)} FROM schedules WHERE id=?", (schedule["id"],),
                )
                if current is None or tuple(current) != tuple(schedule.get(field) for field in fields):
                    raise ScheduleChangedError("Schedule changed before its fire was admitted")
                if not schedule["enabled"] or not schedule["run_at"]:
                    raise ValueError("Only an enabled one-shot can be consumed at admission")
            if new_session is not None:
                from .sessions import insert_session

                await insert_session(tx, new_session)
            if recovery_of:
                original = await self._get_client_submission(tx, session_id, recovery_of)
                if (original is None or original["payload"] is None
                        or original["payload"]["operation"]["kind"] != "message" or decode_payload(payload)["operation"]["kind"] != "message"
                        or original["execution_state"] != "withdrawn" or original["dismissed"]
                        or original["terminal"] is not None and not (original["native_inputs"] and original["delivery_state"] == "none")):
                    raise InputRecoveryUnavailable("This input is not available for recovery. Review its current status.")
                if original["recovered_by"]:
                    raise InputRecoveryUnavailable("This input has already been resubmitted. Review the new request.")
            cursor = await tx.execute(
                "INSERT INTO client_submissions "
                "(session_id,client_id,payload_sha256,request_id,payload,receipt,execution_state,created_at,updated_at,recovery_of) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (session_id, client_id, payload_sha256, request_id,
                 json.dumps(payload, ensure_ascii=False), json.dumps(receipt, ensure_ascii=False),
                 "pending", now, now, recovery_of),
            )
            created = cursor.rowcount == 1
            row = await self._get_client_submission(tx, session_id, client_id)
            assert row is not None
            if schedule is not None:
                await tx.execute("DELETE FROM schedules WHERE id=?", (schedule["id"],))
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
                "state_version=state_version+1,updated_at=? WHERE session_id=? AND request_id=?",
                (owner, generation, boot_identity(), datetime.now(UTC).isoformat(), session_id, request_id),
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
            # Only new, explicit native evidence can distinguish preparation
            # from possible business delivery. Historical running rows retain
            # their conservative classification; no fact is backfilled.
            await tx.execute(
                "UPDATE client_submissions SET execution_state='withdrawn',reason_code='service_restart_before_start',"
                "state_version=state_version+1,updated_at=? WHERE execution_state='running' "
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
