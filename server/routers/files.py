"""In-app file viewer endpoint + `/showme` reference resolver.

Serves checked file bytes from the original session's authorized sources.
`/showme` uses this file boundary for explicit paths and asks the session's
model to interpret descriptions using recent conversation context.

Auth: bearer header OR `?token=`. The query-param path exists because
the dialog renders images/PDFs via `<img src>` / `<iframe src>`, and
those can't carry custom headers. Same auth value as the bearer one;
no second weaker token to leak.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from contextlib import AbstractContextManager, ExitStack
from dataclasses import replace
from functools import partial
from secrets import token_hex
from typing import Annotated

from anyio import create_task_group, from_thread
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse
from starlette.datastructures import MutableHeaders
from starlette.types import Message, Receive, Scope, Send

from ..aio import complete_cleanup
from ..credential_use import CredentialUse
from ..deps import (
    SessionMgr,
    ViewerToken,
    ViewerUser,
    resolve_viewer_authority,
    resolve_viewer_run_authority,
)
from ..file_viewer import (
    DocumentDirectoryChanged,
    FileChanged,
    FileNotFound,
    FilePickerResult,
    FileSources,
    FileTooLarge,
    FileViewerError,
    PathRejected,
    ResolvedFile,
    ResourceDirectory,
    UnsupportedType,
    WorkingDirectoryUnavailable,
    browse_files,
    file_sources,
    find_files,
    load_safe_file,
    open_session_working_directory,
)
from ..harness import HarnessOneshotError, OneShotContext, get_harness
from ..harness.model_settings import effective_settings
from ..models import (
    FilePickerSources,
    FileViewerMetadata,
    ShowMeEditTarget,
    ShowMeFileErrorResponse,
    ShowMeResolveRequest,
    ShowMeResolveResponse,
)
from ..sessions import SessionManager
from ..sessions.configuration import SessionConfigurationError
from ..sessions.runtime_types import SessionOwnerUnavailable
from ..showme_ai import ShowMeTarget, explicit_showme_target, resolve_showme_reference
from ..workspace import paths_for

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/sessions", tags=["files"])
_NO_STORE = {"Cache-Control": "no-store"}
_FILE_ERRORS = {
    DocumentDirectoryChanged: status.HTTP_409_CONFLICT,
    FileChanged: status.HTTP_409_CONFLICT,
    PathRejected: status.HTTP_403_FORBIDDEN,
    FileNotFound: status.HTTP_404_NOT_FOUND,
    FileTooLarge: status.HTTP_413_CONTENT_TOO_LARGE,
    UnsupportedType: status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
}


class _FileReadHTTPError(HTTPException):
    def __init__(self, error: FileViewerError) -> None:
        super().__init__(_FILE_ERRORS[type(error)], str(error), headers=_NO_STORE)
        self.missing = isinstance(error, FileNotFound) and error.missing


async def _working_dir_for(
    session_manager: SessionManager, session_id: str, user_id: str | None
) -> str:
    """Pull working_dir from the live session or, if archived, from the DB.

    Both halves scoped: these routes hand out the *contents of files*, so a
    session id belonging to another account has to be a 404 rather than a path
    to read from (multi-tenancy.md §5).
    """
    live = session_manager.get_session(session_id, user_id)
    if live is not None:
        return live.working_dir
    working_dir = await session_manager.input_database().get_archived_working_dir(
        session_id, user_id
    )
    if working_dir is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Session not found", headers=_NO_STORE)
    return working_dir


async def _source_directory(
    source_directory: str | None = None, source_directory_id: str | None = None,
) -> ResourceDirectory | None:
    if source_directory is None and source_directory_id is None:
        return None
    if not source_directory or not source_directory_id:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "A source directory and its path ID are required.",
            headers=_NO_STORE,
        )
    return ResourceDirectory(source_directory, source_directory_id)


SourceDirectory = Annotated[ResourceDirectory | None, Depends(_source_directory)]


async def _with_viewer_sources[T](
    session_manager: SessionManager,
    session_id: str,
    token: str,
    operation: Callable[[FileSources], T],
) -> T:
    async def current_access() -> tuple[str | None, str, list[str]]:
        authority = await resolve_viewer_authority(token)
        if authority is None:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid token", headers=_NO_STORE)
        working_dir = await _working_dir_for(session_manager, session_id, authority.user_id)
        return authority.user_id, working_dir, authority.extra_roots

    def read() -> T:
        # Authenticate and load grants after acquiring the worker slot. An
        # admitted read may finish its bounded snapshot if authority later changes.
        user_id, working_dir, extra_roots = from_thread.run(current_access)
        workspace = paths_for(user_id).workspace if user_id is not None else None
        return operation(file_sources(working_dir, workspace=workspace, extra_roots=extra_roots))

    try:
        return await run_in_threadpool(read)
    except FileViewerError as error:
        raise _FileReadHTTPError(error) from error


async def _load_viewer_file(
    session_manager: SessionManager,
    session_id: str,
    token: str,
    path: str,
    source_directory: ResourceDirectory | None,
    *,
    source: str | None = None,
    read_content: bool = False,
    canonical_path_id: str | None = None,
    expected_cwd: os.stat_result | None = None,
) -> tuple[ResolvedFile, bytes]:
    return await _with_viewer_sources(session_manager, session_id, token, lambda sources: load_safe_file(
        sources, path, source=source, read_content=read_content,
        canonical_path_id=canonical_path_id, source_directory=source_directory, expected_cwd=expected_cwd,
    ))


@router.get("/{session_id}/files/sources")
async def viewer_sources(
    session_manager: SessionMgr, session_id: str, token: ViewerToken, response: Response,
) -> FilePickerSources:
    response.headers.update(_NO_STORE)
    return await _with_viewer_sources(session_manager, session_id, token, lambda sources: FilePickerSources(
        str(sources.cwd) if sources.cwd is not None else None, list(map(str, sources.roots)),
    ))


async def _listing_until_disconnect(
    request: Request, session_manager: SessionManager, session_id: str, token: str,
    operation: Callable[[FileSources], FilePickerResult],
) -> FilePickerResult:
    result = failure = None
    async with create_task_group() as tasks:
        async def stop_on_disconnect() -> None:
            while (await request.receive())["type"] != "http.disconnect":
                pass
            tasks.cancel_scope.cancel()

        tasks.start_soon(stop_on_disconnect)
        try:
            result = await _with_viewer_sources(session_manager, session_id, token, operation)
        except HTTPException as error:
            failure = error
        finally:
            tasks.cancel_scope.cancel()
    if failure is not None:
        raise failure
    if result is None:
        raise HTTPException(499, "File listing cancelled.", headers=_NO_STORE)
    return result


@router.get("/{session_id}/files/browse")
async def browse_viewer_source(
    session_manager: SessionMgr, session_id: str, token: ViewerToken, request: Request, response: Response,
    source_directory: SourceDirectory, source: str = "", directory: str = "",
) -> FilePickerResult:
    response.headers.update(_NO_STORE)
    return await _listing_until_disconnect(request, session_manager, session_id, token, partial(
        browse_files, source=source, directory=directory, source_directory=source_directory,
        check_cancelled=from_thread.check_cancelled,
    ))


@router.get("/{session_id}/files/find")
async def find_viewer_files(
    session_manager: SessionMgr, session_id: str, token: ViewerToken, request: Request, response: Response,
    source: str = "", query: str = "",
) -> FilePickerResult:
    response.headers.update(_NO_STORE)
    return await _listing_until_disconnect(request, session_manager, session_id, token, partial(
        find_files, source=source, query=query, check_cancelled=from_thread.check_cancelled,
    ))


class _SnapshotResponse(FileResponse):
    """Serve checked bytes using FileResponse's validators and range dispatch."""

    def __init__(self, metadata: ResolvedFile, content: bytes, *, preview: bool) -> None:
        super().__init__(
            "",  # Fail closed if FileResponse ever dispatches to a path reader.
            media_type=metadata.mime_type,
            filename=metadata.abs_path.name,
            stat_result=metadata.stat_result,
            content_disposition_type="inline" if preview and metadata.kind == "pdf" else "attachment",
        )
        self.content = content

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        async def send_no_store(message: Message) -> None:
            if message["type"] == "http.response.start":
                MutableHeaders(scope=message)["Cache-Control"] = "no-store"
            await send(message)

        await super().__call__(scope, receive, send_no_store)

    async def _handle_simple(self, send: Send, send_header_only: bool, send_pathsend: bool) -> None:
        # Always send the snapshot body, including when the server advertises pathsend.
        await send({"type": "http.response.start", "status": self.status_code, "headers": self.raw_headers})
        await send({"type": "http.response.body", "body": b"" if send_header_only else self.content})

    async def _handle_single_range(
        self, send: Send, start: int, end: int, file_size: int, send_header_only: bool,
    ) -> None:
        headers = MutableHeaders(raw=list(self.raw_headers))
        headers["content-range"] = f"bytes {start}-{end - 1}/{file_size}"
        headers["content-length"] = str(end - start)
        await send({"type": "http.response.start", "status": 206, "headers": headers.raw})
        await send({"type": "http.response.body", "body": b"" if send_header_only else self.content[start:end]})

    async def _handle_multiple_ranges(
        self, send: Send, ranges: list[tuple[int, int]], file_size: int, send_header_only: bool,
    ) -> None:
        boundary = token_hex(13)
        length, make_header = self.generate_multipart(ranges, boundary, file_size, self.headers["content-type"])
        headers = MutableHeaders(raw=list(self.raw_headers))
        headers["content-type"] = f"multipart/byteranges; boundary={boundary}"
        headers["content-length"] = str(length)
        await send({"type": "http.response.start", "status": 206, "headers": headers.raw})
        if send_header_only:
            await send({"type": "http.response.body", "body": b""})
            return
        for start, end in ranges:
            await send({"type": "http.response.body", "body": make_header(start, end), "more_body": True})
            await send({"type": "http.response.body", "body": self.content[start:end], "more_body": True})
            await send({"type": "http.response.body", "body": b"\r\n", "more_body": True})
        await send({"type": "http.response.body", "body": f"--{boundary}--".encode("ascii")})


