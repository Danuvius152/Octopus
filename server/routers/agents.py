from __future__ import annotations

import asyncio
from dataclasses import replace
from functools import partial

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from ..agent_manager import AgentError, AgentManager
from ..auth import verify_token
from ..credential_bindings import CredentialBindingError
from ..deps import ScopeUser, SessionMgr
from ..models import (
    AgentCreate,
    AgentRead,
    AgentUpdate,
    CreateAgentScheduleRequest,
    CreateAgentSessionRequest,
    PinOrderRequest,
    ScheduleFromTextRequest,
    ScheduleInfo,
    SessionInfo,
)
from .request_validation import SafeInputValidationRoute

router = APIRouter(prefix="/api/agents", tags=["agents"])

# Injected at startup (mirrors schedules/credentials routers).
_manager: AgentManager | None = None


def set_manager(mgr: AgentManager) -> None:
    global _manager
    _manager = mgr


def _get_manager() -> AgentManager:
    assert _manager is not None
    return _manager


async def _publish(session_manager, kind: str, agent: dict) -> None:
    """Tell every open client about an agent change (sidebar-pins.md §7).

    The same global-event shape applications use: no `session_id`, the row
    comes down whole, and the store mirrors it — `agent_created` /
    `agent_updated` upsert, `agent_archived` / `agent_deleted` remove.
    """
    if kind == "agent_updated":
        session_manager.wake_contexts(agent["id"], agent.get("user_id"))
    await session_manager._broadcast(
        {"type": kind, "agent_id": agent["id"], "agent": AgentRead(**agent).model_dump()},
        # An agent event carries a row, not a session, so the owner is told
        # rather than derived (multi-tenancy.md §7). Without this the sidebar
        # of every open tab would learn about everyone's agents.
        user_id=agent.get("user_id"),
    )


def _agent_http_error(e: AgentError | CredentialBindingError) -> HTTPException:
    msg = str(e)
    if isinstance(e, AgentError) and e.field:
        return HTTPException(status.HTTP_400_BAD_REQUEST, {"message": msg, "field": e.field})
    code = status.HTTP_404_NOT_FOUND if "not found" in msg.lower() else status.HTTP_400_BAD_REQUEST
    return HTTPException(code, msg)


@router.get("", response_model=list[AgentRead])
async def list_agents(
    include_archived: bool = Query(False), user_id: ScopeUser = None, _: str = Depends(verify_token)
):
    agents = await _get_manager().list_agents(include_archived=include_archived, user_id=user_id)
    return [AgentRead(**a) for a in agents]


async def create_agent(
    session_manager: SessionMgr, req: AgentCreate, user_id: ScopeUser = None, _: str = Depends(verify_token)
):
    try:
        # The owner, which is the whole point of the `ScopeUser` above: it was
        # declared and never passed, so every agent created through this route
        # was unowned — invisible to the account that made it, and a 404 the
        # moment they tried to open a session under it.
        agent = await _get_manager().create_agent(
            user_id=user_id, **req.model_dump()
        )
    except AgentError as e:
        raise _agent_http_error(e)
    await _publish(session_manager, "agent_created", agent)
    return AgentRead(**agent)


router.add_api_route("", create_agent, methods=["POST"], response_model=AgentRead,
                     status_code=status.HTTP_201_CREATED, route_class_override=SafeInputValidationRoute)


@router.put("/pin-order", response_model=list[AgentRead])
async def reorder_agent_pins(
    session_manager: SessionMgr, req: PinOrderRequest, user_id: ScopeUser = None, _: str = Depends(verify_token)
):
    """The sidebar order of the pinned agents (sidebar-pins.md). Returns
    every live agent, so the caller replaces its list in one step."""
    mgr = _get_manager()
    try:
        agents, changed = await mgr.reorder_pins(req.ids, user_id)
    except AgentError as e:
        raise _agent_http_error(e)
    # Only the rows whose position moved need to travel to other clients.
    for agent in changed:
        await _publish(session_manager, "agent_updated", agent)
    return [AgentRead(**a) for a in agents]


@router.get("/{agent_id}", response_model=AgentRead)
async def get_agent(agent_id: str, user_id: ScopeUser = None, _: str = Depends(verify_token)):
    agent = await _get_manager().get_agent(agent_id, user_id)
    if agent is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Agent not found")
    return AgentRead(**agent)


