"""REST endpoints for async notification targets."""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import JSONResponse

from ..auth import verify_token
from ..deps import ScopeUser
from ..models import (
    CreateNotifierRequest,
    NotifierInfo,
    NotifierRefreshUnconfirmed,
    UpdateNotifierRequest,
)
from ..notifiers import notifier_manager

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/notifiers", tags=["notifiers"])

_db = None


def set_db(db) -> None:
    global _db
    _db = db


def _require_db():
    if _db is None:
        raise HTTPException(
            status_code=503, detail="notifier database not yet initialized"
        )
    return _db


def _row_to_info(row: dict) -> NotifierInfo:
    return NotifierInfo(
        id=row["id"],
        type=row["type"],
        label=row["label"],
        config=row.get("config", {}),
        enabled=bool(row.get("enabled", True)),
        created_at=row["created_at"],
    )


@router.get("", response_model=list[NotifierInfo])
async def list_notifiers(user_id: ScopeUser = None, _: str = Depends(verify_token)):
    rows = await _require_db().load_notifiers(user_id)
    return [_row_to_info(r) for r in rows]


@router.post(
    "", response_model=NotifierInfo, status_code=status.HTTP_201_CREATED
)
async def create_notifier(
    req: CreateNotifierRequest,
    user_id: ScopeUser = None,
    _: str = Depends(verify_token),
):
    db = _require_db()
    nid = uuid.uuid4().hex[:12]
    created_at = datetime.now(UTC).isoformat()
    await db.save_notifier(
        notifier_id=nid,
        type=req.type.value,
        label=req.label,
        config=req.config,
        created_at=created_at,
        user_id=user_id,
    )
    # Reload manager so the new target is live without restart
    await notifier_manager.load()
    return NotifierInfo(
        id=nid,
        type=req.type,
        label=req.label,
        config=req.config,
        enabled=True,
        created_at=created_at,
    )


@router.patch(
    "/{notifier_id}", response_model=NotifierInfo,
    responses={503: {"model": NotifierRefreshUnconfirmed}},
)
async def update_notifier(
    notifier_id: str,
    req: UpdateNotifierRequest,
    user_id: ScopeUser = None,
    _: str = Depends(verify_token),
):
    db = _require_db()
    updates: dict = {}
    if req.label is not None:
        updates["label"] = req.label
    if req.config is not None:
        updates["config"] = req.config
    if req.enabled is not None:
        updates["enabled"] = req.enabled
    updated = await db.update_notifier(notifier_id, user_id=user_id, **updates)
    if updated is None:
        raise HTTPException(status_code=404, detail="notifier not found")
    if updates:
        try:
            await notifier_manager.load()
        except Exception:
            # Storage fail-stop exits through the shared boundary. A normal
            # reload error can report this confirmed write while health permits.
            db.health.require_healthy()
            logger.exception("Notifier %s committed but runtime refresh failed", notifier_id)
            return JSONResponse(status_code=503, content=NotifierRefreshUnconfirmed(notifier_id=notifier_id).model_dump())
    return _row_to_info(updated)


@router.delete("/{notifier_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_notifier(
    notifier_id: str, user_id: ScopeUser = None, _: str = Depends(verify_token)
):
    db = _require_db()
    # Resolved inside this account's rows first: a DELETE straight to the id
    # would remove anybody's, and answer 204 either way.
    rows = await db.load_notifiers(user_id)
    if not any(r["id"] == notifier_id for r in rows):
        raise HTTPException(status_code=404, detail="notifier not found")
    deleted = await db.delete_notifier(notifier_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="notifier not found")
    await notifier_manager.load()