@router.post(
    "/{session_id}/showme/resolve", response_model=ShowMeResolveResponse, response_model_exclude_unset=True,
    responses={
        **{code: {"model": ShowMeFileErrorResponse} for code in {*_FILE_ERRORS.values(), 401, 502}},
        422: {"description": "Invalid input or unavailable context search", "content": {"application/json": {"schema": {"oneOf": [
            {"$ref": "#/components/schemas/HTTPValidationError"},
            {"$ref": "#/components/schemas/ShowMeFileErrorResponse"},
        ]}}}},
    },
)
async def resolve_showme(
    session_manager: SessionMgr,
    session_id: str,
    req: ShowMeResolveRequest,
    response: Response,
    token: ViewerToken,
    user_id: ViewerUser = None,
    source: str | None = None,
) -> ShowMeResolveResponse | JSONResponse:
    """Check an explicit path or ask the session's model about a description."""
    response.headers.update(_NO_STORE)
    try:
        req.text.encode("utf-8")
        direct = None if req.mode == "description" else explicit_showme_target(req.text)
    except ValueError as error:
        # Keep malformed paths behind the same account and session gate.
        await _working_dir_for(session_manager, session_id, user_id)
        raise HTTPException(422, str(error), headers=_NO_STORE) from error
    if direct is not None:
        if source is not None and direct.source is None:
            direct = replace(direct, source=source)
        return await _resolve_showme_file(session_manager, session_id, token, direct)
    return await _resolve_showme_description(session_manager, session_id, token, user_id, req.text)


