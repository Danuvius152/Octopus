"""Service-owned admission and in-flight observations for an offline handoff.

HTTP response bodies remain owned until ASGI completion. A WebSocket is busy
while processing an input, rather than while waiting for the next input.
"""

from __future__ import annotations

from starlette.responses import JSONResponse


class InstallationAdmission:
    def __init__(self, app):
        self.app = app
        self.http = 0
        self.websocket = 0

    @property
    def idle(self) -> bool:
        return self.http == 0 and self.websocket == 0

    async def __call__(self, scope, receive, send):
        if scope["type"] not in {"http", "websocket"}:
            return await self.app(scope, receive, send)
        state = scope["app"].state
        state.installation_admission = self
        health = getattr(state, "service_health", None)
        control = scope.get("path") in {"/health", "/ready", "/api/installation/quiesce"}
        if health is not None and health.state in {"stopping", "fatal"} and not control:
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1013})
            else:
                await JSONResponse({"detail": "Installation is stopping"}, status_code=503)(scope, receive, send)
            return
        if scope["type"] == "http":
            passive = scope.get("method") == "GET" and scope.get("path", "").startswith("/mcp/")
            tracked = not control and not passive
            self.http += int(tracked)
            try:
                return await self.app(scope, receive, send)
            finally:
                self.http -= int(tracked)
        busy = False

        async def owned_receive():
            nonlocal busy
            if busy:
                self.websocket -= 1
                busy = False
            message = await receive()
            if health is not None and health.state in {"stopping", "fatal"}:
                return {"type": "websocket.disconnect", "code": 1013}
            if message["type"] in {"websocket.connect", "websocket.receive"}:
                busy = True
                self.websocket += 1
            return message

        try:
            return await self.app(scope, owned_receive, send)
        finally:
            if busy:
                self.websocket -= 1
