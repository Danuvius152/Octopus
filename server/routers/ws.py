import json
import logging
import uuid

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect

from .. import deps
from ..deps import SessionMgr
from ..sessions.runtime_types import QuotaExceeded
from ..ws_delivery import SocketDelivery

logger = logging.getLogger(__name__)
router = APIRouter()


@router.websocket("/ws")
async def websocket_endpoint(session_manager: SessionMgr, ws: WebSocket, token: str = Query(...)):
    """The live stream, for one account.

    This compared the ticket to `OCTOPUS_AUTH_TOKEN` directly rather than going
    through `verify_ws_token` — so it would have kept accepting the retired
    global token and rejecting the session bearers everything else had moved to.
    A WebSocket cannot send an Authorization header, which is why the ticket is
    a query parameter, and why this path has to be kept in step by hand.
    """
    # Accept first, THEN close on a bad token. Closing *before* accept makes
    # the browser see a failed handshake (onclose code 1006), not the 4001 the
    # client keys "this token is dead" on — so it retried forever and the page
    # sat on an empty main view with no way to reach sign-in. Accepting first
    # lets a real 4001 close frame through, which the client turns into the
    # sign-in screen.
    principal = await deps.resolve_principal(token)
    await ws.accept()
    if principal is None or principal.credential_kind not in {"account", "legacy"}:
        await ws.close(code=4001, reason="Unauthorized")
        return
    logger.info("WebSocket client connected")

    user_id = principal.user_id
    conn_id = uuid.uuid4().hex
    delivery = SocketDelivery(ws, session_manager, conn_id, token, principal)

    async def broadcast(msg: dict):
        if msg.get("type") == "question_request" and msg.get("answer_target", "human") != "human":
            return
        # The deletion owner captures the trusted account audience before
        # removal. Its minimal receipt cannot look up the deleted row again.
        delivery.offer(msg, session_scoped=msg.get("type") != "session_deleted")

    session_manager.on_broadcast(conn_id, broadcast, user_id, required=False)
    delivery.start()

    try:
        while not delivery.closed.is_set():
            raw = await delivery.receive()
            if raw is None or not await delivery.authorized():
                return
            await delivery.record_activity()
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                delivery.offer({"type": "error", "message": "Invalid JSON"})
                continue
            if not isinstance(data, dict):
                delivery.offer({"type": "error", "message": "Expected a JSON object"})
                continue

            msg_type = data.get("type")
            if msg_type in ("interrupt", "answer_question", "snooze_question"):
                command_id = data.get("client_command_id")
                if not isinstance(command_id, str) or not command_id or len(command_id) > 128:
                    delivery.offer({"type": "error", "session_id": data.get("session_id"),
                                    "message": "Invalid client command identity"}, session_scoped=False)
                    continue
            if "client_command_id" in data:
                from ..sessions.commands import process_command
                delivery.offer(await process_command(session_manager, data, user_id, source=principal.answer_source()), session_scoped=False)
                continue

            if msg_type == "send_message":
                session_id = data.get("session_id")
                content = data.get("content", "")
                client_request_id = data.get("client_request_id")
                if not isinstance(client_request_id, str) or not client_request_id or len(client_request_id) > 128:
                    delivery.offer({"type": "error", "session_id": session_id,
                                    "message": "Invalid client request identity"}, session_scoped=False)
                    continue

                def reject(message: str, code: str | None = None, session_id=session_id, client_request_id=client_request_id) -> None:
                    # Account-local echo of submitted input, containing no
                    # session content. Missing/foreign resources share it.
                    delivery.offer({
                        "type": "submission", "session_id": session_id, "message": message,
                        "client_request_id": client_request_id, "accepted": False,
                        **({"code": code} if code else {}),
                    }, session_scoped=False)
                from ..attachments import AttachmentUnavailable

                recovery_of = data.get("recovery_of_client_id")
                if recovery_of is not None and (not isinstance(recovery_of, str) or not recovery_of or len(recovery_of) > 128):
                    reject("Invalid recovery identity", code="invalid_recovery_identity")
                    continue

                attachment_ids = data.get("attachment_ids", [])
                if not isinstance(attachment_ids, list) or any(not isinstance(a, str) for a in attachment_ids):
                    reject(str(AttachmentUnavailable()), code=AttachmentUnavailable.code)
                    continue
                # Allow attachment-only turns (no text), but require *some*
                # signal so we don't kick a backend turn off an empty payload.
                if not isinstance(session_id, str) or not session_id or not isinstance(content, str) or (not content and not attachment_ids):
                    reject("session_id and content required")
                    continue

                try:
                    from ..wiki.tasks import explicit_mode
                    session = session_manager.get_session(session_id)
                    if session is None or session.user_id != user_id:
                        raise ValueError("Session not found")
                    from ..sessions.client_submissions import submit_client_message
                    response = await submit_client_message(session_manager, session_id, content, attachment_ids, client_request_id,
                                                           wiki_mode=explicit_mode(content, data.get("wiki_mode")),
                                                           recovery_of_client_id=recovery_of)
                    delivery.offer({"type": "submission", **response}, session_scoped=False)
                except ValueError as e:
                    reject(str(e))

            elif msg_type == "resume_dispatch":
                session_id = data.get("session_id")
                session = session_manager.get_session(session_id) if session_id else None
                if session is not None and session.user_id == user_id and isinstance(session_id, str):
                    try:
                        await session_manager.resume_dispatch(session_id)
                    except QuotaExceeded as exc:
                        delivery.offer({"type": "error", "session_id": session_id, "message": str(exc)})

            else:
                delivery.offer({"type": "error", "message": "Unsupported client message type"}, session_scoped=False)

    except WebSocketDisconnect:
        logger.info("WebSocket client disconnected")
    except Exception:
        logger.exception("WebSocket error")
    finally:
        await delivery.aclose()
