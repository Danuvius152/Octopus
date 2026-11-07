"""The latest native root observation and its durable validity, separate from billing."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

ContextInvalidationReason = Literal["configuration_changed", "native_context_changed", "compacted", "operation_unconfirmed"]
ContextObservationKind = Literal["sample", "compaction_started", "compaction_finished", "compacted", "invalidated"]


class ContextRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, allow_inf_nan=False)


class ContextTarget(ContextRecord):
    native_session_id: str | None
    effective_config_key: str = Field(min_length=1)
    agent_settings_revision: int | None = Field(ge=0)


class ContextBinding(ContextTarget):
    binding_revision: int = Field(ge=0)


class ContextSource(ContextRecord):
    request_id: str = Field(min_length=1)
    attempt_id: str = Field(min_length=1)
    connection_generation: str | None
    native_turn_id: str | None
    native_sample_id: str | None = None
    observation_seq: int = Field(ge=1)


class ContextReading(ContextRecord):
    used_tokens: int | None = Field(default=None, ge=0, le=9007199254740991)
    capacity_tokens: int | None = Field(default=None, gt=0, le=9007199254740991)
    used_ratio: float | None = Field(default=None, ge=0)
    basis: Literal["codex_last_total", "codex_recomputed_estimate", "claude_last_request_input", "provider_ratio"]
    capacity_basis: Literal["runtime_model_window"] | None = None


class ContextSample(ContextReading):
    backend: str
    native_session_id: str = Field(min_length=1)
    actual_model: str | None
    binding: ContextBinding
    source: ContextSource
    observed_at: str

    @model_validator(mode="after")
    def has_usage(self) -> Self:
        if self.used_tokens is None and self.used_ratio is None:
            raise ValueError("Context sample has no observed usage")
        return self


class ContextInvalidation(ContextRecord):
    reason: ContextInvalidationReason
    observed_at: str


class ContextState(ContextRecord):
    version: Literal[1] = 1
    revision: int = Field(ge=1)
    binding_revision: int = Field(ge=0)
    binding_target: ContextTarget
    validity: Literal["unknown", "valid", "stale"]
    invalidation: ContextInvalidation | None = None
    sample: ContextSample | None = None
    activity: Literal["compacting"] | None = None

    @model_validator(mode="after")
    def consistent(self) -> Self:
        if self.binding_revision > self.revision:
            raise ValueError("Context binding revision exceeds its state revision")
        if self.validity == "valid" and (self.sample is None or self.invalidation is not None):
            raise ValueError("Valid context requires a sample without invalidation")
        if (self.validity == "stale") != (self.invalidation is not None):
            raise ValueError("Only stale context carries invalidation")
        if self.validity == "valid" and self.sample is not None:
            expected = context_binding(self.binding_target, self)
            if self.sample.binding != expected or self.sample.native_session_id != self.binding_target.native_session_id:
                raise ValueError("Valid context sample does not match its current binding")
        return self


def context_target(session: Any, agent: dict[str, Any] | None) -> ContextTarget:
    from .harness.model_settings import effective_settings

    effective = effective_settings(session, agent)
    key = json.dumps([session.backend, effective["model"] or None, effective["credential_id"] or None], separators=(",", ":"))
    inherits = agent is not None and (not session.model or not session.credential_id)
    return ContextTarget(native_session_id=session.claude_session_id,
        effective_config_key=hashlib.sha256(key.encode()).hexdigest(),
        agent_settings_revision=agent["context_settings_revision"] if inherits and agent is not None else None)


def context_binding(target: ContextTarget, state: ContextState | None) -> ContextBinding:
    return ContextBinding(**target.model_dump(), binding_revision=state.binding_revision if state else 0)


def invalidate_context(state: ContextState | None, target: ContextTarget,
                       reason: ContextInvalidationReason, *, binding: bool = False) -> ContextState:
    revision = state.revision + 1 if state else 1
    return ContextState(revision=revision, binding_revision=revision if binding else state.binding_revision if state else 0,
        binding_target=target, validity="stale", sample=state.sample if state else None,
        invalidation=ContextInvalidation(reason=reason, observed_at=datetime.now(UTC).isoformat()))


def align_context(state: ContextState | None, target: ContextTarget) -> ContextState | None:
    if state is None or state.binding_target == target:
        return state
    previous = state.binding_target
    if (previous.native_session_id is None and target.native_session_id is not None
            and previous.effective_config_key == target.effective_config_key
            and previous.agent_settings_revision == target.agent_settings_revision):
        # First native identity completes the original attempt's binding.
        return state.model_copy(update={"revision": state.revision + 1, "binding_target": target})
    reason: ContextInvalidationReason = "configuration_changed" if (
        previous.effective_config_key != target.effective_config_key
        or previous.agent_settings_revision != target.agent_settings_revision) else "native_context_changed"
    return invalidate_context(state, target, reason, binding=True)


@dataclass(frozen=True)
class ContextObservation:
    backend: str
    native_session_id: str
    actual_model: str | None
    source: ContextSource
    reading: ContextReading | None
    kind: ContextObservationKind = "sample"
    reading_source: ContextSource | None = None
    compacting: bool = False
    invalidation_reason: ContextInvalidationReason = "operation_unconfirmed"
