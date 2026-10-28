"""Agents — the durable assistant definitions that own everything else.

One slice of the persistence layer. These were methods on a single 2,600-line
`Database` class; splitting them by domain (§3 A4) keeps each file about one
subject, while `Database` still presents them as one object so no call site
changed.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import UTC, datetime
from typing import Any

from ..config import settings
from .base import DatabaseBase, _load_json_list
from .schema import _DEFAULT_MCP_SERVERS
from .transaction import Transaction


async def create_default_agent(tx: Transaction, *, user_id: str, created_at: str) -> None:
    """The account transaction also owns its initial protected definition."""
    await tx.execute(
        "INSERT INTO agents (id,user_id,name,is_system,mcp_servers,backend,created_at,updated_at,pin_order) "
        "VALUES (?,?, 'Octo',1,?,?,?,?, "
        "(SELECT COALESCE(MAX(pin_order),0)+1 FROM agents))",
        (uuid.uuid4().hex[:12], user_id, json.dumps(_DEFAULT_MCP_SERVERS),
         settings.default_backend, created_at, created_at),
    )


class AgentsMixin(DatabaseBase):
    # --- agent-scoped enablement join -------------------------------------

    async def set_agent_connector(
        self, agent_id: str, installation_id: str, enabled: bool
    ) -> None:
        """Toggle one connector for one agent (presence in the join = on)."""
        async with self.transaction(write=True) as tx:
            if enabled:
                await tx.execute(
                    "INSERT OR IGNORE INTO agent_connectors "
                    "(agent_id, installation_id) VALUES (?, ?)",
                    (agent_id, installation_id),
                )
            else:
                await tx.execute(
                    "DELETE FROM agent_connectors "
                    "WHERE agent_id = ? AND installation_id = ?",
                    (agent_id, installation_id),
                )

    async def get_agent_connector_ids(self, agent_id: str) -> list[str]:
        """Installation ids enabled for an agent."""
        async with self.transaction(write=False) as tx:
            cursor = await tx.execute(
                "SELECT installation_id FROM agent_connectors WHERE agent_id = ?",
                (agent_id,),
            )
            rows = await cursor.fetchall()
            return [row[0] for row in rows]

    async def replace_agent_connectors(
        self, agent_id: str, installation_ids: list[str], user_id: str | None,
    ) -> list[str]:
        """Validate and replace one complete selection in a single commit."""
        target = sorted(set(installation_ids))
        async with self.transaction(write=True) as tx:
            agent = await tx.fetch_one("SELECT user_id FROM agents WHERE id = ?", (agent_id,))
            if agent is None or agent[0] != user_id:
                raise ValueError("agent not found")
            for iid in target:
                installation = await tx.fetch_one("SELECT user_id FROM connector_installations WHERE id = ?", (iid,))
                if installation is None or installation[0] != user_id:
                    raise ValueError("connector installation not found")
            await tx.execute("DELETE FROM agent_connectors WHERE agent_id = ?", (agent_id,))
            for iid in target:
                await tx.execute("INSERT INTO agent_connectors (agent_id, installation_id) VALUES (?, ?)", (agent_id, iid))
        return target

    async def get_enabled_connectors_for_agent(
        self, agent_id: str
    ) -> list[dict[str, Any]]:
        """Full installation rows for an agent's enabled connectors — the
        join SessionManager reads at spawn time to build the MCP set."""
        async with self.transaction(write=False) as tx:
            cols = self._CONNECTOR_COLS.sql("ci")
            cursor = await tx.execute(
                f"SELECT {cols} FROM connector_installations ci "
                "JOIN agent_connectors ac ON ac.installation_id = ci.id "
                "WHERE ac.agent_id = ? ORDER BY ci.created_at",
                (agent_id,),
            )
            rows = await cursor.fetchall()
            return [self._row_to_connector(row) for row in rows]

    def _row_to_agent(self, row: sqlite3.Row) -> dict[str, Any]:
        agent = self._AGENT_COLS.read(row)
        try:
            agent["mcp_servers"] = json.loads(agent["mcp_servers"]) if agent["mcp_servers"] else []
        except (json.JSONDecodeError, TypeError):
            agent["mcp_servers"] = []
        for field in ("description", "system_prompt", "tool_allow", "tool_deny"):
            agent[field] = agent[field] or ""
        for field in ("is_system", "archived", "pinned"):
            agent[field] = bool(agent[field])
        agent["backend"] = agent["backend"] or "claude-code"
        agent["subagents"] = _load_json_list(agent["subagents"])
        return agent

    async def save_agent(
        self,
        *,
        agent_id: str,
        name: str,
        created_at: str,
        updated_at: str,
        description: str = "",
        avatar: str | None = None,
        system_prompt: str = "",
        model: str | None = None,
        reasoning_effort: str | None = None,
        service_tier: str | None = None,
        credential_id: str | None = None,
        backend: str | None = None,
        mcp_servers: list[str] | None = None,
        tool_allow: str = "",
        tool_deny: str = "",
        is_system: bool = False,
        subagents: list[dict[str, Any]] | None = None,
        user_id: str | None = None,
    ) -> None:
        async with self.transaction(write=True) as tx:
            servers_json = json.dumps(
                mcp_servers if mcp_servers is not None else _DEFAULT_MCP_SERVERS
            )
            await tx.execute(
                "INSERT INTO agents "
                "(id, name, description, avatar, system_prompt, model, "
                " credential_id, backend, mcp_servers, tool_allow, tool_deny, "
                " is_system, archived, created_at, updated_at, subagents, user_id, "
                " reasoning_effort, service_tier, pinned, pin_order) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, 1, "
                f" {self._NEXT_PIN_ORDER.format(table='agents')})",
                (
                    agent_id, name, description, avatar, system_prompt, model,
                    credential_id, backend or settings.default_backend, servers_json,
                    tool_allow, tool_deny, int(bool(is_system)),
                    created_at, updated_at, json.dumps(subagents or []), user_id, reasoning_effort,
                    service_tier,
                ),
            )

    async def load_agents(
        self, *, include_archived: bool = False, user_id: str | None = None
    ) -> list[dict[str, Any]]:
        """Agents, optionally only one account's (multi-tenancy.md §5).

        `user_id=None` means "no scoping asked for" — the boot-time backfills
        want every row. A *request* passes one, and so does the delegation
        manager, which resolves a target agent by name within the delegating
        session's own account (§7): a name is not a way to reach into another
        account and run a turn there.
        """
        async with self.transaction(write=False) as tx:
            query = (
                f"SELECT {self._AGENT_COLS} FROM agents a"
            )
            where: list[str] = []
            params: list[Any] = []
            if not include_archived:
                where.append("a.archived = 0")
            if user_id is not None:
                where.append("a.user_id = ?")
                params.append(user_id)
            if where:
                query += " WHERE " + " AND ".join(where)
            query += " ORDER BY a.is_system DESC, a.created_at"
            cursor = await tx.execute(query, params)
            rows = await cursor.fetchall()
            return [self._row_to_agent(row) for row in rows]

    async def get_agent(
        self, agent_id: str, user_id: str | None = None
    ) -> dict[str, Any] | None:
        """One agent, or None — including when it exists and belongs to someone
        else, which a caller must not be able to tell from "no such agent"."""
        async with self.transaction(write=False) as tx:
            sql = f"SELECT {self._AGENT_COLS} FROM agents a WHERE a.id = ?"
            params: list[Any] = [agent_id]
            if user_id is not None:
                sql += " AND a.user_id = ?"
                params.append(user_id)
            cursor = await tx.execute(sql, params)
            row = await cursor.fetchone()
            return self._row_to_agent(row) if row else None

    async def get_agent_by_name(
        self,
        name: str,
        *,
        include_archived: bool = False,
        user_id: str | None = None,
    ) -> dict[str, Any] | None:
        """One agent by name, within an account.

        Scoped because the uniqueness it guards is per account (see the
        `agents_name_unique` index): unscoped, the second person to want an
        agent called "Octo" would be told somebody else already had one.
        """
        async with self.transaction(write=False) as tx:
            query = (
                f"SELECT {self._AGENT_COLS} FROM agents a "
                "WHERE a.name = ?"
            )
            params: list[Any] = [name]
            if not include_archived:
                query += " AND a.archived = 0"
            if user_id is not None:
                query += " AND a.user_id = ?"
                params.append(user_id)
            cursor = await tx.execute(query, params)
            row = await cursor.fetchone()
            return self._row_to_agent(row) if row else None

    async def get_system_agent(
        self, user_id: str | None = None
    ) -> dict[str, Any] | None:
        """This account's protected Default Agent (is_system=1).

        There is one per account, not one per box (multi-tenancy.md §5): it is
        where a fresh conversation goes when nobody named an agent, so an
        account without one has nowhere to start — and an *unscoped* answer
        would put that conversation in the first account that ever existed.
        The pre-accounts install has exactly one, created by the migration, and
        `user_id=None` still finds it.
        """
        async with self.transaction(write=False) as tx:
            sql = (
                f"SELECT {self._AGENT_COLS} FROM agents a "
                "WHERE a.is_system = 1"
            )
            params: list[Any] = []
            if user_id is not None:
                sql += " AND a.user_id = ?"
                params.append(user_id)
            cursor = await tx.execute(sql + " LIMIT 1", params)
            row = await cursor.fetchone()
            return self._row_to_agent(row) if row else None

    @staticmethod
    def normalize_agent_update(fields: dict[str, Any]) -> dict[str, Any]:
        """The effective patch, shared by validation and persistence."""
        allowed = {
            "name", "description", "avatar", "system_prompt", "model",
            "reasoning_effort", "service_tier", "credential_id", "backend",
            "mcp_servers", "tool_allow", "tool_deny", "subagents", "archived",
        }
        nullable = {"credential_id", "model", "avatar", "reasoning_effort", "service_tier"}
        return {k: v for k, v in fields.items() if k in allowed and (v is not None or k in nullable)}

    async def update_agent(self, agent_id: str, **fields: Any) -> None:
        fields = self.normalize_agent_update(fields)
        async with self.transaction(write=True) as tx:
            updates: dict[str, Any] = {}
            for k, v in fields.items():
                if k in ("mcp_servers", "subagents"):
                    updates[k] = json.dumps(v if v is not None else [])
                elif k == "archived":
                    updates[k] = int(bool(v))
                else:
                    updates[k] = v
            if not updates:
                return
            updates["updated_at"] = datetime.now(UTC).isoformat()
            set_clause = ", ".join(f"{k} = ?" for k in updates)
            values = list(updates.values()) + [agent_id]
            await tx.execute(
                f"UPDATE agents SET {set_clause} WHERE id = ?", values
            )

    async def archive_agent(self, agent_id: str) -> None:
        """Soft-delete an agent and cascade-archive its sessions."""
        async with self.transaction(write=True) as tx:
            await tx.execute(
                "UPDATE agents SET archived = 1, updated_at = ? WHERE id = ?",
                (datetime.now(UTC).isoformat(), agent_id),
            )
            await tx.execute(
                "UPDATE sessions SET archived = 1 WHERE agent_id = ?",
                (agent_id,),
            )

    async def unarchive_agent(self, agent_id: str) -> None:
        """Restore an archived agent. Its sessions stay archived: they were
        archived as a cascade, and silently reviving a dozen old threads is
        not what "restore this agent" means — the archived-sessions page is
        where a session comes back from."""
        async with self.transaction(write=True) as tx:
            await tx.execute(
                "UPDATE agents SET archived = 0, updated_at = ? WHERE id = ?",
                (datetime.now(UTC).isoformat(), agent_id),
            )

    async def set_agent_pinned(self, agent_id: str, pinned: bool) -> None:
        """Put the agent in the sidebar (at the bottom) or take it out."""
        async with self.transaction(write=True) as tx:
            await self._set_pinned(tx, "agents", agent_id, pinned)

    async def reorder_agent_pins(self, ordered_ids: list[str]) -> None:
        """The sidebar order of the pinned agents — see `_reorder_pins`."""
        async with self.transaction(write=True) as tx:
            await self._reorder_pins(tx, "agents", ordered_ids)

    async def delete_agent(self, agent_id: str) -> bool:
        """Hard-delete an agent. FK ON DELETE CASCADE removes its sessions,
        schedules — guarded by AgentManager so this is
        only reached when the agent has no sessions."""
        async with self.transaction(write=True) as tx:
            cursor = await tx.execute(
                "DELETE FROM agents WHERE id = ?", (agent_id,)
            )
            return cursor.rowcount > 0
