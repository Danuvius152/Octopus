"""Owner-only context commits, using the existing short configuration boundary."""

from __future__ import annotations

from datetime import UTC, datetime

from ..context_models import (
    ContextSample,
    ContextState,
    align_context,
    context_binding,
    context_target,
    invalidate_context,
)
from ..harness import HarnessEvent
from .base import Session, SessionManagerBase
from .runtime_types import _Attempt


class ContextMixin(SessionManagerBase):
    def wake_contexts(self, agent_id: str | None, user_id: str | None) -> None:
        for session in self.sessions.values():
            if session.user_id == user_id and (agent_id is None or session.agent_id == agent_id):
                owner = self._session_owners.get(id(session))
                if owner is not None:
                    owner.refresh_context()

    async def _reset_context_owned(self, session: Session) -> None:
        assert self.db is not None
        async with self.db.configuration_lock:
            agent = await self._load_agent(session)
            await self._save_context(session, invalidate_context(session.context_state,
                context_target(session, agent), "native_context_changed", binding=True))

    async def _unconfirmed_context_owned(self, session: Session) -> None:
        assert self.db is not None
        async with self.db.configuration_lock:
            agent = await self._load_agent(session)
            await self._save_context(session, invalidate_context(session.context_state,
                context_target(session, agent), "operation_unconfirmed"))

    async def context_snapshot(self, session: Session) -> tuple[ContextState | None, bool]:
        # Diagnostic reads do not depend on the execution writer's progress.
        # Return committed facts and their display qualification together;
        # only the original Owner may persist an alignment or advance revision.
        if session.context_state is None:
            return None, False
        assert self.db is not None
        async with self.db.configuration_lock:
            agent = await self._load_agent(session)
            state = session.context_state
            pending = state is not None and (
                state.binding_target != context_target(session, agent) or self.session_owner_failed(session.id))
        if pending:
            owner = self._session_owners.get(id(session))
            if owner is not None:
                owner.refresh_context()
        return state, pending

    async def _align_context_locked(self, session: Session, agent: dict | None) -> None:
        await self._save_context(session, align_context(session.context_state, context_target(session, agent)))

    async def _save_context(self, session: Session, state: ContextState | None) -> None:
        if state == session.context_state:
            return
        assert self.db is not None
        await self.db.update_session_field(session.id, context_state=state.model_dump_json() if state else None)
        session.context_state = state
        self._session_owner(session).runtime.context_changed()

    async def _read_context_owned(self, session: Session) -> ContextState | None:
        assert self.db is not None
        async with self.db.configuration_lock:
            agent = await self._load_agent(session)
            await self._align_context_locked(session, agent)
            return session.context_state

    async def _accept_context(self, session: Session, attempt: _Attempt, event: HarnessEvent) -> None:
        observation, captured = event.context, attempt.context_binding
        owner = self._session_owners.get(id(session))
        execution = owner.runtime.current if owner is not None else None
        # Detaching a reusable host on Stop does not retire the original
        # execution's finite evidence reader. That execution owns this commit.
        if (observation is None or captured is None or execution is None or execution.attempt is not attempt
                or not attempt.backend.profile.can_observe_context):
            return
        source = observation.source
        if (source.attempt_id != attempt.attempt_id or source.observation_seq <= attempt.context_observation_seq
                or source.request_id != execution.request.request_id):
            return
        attempt.context_observation_seq = source.observation_seq
        assert self.db is not None
        async with self.db.configuration_lock:
            agent = await self._load_agent(session)
            await self._align_context_locked(session, agent)
            target = context_target(session, agent)
            current = context_binding(target, session.context_state)
            if (captured.binding_revision != current.binding_revision
                    or captured.effective_config_key != current.effective_config_key
                    or captured.agent_settings_revision != current.agent_settings_revision
                    or captured.native_session_id not in (None, observation.native_session_id)
                    or target.native_session_id != observation.native_session_id):
                return
            state = session.context_state
            if observation.kind == "compaction_finished":
                if state is not None and state.activity is not None:
                    await self._save_context(session, state.model_copy(update={"revision": state.revision + 1, "activity": None}))
                return
            if observation.kind == "compaction_started":
                if state is None:
                    state = ContextState(revision=1, binding_revision=current.binding_revision,
                        binding_target=target, validity="unknown", activity="compacting")
                elif state.activity != "compacting":
                    state = state.model_copy(update={"revision": state.revision + 1, "activity": "compacting"})
                await self._save_context(session, state)
                return
            if observation.kind in {"compacted", "invalidated"}:
                state = invalidate_context(state, target,
                    "compacted" if observation.kind == "compacted" else observation.invalidation_reason)
            reading = observation.reading
            if reading is None:
                if (observation.kind == "sample" and observation.actual_model is not None
                        and state is not None and state.sample is not None
                        and state.sample.actual_model != observation.actual_model):
                    state = invalidate_context(state, target, "native_context_changed")
                if state is not None and observation.compacting:
                    state = state.model_copy(update={"activity": "compacting"})
                await self._save_context(session, state)
                return
            sample = ContextSample(**reading.model_dump(), backend=observation.backend,
                native_session_id=observation.native_session_id, actual_model=observation.actual_model,
                binding=current, source=observation.reading_source or source, observed_at=datetime.now(UTC).isoformat())
            revision = (state.revision if observation.kind == "compacted" else state.revision + 1) if state else 1
            await self._save_context(session, ContextState(revision=revision,
                binding_revision=current.binding_revision, binding_target=target, validity="valid", sample=sample,
                activity="compacting" if observation.compacting else None))
