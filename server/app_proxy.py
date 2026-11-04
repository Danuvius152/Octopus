"""Original App response-stream cleanup, shared by proxy and Wiki transport."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable

import httpx

from .aio import complete_cleanup
from .app_backends import BackendUse


class _ResponseStream(httpx.AsyncByteStream):
    """Retain the original close acknowledgment, including an unsuccessful one.

    HTTPX sets Response.is_closed before awaiting stream cleanup. That flag
    therefore cannot confirm cleanup after aiter_raw/aread raises while closing.
    """

    def __init__(self, stream: httpx.AsyncByteStream):
        self.stream = stream
        self._close: asyncio.Task[None] | None = None

    async def __aiter__(self) -> AsyncIterator[bytes]:
        async for chunk in self.stream:
            yield chunk

    async def aclose(self) -> None:
        if self._close is None:
            self._close = asyncio.create_task(self.stream.aclose(), name="app-response-close")
        await complete_cleanup(self._join_close())

    async def _join_close(self) -> None:
        assert self._close is not None
        await asyncio.shield(self._close)


def own_response(use: BackendUse, response: httpx.Response) -> Callable[[], Awaitable[None]]:
    assert isinstance(response.stream, httpx.AsyncByteStream)
    stream = _ResponseStream(response.stream)
    response.stream = stream

    async def close() -> None:
        try:
            await response.aclose()
        finally:
            await stream.aclose()

    use.add_cleanup(close)
    return close
