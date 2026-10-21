"""Sessions, their messages, and the fork graph.

One slice of the persistence layer. These were methods on a single 2,600-line
`Database` class; splitting them by domain (§3 A4) keeps each file about one
subject, while `Database` still presents them as one object so no call site
changed.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aiosqlite

from .base import DatabaseBase
from .transaction import Transaction

# A persisted round is usable only through its actual surviving relationship.
# IS also matches the pre-account installation's NULL owner exactly.
_DELEGATION_RELATION = (
    "FROM sessions child "
    "JOIN sessions parent ON parent.id = child.parent_session_id "
    "JOIN agents target ON target.id = child.agent_id "
    "WHERE child.origin = 'delegation' "
    "AND parent.user_id IS child.user_id AND target.user_id IS child.user_id "
)


class SessionsMixin(DatabaseBase):

    async def get_session_binding(self, session_id: str) -> dict[str, Any] | None:
        """Internal relationship lookup; callers must compare the exact owner."""
        async with self.transaction(write=False) as tx:
            cursor = await tx.execute(
                "SELECT id, agent_id, user_id, archived FROM sessions WHERE id = ?",
                (session_id,),
            )
            row = await cursor.fetchone()
            if row is None:
                return None
            return {"id": row[0], "agent_id": row[1], "user_id": row[2], "archived": bool(row[3])}

    async def load_delegation_states(self) -> list[dict[str, Any]]:
        """Load future round snapshots, including archived child conversations."""
        async with self.transaction(write=False) as tx:
            cursor = await tx.execute(
                "SELECT child.id, child.parent_session_id, child.user_id, "
                "child.agent_id, child.delegation_state " + _DELEGATION_RELATION +
                "AND child.delegation_state IS NOT NULL ORDER BY child.id"
            )
            return [
                {"id": row[0], "parent_session_id": row[1], "user_id": row[2],
                 "agent_id": row[3], "delegation_state": row[4]}
                for row in await cursor.fetchall()
            ]

    async def load_delegation_state(
        self, child_id: str, *, parent_session_id: str, user_id: str | None,
    ) -> str | None:
        """Load the round through its surviving owner and parent relationship."""
        async with self.transaction(write=False) as tx:
            cursor = await tx.execute(
                "SELECT child.delegation_state " + _DELEGATION_RELATION +
                "AND child.id = ? AND child.parent_session_id = ? "
                "AND child.user_id IS ?",
                (child_id, parent_session_id, user_id),
            )
            row = await cursor.fetchone()
            return row[0] if row else None

    async def compare_and_set_delegation_state(
        self, child_id: str, expected_json: str | None, new_json: str,
        *, parent_session_id: str, user_id: str | None, target_agent_id: str,
    ) -> bool:
        """Commit a round only through its captured target and previous snapshot."""
        async with self.transaction(write=True) as tx:
            cursor = await tx.execute(
                "UPDATE sessions SET delegation_state = ? WHERE id IN ("
                "SELECT child.id " + _DELEGATION_RELATION +
                "AND child.id = ? AND child.parent_session_id = ? "
                "AND child.user_id IS ? AND child.agent_id = ? "
                "AND child.delegation_state IS ?)",
                (new_json, child_id, parent_session_id, user_id, target_agent_id, expected_json),
            )
            return cursor.rowcount == 1

    async def get_latest_delegation_request(
        self, child_id: str, work_id: str, *,
        parent_session_id: str, user_id: str | None,
    ) -> str | None:
        """Resolve the last stored turn in this round, including nested relays."""
        async with self.transaction(write=False) as tx:
            cursor = await tx.execute(
                "SELECT (SELECT json_extract(tool_input, '$.request_context.request_id') "
                "FROM messages WHERE session_id = child.id AND role = 'user' "
                "AND type = 'text' "
                "AND json_extract(tool_input, '$.request_context.work_id') = ? "
                "AND json_type(tool_input, '$.request_context.request_id') = 'text' "
                "ORDER BY seq DESC LIMIT 1) " + _DELEGATION_RELATION +
                "AND child.id = ? AND child.parent_session_id = ? AND child.user_id IS ?",
                (work_id, child_id, parent_session_id, user_id),
            )
            row = await cursor.fetchone()
            return row[0] if row else None

    async def save_session(
        self,
        session_id: str,
        name: str,
        working_dir: str,
        created_at: str,
        claude_session_id: str | None = None,
        credential_id: str | None = None,
        agent_id: str | None = None,
        origin: str = "user",
        backend: str = "claude-code",
        parent_session_id: str | None = None,
        delegation_request: str | None = None,
        user_id: str | None = None,
        app_id: str | None = None,
    ) -> None:
        async with self.transaction(write=True) as tx:
            await tx.execute(
                "INSERT INTO sessions "
                "(id, name, working_dir, created_at, claude_session_id, "
                " credential_id, agent_id, origin, backend, "
                " parent_session_id, delegation_request, app_id, user_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    session_id,
                    name,
                    working_dir,
                    created_at,
                    claude_session_id,
                    credential_id,
                    agent_id,
                    origin,
                    backend,
                    parent_session_id,
                    delegation_request,
                    app_id,
                    user_id,
                ),
            )

    async def delete_session(self, session_id: str) -> None:
        async with self.transaction(write=True) as tx:
            await tx.execute("DELETE FROM sessions WHERE id = ?", (session_id,))

    async def create_fork_session(
        self,
        *,
        fork_id: str,
        name: str,
        working_dir: str,
        created_at: str,
        parent_id: str,
        backend: str,
        model: str | None = None,
        reasoning_effort: str | None = None,
        service_tier: str | None = None,
        user_id: str | None = None,
        agent_id: str | None,
        credential_id: str | None,
        resume_id: str | None,
        fork_after_seq: int,
        fork_metadata: str | None = None,
    ) -> None:
        """The DB-only half of the fork saga (session-rewind.md §5.1 step
        5): INSERT the fork `sessions` row (origin='fork',
        fork_status='initializing', pre-minted resume id) and INSERT-SELECT the
        parent's messages with ``seq <= fork_after_seq`` — copied verbatim,
        including their git anchors. For M=0 (`fork_after_seq == -1`) the SELECT
        matches nothing. `fork_metadata` is written at INSERT (not deferred) so a
        /fork duplicate's cleanup-credential pin survives a prepare failure
        (session-fork.md). No FS, no git, no shell here — a clean rollback
        unit: the two writes are wrapped so a failed message-copy rolls back the
        row insert rather than leaving an open transaction a later commit would
        flush (Vera review SHOULD-FIX #1)."""
        async with self.transaction(write=True) as tx:
            await tx.execute(
                "INSERT INTO sessions "
                "(id, name, working_dir, created_at, claude_session_id, "
                " credential_id, agent_id, origin, backend, model, user_id, "
                " forked_from_session_id, fork_after_seq, fork_needs_replay, "
                " fork_status, fork_metadata, reasoning_effort, service_tier) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 'fork', ?, ?, ?, ?, ?, 0, "
                " 'initializing', ?, ?, ?)",
                (
                    fork_id, name, working_dir, created_at, resume_id,
                    credential_id, agent_id, backend, model, user_id, parent_id,
                    fork_after_seq, fork_metadata, reasoning_effort,
                    service_tier,
                ),
            )
            await tx.execute(
                "INSERT INTO messages "
                "(session_id, seq, role, type, content, tool_name, tool_input, "
                " tool_use_id, is_error, session_id_ref, cost, attachments, "
                " git_head, git_status_clean, created_at, runtime_metadata) "
                "SELECT ?, seq, role, type, content, tool_name, tool_input, "
                " tool_use_id, is_error, session_id_ref, cost, attachments, "
                " git_head, git_status_clean, created_at, runtime_metadata "
                "FROM messages WHERE session_id = ? AND seq <= ?",
                (fork_id, parent_id, fork_after_seq),
            )

    async def load_sessions(
        self, *, include_archived: bool = False, user_id: str | None = None
    ) -> list[dict[str, Any]]:
        """Every session row, or only one account's (multi-tenancy.md §5).

        `user_id=None` is "no scoping asked for" — the boot-time load, which
        builds the whole in-memory map, and the recovery sweeps. An *archived*
        row is only ever read from here, so this is where its owner is checked
        too; the live ones are filtered in memory by `list_sessions`.
        """
        async with self.transaction(write=False) as tx:
            # Rows come back as plain tuples and are mapped by POSITION below, so a
            # new column goes on the END of this list. Inserting one in the middle
            # silently shifts every field after it — `fork_metadata` starts reading
            # as an integer and sessions come back with the wrong parent.
            query = (
                "SELECT id, name, working_dir, created_at, claude_session_id, "
                "credential_id, archived, agent_id, origin, backend, "
                "parent_session_id, delegation_request, forked_from_session_id, "
                "fork_after_seq, fork_needs_replay, fork_metadata, "
                "fork_revert_record, fork_status, app_id, model, user_id, reasoning_effort, service_tier "
                "FROM sessions"
            )
            clauses = [] if include_archived else ["archived = 0"]
            params: list[Any] = []
            if user_id is not None:
                clauses.append("user_id = ?")
                params.append(user_id)
            if clauses:
                query += " WHERE " + " AND ".join(clauses)
            cursor = await tx.execute(query, params)
            rows = await cursor.fetchall()
            return [
                {
                    "id": row[0],
                    "name": row[1],
                    "working_dir": row[2],
                    "created_at": row[3],
                    "claude_session_id": row[4],
                    "credential_id": row[5],
                    "archived": bool(row[6]),
                    "agent_id": row[7],
                    "origin": row[8] or "user",
                    "backend": row[9] or "claude-code",
                    "parent_session_id": row[10],
                    "delegation_request": row[11],
                    "forked_from_session_id": row[12],
                    "fork_after_seq": row[13],
                    "fork_needs_replay": bool(row[14]),
                    "fork_metadata": row[15],
                    "fork_revert_record": row[16],
                    "fork_status": row[17],
                    "app_id": row[18],
                    "model": row[19],
                    "user_id": row[20],
                    "reasoning_effort": row[21],
                    "service_tier": row[22],
                }
                for row in rows
            ]

    async def count_messages(self, session_id: str) -> int:
        async with self.transaction(write=False) as tx:
            return await self._count(tx,
                "SELECT COUNT(*) FROM messages WHERE session_id = ?", (session_id,)
            )

    async def has_delivery_notice(self, session_id: str, delivery_id: str) -> bool:
        """A bounded lookup for one retained-result notice, not a task receipt."""
        async with self.transaction(write=False) as tx:
            cursor = await tx.execute(
                "SELECT 1 FROM messages WHERE session_id = ? AND role = 'system' "
                "AND type = 'delivery_notice' AND CASE WHEN json_valid(tool_input) THEN "
                "json_extract(tool_input, '$.delivery_id') = ? ELSE 0 END LIMIT 1",
                (session_id, delivery_id),
            )
            return await cursor.fetchone() is not None

    async def message_sequence_state(self, session_id: str) -> tuple[int, int, int]:
        """Row count, allocation floor and duplicate count are different facts."""
        async with self.transaction(write=False) as tx:
            cursor = await tx.execute(
                "SELECT COUNT(*), COALESCE(MAX(seq) + 1, 0), COUNT(*) - COUNT(DISTINCT seq) "
                "FROM messages WHERE session_id = ?", (session_id,),
            )
            row = await cursor.fetchone()
            assert row is not None
            return row[0], row[1], row[2]

    async def has_messages_before(self, session_id: str, seq: int | None) -> bool:
        async with self.transaction(write=False) as tx:
            if seq is None:
                return False
            cursor = await tx.execute(
                "SELECT 1 FROM messages WHERE session_id = ? AND seq < ? LIMIT 1", (session_id, seq),
            )
            return await cursor.fetchone() is not None

    async def raw_message_rows(self, session_id: str, user_id: str | None) -> AsyncIterator[bytes]:
        """A read-only export retaining storage ids and all duplicate sequences."""
        self.health.require_healthy()

        async def rows(reader: aiosqlite.Connection | Transaction) -> AsyncIterator[bytes]:
            cursor = await reader.execute(
                "SELECT m.* FROM messages m JOIN sessions s ON s.id = m.session_id "
                "WHERE m.session_id = ? AND s.user_id IS ? ORDER BY m.id", (session_id, user_id),
            )
            columns = [item[0] for item in cursor.description]
            try:
                while batch := await cursor.fetchmany(200):
                    for row in batch:
                        yield (json.dumps(dict(zip(columns, row, strict=True)), ensure_ascii=False) + "\n").encode()
            finally:
                await cursor.close()

        if self._db_path == ":memory:":
            # A response consumer must never keep the shared transaction open.
            async with self.transaction(write=False) as tx:
                snapshot = [row async for row in rows(tx)]
            for row in snapshot:
                yield row
        else:
            uri = Path(self._db_path).resolve().as_uri() + "?mode=ro"
            async with aiosqlite.connect(uri, uri=True) as reader:
                async for row in rows(reader):
                    yield row

    async def append_message(
        self,
        session_id: str,
        seq: int,
        role: str,
        type: str,
        content: Any = None,
        tool_name: str | None = None,
        tool_input: dict[str, Any] | None = None,
        tool_use_id: str | None = None,
        is_error: bool | None = None,
        session_id_ref: str | None = None,
        cost: float | None = None,
        attachments: list[dict[str, Any]] | None = None,
        git_head: str | None = None,
        git_status_clean: bool | None = None,
        created_at: str | None = None,
        runtime_metadata: dict[str, Any] | None = None,
        terminal_request_id: str | None = None,
    ) -> None:
        async with self.transaction(write=True) as tx:
            return await self._append_message(tx, session_id, seq, role, type, content, tool_name, tool_input, tool_use_id, is_error, session_id_ref, cost, attachments, git_head, git_status_clean, created_at, runtime_metadata, terminal_request_id)

    async def _append_message(
        self, tx: Transaction,
        session_id: str,
        seq: int,
        role: str,
        type: str,
        content: Any = None,
        tool_name: str | None = None,
        tool_input: dict[str, Any] | None = None,
        tool_use_id: str | None = None,
        is_error: bool | None = None,
        session_id_ref: str | None = None,
        cost: float | None = None,
        attachments: list[dict[str, Any]] | None = None,
        git_head: str | None = None,
        git_status_clean: bool | None = None,
        created_at: str | None = None,
        runtime_metadata: dict[str, Any] | None = None,
        terminal_request_id: str | None = None,
    ) -> None:
        content_str = json.dumps(content) if content is not None else None
        tool_input_str = json.dumps(tool_input) if tool_input is not None else None
        is_error_int = int(is_error) if is_error is not None else None
        attachments_str = (
            json.dumps(attachments) if attachments else None
        )
        git_status_clean_int = (
            int(git_status_clean) if git_status_clean is not None else None
        )

        await tx.execute(
            "INSERT INTO messages "
            "(session_id, seq, role, type, content, tool_name, tool_input, "
            "tool_use_id, is_error, session_id_ref, cost, attachments, "
            "git_head, git_status_clean, created_at, runtime_metadata, terminal_request_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                session_id,
                seq,
                role,
                type,
                content_str,
                tool_name,
                tool_input_str,
                tool_use_id,
                is_error_int,
                session_id_ref,
                cost,
                attachments_str,
                git_head,
                git_status_clean_int,
                created_at or datetime.now(UTC).isoformat(),
                json.dumps(runtime_metadata) if runtime_metadata is not None else None,
                terminal_request_id,
            ),
        )

    async def record_request(self, *, request_id: str, **message: Any) -> None:
        """Confirm one input and its server-owned context before execution.

        The message sequence and request identity are both fixed on retry.
        Context is compared as data here, never interpreted as authority.
        """
        async with self.transaction(write=True) as tx:
            tool_input = message.get("tool_input")
            context = tool_input.get("request_context") if isinstance(tool_input, dict) else None
            if (
                not request_id or not isinstance(context, dict)
                or context.get("request_id") != request_id
                or not isinstance(context.get("knowledge"), dict)
            ):
                raise ValueError("request context identity is missing or inconsistent")

            columns = (
                "session_id", "seq", "role", "type", "content", "tool_name",
                "tool_input", "tool_use_id", "is_error", "session_id_ref", "cost",
                "attachments", "git_head", "git_status_clean", "created_at",
                "runtime_metadata", "terminal_request_id",
            )
            # No extra request registry: the input's existing sequence owns the
            # context. Also find the same request at a different sequence, which
            # would otherwise make an uncertain retry duplicate the input.
            query = (
                f"SELECT {', '.join(columns)} FROM messages WHERE session_id = ? "
                "AND (seq = ? OR CASE WHEN json_valid(tool_input) THEN "
                "json_extract(tool_input, '$.request_context.request_id') = ? ELSE 0 END)"
            )
            params = (message["session_id"], message["seq"], request_id)
            cursor = await tx.execute(query, params)
            rows = list(await cursor.fetchall())
            if len(rows) > 1:
                raise ValueError("request record conflict")
            message = dict(message)
            if not message.get("created_at"):
                message["created_at"] = rows[0][14] if rows else datetime.now(UTC).isoformat()

            json_columns = {"content", "tool_input", "attachments", "runtime_metadata"}

            def canonical(value: Any) -> str | None:
                return json.dumps(value, sort_keys=True, separators=(",", ":")) if value is not None else None

            expected = []
            for column in columns:
                value = message.get(column)
                if column == "attachments" and not value:
                    value = None
                if column in json_columns:
                    value = canonical(value)
                elif column in {"is_error", "git_status_clean"} and value is not None:
                    value = int(value)
                expected.append(value)

            def matches(row: Any) -> bool:
                return all(
                    (canonical(json.loads(value)) if column in json_columns and value is not None else value) == wanted
                    for column, value, wanted in zip(columns, row, expected, strict=True)
                )

            if rows:
                if not matches(rows[0]):
                    raise ValueError("request record conflict")
            else:
                await self._append_message(tx, **message)

    async def record_terminal(self, *, request_id: str, **message: Any) -> None:
        """Record one chosen outcome under its unique request identity."""
        async with self.transaction(write=True) as tx:
            session_id = message["session_id"]
            cursor = await tx.execute(
                "SELECT seq, role, type, content, created_at, is_error, session_id_ref, cost, tool_input "
                "FROM messages WHERE session_id = ? AND terminal_request_id = ?",
                (session_id, request_id),
            )
            row = await cursor.fetchone()
            expected = (
                message["seq"], message["role"], message["type"],
                message.get("content"), message["created_at"],
                message.get("is_error"), message.get("session_id_ref"), message.get("cost"), message.get("tool_input"),
            )
            def matches(record: Any) -> bool:
                actual = (*record[:3], json.loads(record[3]) if record[3] else None,
                          record[4], bool(record[5]) if record[5] is not None else None,
                          record[6], record[7], json.loads(record[8]) if record[8] else None)
                return actual == expected
            if row is not None:
                if not matches(row):
                    raise ValueError("terminal record conflict")
            else:
                await self._append_message(tx, **message, terminal_request_id=request_id)

    async def get_session_owner(self, session_id: str) -> str | None:
        """Resolve an event's owner even after its session leaves the live map."""
        async with self.transaction(write=False) as tx:
            cursor = await tx.execute("SELECT user_id FROM sessions WHERE id = ?", (session_id,))
            row = await cursor.fetchone()
            return row[0] if row else None

    async def get_committed_terminal(self, session_id: str, request_id: str) -> dict[str, Any] | None:
        """Read the outcome after earlier operations have committed or rolled back."""
        async with self.transaction(write=False) as tx:
            cursor = await tx.execute(
                "SELECT content FROM messages WHERE session_id = ? AND terminal_request_id = ?",
                (session_id, request_id),
            )
            row = await cursor.fetchone()
            return json.loads(row[0]) if row and row[0] else None

    async def load_committed_request_messages(self, session_id: str, request_id: str) -> list[dict[str, Any]]:
        """Read one request's outcome and text in one committed snapshot."""
        async with self.transaction(write=False) as tx:
            cursor = await tx.execute(
                "SELECT role,type,content,tool_input,cost FROM messages WHERE session_id=? AND "
                "(terminal_request_id=? OR (role='assistant' AND type='text' "
                "AND json_extract(tool_input,'$.attempt_context.request_id')=?)) ORDER BY seq",
                (session_id, request_id, request_id),
            )
            return [{"role": row[0], "type": row[1], "content": json.loads(row[2]) if row[2] else None,
                     "tool_input": json.loads(row[3]) if row[3] else None, "cost": row[4]}
                    for row in await cursor.fetchall()]

    async def load_messages(
        self,
        session_id: str,
        limit: int = 0,
        offset: int = 0,
        *,
        max_seq: int | None = None,
        newest_first: bool = False,
    ) -> list[dict[str, Any]]:
        """Messages for a session, oldest first by default.

        `max_seq` bounds the range in SQL. Callers that wanted a prefix used to
        load the whole transcript and drop the tail in a list comprehension —
        on the largest session here that is 4,600 rows read to keep a handful
        (polish-2026-09.md §4 B3). `idx_messages_session(session_id, seq)`
        makes the bounded form a range scan.

        `newest_first` exists for windowing: taking the most recent N means
        ordering descending, limiting, and reversing — otherwise LIMIT would
        return the *oldest* N. The result is still returned oldest-first so no
        caller has to care which direction it was fetched in.
        """
        async with self.transaction(write=False) as tx:
            query = (
                "SELECT seq, role, type, content, tool_name, tool_input, tool_use_id, "
                "is_error, session_id_ref, cost, attachments, git_head, "
                "git_status_clean, created_at, runtime_metadata "
                "FROM messages WHERE session_id = ?"
            )
            params: list = [session_id]
            if max_seq is not None:
                query += " AND seq <= ?"
                params.append(max_seq)
            query += " ORDER BY seq DESC" if newest_first else " ORDER BY seq"
            if limit > 0:
                query += " LIMIT ? OFFSET ?"
                params.extend([limit, offset])
            cursor = await tx.execute(query, params)
            rows = list(await cursor.fetchall())
            if newest_first:
                rows.reverse()  # fetched newest-first for the LIMIT; hand back in order
            results = []
            for row in rows:
                content = json.loads(row[3]) if row[3] is not None else None
                tool_input = json.loads(row[5]) if row[5] is not None else None
                is_error = bool(row[7]) if row[7] is not None else None
                attachments = json.loads(row[10]) if row[10] is not None else []
                git_status_clean = bool(row[12]) if row[12] is not None else None
                results.append(
                    {
                        "seq": row[0],
                        "role": row[1],
                        "type": row[2],
                        "content": content,
                        "tool_name": row[4],
                        "tool_input": tool_input,
                        "tool_use_id": row[6],
                        "is_error": is_error,
                        "session_id": row[8],
                        "cost": row[9],
                        "attachments": attachments,
                        "git_head": row[11],
                        "git_status_clean": git_status_clean,
                        "created_at": row[13],
                        "runtime_metadata": json.loads(row[14]) if row[14] else None,
                    }
                )
            return results

    async def clear_credential_from_sessions(self, credential_id: str) -> list[str]:
        """Unbind a credential from every session that pins it, returning the
        session ids changed.

        `sessions.credential_id` predates foreign keys on that table (it was
        added by a plain ALTER), so deleting a credential used to leave live
        sessions pointing at a row that no longer exists — they'd silently stop
        using the auth the user intended and couldn't be repointed, because a
        session's credential was only settable at creation. Clearing it makes
        them fall back to the agent's credential (or the CLI's own login),
        which is what the FK on `agents.credential_id` already does for agents.
        """
        async with self.transaction(write=True) as tx:
            cursor = await tx.execute(
                "SELECT id FROM sessions WHERE credential_id = ?", (credential_id,)
            )
            ids = [row[0] for row in await cursor.fetchall()]
            if ids:
                await tx.execute(
                    "UPDATE sessions SET credential_id = NULL WHERE credential_id = ?",
                    (credential_id,),
                )
            return ids

    async def load_incomplete_forks(self) -> list[dict[str, Any]]:
        """Fork rows whose §5.1 saga didn't reach 'ready' — startup recovery
        dispatches on `fork_status` (session-rewind.md §5.6.7). The
        resume id rides in `claude_session_id` (the pre-minted handle stored in
        the saga's step-5 INSERT)."""
        async with self.transaction(write=False) as tx:
            cursor = await tx.execute(
                "SELECT id, forked_from_session_id, working_dir, backend, "
                "claude_session_id, fork_status, fork_revert_record, credential_id, "
                "agent_id, fork_metadata "
                "FROM sessions WHERE origin = 'fork' AND fork_status IN "
                "('initializing', 'reverting')"
            )
            rows = await cursor.fetchall()
            return [
                {
                    "id": r[0],
                    "forked_from_session_id": r[1],
                    "working_dir": r[2],
                    "backend": r[3] or "claude-code",
                    "resume_id": r[4],
                    "fork_status": r[5],
                    "fork_revert_record": r[6],
                    "credential_id": r[7],
                    "agent_id": r[8],
                    "fork_metadata": r[9],
                }
                for r in rows
            ]

    async def update_session_field(self, session_id: str, **fields: Any) -> None:
        async with self.transaction(write=True) as tx:
            allowed = {
                "name",
                "working_dir",
                "claude_session_id",
                "credential_id",
                "archived",
                "agent_id",
                "origin",
                "backend",
                "model",
                "reasoning_effort",
                "service_tier",
                # Fork columns (session-rewind.md §4). fork_metadata is
                # nullable+clearable (set to None to clear after first turn);
                # the others are written across the §5.1 saga.
                "forked_from_session_id",
                "fork_after_seq",
                "fork_needs_replay",
                "fork_metadata",
                "fork_revert_record",
                "fork_status",
            }
            # Columns that must be writable to NULL. `fork_metadata` clears after
            # the fork's first result; `claude_session_id` must be clearable so a
            # HISTORY_REPLAY fork (Codex) can overwrite the pre-minted resume_id
            # hint with NULL post-prepare_fork — otherwise a restart before the
            # first turn reloads the bogus hint and spawns `codex resume <bogus>`
            # (Vera review BLOCKING #1). Other columns omitted by the caller are
            # left untouched. Model, effort and credential overrides clear to
            # NULL to restore inheritance.
            nullable = {"fork_metadata", "claude_session_id", "model", "reasoning_effort", "credential_id", "service_tier"}
            bool_fields = {"archived", "fork_needs_replay"}
            updates: dict[str, Any] = {}
            for k, v in fields.items():
                if k not in allowed:
                    continue
                if v is None and k not in nullable:
                    continue
                updates[k] = int(bool(v)) if k in bool_fields else v
            if not updates:
                return
            set_clause = ", ".join(f"{k} = ?" for k in updates)
            values = list(updates.values()) + [session_id]
            await tx.execute(
                f"UPDATE sessions SET {set_clause} WHERE id = ?",
                values,
            )

    async def count_active_sessions_for_agent(self, agent_id: str) -> int:
        async with self.transaction(write=False) as tx:
            return await self._count(tx,
                "SELECT COUNT(*) FROM sessions WHERE agent_id = ? AND archived = 0",
                (agent_id,),
            )

    async def count_sessions_for_agent(self, agent_id: str) -> int:
        async with self.transaction(write=False) as tx:
            return await self._count(tx,
                "SELECT COUNT(*) FROM sessions WHERE agent_id = ?",
                (agent_id,),
            )

    async def list_bg_tasks_for_session(
        self, session_id: str, *, limit: int = 200
    ) -> list[dict[str, Any]]:
        async with self.transaction(write=False) as tx:
            cursor = await tx.execute(
                f"SELECT {self._BG_TASK_COLS} FROM bg_tasks "
                "WHERE session_id = ? ORDER BY started_at DESC LIMIT ?",
                (session_id, limit),
            )
            rows = await cursor.fetchall()
            return [self._row_to_bg_task(r) for r in rows]

    async def list_research_jobs_for_session(
        self, session_id: str, *, limit: int = 200
    ) -> list[dict[str, Any]]:
        async with self.transaction(write=False) as tx:
            cursor = await tx.execute(
                f"SELECT {self._RESEARCH_COLS} FROM research_jobs "
                "WHERE session_id = ? ORDER BY created_at DESC LIMIT ?",
                (session_id, limit),
            )
            rows = await cursor.fetchall()
            return [self._row_to_research_job(r) for r in rows]

    async def get_applications_for_session(
        self, session_id: str
    ) -> list[dict[str, Any]]:
        """Every application whose build session is `session_id`. A list (not a
        single row) because nothing stops two applications from being built in
        the same conversation if a future flow wires it that way."""
        async with self.transaction(write=False) as tx:
            cursor = await tx.execute(
                f"SELECT {self._APPLICATION_COLS} FROM applications "
                "WHERE session_id = ?",
                (session_id,),
            )
            rows = await cursor.fetchall()
            return [self._row_to_application(r) for r in rows]
