"""Attachment upload/download for sessions.

Two endpoints, both auth-gated by the session-level bearer token:

  POST   /api/sessions/{session_id}/attachments   multipart upload
  GET    /api/sessions/{session_id}/attachments/{attachment_id}

Upload returns AttachmentMetadata so the frontend can render a chip
immediately and remember the id to include in the next send_message
WebSocket frame. The on-disk path stays server-side; clients only ever
see the metadata and fetch the file back via the GET endpoint.

Attachment storage layout + lifecycle live in `server/attachments.py`.
"""

from __future__ import annotations

import logging

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Request,
    status,
)
from fastapi.responses import FileResponse
from fastapi.routing import APIRoute
from starlette.datastructures import FormData, UploadFile
from starlette.formparsers import MultiPartException, MultiPartParser
from starlette.middleware.body_limit import RequestBodyLimitMiddleware
from starlette.types import Receive, Scope, Send

from ..aio import complete_cleanup
from ..attachments import (
    MAX_FILE_BYTES,
    AttachmentError,
    get_path,
    save_upload,
)
from ..auth import verify_token
from ..deps import ScopeUser, SessionMgr, ViewerUser
from ..models import AttachmentMetadata
from ..sessions import SessionManager
from ..sessions.base import Session

logger = logging.getLogger(__name__)

MAX_UPLOAD_BODY_BYTES = 26 * 1024 * 1024


class _AttachmentRoute(APIRoute):
    async def handle(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["method"] == "POST" and self.methods == {"POST"}:
            # The budget belongs only to upload; proxy and MCP streams retain
            # their own contracts. Authentication still precedes the first read.
            await RequestBodyLimitMiddleware(super().handle, MAX_UPLOAD_BODY_BYTES)(scope, receive, send)
        else:
            await super().handle(scope, receive, send)


router = APIRouter(prefix="/api/sessions", tags=["attachments"], route_class=_AttachmentRoute)


class _ParserFile(UploadFile):
    """Join started filesystem work before the parser can close its handle."""

    async def write(self, data: bytes) -> None:
        if self._will_roll(len(data)):
            await complete_cleanup(super().write(data))
        else:
            await super().write(data)

    async def seek(self, offset: int) -> None:
        if self._in_memory:
            await super().seek(offset)
        else:
            await complete_cleanup(super().seek(offset))


class _UploadParser(MultiPartParser):
    _complete = False

    def on_headers_finished(self) -> None:
        super().on_headers_finished()
        file = self._current_part.file
        if file is not None:
            # Keep the original parser-owned spool; only its async I/O must
            # survive Task.cancel() until a started worker has actually ended.
            self._current_part.file = _ParserFile(file.file, size=file.size, filename=file.filename, headers=file.headers)

    def on_end(self) -> None:
        self._complete = True

    async def parse(self) -> FormData:
        form = await super().parse()
        if not self._complete:
            # Starlette 1.7 can return a partial form at EOF, omitting the
            # unfinished file. Its parser still owns these original handles.
            for file in self._files_to_close_on_error:
                file.close()
            raise MultiPartException("Incomplete multipart data")
        return form


def _require_session(
    session_manager: SessionManager, session_id: str, user_id: str | None = None
) -> Session:
    """404 if the session isn't in memory — or isn't this account's. We don't
    allow uploads to archived sessions: they're read-only history."""
    session = session_manager.get_session(session_id, user_id)
    if session is None or session.user_id is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, f"Session {session_id} not found"
        )
    return session


@router.post(
    "/{session_id}/attachments",
    response_model=AttachmentMetadata,
    status_code=status.HTTP_201_CREATED,
    openapi_extra={"requestBody": {"required": True, "content": {"multipart/form-data": {"schema": {
        "type": "object", "required": ["file"],
        "properties": {"file": {"type": "string", "format": "binary"}},
    }}}}},
)
async def upload_attachment(
    session_manager: SessionMgr,
    session_id: str,
    request: Request,
    user_id: ScopeUser = None,
    _: str = Depends(verify_token),
) -> AttachmentMetadata:
    session = _require_session(session_manager, session_id, user_id)
    owner_id = session.user_id
    assert owner_id is not None

    # Explicit parsing keeps authentication and session admission ahead of any
    # multipart consumption. The parser owns its temporary files until success.
    if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "multipart/form-data":
        raise HTTPException(415, "Expected multipart/form-data")
    try:
        form = await _UploadParser(request.headers, request.stream(), max_files=1, max_fields=0).parse()
    except MultiPartException as error:
        raise HTTPException(400, error.message) from error
    try:
        items = form.multi_items()
        if len(items) != 1 or items[0][0] != "file" or not isinstance(items[0][1], UploadFile):
            raise HTTPException(422, "Supply exactly one file named file")
        file = items[0][1]
        # Bound materialization independently of the declared size. The extra
        # byte distinguishes an exact-limit upload from an oversized one.
        content = await complete_cleanup(file.read(MAX_FILE_BYTES + 1))
        if len(content) > MAX_FILE_BYTES:
            raise HTTPException(413, f"attachment exceeds {MAX_FILE_BYTES} bytes")

        try:
            record = save_upload(
                session_id=session_id,
                filename=file.filename or "file",
                content=content,
                declared_mime=file.content_type,
                user_id=owner_id,
            )
        except AttachmentError as e:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e))

        return AttachmentMetadata(id=record.id, filename=record.filename, size=record.size, mime_type=record.mime_type)
    finally:
        # Once parsing succeeds, this route keeps ownership through async close,
        # including repeated cancellation while a rolled file closes in a worker.
        await complete_cleanup(form.close())


@router.get("/{session_id}/attachments/{attachment_id}")
async def download_attachment(
    session_manager: SessionMgr,
    session_id: str,
    attachment_id: str,
    user_id: ViewerUser = None,
) -> FileResponse:
    """`ViewerUser` rather than `verify_token`, because an `<img src>` cannot
    carry an Authorization header — it admits the request from the header, the
    query or the cookie, and says whose sessions it may read."""
    if not await session_manager.session_belongs_to(session_id, user_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Attachment not found")

    # Don't require the session to still exist in memory — once a message
    # references an attachment, the chat history should be able to render
    # the chip / thumbnail even if the session was just archived. Hard delete
    # closes this API through the ownership check above; original paths may
    # remain readable by existing native transcript or response references.
    #
    # Fork fallback (session-rewind.md §5.1 step 5.2): a fork copies only
    # attachment metadata, so resolve from the fork's own dir first, then walk
    # its `forked_from_session_id` ancestors.
    if session_manager.db is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Attachment not found")
    owner_id = await session_manager.db.get_session_owner(session_id)
    if owner_id is None or user_id is not None and owner_id != user_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Attachment not found")
    try:
        path = get_path(session_id, attachment_id, user_id=owner_id)
        if path is None:
            path = await session_manager._fork_attachment_source(session_id, attachment_id, user_id=owner_id)
    except AttachmentError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Attachment not found")
    if path is None or not path.is_file():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Attachment not found")

    # Filename in the response: strip the `<id>__` prefix we use on disk
    # so the browser's "Save As" suggests the user's original name.
    display_name = path.name.split("__", 1)[1] if "__" in path.name else path.name
    return FileResponse(path, filename=display_name)
