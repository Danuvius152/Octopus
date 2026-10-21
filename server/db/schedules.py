"""Recurring prompts, owned by an agent.

One slice of the persistence layer. These were methods on a single 2,600-line
`Database` class; splitting them by domain (§3 A4) keeps each file about one
subject, while `Database` still presents them as one object so no call site
changed.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

from ..knowledge import require_open
from .base import DatabaseBase
from .transaction import Transaction

if TYPE_CHECKING:
    from ..knowledge import AdmittedRequest


class ScheduleChangedError(ValueError):
    """The definition changed after this edit was prepared."""


class SchedulesMixin(DatabaseBase):

    @asynccontextmanager
    async def _definition_write(self, request: AdmittedRequest | None) -> AsyncIterator[None]:
        """The admission must stay open through the definition's SQL write."""
        require_open(request)
        yield
        require_open(request)

    async def save_schedule(
        self,
        schedule_id: str,
        agent_id: str,
        name: str,
        prompt: str,
        created_at: str,
        interval_seconds: int | None = None,
        cron: str | None = None,
        timezone: str | None = None,
        recurrence_label: str | None = None,
        enabled: bool = True,
        origin_session_id: str | None = None,
        run_at: str | None = None,
        wiki_mode: str | None = None,
        knowledge_context: str | None = None,
        request: AdmittedRequest | None = None,
    ) -> None:
        """Persist a schedule. Recurrence is one of `interval_seconds`, `cron`
        (with `timezone`), or `run_at` (ISO datetime, fires once then auto-deletes).
        `origin_session_id`, when set, is the session the `/schedule` command was
        typed in — fires append into it instead of a throwaway session."""
        async with self.transaction(write=True) as tx:
            async with self._definition_write(request):
                await tx.execute(
                    "INSERT INTO schedules (id, agent_id, origin_session_id, name, prompt, "
                    "interval_seconds, cron, timezone, recurrence_label, enabled, "
                    "created_at, run_at, wiki_mode, knowledge_context) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        schedule_id,
                        agent_id,
                        origin_session_id,
                        name,
                        prompt,
                        interval_seconds,
                        cron,
                        timezone,
                        recurrence_label,
                        int(enabled),
                        created_at,
                        run_at,
                        wiki_mode,
                        knowledge_context,
                    ),
                )

    async def get_schedule_definition(self, schedule_id: str) -> dict[str, Any] | None:
        """Return the current definition; registration belongs to the runner."""
        async with self.transaction(write=False) as tx:
            rows = await self._load_schedules(tx, schedule_id=schedule_id)
            return rows[0] if rows else None

    async def load_schedules(
        self, user_id: str | None = None
    ) -> list[dict[str, Any]]:
        """Every schedule, or only the ones an account owns.

        `schedules` carries no `user_id` of its own: a schedule belongs to an
        agent, and the agent has the owner (multi-tenancy.md §5). Joining is
        therefore the definition, not an optimisation — a copy of the column
        here would be a second answer to the same question, free to disagree
        with the first.

        `user_id=None` is "no scoping asked for", which is what the scheduler
        wants: it fires everybody's jobs from one process.
        """
        async with self.transaction(write=False) as tx:
            return await self._load_schedules(tx, user_id)

    async def _load_schedules(
        self, tx: Transaction, user_id: str | None = None, *, schedule_id: str | None = None,
    ) -> list[dict[str, Any]]:
        sql = (
            "SELECT s.id, s.agent_id, s.name, s.prompt, s.interval_seconds, s.cron, "
            "s.timezone, s.recurrence_label, s.enabled, s.created_at, s.last_run_at, "
            "s.origin_session_id, s.run_at, s.last_run_session_id, s.wiki_mode, "
            "s.invalid_reason, s.knowledge_context FROM schedules s"
        )
        params: list[Any] = []
        clauses = []
        if user_id is not None:
            sql += " JOIN agents a ON a.id = s.agent_id"
            clauses.append("a.user_id = ?")
            params.append(user_id)
        if schedule_id is not None:
            clauses.append("s.id = ?")
            params.append(schedule_id)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        cursor = await tx.execute(sql, params)
        rows = await cursor.fetchall()
        return [
            {
                "id": row[0],
                "agent_id": row[1],
                "name": row[2],
                "prompt": row[3],
                "interval_seconds": row[4],
                "cron": row[5],
                "timezone": row[6],
                "recurrence_label": row[7],
                "enabled": bool(row[8]),
                "created_at": row[9],
                "last_run_at": row[10],
                "origin_session_id": row[11],
                "run_at": row[12],
                "last_run_session_id": row[13],
                "wiki_mode": row[14],
                "invalid_reason": row[15],
                "knowledge_context": row[16],
            }
            for row in rows
        ]

    async def delete_schedule(self, schedule_id: str) -> None:
        async with self.transaction(write=True) as tx:
            await tx.execute("DELETE FROM schedules WHERE id = ?", (schedule_id,))

    async def repoint_schedules_origin(
        self, old_session_id: str, new_session_id: str
    ) -> list[dict[str, Any]]:
        """Move every schedule anchored to `old_session_id` onto
        `new_session_id` (used when a session is archived and replaced — its
        schedules should keep appending into the live successor thread). Returns
        the affected schedule rows (post-update) so the caller can re-register
        their jobs. No-op returning [] when nothing points at the old session."""
        async with self.transaction(write=True) as tx:
            cursor = await tx.execute(
                "SELECT id FROM schedules WHERE origin_session_id = ?",
                (old_session_id,),
            )
            affected = {row[0] for row in await cursor.fetchall()}
            if not affected:
                return []
            await tx.execute(
                "UPDATE schedules SET origin_session_id = ? WHERE origin_session_id = ?",
                (new_session_id, old_session_id),
            )
            return [r for r in await self._load_schedules(tx) if r["id"] in affected]

    async def update_schedule(self, schedule_id: str, *, expected: dict | None = None,
                              request: AdmittedRequest | None = None, **fields: Any) -> None:
        async with self.transaction(write=True) as tx:
            allowed = {
                "name",
                "prompt",
                "interval_seconds",
                "cron",
                "run_at",
                "timezone",
                "recurrence_label",
                "enabled",
                "last_run_at",
                "last_run_session_id",
                "wiki_mode",
                "invalid_reason",
                "knowledge_context",
            }
            updates = {k: v for k, v in fields.items() if k in allowed}
            if not updates:
                return
            if "enabled" in updates:
                updates["enabled"] = int(updates["enabled"])
            set_clause = ", ".join(f"{k} = ?" for k in updates)
            values = list(updates.values()) + [schedule_id]
            # Operational fire timestamps do not change the definition. Every
            # definition field does, including an omitted permission or target.
            definition = allowed - {"last_run_at", "last_run_session_id"}
            compare = {key: expected.get(key) for key in sorted(definition)} if expected is not None else {}
            where = " AND ".join(f"{key} IS ?" for key in compare)
            values.extend(compare.values())
            async with self._definition_write(request):
                cursor = await tx.execute(
                    f"UPDATE schedules SET {set_clause} WHERE id = ?" + (f" AND {where}" if where else ""),
                    values,
                )
                if expected is not None and cursor.rowcount != 1:
                    raise ScheduleChangedError("Schedule changed during this edit; reload and retry")
