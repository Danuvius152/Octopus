"""Durable browser inputs; startup classifies them without replaying work."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from ..submission_models import InputRecoveryUnavailable
from .base import DatabaseBase
from .schedules import ScheduleChangedError
from .transaction import Transaction
from .users import owner_is_available

_COLUMNS = ("session_id", "client_id", "payload_sha256", "request_id", "receipt", "payload", "execution_state",
            "execution_owner", "execution_generation", "reason_code", "state_version", "created_at",
            "updated_at", "recovery_of", "dismissed", "execution_boot", "process_group", "delivery_state")

_SELECT = (
    f"SELECT {','.join('i.' + column for column in _COLUMNS)},successor.client_id,m.content "
    "FROM client_submissions i LEFT JOIN messages m ON m.session_id=i.session_id "
    "AND m.terminal_request_id=COALESCE(i.execution_owner,i.request_id) "
    "LEFT JOIN client_submissions successor ON successor.session_id=i.session_id AND successor.recovery_of=i.client_id "
)


def _submission(row) -> dict[str, Any]:
    result = dict(zip((*_COLUMNS, "recovered_by", "terminal"), row, strict=True))
    for key in ("payload", "receipt", "terminal"):
        result[key] = json.loads(result[key]) if result[key] is not None else None
    return result


class ClientSubmissionsMixin(DatabaseBase):
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
                        or original["execution_state"] != "withdrawn" or original["dismissed"]
                        or original["terminal"] is not None or original["reason_code"] == "legacy_rejected"):
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
                "WHERE i.session_id=? AND i.payload IS NOT NULL AND i.execution_state IN ('withdrawn','unknown') "
                "AND (i.delivery_state='unknown' OR m.id IS NULL) AND i.reason_code IS NOT 'legacy_rejected' "
                "AND i.dismissed=0 AND successor.client_id IS NULL AND (? IS NULL OR i.rowid<?) ORDER BY i.rowid DESC LIMIT ?",
                (session_id, before, before, limit + 1),
            )
            rows = list(await cursor.fetchall())
            return [_submission(row[1:]) for row in rows[:limit]], rows[limit - 1][0] if len(rows) > limit else None

    async def dismiss_client_submission(self, session_id: str, client_id: str) -> bool:
        async with self.transaction(write=True) as tx:
            cursor = await tx.execute(
                "UPDATE client_submissions SET dismissed=1,state_version=state_version+1,updated_at=? "
                "WHERE session_id=? AND client_id=? AND execution_state IN ('withdrawn','unknown')",
                (datetime.now(UTC).isoformat(), session_id, client_id),
            )
            return cursor.rowcount == 1
