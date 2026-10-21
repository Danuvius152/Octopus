"""Durable browser inputs; startup classifies them without replaying work."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from .base import DatabaseBase
from .transaction import Transaction
from .users import owner_is_available

_COLUMNS = ("client_id", "payload_sha256", "request_id", "receipt", "payload", "execution_state",
            "execution_owner", "execution_generation", "reason_code", "state_version", "created_at",
            "updated_at", "recovery_of", "recovered_by", "dismissed", "execution_boot", "process_group")


def _submission(row) -> dict[str, Any]:
    result = dict(zip(_COLUMNS, row, strict=True))
    for key in ("payload", "receipt"):
        result[key] = json.loads(result[key]) if result[key] is not None else None
    return result


class ClientSubmissionsMixin(DatabaseBase):
    async def get_submission_for_request(self, session_id: str, request_id: str) -> dict[str, Any] | None:
        async with self.transaction(write=False) as tx:
            row = await tx.fetch_one(
                f"SELECT {','.join(_COLUMNS)} FROM client_submissions WHERE session_id=? AND request_id=?",
                (session_id, request_id),
            )
            return _submission(row) if row else None

    async def get_client_submission(self, session_id: str, client_id: str) -> dict[str, Any] | None:
        async with self.transaction(write=False) as tx:
            return await self._get_client_submission(tx, session_id, client_id)

    async def _get_client_submission(self, tx: Transaction, session_id: str, client_id: str) -> dict[str, Any] | None:
        cursor = await tx.execute(
            f"SELECT {','.join(_COLUMNS)} FROM client_submissions WHERE session_id=? AND client_id=?",
            (session_id, client_id),
        )
        row = await cursor.fetchone()
        return _submission(row) if row else None

    async def reserve_client_submission(self, session_id: str, client_id: str, payload_sha256: str,
                                        request_id: str, *, payload: dict | None = None,
                                        recovery_of: str | None = None) -> tuple[bool, dict[str, Any]]:
        async with self.transaction(write=True) as tx:
            now = datetime.now(UTC).isoformat()
            existing = await self._get_client_submission(tx, session_id, client_id)
            if existing is not None:
                return False, existing
            if recovery_of:
                original = await self._get_client_submission(tx, session_id, recovery_of)
                if (original is None or original["payload"] is None
                        or original["execution_state"] != "withdrawn" or original["dismissed"]):
                    raise ValueError("This input is not available for recovery. Review its current status.")
                if original["recovered_by"]:
                    raise ValueError("This input has already been resubmitted. Review the new request.")
            cursor = await tx.execute(
                "INSERT INTO client_submissions "
                "(session_id,client_id,payload_sha256,request_id,payload,execution_state,created_at,updated_at,recovery_of) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (session_id, client_id, payload_sha256, request_id,
                 json.dumps(payload, ensure_ascii=False) if payload is not None else None,
                 "reserved" if payload is not None else "unknown", now, now, recovery_of),
            )
            created = cursor.rowcount == 1
            if recovery_of:
                await tx.execute(
                    "UPDATE client_submissions SET recovered_by=?,state_version=state_version+1,updated_at=? "
                    "WHERE session_id=? AND client_id=?", (client_id, now, session_id, recovery_of),
                )
            row = await self._get_client_submission(tx, session_id, client_id)
            assert row is not None
            return created, row

    async def settle_client_submission(self, session_id: str, client_id: str, request_id: str, receipt: dict[str, Any]) -> None:
        async with self.transaction(write=True) as tx:
            now = datetime.now(UTC).isoformat()
            await tx.execute(
                "UPDATE client_submissions SET receipt=?,execution_state=CASE "
                "WHEN ?=0 THEN 'rejected' WHEN execution_state='reserved' THEN 'queued' ELSE execution_state END, "
                "state_version=state_version+1,updated_at=? "
                "WHERE session_id=? AND client_id=? AND request_id=? AND receipt IS NULL",
                (json.dumps(receipt, ensure_ascii=False), receipt["accepted"], now, session_id, client_id, request_id),
            )
            if not receipt["accepted"]:
                await tx.execute(
                    "UPDATE client_submissions SET recovered_by=NULL WHERE session_id=? AND recovered_by=?",
                    (session_id, client_id),
                )

    async def claim_client_submission(self, session_id: str, request_id: str, owner: str, generation: str) -> bool:
        async with self.transaction(write=True) as tx:
            from ..execution_identity import boot_identity
            cursor = await tx.execute(
                "SELECT execution_state,execution_generation FROM client_submissions WHERE session_id=? AND request_id=?",
                (session_id, request_id),
            )
            row = await cursor.fetchone()
            if row is None:
                return True
            if row[0] not in {"reserved", "queued"} and not (row[0] == "claimed" and row[1] == generation):
                return False
            await tx.execute(
                "UPDATE client_submissions SET execution_state='claimed',reason_code=NULL,execution_owner=?,execution_generation=?,execution_boot=?,"
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
                "UPDATE client_submissions SET reason_code=?,state_version=state_version+1,updated_at=? "
                "WHERE session_id=? AND request_id=? AND execution_state='claimed'",
                (reason, datetime.now(UTC).isoformat(), session_id, request_id),
            )
            return True

    async def note_submission_process(self, session_id: str, owner: str, group: int) -> None:
        async with self.transaction(write=True) as tx:
            await tx.execute(
                "UPDATE client_submissions SET process_group=? WHERE session_id=? AND execution_owner=? AND execution_state='claimed'",
                (group, session_id, owner),
            )

    async def uncertain_submission_writers(self, session_id: str) -> list[dict[str, Any]]:
        async with self.transaction(write=False) as tx:
            cursor = await tx.execute(
                f"SELECT {','.join(_COLUMNS)} FROM client_submissions WHERE session_id=? AND payload IS NOT NULL "
                "AND execution_state='unknown' AND execution_generation IS NOT NULL", (session_id,),
            )
            return [_submission(row) for row in await cursor.fetchall()]

    async def complete_client_submission(self, session_id: str, request_id: str, *, owner: str,
                                         state: str, reason: str | None = None) -> None:
        async with self.transaction(write=True) as tx:
            await tx.execute(
                "UPDATE client_submissions SET execution_state=CASE WHEN reason_code='steer_delivery_unknown' THEN 'unknown' ELSE ? END,"
                "execution_owner=?,reason_code=CASE WHEN reason_code='steer_delivery_unknown' THEN reason_code ELSE ? END,"
                "state_version=state_version+1,updated_at=? WHERE session_id=? AND request_id=? "
                "AND execution_state IN ('reserved','queued','claimed')",
                (state, owner, reason, datetime.now(UTC).isoformat(), session_id, request_id),
            )

    async def recover_client_submissions(self) -> None:
        async with self.transaction(write=True) as tx:
            await tx.execute(
                "UPDATE client_submissions SET reason_code=CASE WHEN reason_code='steer_delivery_unknown' THEN reason_code "
                "WHEN execution_state IN ('reserved','queued') "
                "THEN 'service_restart_before_start' ELSE 'service_restart_execution_unknown' END, "
                "execution_state=CASE WHEN execution_state IN ('reserved','queued') THEN 'withdrawn' ELSE 'unknown' END, "
                "state_version=state_version+1,updated_at=? WHERE execution_state IN ('reserved','queued','claimed')",
                (datetime.now(UTC).isoformat(),),
            )

    async def list_recoverable_submissions(self, session_id: str, *, before: int | None = None,
                                            limit: int = 20) -> tuple[list[dict[str, Any]], int | None]:
        async with self.transaction(write=False) as tx:
            cursor = await tx.execute(
                f"SELECT rowid,{','.join(_COLUMNS)} FROM client_submissions "
                "WHERE session_id=? AND payload IS NOT NULL AND execution_state IN ('withdrawn','unknown') "
                "AND (reason_code='steer_delivery_unknown' OR NOT EXISTS (SELECT 1 FROM messages "
                "WHERE messages.session_id=client_submissions.session_id "
                "AND terminal_request_id=COALESCE(execution_owner,request_id))) "
                "AND dismissed=0 AND recovered_by IS NULL AND (? IS NULL OR rowid<?) ORDER BY rowid DESC LIMIT ?",
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
