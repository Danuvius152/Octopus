import json
import logging
import uuid

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect

from .. import deps
from ..deps import SessionMgr
from ..sessions.turns import QuotaExceeded
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
        delivery.offer(msg)

    session_manager.on_broadcast(conn_id, broadcast, user_id)
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
                session_id = data.get("session_id")
                content = data.get("content", "")
                client_request_id = data.get("client_request_id")
                if client_request_id is not None and (
                    not isinstance(client_request_id, str) or not client_request_id
                    or len(client_request_id) > 128
                ):
                    delivery.offer({"type": "error", "session_id": session_id,
                                    "message": "Invalid client request identity"}, session_scoped=False)
                    continue

                def reject(message: str, session_id=session_id, client_request_id=client_request_id) -> None:
                    # Account-local echo of submitted input, containing no
                    # session content. Missing/foreign resources share it.
                    delivery.offer({
                        "type": "submission" if client_request_id else "error",
                        "session_id": session_id, "message": message,
                        **({"client_request_id": client_request_id, "accepted": False}
                           if client_request_id else {}),
                    }, session_scoped=False)
                raw_aids = data.get("attachment_ids") or []
                attachment_ids = (
                    [a for a in raw_aids if isinstance(a, str)]
                    if isinstance(raw_aids, list)
                    else []
                )
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
                    # The only steerable caller: this is a person typing.
                    # Everything else that injects a turn (bg results,
                    # delegation replies, schedules, research) needs its own
                    # turn, not to land inside an unrelated one.
                    receipt = await session_manager.start_message(
                        session_id,
                        content,
                        attachment_ids=attachment_ids,
                        steerable=True,
                        **({"wiki_mode": mode} if (mode := explicit_mode(content, data.get("wiki_mode"))) else {}),
                    )
                    if not receipt.accepted:
                        reject("This conversation is closed to new messages. Your message was not accepted.")
                    elif client_request_id:
                        delivery.offer({"type": "submission", "session_id": session_id,
                                        "client_request_id": client_request_id, "accepted": True,
                                        "request_id": receipt.request_id,
                                        "disposition": receipt.disposition})
                except QuotaExceeded as e:
                    # Its own branch: this is "you, shortly" rather than
                    # "that request was wrong", and the difference is what the
                    # person needs to read (multi-tenancy.md §8).
                    reject(str(e))
                except ValueError as e:
                    reject(str(e))

            elif msg_type == "interrupt":
                session_id = data.get("session_id")
                session = session_manager.get_session(session_id) if session_id else None
                if session is not None and session.user_id == user_id:
                    await session_manager.interrupt(session_id)

            elif msg_type == "resume_dispatch":
                session_id = data.get("session_id")
                session = session_manager.get_session(session_id) if session_id else None
                if session is not None and session.user_id == user_id:
                    try:
                        await session_manager.resume_dispatch(session_id)
                    except QuotaExceeded as exc:
                        delivery.offer({"type": "error", "session_id": session_id, "message": str(exc)})

            elif msg_type == "approve_tool":
                session_id = data.get("session_id")
                tool_use_id = data.get("tool_use_id")
                session = session_manager.get_session(session_id) if session_id else None
                if session is not None and session.user_id == user_id and tool_use_id:
                    ok = await session_manager.approve_tool(session_id, tool_use_id)
                    if not ok:
                        delivery.offer(
                            {
                                "type": "error",
                                "session_id": session_id,
                                "message": "No pending approval found",
                            }
                        )

            elif msg_type == "deny_tool":
                session_id = data.get("session_id")
                tool_use_id = data.get("tool_use_id")
                reason = data.get("reason", "")
                session = session_manager.get_session(session_id) if session_id else None
                if session is not None and session.user_id == user_id and tool_use_id:
                    ok = await session_manager.deny_tool(
                        session_id, tool_use_id, reason
                    )
                    if not ok:
                        delivery.offer(
                            {
                                "type": "error",
                                "session_id": session_id,
                                "message": "No pending approval found",
                            }
                        )

            elif msg_type == "answer_question":
                session_id = data.get("session_id")
                question_id = data.get("question_id")
                answers = data.get("answers")
                session = session_manager.get_session(session_id) if session_id else None
                if session is not None and session.user_id == user_id and question_id and isinstance(answers, list):
                    ok = await session_manager.answer_question(
                        session_id, question_id, answers
                    )
                    if not ok:
                        delivery.offer(
                            {
                                "type": "error",
                                "session_id": session_id,
                                "message": "No pending question found",
                            }
                        )

            else:
                logger.debug("Ignoring unknown client message type: %s", msg_type)

    except WebSocketDisconnect:
        logger.info("WebSocket client disconnected")
    except Exception:
        logger.exception("WebSocket error")
    finally:
        await delivery.aclose()
