"""AgentManager — stateless CRUD + business rules over the `agents` table.

Agents are the durable definition of an assistant (agent-refactor.md §5.1):
pure DB rows, no in-memory subprocess. This layer enforces name uniqueness,
`is_system` protection, and the delete/archive guards for the routes.
SessionManager reads agent rows directly through the Database at spawn time
(so editing an agent affects its open sessions on their next turn); it does
not go through this manager.
"""

from __future__ import annotations

import asyncio
import sqlite3
import uuid
from datetime import UTC, datetime
from functools import wraps
from typing import Any

from . import agent_memory
from .aio import complete_cleanup
from .config import settings
from .credential_bindings import (
    CredentialBindingError,
    validate_credential_binding,
    validate_failed_credential_write,
)
from .database import Database
from .harness import RunConfig, get_harness, model_settings


def _validate_runtime_configuration(backend: str, fields: dict[str, Any]) -> None:
    harness = get_harness(backend)
    allow, deny = fields.get("tool_allow"), fields.get("tool_deny")
    harness.profile.tool_policy.validate(backend, allow, deny)
    harness.validate_configuration(RunConfig(
        transport=settings.codex_transport if backend == "codex" else None,
        system_prompt=fields.get("system_prompt"), model=fields.get("model"),
        reasoning_effort=fields.get("reasoning_effort"), service_tier=fields.get("service_tier"),
        subagents=fields.get("subagents") or [], mcp_servers=[],
        tool_allow=[line.strip() for line in (allow or "").splitlines() if line.strip()],
        tool_deny=[line.strip() for line in (deny or "").splitlines() if line.strip()],
    ))


class AgentError(Exception):
    """Agent business-rule violation. Routes map this to a 400/409."""


def _memory_mutation(operation):
    """Own create, restore and deletion through their database and file effects."""
    @wraps(operation)
    async def mutate(self, *args, **kwargs):
        async def command():
            self.db.health.require_healthy()
            try:
                # Refuse a competing memory operation before either mutates
                # the same agent, including a released lock with a waiter.
                async with asyncio.timeout(0):
                    await self.db._agent_memory_lock.acquire()
            except TimeoutError as error:
                raise AgentError("Agent memory is busy with another memory operation; retry") from error
            try:
                return await operation(self, *args, **kwargs)
            finally:
                self.db._agent_memory_lock.release()

        return await complete_cleanup(command())

    return mutate


