"""The verified relationship a schedule may use, shared by routes and timers."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .database import Database


class ScheduleTargetError(ValueError):
    def __init__(self, reason: str, message: str, status_code: int = 409):
        super().__init__(message)
        self.reason = reason
        self.status_code = status_code


async def resolve_schedule_target(
    db: Database, agent_id: str | None, origin_session_id: str | None,
    *, allow_missing_origin: bool = False,
) -> dict:
    agent = await db.get_agent(agent_id) if agent_id is not None else None
    if agent is None:
        raise ScheduleTargetError("agent_not_found", "Agent not found", 404)
    if agent["archived"]:
        raise ScheduleTargetError("agent_archived", "Agent is archived")
    if origin_session_id:
        session = await db.get_session_binding(origin_session_id)
        if session is None:
            if not allow_missing_origin:
                raise ScheduleTargetError("origin_not_found", "Session not found", 404)
        elif session["user_id"] != agent.get("user_id"):
            raise ScheduleTargetError("origin_owner_mismatch", "Session not found", 404)
        elif session["agent_id"] != agent_id:
            raise ScheduleTargetError(
                "origin_agent_mismatch", "Session belongs to another agent",
            )
    return agent
