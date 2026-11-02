"""One native connection's wire identities and response waiters, without I/O policy."""

from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import dataclass
from typing import Any

# Reserve native identity metadata inside HarnessRun's 4 MiB JSONL budget.
FRAME_LIMIT_BYTES = 4 * 1024 * 1024 - 4096
RPC_TIMEOUT = 20.0


class NativeProtocolError(RuntimeError):
    pass


class NativeRpcError(NativeProtocolError):
    """Retain the exact RPC error alongside its typed provider facts."""

    def __init__(self, error: dict[str, Any]) -> None:
        self.error = error
        details = error.get("data")
        self.provider_error = {**(details if isinstance(details, dict) else {}), "message": error.get("message")}
        super().__init__(json.dumps(error))


def encode_frame(identifier: str, method: str, params: dict[str, Any]) -> bytes:
    data = (json.dumps({"id": identifier, "method": method, "params": params}, ensure_ascii=False) + "\n").encode()
    if len(data) > FRAME_LIMIT_BYTES:
        raise ValueError(f"Codex JSONL {method} uses {len(data)} bytes; limit is {FRAME_LIMIT_BYTES} bytes")
    return data


@dataclass(frozen=True)
class RpcRequest:
    identifier: str
    data: bytes
    response: asyncio.Future[dict[str, Any]]
    _connection: RpcConnection

    def __enter__(self) -> RpcRequest:
        return self

    def __exit__(self, *_: Any) -> None:
        self._connection._release(self)


class RpcConnection:
    def __init__(self) -> None:
        self.generation = uuid.uuid4().hex
        self._serial = 0
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._closed = False

    def request(self, method: str, params: dict[str, Any]) -> RpcRequest:
        if self._closed:
            raise NativeProtocolError("Native connection is closed")
        self._serial += 1
        identifier = f"octopus:{self._serial}"
        data = encode_frame(identifier, method, params)
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[identifier] = future
        return RpcRequest(identifier, data, future, self)

    def _release(self, request: RpcRequest) -> None:
        self._pending.pop(request.identifier, None)
        if request.response.done() and not request.response.cancelled():
            request.response.exception()

    def waiting(self, identifier: str) -> bool:
        return identifier in self._pending

    def resolve(self, identifier: str, result: dict[str, Any]) -> None:
        future = self._pending.get(identifier)
        if future is not None and not future.done():
            future.set_result(result)

    def reject(self, identifier: str, error: BaseException) -> None:
        future = self._pending.get(identifier)
        if future is not None and not future.done():
            future.set_exception(error)

    def receive(self, frame: dict[str, Any]) -> None:
        identifier = frame.get("id")
        if identifier not in self._pending:
            return
        if "error" in frame:
            self.reject(identifier, NativeRpcError(frame["error"]))
        elif isinstance(frame.get("result"), dict):
            self.resolve(identifier, frame["result"])
        else:
            raise NativeProtocolError("Malformed native RPC response")

    def reject_all(self, error: BaseException, *, exclude: set[str] | None = None) -> None:
        for identifier in self._pending:
            if not exclude or identifier not in exclude:
                self.reject(identifier, error)

    def close(self, error: BaseException, *, exclude: set[str] | None = None) -> None:
        self._closed = True
        self.reject_all(error, exclude=exclude)
