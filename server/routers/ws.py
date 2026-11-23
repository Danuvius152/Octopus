import json
import logging
import uuid

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect
from pydantic import ValidationError

from .. import deps
from ..attachments import AttachmentUnavailable
from ..browser_messages import event_view, unavailable_event
from ..deps import SessionMgr
from ..session_operations import is_utf8_text
from ..sessions import SessionManager
from ..sessions.client_submissions import submit_client_operation
from ..sessions.runtime_types import QuotaExceeded
from ..submission_models import ClientSubmissionRequest
from ..ws_delivery import SocketDelivery

logger = logging.getLogger(__name__)
router = APIRouter()


async def _message_submission(manager: SessionManager, data: dict, user_id: str | None) -> dict:
    session_id = data.get("session_id")
    if not isinstance(session_id, str) or not is_utf8_text(session_id):
        session_id = None
    rejection = {"type": "submission", "session_id": session_id,
                 "client_request_id": data.get("client_request_id"), "accepted": False}
    try:
        request = ClientSubmissionRequest.model_validate({
            key: value for key, value in data.items() if key not in {"type", "session_id"}
        })
    except ValidationError as error:
        errors = error.errors(include_input=False, include_context=False, include_url=False)
        fields = {item["loc"] for item in errors if item["type"] != "extra_forbidden"}
        if ("client_request_id",) in fields:
            return {"type": "error", "session_id": session_id, "message": "Invalid client request identity"}
        if ("recovery_of_client_id",) in fields:
            return {**rejection, "code": "invalid_recovery_identity", "message": "Invalid recovery identity"}
        if any(item["type"] in {"list_type", "string_type", "string_unicode"} and (
            item["loc"][:1] == ("attachment_ids",) or item["loc"][:3] == ("operation", "message", "attachment_ids")
        ) for item in errors):
            return {**rejection, "code": AttachmentUnavailable.code, "message": str(AttachmentUnavailable())}
        detail = errors[0]
        message = "Invalid submission input"
        if detail["type"] == "extra_forbidden":
            message = "Unknown submission field"
        elif detail["loc"][-1:] == ("wiki_mode",):
            message = "Unknown knowledge request mode"
        elif detail["type"] == "string_unicode":
            message = "Input must be valid Unicode text"
        elif detail["type"] == "value_error" and not detail["loc"]:
            message = detail["msg"].removeprefix("Value error, ")
        return {**rejection, "code": "admission_refused", "message": message}

    session = manager.get_session(session_id) if session_id is not None else None
    if session is None or session.user_id != user_id:
        return {**rejection, "message": "Session not found"}
    return {"type": "submission", **await submit_client_operation(manager, session.id, request)}


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
    try:
        principal = await deps.resolve_principal(token)
    except Exception:
        # Uvicorn logs the traceback without the path when no handshake
        # response was sent. Record the path here and let it handle the failure.
        logger.error("WebSocket %s handshake failed during authentication", ws.scope["path"])
        raise
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
        try:
            projected = event_view(msg)
        except Exception:
            logger.exception("Browser event projection failed type=%s", msg.get("type"))
            projected = unavailable_event(msg)
        delivery.offer(projected, session_scoped=msg.get("type") != "session_deleted")

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
            if msg_type == "send_message":
                delivery.offer(await _message_submission(session_manager, data, user_id), session_scoped=False)
                continue

            if msg_type in ("interrupt", "answer_question", "snooze_question", "cancel_compact"):
                command_id = data.get("client_command_id")
                if not isinstance(command_id, str) or not command_id or len(command_id) > 128:
                    delivery.offer({"type": "error", "session_id": data.get("session_id"),
                                    "message": "Invalid client command identity"}, session_scoped=False)
                    continue
            if "client_command_id" in data:
                from ..sessions.commands import process_command
                delivery.offer(await process_command(session_manager, data, user_id, source=principal.answer_source()), session_scoped=False)
                continue

            if msg_type == "resume_dispatch":
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