async def update_agent(
    session_manager: SessionMgr,
    agent_id: str,
    req: AgentUpdate,
    user_id: ScopeUser = None, _: str = Depends(verify_token),
):
    # exclude_unset so omitting a field leaves it untouched while explicitly
    # passing null clears a nullable field (model/credential_id/avatar).
    fields = req.model_dump(exclude_unset=True)
    try:
        agent = await _get_manager().update_agent(agent_id, user_id, **fields)
    except AgentError as e:
        raise _agent_http_error(e)
    await _publish(session_manager, "agent_updated", agent)
    return AgentRead(**agent)


router.add_api_route("/{agent_id}", update_agent, methods=["PATCH"], response_model=AgentRead,
                     route_class_override=SafeInputValidationRoute)


@router.post("/{agent_id}/archive", response_model=AgentRead)
async def archive_agent(session_manager: SessionMgr, agent_id: str, user_id: ScopeUser = None, _: str = Depends(verify_token)):
    manager = _get_manager()
    agent = await manager.get_agent(agent_id, user_id)
    if agent is None:
        raise HTTPException(status_code=404, detail="agent not found")
    if agent["is_system"]:
        raise _agent_http_error(AgentError("The Default Agent cannot be archived"))
    async def finish_archive() -> AgentRead:
        # Keep retirement, persistence and notification under one owner. A
        # disconnected HTTP waiter cannot strand evicted sessions in live rows.
        try:
            await session_manager.archive_agent(agent_id)
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        archived = await manager.get_agent(agent_id, user_id)
        if archived is None:
            raise HTTPException(status_code=404, detail="agent not found")
        await _publish(session_manager, "agent_archived", archived)
        return AgentRead(**archived)

    operation = asyncio.create_task(finish_archive(), name=f"archive-agent-{agent_id}")
    session_manager._own_task(operation)
    return await asyncio.shield(operation)


@router.post("/{agent_id}/unarchive", response_model=AgentRead)
async def unarchive_agent(
    session_manager: SessionMgr, agent_id: str, user_id: ScopeUser = None, _: str = Depends(verify_token)
):
    """Restore an archived agent (the Agents page's Archived section). It
    comes back pinned; its sessions stay archived — those come back from the
    archived-sessions page individually."""
    try:
        agent = await session_manager.unarchive_agent(agent_id, user_id)
    except AgentError as e:
        raise _agent_http_error(e)
    except ValueError as e:
        raise HTTPException(409, str(e)) from e
    await _publish(session_manager, "agent_updated", agent)
    return AgentRead(**agent)


async def _set_pinned(
    session_manager, agent_id: str, pinned: bool, user_id: str | None
) -> AgentRead:
    try:
        agent = await _get_manager().set_pinned(agent_id, pinned, user_id)
    except AgentError as e:
        raise _agent_http_error(e)
    await _publish(session_manager, "agent_updated", agent)
    return AgentRead(**agent)


@router.post("/{agent_id}/pin", response_model=AgentRead)
async def pin_agent(
    session_manager: SessionMgr, agent_id: str, user_id: ScopeUser = None, _: str = Depends(verify_token)
):
    """Put the agent in the sidebar, at the bottom of the pinned ones."""
    return await _set_pinned(session_manager, agent_id, True, user_id)


@router.post("/{agent_id}/unpin", response_model=AgentRead)
async def unpin_agent(
    session_manager: SessionMgr, agent_id: str, user_id: ScopeUser = None, _: str = Depends(verify_token)
):
    """Take the agent out of the sidebar. It stays live — listed on the
    Agents page, callable, its schedules running."""
    return await _set_pinned(session_manager, agent_id, False, user_id)


@router.delete("/{agent_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_agent(
    session_manager: SessionMgr, agent_id: str, user_id: ScopeUser = None, _: str = Depends(verify_token)
):
    try:
        agent = await _get_manager().delete_agent(agent_id, user_id=user_id)
    except AgentError as e:
        raise _agent_http_error(e)
    await _publish(session_manager, "agent_deleted", agent)


