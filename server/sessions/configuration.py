"""Save live overrides and capture request defaults at one short boundary."""

from __future__ import annotations

import asyncio
from contextlib import AbstractAsyncContextManager
from dataclasses import replace
from typing import Any

from ..aio import complete_cleanup
from ..context_models import align_context, context_target, invalidate_context
from ..credential_bindings import validate_credential_binding
from ..database import Database
from ..db.credentials import CredentialOwnerUnavailable
from ..harness import all_backends, get_harness, has_backend, model_settings
from ..models import SessionInfo
from .base import Session, SessionManagerBase
from .presentation import session_info
from .runtime_types import SessionOwnerUnavailable


class SessionConfigurationError(ValueError):
    def __init__(self, message: str, status_code: int = 400, *, code: str | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code


async def validate_session_settings(db: Database, candidate: Session, agent: dict[str, Any] | None) -> None:
    effective = model_settings.effective_settings(candidate, agent)
    credential = effective["credential_id"]
    if effective["reasoning_effort"] is None and effective["service_tier"] is None:
        await validate_credential_binding(db, credential, candidate.backend, candidate.user_id)
        return
    catalog = await model_settings.model_settings_for(db, candidate.backend, credential, candidate.user_id)
    try:
        model_settings.validate_controls(catalog, effective["model"], effective["reasoning_effort"], effective["service_tier"])
    except model_settings.ModelControlError as error:
        raise SessionConfigurationError(error.describe(
            candidate.backend, inherited=not bool(getattr(candidate, error.field)),
        )) from error


def validate_session_tool_policy(backend: str, agent: dict[str, Any] | None) -> None:
    settings = agent or {}
    try:
        get_harness(backend).profile.tool_policy.validate(backend, settings.get("tool_allow"), settings.get("tool_deny"))
    except ValueError as error:
        raise SessionConfigurationError(str(error)) from error


class ConfigurationMixin(SessionManagerBase):
    @property
    def configuration_lock(self) -> asyncio.Lock:
        assert self.db is not None
        return self.db.configuration_lock

    def _configuration_boundary(self, session: Session) -> AbstractAsyncContextManager[None]:
        lock = self.configuration_lock
        owner = self._session_owners.get(id(session))
        if owner is not None and asyncio.current_task() is owner.task:
            return owner.runtime.configuration_boundary(lock)
        return lock

    async def capture_request_configuration(self, context: Session) -> tuple[Session, dict[str, Any] | None]:
        """Read a consistent configuration without blocking the execution writer."""
        defaults = self.get_session(context.id, context.user_id)
        if defaults is None:
            raise SessionConfigurationError("Session not found", 404)
        if defaults._archive_reservation is not None or self.session_owner_failed(defaults.id):
            raise SessionOwnerUnavailable("Session owner is unavailable; retained input requires review")
        owner = self._session_owners.get(id(defaults))
        if owner is not None:
            owner.require_available()
        async with self.configuration_lock:
            agent = await self._load_agent(defaults)
            if self.get_session(context.id, context.user_id) is not defaults:
                raise SessionConfigurationError("Session not found", 404)
            if defaults._admission_closed:
                raise SessionConfigurationError("Session admission is closed", 409)
            if owner is not None:
                owner.require_available()
            # Adopt the same binding the Owner will compare when accepting
            # an observation. Only the Owner persists live context changes.
            state = align_context(defaults.context_state, context_target(defaults, agent))
            return replace(context, backend=defaults.backend, credential_id=defaults.credential_id,
                           model=defaults.model, reasoning_effort=defaults.reasoning_effort,
                           service_tier=defaults.service_tier, defaults_source=defaults.defaults_source,
                           context_state=state), agent

    async def update_session_configuration(
        self, session_id: str, fields: dict[str, Any], user_id: str | None = None,
    ) -> SessionInfo:
        from .owner_runtime import UpdateConfiguration

        session = self.get_session(session_id, user_id)
        if session is None:
            raise SessionConfigurationError("Session not found", 404)
        return await self._session_owner(session).call_runtime(UpdateConfiguration(dict(fields), user_id))

    async def _save_session_configuration(
        self, session_id: str, fields: dict[str, Any], user_id: str | None,
    ) -> SessionInfo:
        session = self.get_session(session_id, user_id)
        if session is None:
            raise SessionConfigurationError("Session not found", 404)
        assert self.db is not None
        expected = fields.get("expected_backend")
        if expected is not None and expected != session.backend:
            raise SessionConfigurationError(
                "The conversation engine changed. Reload its settings.", 409, code="engine_changed",
            )
        backend = fields.get("backend") or session.backend
        switching = backend != session.backend
        updates: dict[str, Any] = {
            field: fields[field] or None for field in model_settings.SESSION_OVERRIDE_FIELDS if field in fields
        }
        if updates.get("model"):
            updates["model"] = str(updates["model"]).strip() or None
        has_inputs = None
        if switching:
            get_harness(backend)
            if any(field not in updates for field in model_settings.SESSION_OVERRIDE_FIELDS):
                raise SessionConfigurationError("Changing engines requires model, reasoning effort, speed, and sign-in settings.")
            if (session._forking or session._archive_reservation is not None or session._admission_closed
                    or self.session_deleting(session.id)):
                raise SessionConfigurationError("The conversation is busy. Try again shortly.", 409, code="conversation_busy")
            has_inputs = await self.db.session_has_inputs(session.id)
            if session.claude_session_id or self.session_is_executing(session) or has_inputs:
                raise SessionConfigurationError(
                    "This conversation already has input. Start a new conversation to change engines.", 409,
                    code="conversation_started",
                )
            updates["backend"] = backend
        state = session.context_state
        if updates:
            agent = await self.db.get_agent(session.agent_id, user_id) if session.agent_id else None
            if switching:
                validate_session_tool_policy(backend, agent)
                updates["defaults_source"] = "agent" if agent is not None and agent["backend"] == backend else "backend"
            candidate = replace(session, **updates)
            await validate_session_settings(self.db, candidate, agent)
            target = context_target(candidate, agent)
            state = align_context(state, target)
            if state is None and context_target(session, agent) != target:
                state = invalidate_context(None, target, "configuration_changed", binding=True)
        if fields.get("name") is not None:
            name = str(fields["name"]).strip()
            if not name:
                raise SessionConfigurationError("Session name cannot be empty")
            updates["name"] = name
        if has_inputs is None:
            has_inputs = await self.db.session_has_inputs(session.id)
        if updates:
            owner = self._require_session_writer(session)
            if state != session.context_state:
                updates["context_state"] = state.model_dump_json() if state else None
            await self.db.update_session_field(session_id, **updates)
            for key, value in updates.items():
                if key != "context_state":
                    setattr(session, key, value)
            if state != session.context_state:
                session.context_state = state
                if owner is not None:
                    owner.runtime.context_changed()
        return session_info(session, has_inputs=has_inputs)

    async def delete_credential_binding(self, credential_id: str, user_id: str | None) -> dict[str, Any] | None:
        """Propagate only a committed deletion; captured runs remain revocable."""
        assert self.db is not None
        async def remove() -> dict[str, Any] | None:
            assert self.db is not None
            row = await self.db.get_credential(credential_id, user_id)
            if row is None:
                driver = next((login for backend in all_backends()
                               if (login := get_harness(backend).login) is not None
                               and login.owns_credential(credential_id, user_id)), None)
                if driver is None:
                    return None
            else:
                driver = get_harness(row["backend"]).login if has_backend(row["backend"]) else None
            if driver is not None:
                return await driver.delete_credential(credential_id, user_id, remove_binding)
            return await remove_binding()

        async def remove_binding() -> dict[str, Any] | None:
            assert self.db is not None
            async with self.configuration_lock:
                try:
                    row = await self.db.delete_owned_credential(credential_id, user_id)
                except CredentialOwnerUnavailable as error:
                    raise SessionConfigurationError(str(error), 403) from error
                if row is None:
                    return None
                for live in self.sessions.values():
                    if live.credential_id == credential_id:
                        live.credential_id = None
                return row

        row = await complete_cleanup(remove())
        if row is not None:
            self.wake_contexts(None, user_id)
        return row
