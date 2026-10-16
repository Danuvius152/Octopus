"""One browser connection owns its bounded queue and every socket write.

The session bus only offers frames. A slow transport can end its connection,
but cannot hold result recording, Stop, or an internal business subscriber.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from dataclasses import dataclass
from typing import TYPE_CHECKING

from fastapi import WebSocket, WebSocketDisconnect

from . import deps
from .config import settings

if TYPE_CHECKING:
    from .sessions import SessionManager

logger = logging.getLogger(__name__)
MAX_PENDING_FRAMES = 256
MAX_PENDING_BYTES = 1024 * 1024
SEND_TIMEOUT_SECONDS = 10.0
CLOSE_TIMEOUT_SECONDS = 1.0
TOKEN_ACTIVITY_INTERVAL_SECONDS = 60.0
_connections: set[SocketDelivery] = set()


@dataclass(frozen=True)
class _Frame:
    text: str | None
    size: int
    session_id: str | None
    revoke: bool = False


class SocketDelivery:
    def __init__(self, ws: WebSocket, manager: SessionManager, key: str,
                 token: str, principal: deps.Principal) -> None:
        self.ws = ws
        self.manager = manager
        self.key = key
        self.token = token
        self.principal = principal
        self._queue: deque[_Frame] = deque()
        self._pending_frames = 0
        self._pending_bytes = 0
        self._wake = asyncio.Event()
        self.closed = asyncio.Event()
        self._closing = False
        self._close_code = 1000
        self._close_reason = ""
        self._last_activity: float | None = None
        self._writer: asyncio.Task | None = None
        self._writer_started = False
        self._revocation_pending = False

    def start(self) -> None:
        _connections.add(self)
        self._writer = asyncio.create_task(self._write(), name=f"websocket-writer:{self.key}")

    def offer(self, message: dict, *, session_scoped: bool = True) -> None:
        """Bounded, synchronous admission; never awaits a socket or database."""
        if self._closing or self._revocation_pending:
            return
        auth_only = False
        revoke = False
        if message.get("type") == "auth_token_rotated":
            if self.principal.credential_kind != "legacy":
                # Install credentials do not replace an account/MCP bearer,
                # but this control boundary still validates its live authority.
                auth_only = True
            elif message.get("token") is None:
                # Revocation does not authorize old queued application frames.
                # Deliver only the explicit notice, then close; adoption is
                # checked again by the writer and cannot use this exception.
                self._revocation_pending = revoke = True
                for frame in self._queue:
                    self._pending_frames -= 1
                    self._pending_bytes -= frame.size
                self._queue.clear()
            # The bus is a trusted server boundary. Adopted accounts never get
            # this exception, and the writer still validates the era before send.
            elif isinstance(message.get("token"), str) and message["token"] == settings.auth_token:
                self.token = message["token"]
            else:
                self.request_close(4001, "Unauthorized")
                return
        try:
            text = None if auth_only else json.dumps(
                message, separators=(",", ":"), ensure_ascii=False, allow_nan=False,
            )
        except (TypeError, ValueError):
            logger.exception("WebSocket frame could not be encoded")
            self.request_close(1011, "Reconnect to synchronize")
            return
        size = len(text.encode("utf-8")) if text is not None else 0
        if (self._pending_frames + 1 > MAX_PENDING_FRAMES
                or self._pending_bytes + size > MAX_PENDING_BYTES):
            self.request_close(4001 if revoke else 1013, "Unauthorized" if revoke else "Reconnect to synchronize")
            return
        sid = (message.get("session_id") or message.get("old_session_id")) if session_scoped else None
        self._queue.append(_Frame(text, size, sid if isinstance(sid, str) else None, revoke))
        self._pending_frames += 1
        self._pending_bytes += size
        self._wake.set()

    def request_close(self, code: int = 1000, reason: str = "") -> None:
        if self._closing:
            return
        self._closing = True
        self._close_code, self._close_reason = code, reason
        self.manager.remove_broadcast(self.key)
        self._queue.clear()
        self._pending_frames = self._pending_bytes = 0
        self._wake.set()
        self.closed.set()
        if (self._writer_started and self._writer is not None
                and self._writer is not asyncio.current_task() and not self._writer.done()):
            self._writer.cancel()

    async def authorized(self, session_id: str | None = None) -> bool:
        session = None
        owner = self.principal.user_id
        if session_id is not None and self.principal.user_id is not None:
            session = self.manager.get_session(session_id)
            owner = session.user_id if session is not None else (
                await self.manager.db.get_session_owner(session_id) if self.manager.db else None
            )
        current = await deps.resolve_principal(self.token)
        if self._closing:
            return False
        # Account/token validation is the last await before send. Live ownership
        # is read again here in case it changed during that validation.
        if session is not None:
            owner = session.user_id
        if current != self.principal or owner != self.principal.user_id:
            self.request_close(4001, "Unauthorized")
            return False
        return True

    async def record_activity(self) -> None:
        if self.principal.credential_kind != "account" or deps._user_manager is None:
            return
        now = time.monotonic()
        if self._last_activity is None or now - self._last_activity >= TOKEN_ACTIVITY_INTERVAL_SECONDS:
            self._last_activity = now
            await deps._user_manager.record_token_activity(self.token)

    async def receive(self) -> str | None:
        """Wake the receiver when its writer fails, even if ASGI is still waiting."""
        receive = asyncio.create_task(self.ws.receive_text())
        closing = asyncio.create_task(self.closed.wait())
        try:
            done, _ = await asyncio.wait({receive, closing}, return_when=asyncio.FIRST_COMPLETED)
            if closing in done:
                return None
            return receive.result()
        finally:
            for task in (receive, closing):
                if not task.done():
                    task.cancel()
            await asyncio.gather(receive, closing, return_exceptions=True)

    async def aclose(self, code: int = 1000, reason: str = "") -> None:
        self.request_close(code, reason)
        if self._writer is not None:
            await asyncio.shield(self._writer)

    async def _write(self) -> None:
        self._writer_started = True
        try:
            while not self._closing:
                await self._wake.wait()
                while self._queue and not self._closing:
                    frame = self._queue.popleft()
                    if frame.revoke:
                        if await deps.accounts_exist():
                            self.request_close(4001, "Unauthorized")
                            return
                    elif not await self.authorized(frame.session_id):
                        return
                    if frame.text is not None:
                        await asyncio.wait_for(self.ws.send_text(frame.text), SEND_TIMEOUT_SECONDS)
                    self._pending_frames -= 1
                    self._pending_bytes -= frame.size
                    if frame.revoke:
                        self.request_close(4001, "Unauthorized")
                self._wake.clear()
        except asyncio.CancelledError:
            pass  # The connection owner interrupted an in-flight send.
        except TimeoutError:
            self.request_close(1013, "Reconnect to synchronize")
        except (WebSocketDisconnect, RuntimeError):
            self.request_close()
        except Exception:
            logger.exception("WebSocket writer failed")
            self.request_close(1011, "Reconnect to synchronize")
        finally:
            # This coroutine is the sole sender, including its close frame.
            self._closing = True
            self.manager.remove_broadcast(self.key)
            self._queue.clear()
            self._pending_frames = self._pending_bytes = 0
            self.closed.set()
            try:
                await asyncio.wait_for(
                    self.ws.close(code=self._close_code, reason=self._close_reason),
                    CLOSE_TIMEOUT_SECONDS,
                )
            except (TimeoutError, WebSocketDisconnect, RuntimeError):
                pass
            except Exception:
                logger.debug("WebSocket close failed", exc_info=True)
            finally:
                _connections.discard(self)


async def shutdown_connections() -> None:
    """Drain each connection's sender before the application's DB goes away."""
    owners = list(_connections)
    for owner in owners:
        owner.request_close(1012, "Service restarting")
    if owners:
        await asyncio.gather(*(owner.aclose() for owner in owners))
