"""Model metadata advertised by a runtime, without credentials or config contents."""

from dataclasses import dataclass, field


@dataclass
class ModelOption:
    id: str
    name: str
    reasoning_efforts: list[str] = field(default_factory=list)
    default_reasoning_effort: str | None = None


@dataclass
class ModelSettings:
    models: list[ModelOption] = field(default_factory=list)
    reasoning_efforts: list[str] = field(default_factory=list)
    default_model: str | None = None
    default_reasoning_effort: str | None = None

    def efforts_for(self, model: str | None) -> list[str]:
        selected = model or self.default_model
        option = next((m for m in self.models if m.id == selected), None)
        return option.reasoning_efforts if option else self.reasoning_efforts
