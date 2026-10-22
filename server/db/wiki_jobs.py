"""Receive Wiki notices once; committed receipts are the original entry's cards."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from ..wiki.contracts import JobCard, JobSnapshot
from ..wiki.store import WikiError
from .base import DatabaseBase


class WikiJobsMixin(DatabaseBase):
    async def receive_wiki_job_notice(
        self, vault_id: str, user_id: str | None, delivery: dict[str, Any],
    ) -> str:
        """Commit before the Wiki ACK; replay must match all original coordinates."""
        owner = user_id if user_id is not None else "legacy"
        if delivery["owner"] != owner:
            from ..wiki.account_upgrade import adopts_notice

            if not adopts_notice(user_id, vault_id, delivery):
                raise WikiError("Wiki notification account changed", 403)
        body = delivery["body"]
        if len(body.encode()) > 24000:
            raise WikiError("Wiki notification exceeds its durable budget", 503)
        snapshot = JobSnapshot.model_validate_json(body)
        if (snapshot.job_id, snapshot.state_version) != (delivery["job_id"], delivery["state_version"]):
            raise WikiError("Wiki notification snapshot identity changed", 503)
        key = (vault_id, delivery["event_id"], delivery["subscriber_id"])
        values = (user_id, snapshot.job_id, snapshot.state_version, delivery["kind"],
                  delivery["target"], delivery["request_id"], delivery["client_id"], body)
        async with self.transaction(write=True) as tx:
            cursor = await tx.execute(
                "SELECT user_id,job_id,state_version,kind,target,request_id,client_id,body,outcome "
                "FROM wiki_job_notifications WHERE vault_id=? AND event_id=? AND subscriber_id=?", key,
            )
            previous = await cursor.fetchone()
            if previous is not None:
                if tuple(previous[:8]) != values:
                    raise WikiError("Wiki notification conflicts with its original receipt", 409)
                outcome = previous[8]
            else:
                outcome = "unavailable"
                if delivery["kind"] in {"chat", "schedule"}:
                    cursor = await tx.execute(
                        "SELECT 1 FROM sessions WHERE id=? AND user_id IS ? "
                        "AND (?='' OR app_id=?)",
                        (delivery["target"], user_id, delivery["client_id"], delivery["client_id"]),
                    )
                    if await cursor.fetchone():
                        outcome = "delivered"
                elif delivery["kind"] == "app":
                    cursor = await tx.execute(
                        "SELECT 1 FROM applications WHERE id=? AND user_id IS ?", (delivery["client_id"], user_id),
                    )
                    if await cursor.fetchone():
                        outcome = "delivered"
                elif delivery["kind"] == "human" and not delivery["target"] and not delivery["client_id"]:
                    outcome = "delivered"
                await tx.execute(
                    "INSERT INTO wiki_job_notifications VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (*key, *values, outcome, datetime.now(UTC).isoformat()),
                )
            return outcome

    async def wiki_job_cards(
        self, session_id: str, user_id: str | None, *, offset: int = 0, limit: int = 30,
        job_id: str | None = None, vault_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Select the newest committed version, including for archived chats."""
        async with self.transaction(write=False) as tx:
            if offset < 0 or not 1 <= limit <= 100:
                raise ValueError("Invalid Wiki card pagination")
            if (job_id is None) != (vault_id is None):
                raise ValueError("Supply both Wiki job and vault identities")
            selected = " AND n.vault_id=? AND n.job_id=?" if job_id is not None else ""
            identity = (vault_id, job_id) if job_id is not None else ()
            cursor = await tx.execute(
                "SELECT vault_id,subscriber_id,request_id,body,received_at FROM ("
                "SELECT n.*,row_number() OVER (PARTITION BY vault_id,job_id "
                "ORDER BY state_version DESC,event_id) AS position "
                "FROM wiki_job_notifications n JOIN sessions s ON s.id=n.target AND s.user_id IS n.user_id "
                "WHERE n.user_id IS ? AND n.target=? AND n.kind IN ('chat','schedule') AND n.outcome='delivered'"
                + selected +
                ") WHERE position=1 ORDER BY received_at DESC LIMIT ? OFFSET ?",
                (user_id, session_id, *identity, limit, offset),
            )
            return [JobCard(**json.loads(row[3]), vault_id=row[0], subscriber_id=row[1],
                            origin_request_id=row[2], received_at=row[4]).model_dump() for row in await cursor.fetchall()]
