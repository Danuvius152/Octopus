"""Backend catalog and account-scoped discovery of model controls."""

from dataclasses import replace
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from ..auth import verify_token
from ..credential_bindings import CredentialBindingError
from ..deps import ScopeUser, SessionMgr
from ..harness.model_settings import ModelSettings, model_settings_for
from ..harness.profile import ToolPolicySupport
from ..models import BackendKind

router = APIRouter(prefix="/api/backends", tags=["backends"])


class BackendCatalog(BaseModel):
    supported: list[str] = Field(description="Registered runtime profiles, including uninstalled engines.")
    available: list[str] = Field(description="Registered engines whose conversation CLI resolves on this host.")
    models: dict[str, list[str]]
    default: str = Field(description="Deployment preference for new agents.")
    tool_policy: dict[str, ToolPolicySupport]


@router.get("/{backend}/models", response_model=ModelSettings)
async def model_settings(
    session_manager: SessionMgr, backend: BackendKind,
    credential_id: str | None = None, user_id: ScopeUser = None,
    saved_model: Annotated[list[str] | None, Query(max_length=2)] = None,
    _: str = Depends(verify_token),
):
    try:
        assert session_manager.db is not None
        catalog = await model_settings_for(session_manager.db, backend.value, credential_id, user_id)
        if saved_model:
            return replace(catalog, saved_model_options=catalog.options_for_saved_models(saved_model))
        return catalog
    except CredentialBindingError as error:
        raise HTTPException(404 if str(error) == "Credential not found" else 400, str(error)) from error
