"""Durable admission identities; the dispatch queue remains process-local."""

from __future__ import annotations

import json
from typing import Any

from .base import DatabaseBase, serialized_write


class ClientSubmissionsMixin(DatabaseBase):
    async def get_client_submission(self, session_id: str, client_id: str) -> dict[str, Any] | None:
        cursor = await self.conn.execute(
            "SELECT payload_sha256, request_id, receipt FROM client_submissions WHERE session_id=? AND client_id=?",
            (session_id, client_id),
        )
        row = await cursor.fetchone()
        return {"payload_sha256": row[0], "request_id": row[1], "receipt": json.loads(row[2]) if row[2] else None} if row else None

    @serialized_write
    async def reserve_client_submission(self, session_id: str, client_id: str, payload_sha256: str, request_id: str) -> tuple[bool, dict[str, Any]]:
        cursor = await self.conn.execute(
            "INSERT OR IGNORE INTO client_submissions(session_id,client_id,payload_sha256,request_id) VALUES (?,?,?,?)",
            (session_id, client_id, payload_sha256, request_id),
        )
        created = cursor.rowcount == 1
        # No execution may start until the reservation is durable. An orphaned
        # reservation is unknown after restart, never permission to replay.
        await self.conn.commit()
        row = await self.get_client_submission(session_id, client_id)
        assert row is not None
        return created, row

    @serialized_write
    async def settle_client_submission(self, session_id: str, client_id: str, request_id: str, receipt: dict[str, Any]) -> None:
        await self.conn.execute(
            "UPDATE client_submissions SET receipt=? WHERE session_id=? AND client_id=? AND request_id=? AND receipt IS NULL",
            (json.dumps(receipt, ensure_ascii=False), session_id, client_id, request_id),
        )
        await self.conn.commit()
