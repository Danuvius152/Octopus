"""Deep-research jobs.

One slice of the persistence layer. These were methods on a single 2,600-line
`Database` class; splitting them by domain (§3 A4) keeps each file about one
subject, while `Database` still presents them as one object so no call site
changed.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from .base import DatabaseBase
from .transaction import Transaction


class ResearchMixin(DatabaseBase):

    async def advance_research_delivery(self, job_id: str, request_id: str, stage: str, *, error: str | None = None) -> dict[str, Any] | None:
        async with self.transaction(write=True) as tx:
            row = await self._get_research_job(tx, job_id)
            if row is None:
                return None
            previous = row.get("delivery_receipt") or {}
            if previous.get("request_id") not in {None, request_id}:
                raise ValueError("Research delivery belongs to another request")
            terminal = {"processed", "failed", "recording_unconfirmed"}
            if previous.get("stage") in terminal or previous.get("stage") == stage or (previous.get("stage") == "processing" and stage == "queued"):
                return previous
            receipt = {**previous, "request_id": request_id, "stage": stage,
                       "version": previous.get("version", 0) + 1, "error": error}
            if stage == "processed":
                receipt["processed_at"] = datetime.now(UTC).isoformat()
            await tx.execute("UPDATE research_jobs SET delivery_receipt=? WHERE id=?", (json.dumps(receipt), job_id))
            return receipt

    async def acknowledge_research_delivery(self, job_id: str, request_id: str, version: int) -> dict[str, Any] | None:
        async with self.transaction(write=True) as tx:
            row = await self._get_research_job(tx, job_id)
            receipt = (row or {}).get("delivery_receipt")
            if not receipt or receipt.get("request_id") != request_id or receipt.get("version") != version:
                return None
            receipt = {**receipt, "browser_confirmed_version": version, "browser_confirmed_at": datetime.now(UTC).isoformat()}
            await tx.execute("UPDATE research_jobs SET delivery_receipt=? WHERE id=?", (json.dumps(receipt), job_id))
            return receipt

    async def pending_research_deliveries(self) -> list[dict[str, Any]]:
        async with self.transaction(write=False) as tx:
            cursor = await tx.execute(f"SELECT {self._RESEARCH_COLS} FROM research_jobs WHERE delivery_receipt IS NOT NULL")
            return [row for raw in await cursor.fetchall() if (row := self._row_to_research_job(raw))["delivery_receipt"]["stage"] in {"queued", "processing", "unknown"}]


    async def create_research_job(
        self, job_id: str, session_id: str, question: str, created_at: str,
        knowledge_context: str | None = None,
    ) -> None:
        async with self.transaction(write=True) as tx:
            await tx.execute(
                "INSERT INTO research_jobs "
                "(id, session_id, question, status, phase, created_at, injection_status, knowledge_context) "
                "VALUES (?, ?, ?, 'running', 'scope', ?, 'pending', ?)",
                (job_id, session_id, question, created_at, knowledge_context),
            )

    async def update_research_job(self, job_id: str, **fields: Any) -> None:
        """Patch progress, delivery status or the server-encoded context."""
        async with self.transaction(write=True) as tx:
            allowed = {
                "status", "phase", "error", "report_path", "cost", "completed_at",
                "injection_status", "injected_at", "knowledge_context",
            }
            updates = {k: v for k, v in fields.items() if k in allowed}
            if not updates:
                return
            set_clause = ", ".join(f"{k} = ?" for k in updates)
            await tx.execute(
                f"UPDATE research_jobs SET {set_clause} WHERE id = ?",
                list(updates.values()) + [job_id],
            )

    async def get_research_job(self, job_id: str) -> dict[str, Any] | None:
        async with self.transaction(write=False) as tx:
            return await self._get_research_job(tx, job_id)

    async def _get_research_job(self, tx: Transaction, job_id: str) -> dict[str, Any] | None:
        cursor = await tx.execute(
            f"SELECT {self._RESEARCH_COLS} FROM research_jobs WHERE id = ?", (job_id,)
        )
        row = await cursor.fetchone()
        return self._row_to_research_job(row) if row else None

    async def mark_in_flight_research_jobs_interrupted(self, completed_at: str) -> int:
        """Boot sweep: a `running` row belongs to a prior process — its task is
        gone, so flip it to `interrupted` (native-deep-research.md §6). v1 does
        not resume mid-pipeline. Returns rows updated."""
        async with self.transaction(write=True) as tx:
            cursor = await tx.execute(
                "UPDATE research_jobs SET status = 'interrupted', completed_at = ? "
                "WHERE status = 'running'",
                (completed_at,),
            )
            return cursor.rowcount
