"""Agents — the durable assistant definitions that own everything else.

One slice of the persistence layer. These were methods on a single 2,600-line
`Database` class; splitting them by domain (§3 A4) keeps each file about one
subject, while `Database` still presents them as one object so no call site
changed.
"""

from __future__ import annotations

import logging
import sqlite3
import uuid
from datetime import UTC, datetime
from typing import Any

from ..agent_configuration import (
    AGENT_CONFIGURATION_FIELDS,
    AgentConfigurationError,
    decode_agent_configuration,
    encode_agent_configuration,
)
from ..config import settings
from .base import DatabaseBase
from .schema import _DEFAULT_MCP_SERVERS
from .transaction import Transaction

logger = logging.getLogger(__name__)


async def _insert_agent(tx: Transaction, agent_id: str, fields: dict[str, Any]) -> None:
    fields = encode_agent_configuration(agent_id, fields)
    columns = ", ".join(fields)
    placeholders = ", ".join("?" for _ in fields)
    await tx.execute(
        f"INSERT INTO agents (id, {columns}, pin_order) VALUES (?, {placeholders}, "
        f"{DatabaseBase._NEXT_PIN_ORDER.format(table='agents')})",
        (agent_id, *fields.values()),
    )


async def create_default_agent(tx: Transaction, *, user_id: str, created_at: str) -> None:
    """The account transaction also owns its initial protected definition."""
    await _insert_agent(tx, uuid.uuid4().hex[:12], {
        "user_id": user_id, "name": "Octo", "is_system": 1,
        "description": "", "avatar": None, "system_prompt": "", "model": None,
        "reasoning_effort": None, "service_tier": None, "credential_id": None,
        "mcp_servers": _DEFAULT_MCP_SERVERS, "subagents": [], "backend": settings.default_backend,
        "tool_allow": "", "tool_deny": "", "created_at": created_at, "updated_at": created_at,
    })


async def _require_connector_agent(tx: Transaction, agent_id: str, user_id: str | None) -> None:
    from .users import owner_is_available

    agent = await tx.fetch_one("SELECT user_id FROM agents WHERE id = ?", (agent_id,))
    if agent is None or agent[0] != user_id or not await owner_is_available(tx, user_id):
        raise ValueError("agent not found")


async def _require_connector_binding_scope(
    tx: Transaction, agent_id: str, installation_id: str, user_id: str | None,
) -> None:
    await _require_connector_agent(tx, agent_id, user_id)
    installation = await tx.fetch_one(
        "SELECT user_id FROM connector_installations WHERE id = ?", (installation_id,),
    )
    if installation is None or installation[0] != user_id:
        raise ValueError("connector installation not found")


