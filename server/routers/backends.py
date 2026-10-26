"""Account-scoped discovery of models, reasoning and speed controls."""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ..auth import verify_token
from ..credential_bindings import CredentialBindingError, validate_credential_binding
from ..deps import ScopeUser, SessionMgr
from ..harness import get_harness
from ..harness.model_settings import ModelSettings
from ..harness.profile import ToolPolicySupport
from ..models import BackendKind
from ..sessions import SessionManager

router = APIRouter(prefix="/api/backends", tags=["backends"])


class BackendAvailability(BaseModel):
    available: list[str]
    models: dict[str, list[str]]
    default: str
    tool_policy: dict[str, ToolPolicySupport]


async def model_settings_for(
    session_manager: SessionManager, backend: str, credential_id: str | None,
    user_id: str | None,
) -> ModelSettings:
    harness = get_harness(backend)
    try:
        await validate_credential_binding(session_manager.db, credential_id, backend, user_id)
    except CredentialBindingError as error:
        raise HTTPException(404 if str(error) == "Credential not found" else 400, str(error)) from error
    credential = None
    if harness.profile.model_settings:
        credential = await session_manager.resolve_credential_by_id(
            credential_id, backend=backend, require_auth=False,
            context="model settings",
            user_id=user_id,
        )
    return harness.model_settings(credential)


def validate_reasoning(settings: ModelSettings, model: str | None, effort: str | None) -> None:
    if effort is None:
        return
    allowed = settings.efforts_for(model)
    if effort not in allowed:
        detail = (
            f"Reasoning effort {effort!r} is unavailable for this model. "
            f"Choose one of: {', '.join(allowed)}."
            if allowed else "This backend does not offer reasoning effort controls."
        )
        raise HTTPException(400, detail)


def validate_service_tier(settings: ModelSettings, model: str | None, tier: str | None) -> None:
    if tier is None:
        return
    allowed = settings.tiers_for(model)
    if tier not in allowed:
        detail = (
            f"Speed {tier!r} is unavailable for this model. "
            f"Choose one of: {', '.join(allowed)}."
            if allowed else "This backend does not offer speed controls."
        )
        raise HTTPException(400, detail)


@router.get("/{backend}/models", response_model=ModelSettings)
async def model_settings(
    session_manager: SessionMgr, backend: BackendKind,
    credential_id: str | None = None, user_id: ScopeUser = None,
    _: str = Depends(verify_token),
):
    return await model_settings_for(session_manager, backend.value, credential_id, user_id)
