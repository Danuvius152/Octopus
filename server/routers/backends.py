"""Account-scoped discovery of models, reasoning and speed controls."""

from collections.abc import Mapping

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ..auth import verify_token
from ..credential_bindings import CredentialBindingError, validate_credential_binding
from ..deps import ScopeUser, SessionMgr
from ..harness import get_harness
from ..harness.model_settings import ModelControlError, ModelSettings
from ..harness.model_settings import validate_controls as check_controls
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


def validate_controls(
    settings: ModelSettings, model: str | None, effort: str | None, tier: str | None,
    *, backend: str, overrides: Mapping[str, object] | None = None,
) -> None:
    try:
        check_controls(settings, model, effort, tier)
    except ModelControlError as error:
        raise HTTPException(400, error.describe(
            backend, inherited=not bool(overrides and overrides.get(error.field)),
        )) from error


@router.get("/{backend}/models", response_model=ModelSettings)
async def model_settings(
    session_manager: SessionMgr, backend: BackendKind,
    credential_id: str | None = None, user_id: ScopeUser = None,
    _: str = Depends(verify_token),
):
    return await model_settings_for(session_manager, backend.value, credential_id, user_id)
