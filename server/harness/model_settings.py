"""Model metadata advertised by a runtime, without credentials or config contents."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from ..database import Database


async def model_settings_for(
    database: Database, backend: str, credential_id: str | None, user_id: str | None,
) -> ModelSettings:
    """Read the selected runtime's local catalog without login or refresh."""
    from ..credential_bindings import validate_credential_binding
    from .events import HarnessCredential
    from .registry import get_harness

    await validate_credential_binding(database, credential_id, backend, user_id)
    harness = get_harness(backend)
    credential = None
    if credential_id and harness.profile.credential_style == "home_dir":
        from ..codex_login import codex_home_for

        credential = HarnessCredential(backend=backend, auth_type="oauth", home_dir=codex_home_for(credential_id))
    return harness.model_settings(credential)


SESSION_OVERRIDE_FIELDS = ("model", "reasoning_effort", "service_tier", "credential_id")


def effective_settings(session: Any, agent: Mapping[str, Any] | None) -> dict[str, Any]:
    """Resolve overrides using the source saved when the engine was chosen."""
    read = session.get if isinstance(session, Mapping) else lambda field, default=None: getattr(session, field, default)
    defaults = (agent or {}) if read("defaults_source", "agent") == "agent" else {}
    return {field: read(field) or defaults.get(field) for field in SESSION_OVERRIDE_FIELDS}


@dataclass
class ModelOption:
    id: str
    name: str
    reasoning_efforts: list[str] = field(default_factory=list)
    default_reasoning_effort: str | None = None
    service_tiers: list[str] = field(default_factory=list)


@dataclass
class ModelSettings:
    models: list[ModelOption] = field(default_factory=list)
    reasoning_efforts: list[str] = field(default_factory=list)
    default_model: str | None = None
    default_reasoning_effort: str | None = None
    service_tiers: list[str] = field(default_factory=list)
    default_service_tier: str | None = None
    catalog_error: str | None = None
    saved_model_options: list[ModelOption] = field(default_factory=list)

    def __post_init__(self) -> None:
        # Hidden models constrain validation. Only explicitly requested saved
        # IDs are projected for editing; they stay out of the picker.
        self._known_models = {model.id: model for model in self.models}

    def add_model(self, option: ModelOption, *, visible: bool) -> None:
        self._known_models[option.id] = option
        if visible:
            self.models.append(option)

    def options_for_saved_models(self, model_ids: list[str]) -> list[ModelOption]:
        visible = {model.id for model in self.models}
        return [self._known_models[model_id] for model_id in dict.fromkeys(model_ids)
                if model_id in self._known_models and model_id not in visible]

    def efforts_for(self, model: str | None) -> list[str]:
        selected = model or self.default_model
        option = self._known_models.get(selected) if selected else None
        return option.reasoning_efforts if option else self.reasoning_efforts

    def tiers_for(self, model: str | None) -> list[str]:
        selected = model or self.default_model
        option = self._known_models.get(selected) if selected else None
        return option.service_tiers if option else self.service_tiers


class ModelControlError(ValueError):
    """A nonempty control cannot be honored by the selected runtime/model."""

    def __init__(self, field: Literal['reasoning_effort', 'service_tier'], detail: str) -> None:
        super().__init__(detail)
        self.field = field

    def describe(self, backend: str, *, inherited: bool) -> str:
        source, entry = ('Agent default', 'Agent settings') if inherited else ('session override', 'session /model settings')
        return f'{backend}: {self.field} from {source}. {self} Edit {entry}.'


def validate_controls(
    settings: ModelSettings, model: str | None, effort: str | None, tier: str | None,
) -> None:
    """An empty capability rejects nonempty controls; custom models use runtime defaults."""
    selected = model or settings.default_model or 'runtime default'
    controls: tuple[tuple[Literal['reasoning_effort', 'service_tier'], str | None, list[str], str], ...] = (
        ('reasoning_effort', effort, settings.efforts_for(model), 'Reasoning effort'),
        ('service_tier', tier, settings.tiers_for(model), 'Fast speed' if tier == 'fast' else 'Speed'),
    )
    for control_field, value, allowed, label in controls:
        if value and value not in allowed:
            detail = (
                f'{label} {value!r} is unavailable for model {selected!r}. Choose one of: {", ".join(allowed)}.'
                if allowed else f'This backend does not offer {label.lower()} controls for model {selected!r}.'
            )
            raise ModelControlError(control_field, detail)