class AgentsMixin(DatabaseBase):
    # --- agent-scoped enablement join -------------------------------------

    async def read_owned_agent_connector(
        self, agent_id: str, installation_id: str, user_id: str | None,
    ) -> bool:
        async with self.transaction(write=False) as tx:
            await _require_connector_binding_scope(tx, agent_id, installation_id, user_id)
            row = await tx.fetch_one(
                "SELECT 1 FROM agent_connectors WHERE agent_id = ? AND installation_id = ?",
                (agent_id, installation_id),
            )
            return row is not None

    async def set_owned_agent_connector(
        self, agent_id: str, installation_id: str, enabled: bool, user_id: str | None,
    ) -> None:
        async with self.transaction(write=True) as tx:
            await _require_connector_binding_scope(tx, agent_id, installation_id, user_id)
            if enabled:
                await tx.execute(
                    "INSERT OR IGNORE INTO agent_connectors (agent_id, installation_id) VALUES (?, ?)",
                    (agent_id, installation_id),
                )
            else:
                await tx.execute(
                    "DELETE FROM agent_connectors WHERE agent_id = ? AND installation_id = ?",
                    (agent_id, installation_id),
                )

    async def list_owned_agent_connectors(self, agent_id: str, user_id: str | None) -> list[str]:
        async with self.transaction(write=False) as tx:
            await _require_connector_agent(tx, agent_id, user_id)
            rows = await tx.fetch_all(
                "SELECT installation_id FROM agent_connectors WHERE agent_id = ?", (agent_id,),
            )
            return [row[0] for row in rows]

    async def replace_agent_connectors(
        self, agent_id: str, installation_ids: list[str], user_id: str | None,
    ) -> list[str]:
        """Validate and replace one complete selection in a single commit."""
        target = sorted(set(installation_ids))
        async with self.transaction(write=True) as tx:
            await _require_connector_agent(tx, agent_id, user_id)
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
        agent.update(decode_agent_configuration(agent["id"], {
            field: agent[field] for field in AGENT_CONFIGURATION_FIELDS
        }))
        for field in ("is_system", "archived", "pinned"):
            agent[field] = bool(agent[field])
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
            await _insert_agent(tx, agent_id, {
                "name": name, "description": description, "avatar": avatar,
                "system_prompt": system_prompt, "model": model, "credential_id": credential_id,
                "backend": settings.default_backend if backend is None else backend,
                "mcp_servers": _DEFAULT_MCP_SERVERS if mcp_servers is None else mcp_servers,
                "tool_allow": tool_allow, "tool_deny": tool_deny, "is_system": int(is_system),
                "created_at": created_at, "updated_at": updated_at,
                "subagents": [] if subagents is None else subagents, "user_id": user_id,
                "reasoning_effort": reasoning_effort, "service_tier": service_tier,
            })

    async def load_agents(
        self, *, include_archived: bool = False, user_id: str | None = None
    ) -> list[dict[str, Any]]:
        rows = await self._load_agent_rows(include_archived=include_archived, user_id=user_id)
        return [self._row_to_agent(row) for row in rows]

    async def load_agents_for_display(
        self, *, include_archived: bool = False, user_id: str | None = None
    ) -> list[dict[str, Any]]:
        rows = await self._load_agent_rows(include_archived=include_archived, user_id=user_id)
        return self._agents_for_display(rows)

    def _agents_for_display(self, rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
        agents = []
        for row in rows:
            try:
                agents.append(self._row_to_agent(row))
            except AgentConfigurationError as exc:
                logger.warning("%s", exc)
        return agents

    async def _load_agent_rows(
        self, *, include_archived: bool, user_id: str | None
    ) -> list[sqlite3.Row]:
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
            return list(await tx.fetch_all(query, params))

    async def get_agent(
        self, agent_id: str, user_id: str | None = None
    ) -> dict[str, Any] | None:
        """One agent, or None — including when it exists and belongs to someone
        else, which a caller must not be able to tell from "no such agent"."""
        row = await self._get_agent_row(agent_id, user_id, str(self._AGENT_COLS))
        return self._row_to_agent(row) if row is not None else None

    async def _get_agent_row(
        self, agent_id: str, user_id: str | None, columns: str, *, match_owner: bool = False,
    ) -> sqlite3.Row | None:
        async with self.transaction(write=False) as tx:
            sql = f"SELECT {columns} FROM agents a WHERE a.id = ?"
            params: list[Any] = [agent_id]
            if match_owner or user_id is not None:
                sql += " AND a.user_id IS ?"
                params.append(user_id)
            return await tx.fetch_one(sql, params)

    async def get_live_agent_configuration(self, agent_id: str, user_id: str | None) -> dict[str, Any] | None:
        async with self.transaction(write=False) as tx:
            row = await tx.fetch_one(
                f"SELECT {self._AGENT_COLS} FROM agents a WHERE a.id = ? AND a.user_id IS ? AND a.archived = 0",
                (agent_id, user_id),
            )
        return self._row_to_agent(row) if row is not None else None

    async def get_agent_identity(self, agent_id: str, user_id: str | None = None) -> dict[str, Any] | None:
        fields = ("id", "name", "user_id", "archived", "is_system")
        row = await self._get_agent_row(agent_id, user_id, ", ".join(fields))
        return dict(zip(fields, row, strict=True)) if row is not None else None

    async def get_agent_context_fields(self, agent_id: str, user_id: str | None) -> dict[str, Any] | None:
        fields = ("model", "credential_id", "context_settings_revision")
        row = await self._get_agent_row(agent_id, user_id, ", ".join(fields), match_owner=True)
        return dict(zip(fields, row, strict=True)) if row is not None else None

    async def get_agent_credential_id(self, agent_id: str, user_id: str | None) -> str | None:
        row = await self._get_agent_row(agent_id, user_id, "credential_id", match_owner=True)
        return decode_agent_configuration(agent_id, {"credential_id": row[0]})["credential_id"] if row else None

    async def find_live_agent_id(self, name: str, user_id: str | None) -> str | None:
        async with self.transaction(write=False) as tx:
            row = await tx.fetch_one(
                "SELECT id FROM agents WHERE name = ? AND user_id IS ? AND archived = 0", (name, user_id),
            )
            return row[0] if row else None

    async def list_live_agent_directory(self, user_id: str | None) -> list[dict[str, Any]]:
        fields = ("name", "description", "avatar", "is_system")
        async with self.transaction(write=False) as tx:
            rows = await tx.fetch_all(
                f"SELECT {', '.join(fields)} FROM agents WHERE user_id IS ? AND archived = 0 "
                "ORDER BY is_system DESC, created_at", (user_id,),
            )
        return [dict(zip(fields, row, strict=True)) for row in rows]

    async def list_account_agent_ids(self, user_id: str) -> list[str]:
        async with self.transaction(write=False) as tx:
            rows = await tx.fetch_all("SELECT id FROM agents WHERE user_id = ?", (user_id,))
        return [row[0] for row in rows]

    async def match_live_agents(self, name: str, user_id: str | None) -> list[tuple[str, str]]:
        async with self.transaction(write=False) as tx:
            rows = await tx.fetch_all(
                "SELECT id, name FROM agents WHERE user_id IS ? AND archived = 0 ORDER BY is_system DESC, created_at",
                (user_id,),
            )
        # SQLite's built-in lower() only folds ASCII. Keep the public name rules.
        wanted = name.strip().lower()
        return [(agent_id, label) for agent_id, label in rows if isinstance(label, str) and label.lower() == wanted]

    async def resolve_app_agent_identity(self, agent_id: str | None, user_id: str | None) -> tuple[str, str] | None:
        async with self.transaction(write=False) as tx:
            row = await tx.fetch_one(
                "SELECT id, name FROM agents WHERE user_id IS ? AND archived = 0 AND (id = ? OR is_system = 1) "
                "ORDER BY (id IS ?) DESC, created_at LIMIT 1", (user_id, agent_id, agent_id),
            )
            return (row[0], row[1]) if row else None

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
        self.health.require_healthy()
        fields = self.normalize_agent_update(fields)
        if not fields:
            return
        updates = encode_agent_configuration(agent_id, fields)
        if "archived" in updates:
            updates["archived"] = int(bool(updates["archived"]))
        async with self.transaction(write=True) as tx:
            context_fields = ("backend", "model", "credential_id")
            columns = ", ".join(AGENT_CONFIGURATION_FIELDS)
            row = await tx.fetch_one(f"SELECT {columns} FROM agents WHERE id = ?", (agent_id,))
            if row is None:
                return
            previous = dict(zip(AGENT_CONFIGURATION_FIELDS, row, strict=True))
            decode_agent_configuration(agent_id, {key: value for key, value in previous.items() if key not in updates})
            if any(key in updates and (updates[key] or None) != (previous[key] or None)
                   for key in context_fields):
                await tx.execute("UPDATE agents SET context_settings_revision=context_settings_revision+1 WHERE id=?", (agent_id,))
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

    async def reorder_agent_pins(
        self, ordered_ids: list[str], *, user_id: str | None = None,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Validate membership and commit the sidebar order together."""
        async with self.transaction(write=True) as tx:
            current = await self._ordered_pins(tx, "agents", user_id=user_id)
            stray = [identifier for identifier in ordered_ids if identifier not in current]
            if stray:
                raise ValueError(f"Not a pinned agent: {', '.join(stray)}")
            await self._reorder_pins(tx, "agents", ordered_ids, current, user_id=user_id)
            scope = " AND a.user_id = ?" if user_id is not None else ""
            rows = await tx.fetch_all(
                f"SELECT {self._AGENT_COLS} FROM agents a WHERE a.archived = 0{scope} "
                "ORDER BY a.is_system DESC, a.created_at",
                () if user_id is None else (user_id,),
            )
        agents = self._agents_for_display(rows)
        return agents, [a for a in agents if a["id"] in current and a["pin_order"] != current[a["id"]]]

    async def delete_agent(self, agent_id: str) -> bool:
        """Hard-delete an agent. FK ON DELETE CASCADE removes its sessions,
        schedules — guarded by AgentManager so this is
        only reached when the agent has no sessions."""
        async with self.transaction(write=True) as tx:
            cursor = await tx.execute(
                "DELETE FROM agents WHERE id = ?", (agent_id,)
            )
            return cursor.rowcount > 0
