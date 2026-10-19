"""Maintenance restrictions, independent of an execution's current grant."""

from dataclasses import dataclass


@dataclass(frozen=True)
class MaintenanceLimit:
    __pydantic_config__ = {"extra": "forbid"}

    may_submit: bool
    required_selections: tuple[str, ...] = ()

    def restrict(self, other: "MaintenanceLimit") -> "MaintenanceLimit":
        return MaintenanceLimit(
            self.may_submit and other.may_submit,
            tuple(dict.fromkeys((*self.required_selections, *other.required_selections))),
        )
