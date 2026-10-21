"""Background shell tasks that outlive the turn that started them.

One slice of the persistence layer. These were methods on a single 2,600-line
`Database` class; splitting them by domain (§3 A4) keeps each file about one
subject, while `Database` still presents them as one object so no call site
changed.
"""

from __future__ import annotations

from typing import Any

from .base import DatabaseBase


class BgTasksMixin(DatabaseBase):
    # --- Background tasks (cross-turn) ---


    async def create_bg_task(
        self,
        task_id: str,
        session_id: str,
        command: str,
        description: str | None,
        working_dir: str,
        started_at: str,
        idle_timeout_seconds: int | None = None,
        timeout_seconds: int | None = None,
        work_id: str | None = None,
        knowledge_context: str | None = None,
    ) -> None:
        async with self.transaction(write=True) as tx:
            await tx.execute(
                "INSERT INTO bg_tasks "
                "(id, session_id, command, description, working_dir, status, "
                " stdout, stderr, truncated, started_at, idle_timeout_seconds, timeout_seconds, work_id, knowledge_context) "
                "VALUES (?, ?, ?, ?, ?, 'running', '', '', 0, ?, ?, ?, ?, ?)",
                (task_id, session_id, command, description, working_dir, started_at,
                 idle_timeout_seconds, timeout_seconds, work_id, knowledge_context),
            )

    async def update_bg_task(self, task_id: str, **fields: Any) -> None:
        """Patch task progress or its server-encoded return context."""
        async with self.transaction(write=True) as tx:
            allowed = {
                "status",
                "exit_code",
                "stdout",
                "stderr",
                "truncated",
                "completed_at",
                "stop_reason",
                "knowledge_context",
            }
            updates: dict[str, Any] = {}
            for k, v in fields.items():
                if k not in allowed:
                    continue
                if k == "truncated":
                    updates[k] = int(bool(v))
                else:
                    updates[k] = v
            if not updates:
                return
            set_clause = ", ".join(f"{k} = ?" for k in updates)
            values = list(updates.values()) + [task_id]
            await tx.execute(
                f"UPDATE bg_tasks SET {set_clause} WHERE id = ?", values
            )

    async def get_bg_task(self, task_id: str) -> dict[str, Any] | None:
        async with self.transaction(write=False) as tx:
            cursor = await tx.execute(
                f"SELECT {self._BG_TASK_COLS} FROM bg_tasks WHERE id = ?", (task_id,)
            )
            row = await cursor.fetchone()
            return self._row_to_bg_task(row) if row else None

    async def mark_in_flight_bg_tasks_interrupted(
        self, completed_at: str
    ) -> int:
        """Called once at startup: any row left in `running` belongs to a
        prior FastAPI process that crashed or was restarted. The
        subprocess is gone (child of the dead parent), so the row is
        garbage — flip it to `interrupted` so the chat doesn't show a
        spinner that will never resolve. Returns rows updated.
        """
        async with self.transaction(write=True) as tx:
            cursor = await tx.execute(
                "UPDATE bg_tasks SET status = 'interrupted', completed_at = ?, "
                "stop_reason = 'server_restart' "
                "WHERE status IN ('running', 'pending')",
                (completed_at,),
            )
            return cursor.rowcount