class AgentManager:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def _validate_controls(self, backend: str, fields: dict[str, Any], user_id: str | None) -> None:
        if fields.get("reasoning_effort") is None and fields.get("service_tier") is None:
            return
        catalog = await model_settings.model_settings_for(self.db, backend, fields.get("credential_id"), user_id)
        try:
            model_settings.validate_controls(catalog, fields.get("model"), fields.get("reasoning_effort"), fields.get("service_tier"))
        except model_settings.ModelControlError as error:
            raise AgentError(error.describe(backend, inherited=True)) from error


    async def list_agents(
        self, *, include_archived: bool = False, user_id: str | None = None
    ) -> list[dict[str, Any]]:
        return await self.db.load_agents(
            include_archived=include_archived, user_id=user_id
        )

    async def get_agent(
        self, agent_id: str, user_id: str | None = None
    ) -> dict[str, Any] | None:
        """One agent, scoped when a scope is given. `None` covers "belongs to
        someone else" as well as "does not exist", which a caller must not be
        able to tell apart."""
        return await self.db.get_agent(agent_id, user_id=user_id)

    async def get_default_agent(
        self, user_id: str | None = None
    ) -> dict[str, Any] | None:
        """This account's protected Default Agent (is_system=1)."""
        return await self.db.get_system_agent(user_id)

    @_memory_mutation
    async def create_agent(
        self,
        *,
        name: str,
        description: str = "",
        avatar: str | None = None,
        system_prompt: str = "",
        model: str | None = None,
        reasoning_effort: str | None = None,
        service_tier: str | None = None,
        credential_id: str | None = None,
        user_id: str | None = None,
        backend: str | None = None,
        mcp_servers: list[str] | None = None,
        tool_allow: str = "",
        tool_deny: str = "",
        subagents: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        name = (name or "").strip()
        if not name:
            raise AgentError("Agent name is required")
        if await self.db.get_agent_by_name(name, user_id=user_id) is not None:
            raise AgentError(f"An agent named {name!r} already exists")
        backend = backend or settings.default_backend
        credential_id = credential_id or None
        try:
            _validate_runtime_configuration(backend, {
                "system_prompt": system_prompt, "model": model, "reasoning_effort": reasoning_effort,
                "service_tier": service_tier, "subagents": subagents, "tool_allow": tool_allow, "tool_deny": tool_deny,
            })
            await validate_credential_binding(self.db, credential_id, backend, user_id)
            await self._validate_controls(backend, {
                "model": model, "reasoning_effort": reasoning_effort,
                "service_tier": service_tier, "credential_id": credential_id,
            }, user_id)
        except ValueError as exc:
            raise AgentError(str(exc)) from exc
        agent_id = uuid.uuid4().hex[:12]
        now = datetime.now(UTC).isoformat()
        try:
            await self.db.save_agent(
                agent_id=agent_id,
                name=name,
                created_at=now,
                updated_at=now,
                description=description,
                avatar=avatar,
                system_prompt=system_prompt,
                model=model,
                reasoning_effort=reasoning_effort,
                service_tier=service_tier,
                credential_id=credential_id,
                backend=backend,
                mcp_servers=mcp_servers,
                tool_allow=tool_allow,
                tool_deny=tool_deny,
                subagents=subagents,
                is_system=False,
                user_id=user_id,
            )
        except sqlite3.IntegrityError as exc:
            try:
                await validate_failed_credential_write(
                    self.db, exc, credential_id, backend, user_id
                )
            except CredentialBindingError as binding_error:
                raise AgentError(str(binding_error)) from exc
            raise
        agent = await self.db.get_agent(agent_id)
        assert agent is not None
        # Provision the agent's canonical memory/ dir up front; also ensured
        # lazily per turn. Under the owner's root (multi-tenancy.md §6), so two
        # accounts' agents cannot read each other's memory.
        agent_memory.ensure_agent_dirs(agent_id, user_id)
        return agent

    async def update_agent(
        self, agent_id: str, user_id: str | None = None, **fields: Any
    ) -> dict[str, Any]:
        async def save() -> dict[str, Any]:
            async with self.db.configuration_lock:
                return await self._update_agent(agent_id, user_id, **fields)

        return await complete_cleanup(save())

    async def _update_agent(
        self, agent_id: str, user_id: str | None, **fields: Any,
    ) -> dict[str, Any]:
        agent = await self.db.get_agent(agent_id, user_id=user_id)
        if agent is None:
            raise AgentError("Agent not found")
        fields = self.db.normalize_agent_update(fields)
        if "credential_id" in fields:
            fields["credential_id"] = fields["credential_id"] or None
        credential_id = fields.get("credential_id", agent.get("credential_id"))
        backend = fields.get("backend")
        if backend is None:
            backend = agent["backend"]
        owner_id = agent.get("user_id")
        try:
            _validate_runtime_configuration(backend, {**agent, **fields})
            await validate_credential_binding(
                self.db, credential_id, backend, owner_id
            )
            if {"model", "reasoning_effort", "service_tier", "backend", "credential_id"} & fields.keys():
                await self._validate_controls(backend, {**agent, **fields}, owner_id)
        except ValueError as exc:
            raise AgentError(str(exc)) from exc
        if "name" in fields and fields["name"] is not None:
            new_name = fields["name"].strip()
            if not new_name:
                raise AgentError("Agent name cannot be empty")
            clash = await self.db.get_agent_by_name(
                new_name, user_id=agent.get("user_id")
            )
            if clash is not None and clash["id"] != agent_id:
                raise AgentError(f"An agent named {new_name!r} already exists")
            fields["name"] = new_name
        try:
            await self.db.update_agent(agent_id, **fields)
        except sqlite3.IntegrityError as exc:
            try:
                await validate_failed_credential_write(
                    self.db, exc, credential_id, backend, owner_id
                )
            except CredentialBindingError as binding_error:
                raise AgentError(str(binding_error)) from exc
            raise
        updated = await self.db.get_agent(agent_id)
        assert updated is not None
        return updated

    async def archive_agent(self, agent_id: str) -> None:
        agent = await self.db.get_agent(agent_id)
        if agent is None:
            raise AgentError("Agent not found")
        if agent["is_system"]:
            raise AgentError("The Default Agent cannot be archived")
        await self.db.archive_agent(agent_id)

    @_memory_mutation
    async def unarchive_agent(
        self, agent_id: str, user_id: str | None = None
    ) -> dict[str, Any]:
        """Bring an archived agent back. Refuses if a live agent has taken the
        name in the meantime — the unique index only covers live rows, so two
        live agents could otherwise share one."""
        agent = await self.db.get_agent(agent_id, user_id=user_id)
        if agent is None:
            raise AgentError("Agent not found")
        if not agent["archived"]:
            return agent
        clash = await self.db.get_agent_by_name(
            agent["name"], user_id=agent.get("user_id")
        )
        if clash is not None and clash["id"] != agent_id:
            raise AgentError(
                f"An agent named {agent['name']!r} already exists — rename it "
                f"before restoring this one"
            )
        await self.db.unarchive_agent(agent_id)
        # Restoring is choosing to use it again: it comes back to the sidebar
        # (where it was, if it was pinned when it left).
        await self.db.set_agent_pinned(agent_id, True)
        agent_memory.ensure_agent_dirs(agent_id, agent.get("user_id"))
        restored = await self.db.get_agent(agent_id)
        assert restored is not None
        return restored

    async def set_pinned(
        self, agent_id: str, pinned: bool, user_id: str | None = None
    ) -> dict[str, Any]:
        """Pin an agent to the sidebar or unpin it (sidebar-pins.md).

        Only where it is *shown* changes: an unpinned agent is still listed
        on the Agents page, still reachable by delegation, and its schedules
        still fire. The Default Agent stays pinned — it is where a fresh
        session goes when nothing else is chosen, so it can't go missing.
        """
        agent = await self.db.get_agent(agent_id, user_id=user_id)
        if agent is None:
            raise AgentError("Agent not found")
        if agent["archived"]:
            raise AgentError("Restore the agent before pinning it")
        if agent["is_system"] and not pinned:
            raise AgentError("The Default Agent is always pinned")
        await self.db.set_agent_pinned(agent_id, pinned)
        updated = await self.db.get_agent(agent_id)
        assert updated is not None
        return updated

    async def reorder_pins(
        self, ordered_ids: list[str], user_id: str | None = None
    ) -> list[dict[str, Any]]:
        """Set the sidebar order of the pinned agents; returns every live
        agent. Every id must name a pinned, live agent of this account's,
        once — an id from another account is "not a pinned agent" rather than
        a row this reorders."""
        if len(set(ordered_ids)) != len(ordered_ids):
            raise AgentError("An agent appears twice in the order")
        pinned = {
            a["id"]
            for a in await self.db.load_agents(user_id=user_id)
            if a["pinned"]
        }
        stray = [i for i in ordered_ids if i not in pinned]
        if stray:
            raise AgentError(f"Not a pinned agent: {', '.join(stray)}")
        await self.db.reorder_agent_pins(ordered_ids)
        return await self.db.load_agents(user_id=user_id)

    @_memory_mutation
    async def delete_agent(self, agent_id: str) -> None:
        agent = await self.db.get_agent(agent_id)
        if agent is None:
            raise AgentError("Agent not found")
        if agent["is_system"]:
            raise AgentError("The Default Agent cannot be deleted")
        if await self.db.count_sessions_for_agent(agent_id) > 0:
            raise AgentError(
                "Agent still has sessions; archive it instead of deleting"
            )
        owner_id = agent.get("user_id")
        await self.db.delete_agent(agent_id)
        # Hard delete also removes the agent's memory dir. Archiving keeps it,
        # mirroring archived-session history.
        agent_memory.remove_agent_dir(agent_id, owner_id)
