"""In-app file viewer endpoint + `/showme` reference resolver.

Streams a file from the session's working_dir to the browser modal.
`/showme` is a client-only flow: the browser sends the user's reference to
the resolver below (a one-shot model call interprets it in conversation
context), receives a concrete path, then fetches bytes through the
endpoints here. Security gates live in `server/file_viewer.py` so the read
paths can't drift.

Auth: bearer header OR `?token=`. The query-param path exists because
the dialog renders images/PDFs via `<img src>` / `<iframe src>`, and
those can't carry custom headers. Same auth value as the bearer one;
no second weaker token to leak.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, replace
from functools import partial
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import FileResponse, JSONResponse

from ..deps import SessionMgr, ViewerUser
from ..file_viewer import (
    DocumentDirectoryChanged,
    FileNotFound,
    FileTooLarge,
    PathRejected,
    ResolvedFile,
    ResourceDirectory,
    UnsupportedType,
    resolve_safe_path,
)
from ..harness import HarnessOneshotError, get_harness
from ..models import ShowMeResolveRequest, ShowMeResolveResponse
from ..sessions import SessionManager
from ..showme_ai import resolve_local_path, resolve_showme_reference

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/sessions", tags=["files"])


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
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Session not found")
    return working_dir


def _source_directory(
    source_directory: str | None = None, source_directory_id: str | None = None,
) -> ResourceDirectory | None:
    if source_directory is None and source_directory_id is None:
        return None
    if not source_directory or not source_directory_id:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "A source directory and its path ID are required.",
        )
    return ResourceDirectory(source_directory, source_directory_id)


SourceDirectory = Annotated[ResourceDirectory | None, Depends(_source_directory)]


def _resolve_or_raise(
    working_dir: str,
    path: str,
    source_directory: ResourceDirectory | None,
    *,
    canonical: bool = False,
) -> ResolvedFile:
    try:
        return resolve_safe_path(working_dir, path, canonical=canonical, source_directory=source_directory)
    except DocumentDirectoryChanged as e:
        raise HTTPException(status.HTTP_409_CONFLICT, str(e)) from e
    except PathRejected as e:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(e))
    except FileNotFound as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(e))
    except FileTooLarge as e:
        raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, str(e))
    except UnsupportedType as e:
        raise HTTPException(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, str(e))


@router.post("/{session_id}/showme/resolve", response_model=ShowMeResolveResponse)
async def resolve_showme(
    session_manager: SessionMgr,
    session_id: str,
    req: ShowMeResolveRequest,
    user_id: ViewerUser = None,
) -> ShowMeResolveResponse:
    """Resolve a human file reference into a concrete viewer path."""
    session = session_manager.get_session(session_id, user_id)
    if session is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Session not found")
    if session.agent_id is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Agent not found")

    assert session_manager.db is not None
    if await session_manager.db.get_agent_identity(session.agent_id, user_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Agent not found")

    direct = resolve_local_path(req.text, session.working_dir)
    if direct is not None:
        return ShowMeResolveResponse(path=direct)

    agent = await session_manager.db.get_agent(session.agent_id, user_id)
    if agent is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Agent not found")
    harness = get_harness(session.backend)
    # Resolve through the same path the turn engine uses so the harness gets a
    # proper HarnessCredential (decrypted, OAuth-refreshed for Claude, or
    # `home_dir` for Codex), not a raw DB row. Effective id: session override
    # wins over the agent's default (agent-refactor.md §5.2 / decision #2).
    cred_id = session.credential_id or agent.get("credential_id")
    backend, db = session.backend, session_manager.db

    async def run_resolver(ctx):
        credential = await session_manager.resolve_credential_by_id(
            cred_id, backend=backend, context=f"showme resolver for session {session_id}",
            user_id=user_id,
        )
        return await harness.run_oneshot(replace(
            ctx, credential=credential,
            authorize_spawn=partial(db.require_run_authority, user_id, cred_id, backend),
        ))

    try:
        result = await resolve_showme_reference(
            req.text,
            harness=harness,
            model=session.model or agent.get("model"),
            reasoning_effort=session.reasoning_effort or agent.get("reasoning_effort"),
            service_tier=session.service_tier or agent.get("service_tier"),
            credential=None,
            runner=run_resolver,
            working_dir=session.working_dir,
            messages=await session_manager.db.load_messages(session_id, limit=12, newest_first=True),
            session_name=session.name,
        )
    except (ValueError, HarnessOneshotError) as error:
        raise HTTPException(422, str(error)) from error
    except Exception:
        # Don't leak raw model/harness errors (paths, credential labels,
        # tracebacks) to the client; log them for the operator.
        logger.exception("showme resolve failed for session %s", session_id)
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            "Couldn't resolve the file reference.",
        )
    return ShowMeResolveResponse(path=result.path, message=result.message)


@router.get("/{session_id}/files/meta")
async def file_meta(
    session_manager: SessionMgr,
    session_id: str,
    source_directory: SourceDirectory,
    path: str = Query(...),
    user_id: ViewerUser = None,
) -> JSONResponse:
    """Metadata-only sibling of /files.

    The dialog hits this first to decide which renderer to mount and
    whether to skip the bytes fetch entirely (e.g. for an oversized
    file, we show the error inline rather than streaming kilobytes
    that will never render). Same security path as /files.
    """
    working_dir = await _working_dir_for(session_manager, session_id, user_id)
    resolved = _resolve_or_raise(working_dir, path, source_directory)
    return JSONResponse(
        {
            "path": resolved.relative_path,
            "canonical_path_id": resolved.canonical_path_id,
            "canonical_directory": asdict(resolved.canonical_directory),
            "kind": resolved.kind,
            "mime_type": resolved.mime_type,
            "size": resolved.size,
        }
    )


@router.get("/{session_id}/files")
async def get_file(
    session_manager: SessionMgr,
    session_id: str,
    source_directory: SourceDirectory,
    path: str = Query(...),
    preview: bool = False,
    canonical_path_id: str | None = None,
    user_id: ViewerUser = None,
) -> FileResponse:
    working_dir = await _working_dir_for(session_manager, session_id, user_id)
    resolved = _resolve_or_raise(
        working_dir, path, source_directory, canonical=canonical_path_id is not None,
    )
    if canonical_path_id is not None and (
        resolved.relative_path != path or resolved.canonical_path_id != canonical_path_id
    ):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "The file path changed. Reopen the file to read it.",
        )
    # FileResponse handles streaming + ETag + Range. We override
    # media_type so e.g. .md is delivered as text/markdown rather
    # than the default application/octet-stream.
    return FileResponse(
        resolved.abs_path,
        media_type=resolved.mime_type,
        headers={"Cache-Control": "no-store"} if source_directory is not None else None,
        filename=resolved.abs_path.name,
        content_disposition_type="inline" if preview and resolved.kind == "pdf" else "attachment",
    )