def _showme_error(status_code: int, failure: ShowMeFileErrorResponse) -> JSONResponse:
    return JSONResponse(failure.model_dump(exclude_unset=True, exclude_none=True), status_code=status_code, headers=_NO_STORE)


async def _resolve_showme_file(
    session_manager: SessionManager, session_id: str, token: str, file: ShowMeTarget, *,
    description: str | None = None, expected_cwd: os.stat_result | None = None,
) -> ShowMeResolveResponse | JSONResponse:
    target = ShowMeEditTarget(path=file.path)
    if file.position is not None:
        target.position = file.position
    if file.source is not None:
        target.source = file.source
    try:
        resolved, _ = await _load_viewer_file(
            session_manager, session_id, token, file.requested_path, None, source=file.source, expected_cwd=expected_cwd,
        )
    except _FileReadHTTPError as error:
        failure = ShowMeFileErrorResponse(detail=error.detail, description=description)
        if description is None or error.status_code != 403:
            failure.target = target
        if file.ambiguous_filename and error.missing:
            failure.can_search_description = True
        return _showme_error(error.status_code, failure)
    metadata = _file_metadata(resolved)
    # Re-select the checked source when an older client opens the path.
    # The canonical absolute path can instead select a narrower grant.
    path = metadata.absolute_path if file.source is not None else file.requested_path
    if resolved.is_cwd_source:
        path = f"./{metadata.path}" if metadata.path.startswith("~") else metadata.path
    result = ShowMeResolveResponse(path=path, message=None, metadata=metadata, target=target)
    if description is not None:
        result.description = description
    return result