@router.get("/{agent_id}/sessions", response_model=list[SessionInfo])
async def list_agent_sessions(session_manager: SessionMgr, agent_id: str, user_id: ScopeUser = None, _: str = Depends(verify_token)):
    if await _get_manager().db.get_agent_identity(agent_id, user_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Agent not found")
    return await session_manager.list_live_session_infos(user_id, agent_id=agent_id)


@router.post(
    "/{agent_id}/sessions",
    response_model=SessionInfo,
    status_code=status.HTTP_201_CREATED,
)
async def create_agent_session(
    session_manager: SessionMgr,
    agent_id: str, req: CreateAgentSessionRequest, user_id: ScopeUser = None, _: str = Depends(verify_token)
):
    """Preferred path to start a session — the agent comes from the URL, so
    the body's `agent_id` (if any) is ignored."""
    from .sessions import create_owned_session

    return await create_owned_session(session_manager, agent_id, user_id, req)


@router.get("/{agent_id}/schedules", response_model=list[ScheduleInfo])
async def list_agent_schedules(agent_id: str, user_id: ScopeUser = None, _: str = Depends(verify_token)):
    if await _get_manager().db.get_agent_identity(agent_id, user_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Agent not found")
    from .schedules import _get_db, to_schedule_info

    rows = await _get_db().load_schedules()
    return [to_schedule_info(r) for r in rows if r["agent_id"] == agent_id]


@router.post(
    "/{agent_id}/schedules",
    response_model=ScheduleInfo,
    status_code=status.HTTP_201_CREATED,
)
async def create_agent_schedule(
    request: Request,
    session_manager: SessionMgr,
    agent_id: str, req: CreateAgentScheduleRequest, user_id: ScopeUser = None, _: str = Depends(verify_token)
):
    from ..knowledge import derive
    from .schedules import (
        create_schedule_for_agent,
        schedule_edit,
        to_schedule_info,
        validate_schedule_target,
    )
    edit, human = schedule_edit(request, session_manager, req.prompt, user_id=user_id,
                            selected=req.wiki_mode, session_id=req.session_id)
    context = derive(edit, req.prompt, selected=req.wiki_mode).execution
    if await _get_manager().db.get_agent_identity(agent_id, user_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Agent not found")

    await validate_schedule_target(agent_id, req.session_id)
    row = await create_schedule_for_agent(
        agent_id,
        req.name,
        req.prompt,
        interval_seconds=req.interval_seconds,
        origin_session_id=req.session_id,
        knowledge_context=context,
        requesting=edit,
    )
    return to_schedule_info(row)


@router.post(
    "/{agent_id}/schedules/from_text",
    response_model=ScheduleInfo,
    status_code=status.HTTP_201_CREATED,
)
async def create_agent_schedule_from_text(
    request: Request,
    session_manager: SessionMgr,
    agent_id: str, req: ScheduleFromTextRequest, user_id: ScopeUser = None, _: str = Depends(verify_token)
):
    """Natural-language schedule creation. Parses `text` (rigid `<interval>
    <prompt>` fast-path, else a one-shot AI parse on the agent's own harness)
    into a recurrence + prompt, then creates the schedule. Parse failures
    surface as 422 with a user-facing detail."""
    from ..harness import get_harness
    from ..knowledge import derive
    from ..schedule_ai import ScheduleParseError, parse_schedule_text
    from .schedules import (
        create_schedule_for_agent,
        schedule_edit,
        to_schedule_info,
        validate_schedule_target,
    )
    edit, human = schedule_edit(request, session_manager, req.text, user_id=user_id,
                            selected=req.wiki_mode, session_id=req.session_id)
    db = _get_manager().db
    if await db.get_agent_identity(agent_id, user_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Agent not found")

    # A rejected relationship must not launch even a one-shot parse.
    await validate_schedule_target(agent_id, req.session_id)
    async def run_parser(ctx):
        agent = await db.get_live_agent_configuration(agent_id, user_id)
        if agent is None:
            raise HTTPException(409, "Agent is unavailable for schedule parsing")
        backend, cred_id = agent["backend"], agent["credential_id"]
        harness = get_harness(backend)
        try:
            credential = await session_manager.resolve_credential_by_id(
                cred_id, backend=backend, context=f"schedule-parse agent {agent_id}",
                user_id=user_id,
            )
        except ValueError as error:
            raise ScheduleParseError(str(error)) from error
        return await harness.run_oneshot(replace(
            ctx, credential=credential, model=agent["model"] or None,
            reasoning_effort=agent["reasoning_effort"] or None, service_tier=agent["service_tier"] or None,
            authorize_spawn=partial(db.require_run_authority, user_id, cred_id, backend),
        ))
    try:
        parsed = await parse_schedule_text(
            req.text,
            timezone=req.timezone,
            now_iso=req.now,
            runner=run_parser,
        )
    except ScheduleParseError as e:
        raise HTTPException(422, str(e))

    row = await create_schedule_for_agent(
        agent_id,
        parsed.name,
        parsed.prompt,
        interval_seconds=parsed.interval_seconds,
        cron=parsed.cron,
        tz=parsed.timezone,
        recurrence_label=parsed.recurrence_label,
        origin_session_id=req.session_id,
        run_at=parsed.run_at,
        knowledge_context=derive(edit, parsed.prompt, selected=req.wiki_mode).execution,
        requesting=edit,
    )
    return to_schedule_info(row)
