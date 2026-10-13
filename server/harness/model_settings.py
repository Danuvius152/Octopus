"""Model metadata advertised by a runtime, without credentials or config contents."""

from dataclasses import dataclass, field


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

    def __post_init__(self) -> None:
        # Not a dataclass field: hidden models constrain validation without
        # leaking into the public picker or the serialized catalog.
        self._known_models = {model.id: model for model in self.models}

    def add_model(self, option: ModelOption, *, visible: bool) -> None:
        self._known_models[option.id] = option
        if visible:
            self.models.append(option)

    def efforts_for(self, model: str | None) -> list[str]:
        selected = model or self.default_model
        option = self._known_models.get(selected) if selected else None
        return option.reasoning_efforts if option else self.reasoning_efforts

    def tiers_for(self, model: str | None) -> list[str]:
        selected = model or self.default_model
        option = self._known_models.get(selected) if selected else None
        return option.service_tiers if option else self.service_tiers