async def _resolve_showme_description(
    session_manager: SessionManager, session_id: str, token: str, user_id: str | None, description: str,
) -> ShowMeResolveResponse | JSONResponse:
    try:
        return await _infer_showme_file(session_manager, session_id, token, user_id, description)
    except WorkingDirectoryUnavailable as error:
        return _showme_error(403, ShowMeFileErrorResponse(
            detail=str(error), description=description, code="working_directory_unavailable",
        ))
    except HTTPException as error:
        return _showme_error(error.status_code, ShowMeFileErrorResponse(detail=error.detail, description=description))
    except (SessionConfigurationError, SessionOwnerUnavailable) as error:
        return _showme_error(
            error.status_code if isinstance(error, SessionConfigurationError) else 409,
            ShowMeFileErrorResponse(detail=str(error), description=description),
        )
    except (ValueError, HarnessOneshotError) as error:
        return _showme_error(422, ShowMeFileErrorResponse(detail=str(error), description=description))
    except Exception:
        logger.exception("showme resolve failed for session %s", session_id)
        return _showme_error(502, ShowMeFileErrorResponse(
            detail="Couldn't resolve the file reference.", description=description,
        ))


async def _infer_showme_file(
    session_manager: SessionManager, session_id: str, token: str, user_id: str | None, description: str,
) -> ShowMeResolveResponse | JSONResponse:
    if not description.strip():
        raise ValueError("Enter a file description.")
    live = session_manager.get_session(session_id, user_id)
    if live is None:
        raise HTTPException(404, "Session not found")
    session_manager.require_independent_session_admission(live)
    assert session_manager.db is not None
    db = session_manager.db
    if live.agent_id is None or await db.get_agent_identity(live.agent_id, user_id) is None:
        raise HTTPException(404, "Agent not found")

    session, agent = await session_manager.capture_request_configuration(live)
    if agent is None:
        raise HTTPException(404, "Agent not found")
    working_dir = session.working_dir
    if user_id is None:
        raise HTTPException(401, "Invalid token")
    workspace = paths_for(user_id).workspace
    backend = session.backend
    harness = get_harness(backend)
    effective = effective_settings(session, agent)
    cred_id = effective["credential_id"]

    async def authorize_spawn(use: CredentialUse | None) -> AbstractContextManager[int]:
        authority = await resolve_viewer_run_authority(token, user_id, cred_id, backend, use)
        if authority is None or authority.user_id is None:
            raise HTTPException(401, "Invalid token")
        try:
            session_manager.require_independent_session_admission(live)
        except SessionConfigurationError as error:
            raise HTTPException(error.status_code, str(error)) from error
        if live.working_dir != working_dir:
            raise WorkingDirectoryUnavailable()
        return open_session_working_directory(working_dir, workspace, authority.extra_roots, expected=expected_cwd)

    async def run_resolver(ctx: OneShotContext) -> str:
        credential = await session_manager.resolve_credential_by_id(
            cred_id, backend=backend, context=f"showme resolver for session {session_id}",
            user_id=user_id,
        )
        return await harness.run_oneshot(replace(
            ctx, credential=credential, authorize_spawn=partial(authorize_spawn, credential.use if credential else None),
        ))

    # Keep the original directory alive through candidate validation so its
    # inode cannot be reused. The spawn owns its separately authorized handle.
    with ExitStack() as retained_directory:
        def pin_initial_directory() -> os.stat_result:
            authority = from_thread.run(resolve_viewer_authority, token)
            if authority is None or authority.user_id != user_id:
                raise HTTPException(401, "Invalid token")
            fd = retained_directory.enter_context(open_session_working_directory(working_dir, workspace, authority.extra_roots))
            return os.fstat(fd)

        # Join the worker before closing the stack, including when the request is cancelled.
        expected_cwd = await complete_cleanup(run_in_threadpool(pin_initial_directory))
        result = await resolve_showme_reference(
            description,
            harness=harness,
            model=effective["model"],
            reasoning_effort=effective["reasoning_effort"],
            service_tier=effective["service_tier"],
            credential=None,
            runner=run_resolver,
            working_dir=working_dir,
            messages=await db.load_messages(session_id, limit=12, newest_first=True),
            session_name=session.name,
        )
        if result.path is None:
            return ShowMeResolveResponse(path=None, message=result.message, description=description)
        result.path.encode("utf-8")
        return await _resolve_showme_file(
            session_manager, session_id, token, ShowMeTarget(result.path, quoted=True),
            description=description, expected_cwd=expected_cwd,
        )


