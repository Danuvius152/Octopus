"""Per-page authorization sequences, retained until this service retires.

The database remains the only authorization list. A flow holds just identity,
one gate and its closed high-water mark, so confirmation can retire a delayed
PUT even when that request has not reached the server yet.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from .models import WorkspaceEditReceipt, WorkspaceInfo
from .users import UserError, _expired
from .workspace import normalise_root, paths_for

if TYPE_CHECKING:
    from .database import Database
    from .db.transaction import Transaction


@dataclass
class WorkspaceFlow:
    operator: str
    target: str
    closed: int = 0
    gate: asyncio.Lock = field(default_factory=asyncio.Lock)


def workspace_info(user_id: str, raw: str | None) -> WorkspaceInfo:
    try:
        roots = json.loads(raw) if raw else []
    except (json.JSONDecodeError, TypeError):
        roots = []
    return WorkspaceInfo(workspace=str(paths_for(user_id).workspace),
                         extra_roots=[str(root) for root in roots] if isinstance(roots, list) else [], confined=True)


class WorkspaceEdits:
    def __init__(self, db: Database, *, previous_writers_retired: bool) -> None:
        self.db = db
        self.generation = uuid.uuid4().hex
        self.previous_writers_retired = previous_writers_retired
        self.flows: dict[str, WorkspaceFlow] = {}

    def _flow(self, flow_id: str, operator: str, target: str, *, create: bool = False) -> WorkspaceFlow:
        flow = self.flows.get(flow_id)
        if flow is None:
            if not create:
                raise UserError("Unknown workspace editing flow", status_code=409)
            flow = self.flows[flow_id] = WorkspaceFlow(operator, target)
        if (flow.operator, flow.target) != (operator, target):
            raise UserError("Workspace editing flow belongs to another account", status_code=403)
        return flow

    async def _authorize(self, tx: Transaction, operator: str, token_hash: str, target: str) -> WorkspaceInfo:
        row = await tx.fetch_one(
            "SELECT u.is_admin, u.disabled_at, t.revoked_at, t.expires_at FROM users u "
            "JOIN auth_tokens t ON t.user_id=u.id WHERE u.id=? AND t.token_hash=?",
            (operator, token_hash),
        )
        if row is None or row[1] or row[2] or _expired(row[3]):
            raise UserError("Invalid token", status_code=401)
        if not row[0]:
            raise UserError("Admins only", status_code=403)
        target_row = await tx.fetch_one("SELECT extra_roots FROM users WHERE id=?", (target,))
        if target_row is None:
            raise UserError("No such account", status_code=404)
        return workspace_info(target, target_row[0])

    def _receipt(self, flow_id: str, flow: WorkspaceFlow, info: WorkspaceInfo, *, retired: bool = False) -> WorkspaceEditReceipt:
        return WorkspaceEditReceipt(flow_id=flow_id, service_generation=self.generation,
                                    sealed_through=flow.closed, previous_generation_retired=retired, workspace=info)

    async def open(self, operator: str, token_hash: str, target: str, flow_id: str) -> WorkspaceEditReceipt:
        # Registration is idempotent; a lost registration receipt has no PUT to replay.
        flow = self._flow(flow_id, operator, target, create=True)
        async with flow.gate:
            async with self.db.transaction(write=False) as tx:
                info = await self._authorize(tx, operator, token_hash, target)
            return self._receipt(flow_id, flow, info)

    async def replace(self, operator: str, token_hash: str, target: str, flow_id: str,
                      generation: str, sequence: int, roots: list[str]) -> WorkspaceInfo:
        if generation != self.generation:
            raise UserError("Workspace service generation has retired", status_code=409)
        flow = self._flow(flow_id, operator, target)
        normalized = list(dict.fromkeys(str(normalise_root(root)) for root in roots))
        async with flow.gate:
            if sequence <= flow.closed:
                raise UserError("Workspace write sequence is sealed", status_code=409)
            try:
                async with self.db.transaction(write=True) as tx:
                    await self._authorize(tx, operator, token_hash, target)
                    await tx.execute("UPDATE users SET extra_roots=? WHERE id=?", (json.dumps(normalized), target))
                return workspace_info(target, json.dumps(normalized))
            finally:
                # Database.transaction propagates cancellation only after the
                # original driver has acknowledged commit/rollback. Never
                # release this gate while that driver can still replace roots.
                flow.closed = max(flow.closed, sequence)

    async def confirm(self, operator: str, token_hash: str, target: str, flow_id: str,
                      generation: str, sequence: int) -> WorkspaceEditReceipt:
        retired = generation != self.generation
        if retired and not self.previous_writers_retired:
            raise UserError("Previous workspace writer retirement is not established; retry confirmation", status_code=409)
        flow = self._flow(flow_id, operator, target, create=retired)
        async with flow.gate:
            async with self.db.transaction(write=False) as tx:
                info = await self._authorize(tx, operator, token_hash, target)
            # No await between closing the original sequence and constructing
            # the receipt; the gate excludes every same-flow PUT throughout.
            flow.closed = max(flow.closed, sequence)
            return self._receipt(flow_id, flow, info, retired=retired)
