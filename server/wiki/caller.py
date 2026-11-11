"""Immutable request evidence, distinct from an operation's current permission."""

from dataclasses import dataclass

from ..mcp_identity import McpScope
from .store import WikiError


@dataclass(frozen=True)
class AppBinding:
    app_id: str
    registration_generation: str


@dataclass(frozen=True)
class WikiCaller:
    scope: McpScope | None = None
    app_binding: AppBinding | None = None

    @property
    def app_id(self) -> str | None:
        return self.app_binding.app_id if self.app_binding else None

    def require_binding(self, registration: dict) -> None:
        if self.app_binding is None:
            return
        if registration.get("app_id") != self.app_binding.app_id:
            raise WikiError("This application is not bound to this wiki", 403)
        if registration["registration_generation"] != self.app_binding.registration_generation:
            from .maintenance_selections import error

            raise error("registration_changed", "Wiki registration changed; retry with the current binding", 409)

    def task(self, user_id, registration, **policy):
        from .tasks import tasks

        self.require_binding(registration)
        return tasks.caller(user_id, self.scope, registration, app_id=self.app_id, **policy)