async def _viewer_metadata(
    session_manager: SessionMgr,
    session_id: str,
    source_directory: SourceDirectory,
    token: ViewerToken,
    path: str = Query(...),
    source: str | None = None,
) -> ResolvedFile:
    resolved, _ = await _load_viewer_file(session_manager, session_id, token, path, source_directory, source=source)
    return resolved


async def _viewer_snapshot(
    session_manager: SessionMgr,
    session_id: str,
    source_directory: SourceDirectory,
    token: ViewerToken,
    path: str = Query(...),
    source: str | None = None,
    canonical_path_id: str | None = None,
    preview: bool = False,
    frame: bool = False,
) -> Response:
    try:
        resolved, content = await _load_viewer_file(
            session_manager, session_id, token, path, source_directory,
            source=source, read_content=True, canonical_path_id=canonical_path_id,
        )
    except HTTPException as error:
        if not (preview and frame):
            raise
        # An iframe load event cannot expose its navigation's HTTP status.
        return JSONResponse(
            {"detail": error.detail, "status": error.status_code},
            status_code=error.status_code, headers=error.headers,
        )
    return _SnapshotResponse(resolved, content, preview=preview)


@router.get("/{session_id}/files/meta")
async def file_meta(
    resolved: Annotated[ResolvedFile, Depends(_viewer_metadata)],
    response: Response,
) -> FileViewerMetadata:
    """Metadata-only sibling of /files.

    Supplies the renderer and the source binding before any byte request.
    Explicit /showme commands receive the same metadata in their response.
    """
    response.headers.update(_NO_STORE)
    return _file_metadata(resolved)


def _file_metadata(resolved: ResolvedFile) -> FileViewerMetadata:
    return FileViewerMetadata(
        source=str(resolved.source_root),
        path=resolved.relative_path,
        absolute_path=str(resolved.abs_path),
        canonical_path_id=resolved.canonical_path_id,
        canonical_directory=resolved.canonical_directory,
        kind=resolved.kind,
        mime_type=resolved.mime_type,
        size=resolved.size,
    )


@router.get("/{session_id}/files")
async def get_file(snapshot: Annotated[Response, Depends(_viewer_snapshot)]) -> Response:
    return snapshot
