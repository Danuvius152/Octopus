"""Validation responses that can safely describe malformed JSON input."""

from collections.abc import Callable, Coroutine
from typing import Any

from fastapi import Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from fastapi.routing import APIRoute

from ..session_operations import is_utf8_text


class SafeInputValidationRoute(APIRoute):
    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        handle = super().get_route_handler()

        async def handle_input(request: Request) -> Response:
            try:
                return await handle(request)
            except RequestValidationError as error:
                # Raw values and error context need not be serializable as JSON.
                detail = [{
                    "type": item["type"],
                    "loc": [part if isinstance(part, int) or is_utf8_text(part) else "<invalid field>"
                            for part in item["loc"]],
                    "msg": item["msg"] if is_utf8_text(item["msg"]) else "Input must be valid Unicode text",
                } for item in error.errors()]
                return JSONResponse(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, content={"detail": detail})

        return handle_input
