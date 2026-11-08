"""Authenticated knowledge API shared by human clients and task-scoped agents."""

from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.dependencies.utils import request_body_to_args, request_params_to_args
from fastapi.responses import JSONResponse, Response
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field, StrictInt

from ..auth import verify_token
from ..deps import ScopeUser, SessionMgr
from ..mcp_identity import McpScope, verify
from ..wiki import WikiError, registry
from ..wiki.contracts import (
    CreationRequest,
    GrantReadmissionRequest,
    GuideModule,
    JobControlRequest,
    JobDetail,
    JobOutputPage,
    MaintenanceBatchRequest,
    MaintenanceSelectionRequest,
    QueryKind,
    ReadBatch,
    ReadView,
    ReviewObservation,
    Revision,
    Source,
)
from ..wiki.curation import WikiCuration
from ..wiki.history import WikiHistory
from ..wiki.jobs import WikiJobs
from ..wiki.tasks import tasks
from ..workspace import confine

router = APIRouter(prefix="/api/wiki", tags=["wiki"])
app_router = APIRouter(tags=["wiki-app"])


@app_router.api_route(
    "/apps/{app_id}/wiki/{operation:path}",
    methods=["GET", "POST", "PUT"],
    include_in_schema=False,
)
async def app_wiki(request: Request, app_id: str, operation: str, session_mgr: SessionMgr):
    import anyio
    from pydantic import ValidationError

    from .applications import _application_for_request

    row = await _application_for_request(request, app_id)
    user_id = row.get("user_id")
    if registry.config(user_id).get("app_id") != app_id:
        raise WikiError("This application is not bound to this wiki", 403)
    handlers: dict[tuple[str, str], Callable[..., Any]] = {
        ("GET", "config"): get_config,
        ("GET", "info"): info,
        ("GET", "image"): image,
        ("GET", "search"): search,
        ("GET", "read"): read,
        ("POST", "read-many"): read_many,
        ("GET", "blocks"): get_blocks,
        ("GET", "history"): history,
        ("PUT", "document"): edit_document,
        ("POST", "restore"): restore,
        ("POST", "publish"): publish,
        ("POST", "restructure"): restructure,
        ("POST", "organize"): organize,
        ("POST", "revise"): revise_document,
        ("POST", "snapshot"): snapshot,
    }
    handlers |= {
        ("POST", "maintain"): maintain,
        ("POST", "curate"): curate,
        ("GET", "jobs"): list_jobs,
        ("GET", "documents"): documents,
        ("GET", "graph"): graph,
        ("GET", "maintenance-status"): maintenance_status,
        ("POST", "maintenance-selections"): create_maintenance_selection,
        ("POST", "maintain-batch"): maintain_batch,
        ("GET", "snapshots"): snapshots,
        ("POST", "snapshot/restore"): restore_snapshot,
    }
    if operation.startswith("maintenance-selections/") and request.method == "GET" and len(operation.split("/")) == 2:
        handlers[("GET", operation)] = get_maintenance_selection
    if operation.startswith("jobs/"):
        parts = operation.split("/")
        if len(parts) == 2 and request.method == "GET":
            handlers[("GET", operation)] = get_job
        elif len(parts) == 3 and parts[2] == "details" and request.method == "GET":
            handlers[("GET", operation)] = job_details
        elif len(parts) == 4 and parts[2] == "outputs" and request.method == "GET":
            handlers[("GET", operation)] = job_output
        elif (
            len(parts) == 3
            and request.method == "POST"
            and parts[2] in {"cancel", "retry", "verify"}
        ):
            controls: dict[str, Callable[..., Any]] = {"cancel": cancel_job, "retry": retry_job, "verify": verify_job}
            handlers[("POST", operation)] = controls[parts[2]]
    handler = handlers.get((request.method, operation))
    if handler is None:
        raise WikiError("Knowledge operation not found", 404)
    function = handler
    kwargs = {"request": request, "user_id": user_id, "_": "app"}
    if function is retry_job:
        kwargs["session_mgr"] = session_mgr
    try:
        route = next(route for route in router.routes if isinstance(route, APIRoute) and route.endpoint is function)
        query, errors = request_params_to_args(route.dependant.query_params, request.query_params)
        if errors:
            raise WikiError(f"Invalid knowledge request: {errors}", 422)
        kwargs |= query
        if operation.startswith("jobs/"):
            kwargs["job_id"] = operation.split("/")[1]
            if len(operation.split("/")) == 4:
                kwargs["receipt_id"] = operation.split("/")[3]
        elif operation.startswith("maintenance-selections/"):
            kwargs["selection_id"] = operation.split("/")[1]
        if route.dependant.body_params:
            raw = await request.body()
            content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            is_json = not content_type or content_type == "application/json" or (
                content_type.startswith("application/") and content_type.endswith("+json")
            )
            payload = (await request.json() if is_json else raw) if raw else None
            body, errors = await request_body_to_args(route.dependant.body_params, payload, route._embed_body_fields)
            if errors:
                raise WikiError(f"Invalid knowledge request: {errors}", 422)
            kwargs |= body
    except (ValueError, KeyError, ValidationError) as exc:
        raise WikiError(f"Invalid knowledge request: {exc}", 422)
    # The same handlers enforce task grants when an MCP credential reaches this app path.
    import inspect

    if inspect.iscoroutinefunction(function):
        return await function(**kwargs)
    return await anyio.to_thread.run_sync(lambda: function(**kwargs))


def _mcp_scope(request: Request) -> McpScope | None:
    from .applications import _presented_token

    return verify(_presented_token(request))


def access(
    request: Request,
    *,
    human_only: bool = False,
):
    scope = _mcp_scope(request)
    if scope:
        if human_only:
            raise WikiError("This operation requires the account owner", 403)
        return tasks.call(tasks.require(scope), scope)
    return None


class WikiConfig(BaseModel):
    root: str
    curator_agent_id: str
    app_id: str | None = None


class RecoveryRetention(BaseModel):
    model_config = ConfigDict(extra="forbid")

    doc: str = Field(min_length=1, max_length=512)
    path: str = Field(min_length=1, max_length=4096)
    expected_revision: str = Field(pattern=r"^[0-9a-f]{64}$")


class RecoveryPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    retain: list[RecoveryRetention] = Field(default_factory=list, max_length=5000)


class RecoveryApproval(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plan_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


@router.get("/recovery")
def recovery_inspect(request: Request, user_id: ScopeUser = None, _: str = Depends(verify_token)):
    access(request, human_only=True)
    return registry.recoveries().inspect(user_id)


@router.post("/recovery/export")
def recovery_export(request: Request, user_id: ScopeUser = None, _: str = Depends(verify_token)):
    access(request, human_only=True)
    return registry.recoveries().export(user_id)


@router.post("/recovery/plan")
def recovery_plan(body: RecoveryPlan, request: Request, user_id: ScopeUser = None, _: str = Depends(verify_token)):
    access(request, human_only=True)
    return registry.recoveries().plan(user_id, [entry.model_dump() for entry in body.retain])


@router.get("/recovery/{recovery_id}/plan")
def recovery_full_plan(recovery_id: str, request: Request, user_id: ScopeUser = None, _: str = Depends(verify_token)):
    access(request, human_only=True)
    return registry.recoveries().full_plan(user_id, recovery_id)


@router.post("/recovery/{recovery_id}/approve")
def recovery_approve(recovery_id: str, body: RecoveryApproval, request: Request,
                     user_id: ScopeUser = None, _: str = Depends(verify_token)):
    access(request, human_only=True)
    return registry.recoveries().approve(user_id, recovery_id, body.plan_hash)


@router.post("/recovery/{recovery_id}/apply")
def recovery_apply(recovery_id: str, body: RecoveryApproval, request: Request,
                   user_id: ScopeUser = None, _: str = Depends(verify_token)):
    access(request, human_only=True)
    return registry.recoveries().apply(user_id, recovery_id, body.plan_hash)


@router.get("/recovery/{recovery_id}")
def recovery_status(recovery_id: str, request: Request, user_id: ScopeUser = None, _: str = Depends(verify_token)):
    access(request, human_only=True)
    return registry.recoveries().status(user_id, recovery_id)


@router.get("/documents")
def documents(
    request: Request,
    kind: QueryKind | None = None,
    quality: str | None = None,
    source: str | None = None,
    include_assets: bool = False,
    directory: str | None = None,
    view: Literal["full", "compact"] = "full",
    cursor: str | None = None,
    offset: int = Query(0, ge=0),
    limit: int = Query(30, ge=1, le=100),
    user_id: ScopeUser = None,
    _: str = Depends(verify_token),
):
    task = access(request)
    return registry.store(user_id).documents(
        kind=kind,
        quality=quality,
        source=source,
        offset=offset,
        limit=limit,
        include_assets=include_assets,
        directory=directory,
        view=view,
        cursor=cursor,
        model_response=task is not None,
    )


@router.get("/graph")
def graph(
    request: Request,
    doc: str,
    offset: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=100),
    cursor: str | None = None,
    user_id: ScopeUser = None,
    _: str = Depends(verify_token),
):
    task = access(request)
    return registry.store(user_id).graph(doc, offset, limit, cursor, model_response=task is not None)


@router.get("/maintenance-status")
def maintenance_status(
    request: Request,
    doc: str | None = None,
    directory: str = "Library",
    view: Literal["state", "links"] = "state",
    limit: int = Query(20, ge=1, le=50),
    cursor: str | None = None,
    user_id: ScopeUser = None,
    _: str = Depends(verify_token),
):
    from ..wiki.maintenance_observation import status

    return status(user_id, scope=_mcp_scope(request), app_id=request.path_params.get("app_id"),
                  doc=doc, directory=directory, view=view, limit=limit, cursor=cursor)


@router.post("/maintenance-selections")
def create_maintenance_selection(
    body: MaintenanceSelectionRequest,
    request: Request,
    user_id: ScopeUser = None,
    _: str = Depends(verify_token),
):
    from ..wiki.maintenance_observation import create

    return create(user_id, scope=_mcp_scope(request), app_id=request.path_params.get("app_id"),
                  directory=body.directory, limit=body.limit)


@router.get("/maintenance-selections/{selection_id}")
def get_maintenance_selection(
    selection_id: str,
    request: Request,
    limit: int | None = Query(None, ge=1, le=200),
    cursor: str | None = None,
    user_id: ScopeUser = None,
    _: str = Depends(verify_token),
):
    from ..wiki.maintenance_observation import read

    return read(user_id, selection_id, scope=_mcp_scope(request), app_id=request.path_params.get("app_id"),
                limit=limit, cursor=cursor)


@router.put("/config")
async def configure_wiki(
    body: WikiConfig,
    request: Request,
    session_manager: SessionMgr,
    user_id: ScopeUser = None,
    _: str = Depends(verify_token),
):
    access(request, human_only=True)
    if (
        session_manager.db is None
        or await session_manager.db.get_agent(body.curator_agent_id, user_id) is None
    ):
        raise HTTPException(404, "Curator agent not found")
    roots = await session_manager._owner_extra_roots(user_id)
    root = confine(body.root, user_id=user_id, extra_roots=roots)
    if body.app_id:
        application = await session_manager.db.get_application(body.app_id, user_id)
        if application is None:
            raise WikiError("Application not found", 404)
    import anyio

    return await anyio.to_thread.run_sync(
        lambda: registry.configure(
            user_id, Path(root), body.curator_agent_id, body.app_id
        )
    )


@router.get("/config")
def get_config(
    request: Request, user_id: ScopeUser = None, _: str = Depends(verify_token)
):
    access(request)
    return registry.config(user_id)


@router.get("/info")
def info(request: Request, view: ReadView = "full", user_id: ScopeUser = None, _: str = Depends(verify_token)):
    from ..wiki.capabilities import CAPABILITIES
    from ..wiki.read_response import COMPACT_ERROR_LIMIT, compact_index
    from ..wiki.retention import POLICY
    from ..wiki.workflow import view as policy_view

    task = access(request)
    config = registry.config(user_id)
    store = registry.store(user_id)
    store.synchronize()
    current = policy_view(task) if task else None
    state = store.index_state(error_limit=COMPACT_ERROR_LIMIT if view == "compact" else None)
    return {
        "vault_id": config["vault_id"],
        "curator_agent_id": config["curator_agent_id"],
        "organization": "Library categories, Key note, concept tags, useful links",
        "mode": task.mode if task else "human",
        "history_policy": POLICY,
        "snapshot_creation_enabled": False,
        "capabilities": CAPABILITIES,
        "writable_files": current.card.get("writable_files", []) if current else [],
        "allowed_new_paths": current.card.get("allowed_new_paths", []) if current else [],
        "grant_version": current.card.get("grant_version") if current else None,
        **({"operation": current.card} if current else {}),
        "excluded": [
            "PDF processing",
            "Codex Skills",
            "temporary/static assets in default results",
        ],
        **(compact_index(state) if view == "compact" else state),
    }


@router.get("/guide")
def get_guide(request: Request, module: GuideModule, known_context_digest: str | None = None, user_id: ScopeUser = None, _: str = Depends(verify_token)):
    from ..wiki.workflow import view

    task = access(request)
    scope = verify(request.headers.get("authorization", "").split(" ", 1)[-1])
    if task is None or scope is None or task.user_id != user_id:
        raise WikiError("Wiki guides require an active authorized turn", 403)
    current = view(task)
    response = current.guide_response(module, known_context_digest)
    if task.job_id:
        response['reading'] = tasks.reading_summary(task)
    return response


class SearchSection(BaseModel):
    title: str
    start: int
    end: int
    line_start: int
    line_end: int


class SearchItem(BaseModel):
    id: str
    path: str
    revision: str
    title: str
    kind: str
    quality: str
    metadata: dict[str, Any]
    pages: list[dict]
    stale: bool
    characters: int
    snippet: str
    snippet_start: int
    score: float
    section: SearchSection
    sections: list[SearchSection]
    freshness: Literal["indexed", "live"]


class CompactSearchSection(BaseModel):
    id: str
    heading_path: list[str]
    section_role: str
    start: int
    end: int


class CompactSearchItem(BaseModel):
    id: str
    uid: str | None
    path: str
    title: str
    doc_role: str
    description: str
    revision: str
    freshness: Literal["indexed"]
    matched_fields: list[str]
    sections: list[CompactSearchSection]
    matched_section_count: int
    snippet: str
    snippet_start: int
    stale: bool | None = None
    resource_error: Literal["document_too_large"] | None = None
    error: str | None = None


class SearchResult(BaseModel):
    items: list[SearchItem | CompactSearchItem]
    total: int
    next_offset: int | None
    index_status: str
    index_errors: list[dict[str, str]]
    index_error_count: int
    index_errors_truncated: bool = False
    match_mode: str
    match_explanation: str
    query_terms: list[str]
    index_epoch: int
    index_checked_at: str | None
    pending_changes: int
    view: str
    intent: str
    next_cursor: str | None


@router.get("/search", response_model=SearchResult, response_model_exclude_unset=True)
def search(
    request: Request,
    query: str,
    limit: int = Query(12, ge=1, le=50),
    offset: int = Query(0, ge=0),
    kind: QueryKind | None = None,
    include_assets: bool = False,
    directory: str | None = None,
    intent: Literal["lookup", "question", "overview", "compare"] | None = None,
    view: Literal["full", "compact"] = "full",
    cursor: str | None = None,
    user_id: ScopeUser = None,
    _: str = Depends(verify_token),
):
    task = access(request)
    result = registry.store(user_id).search(query, limit, offset, kind, include_assets, directory, intent, view, cursor, model_response=task is not None)
    # This is the exact fitted model projection: bypass response_model's later
    # defaults/filtering, which could grow the envelope or erase clipping flags.
    # Preserve the existing human envelope default, while absent compact item
    # warnings remain absent through the typed response projection.
    return JSONResponse(result) if task is not None else {"index_errors_truncated": False, **result}


@router.get("/read")
def read(
    request: Request,
    doc: str,
    start: int = Query(0, ge=0),
    length: int = Query(16000, ge=1, le=24000),
    expected_revision: str | None = None,
    page: int | None = Query(None, ge=1),
    view: ReadView = "full",
    location: str | None = None,
    source_cursor: str | None = None,
    source_limit: int = Query(8, ge=1, le=20),
    user_id: ScopeUser = None,
    _: str = Depends(verify_token),
):
    from ..wiki.reading import read as read_document

    return read_document(registry.store(user_id), doc, user_id, start=start, length=length,
                         expected_revision=expected_revision, page=page, view=view, location=location,
                         source_cursor=source_cursor, source_limit=source_limit, task=access(request))


@router.post("/read-many", operation_id="wiki_read_many")
def read_many(body: ReadBatch, request: Request, user_id: ScopeUser = None, _: str = Depends(verify_token)):
    from ..wiki.batch_read import read_many

    return read_many(registry.store(user_id), body.items, body.budget_chars, access(request),
                     view=body.view, reuse_delivered=body.reuse_delivered, user_id=user_id)


@router.get("/image")
def image(
    request: Request,
    path: str,
    user_id: ScopeUser = None,
    _: str = Depends(verify_token),
):
    access(request)
    store = registry.store(user_id)
    target = store.path(path)
    formats = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
        ".gif": "image/gif",
    }
    mime = formats.get(target.suffix.lower())
    if mime is None:
        raise WikiError(
            "Inspect associated PNG/JPEG/WebP/GIF images; PDF content is excluded", 422
        )
    if not target.is_file():
        raise WikiError("Image not found", 404)
    if target.stat().st_size > 10 * 1024 * 1024:
        raise WikiError("Image exceeds the 10 MiB reading limit", 413)
    from ..wiki.history import WikiHistory

    with WikiHistory(store).locked(shared=True):
        return Response(
            target.read_bytes(), media_type=mime, headers={"Cache-Control": "no-store"}
        )


class Edit(BaseModel):
    path: str
    content: str
    expected_revision: str | None
    reason: str


@router.put("/document")
def edit_document(
    body: Edit,
    request: Request,
    user_id: ScopeUser = None,
    _: str = Depends(verify_token),
):
    access(request, human_only=True)
    store = registry.store(user_id)
    if Path(body.path).suffix.lower() not in {".md", ".markdown", ".txt", ".html", ".htm"}:
        raise WikiError("This endpoint edits text documents only")
    return WikiHistory(store).write(
        body.path, body.content.encode(), body.expected_revision, "human", body.reason
    )


@router.get("/history")
def history(
    request: Request,
    path: str,
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    user_id: ScopeUser = None,
    _: str = Depends(verify_token),
):
    access(request)
    store = registry.store(user_id)
    store.path(path)
    return WikiHistory(store).history(path, offset, limit)


class Restore(BaseModel):
    change_id: str
    expected_revision: str | None
    side: str = "before"


@router.post("/restore")
def restore(
    body: Restore,
    request: Request,
    user_id: ScopeUser = None,
    _: str = Depends(verify_token),
):
    access(request, human_only=True)
    return WikiHistory(registry.store(user_id)).restore(
        body.change_id, body.expected_revision, body.side, "human"
    )


@router.post("/snapshot")
def snapshot(
    request: Request, user_id: ScopeUser = None, _: str = Depends(verify_token)
):
    access(request, human_only=True)
    return WikiHistory(registry.store(user_id)).snapshot("Requested knowledge snapshot")


@router.get("/snapshots")
def snapshots(
    request: Request, user_id: ScopeUser = None, _: str = Depends(verify_token)
):
    access(request, human_only=True)
    return WikiHistory(registry.store(user_id)).snapshots()


class SnapshotRestore(BaseModel):
    snapshot_id: str
    paths: list[str]
    expected_revisions: dict[str, str | None]


@router.post("/snapshot/restore")
def restore_snapshot(
    body: SnapshotRestore,
    request: Request,
    user_id: ScopeUser = None,
    _: str = Depends(verify_token),
):
    access(request, human_only=True)
    return WikiHistory(registry.store(user_id)).restore_snapshot(
        body.snapshot_id, body.paths, body.expected_revisions
    )


class Publish(Edit):
    kind: str
    sources: list[Source] | None = None
    frontmatter_patch: dict[str, Any] | None = None
    tags: list[str] | None = None
    aliases: list[str] | None = None
    item_doc: str | None = None
    reservation_id: str | None = None


@router.post("/publish")
def publish(
    body: Publish,
    request: Request,
    user_id: ScopeUser = None,
    _: str = Depends(verify_token),
):
    from ..wiki.content_mutations import publish

    return publish(user_id, scope=_mcp_scope(request), app_id=request.path_params.get("app_id"),
                   **body.model_dump())


@router.get("/blocks")
def get_blocks(
    request: Request,
    doc: str,
    view: str = "full",
    ids: list[str] | None = Query(None),
    expected_revision: str | None = None,
    budget_chars: int | None = Query(None, ge=1, le=96000),
    offset: int = Query(0, ge=0),
    limit: int | None = Query(None, ge=1, le=100),
    start: int | None = None,
    tail: int | None = Query(None, ge=1, le=100),
    user_id: ScopeUser = None,
    _: str = Depends(verify_token),
):
    task = access(request)
    return WikiCuration(registry.store(user_id)).blocks(doc, view=view, ids=ids, expected_revision=expected_revision, budget_chars=budget_chars, offset=offset, limit=limit, start=start, tail=tail, task=task)


@router.post("/revise")
def revise_document(body: Revision, request: Request, user_id: ScopeUser = None, _: str = Depends(verify_token)):
    from ..wiki.content_mutations import revise

    return revise(user_id, body, scope=_mcp_scope(request), app_id=request.path_params.get("app_id"))


class Addition(BaseModel):
    before: int
    content: str
    sources: list[Source] = []


class Relation(Source):
    explanation: str


class Organize(BaseModel):
    path: str
    expected_revision: str
    key_note: str | None = None
    tags: list[str] | None = None
    relations: list[Relation] | None = None
    sources: list[Source] | None = None
    aliases: list[str] | None = None
    reuse_existing_summary: bool = False
    reason: str
    frontmatter_patch: dict[str, Any] | None = None
    item_doc: str | None = None


@router.post("/organize")
def organize(
    body: Organize,
    request: Request,
    user_id: ScopeUser = None,
    _: str = Depends(verify_token),
):
    from ..wiki.content_mutations import organize

    return organize(user_id, scope=_mcp_scope(request), app_id=request.path_params.get("app_id"),
                    **body.model_dump())


class Restructure(BaseModel):
    path: str
    expected_revision: str
    order: list[str]
    additions: list[Addition] = []
    reason: str
    frontmatter_patch: dict[str, Any] | None = None
    tags: list[str] | None = None
    aliases: list[str] | None = None
    item_doc: str | None = None


@router.post("/restructure")
def restructure(
    body: Restructure,
    request: Request,
    user_id: ScopeUser = None,
    _: str = Depends(verify_token),
):
    from ..wiki.content_mutations import restructure

    return restructure(user_id, scope=_mcp_scope(request), app_id=request.path_params.get("app_id"),
                       **body.model_dump())


class Maintenance(BaseModel):
    model_config = ConfigDict(extra="forbid")
    idempotency_key: str
    material: str = ""
    scope: str = "保留原文，补充 Key note、必要标签与关联"
    paths: list[str] = []
    target: str | None = None
    sources: list[Source] = []
    created_changes: dict[str, str] = {}
    review: ReviewObservation | None = None
    selection_id: str | None = None
    selection_ordinal: StrictInt | None = None
    creation: CreationRequest | None = None


@router.post("/maintain")
def maintain(
    body: Maintenance,
    request: Request,
    user_id: ScopeUser = None,
    _: str = Depends(verify_token),
):
    from ..wiki.maintenance_admission import submit

    return submit(
        user_id,
        body.idempotency_key,
        body.material,
        body.scope,
        body.paths,
        body.target,
        [s.model_dump() for s in body.sources],
        body.created_changes,
        review=body.review.model_dump() if body.review is not None else None,
        mcp_scope=_mcp_scope(request),
        app_id=request.path_params.get("app_id"),
        selection_id=body.selection_id,
        selection_ordinal=body.selection_ordinal,
        creation=body.creation.model_dump() if body.creation is not None else None,
    )


@router.post("/maintain-batch")
def maintain_batch(body: MaintenanceBatchRequest, request: Request, user_id: ScopeUser = None, _: str = Depends(verify_token)):
    from ..wiki.maintenance_admission import submit_batch

    return submit_batch(user_id, body.selection_id, body.ordinals,
                        mcp_scope=_mcp_scope(request), app_id=request.path_params.get("app_id"))


class Curate(BaseModel):
    idempotency_key: str
    scope: str = "全量来源登记、保留原文和路径、主题与概念组织、质量问题记录"


@router.post("/curate")
async def curate(
    body: Curate,
    request: Request,
    user_id: ScopeUser = None,
    _: str = Depends(verify_token),
):
    import anyio

    from ..wiki.maintenance_admission import curate

    return await anyio.to_thread.run_sync(
        lambda: curate(user_id, body.idempotency_key, body.scope, mcp_scope=_mcp_scope(request),
                       app_id=request.path_params.get("app_id"))
    )


@router.get("/jobs")
def list_jobs(
    request: Request,
    offset: int = Query(0, ge=0),
    limit: int = Query(30, ge=1, le=100),
    view: Literal["compact", "full"] = "full",
    idempotency_key: str | None = None,
    user_id: ScopeUser = None,
    _: str = Depends(verify_token),
):
    task = access(request)
    return WikiJobs(user_id).list(offset, limit, view=view, idempotency_key=idempotency_key, model_response=task is not None)


@router.get("/jobs/{job_id}")
def get_job(
    job_id: str,
    request: Request,
    view: Literal["compact", "full"] = "full",
    user_id: ScopeUser = None,
    _: str = Depends(verify_token),
):
    task = access(request)
    if view not in {"compact", "full"}:
        raise WikiError("Invalid maintenance view", 422)
    result = WikiJobs(user_id).get(job_id)
    if view == "compact":
        result = WikiJobs.summary(result, exact_key=task is not None)
        if task is not None:
            from ..wiki.model_response import fit_prefix

            return fit_prefix(lambda _: result, 0)
    return result


@router.get("/jobs/{job_id}/details", response_model=JobDetail)
def job_details(
    job_id: str, request: Request, attempt: int | None = Query(None, ge=0),
    offset: int = Query(0, ge=0), limit: int = Query(30, ge=1, le=100),
    user_id: ScopeUser = None, _: str = Depends(verify_token),
):
    access(request)
    from ..wiki.job_details import detail

    return detail(WikiJobs(user_id), job_id, attempt=attempt, offset=offset, limit=limit)


@router.get("/jobs/{job_id}/outputs/{receipt_id}", response_model=JobOutputPage)
def job_output(
    job_id: str, receipt_id: str, request: Request, attempt: int = Query(ge=0),
    view: Literal["history", "current"] = "history", start: int = Query(0, ge=0),
    length: int = Query(16000, ge=1, le=24000), expected_revision: str | None = None,
    user_id: ScopeUser = None, _: str = Depends(verify_token),
):
    access(request)
    from ..wiki.job_details import output_page

    return output_page(WikiJobs(user_id), job_id, receipt_id, attempt=attempt, view=view,
                       start=start, length=length, expected_revision=expected_revision)


@router.post("/jobs/{job_id}/cancel")
async def cancel_job(
    job_id: str,
    request: Request,
    body: JobControlRequest | None = None,
    user_id: ScopeUser = None,
    _: str = Depends(verify_token),
):
    from ..wiki.job_controls import control

    return await control(user_id, job_id, "cancel", body.model_dump() if body else {},
                         mcp_scope=_mcp_scope(request), app_id=request.path_params.get("app_id"))


@router.post("/jobs/{job_id}/retry")
async def retry_job(
    job_id: str,
    request: Request,
    session_mgr: SessionMgr,
    body: JobControlRequest | None = None,
    user_id: ScopeUser = None,
    _: str = Depends(verify_token),
):
    from ..wiki.job_controls import control

    return await control(user_id, job_id, "retry", body.model_dump() if body else {}, manager=session_mgr,
                         mcp_scope=_mcp_scope(request), app_id=request.path_params.get("app_id"))


@router.post("/jobs/{job_id}/verify")
async def verify_job(
    job_id: str, request: Request, body: JobControlRequest,
    user_id: ScopeUser = None, _: str = Depends(verify_token),
):
    from ..wiki.job_controls import control

    return await control(user_id, job_id, "verify", body.model_dump(),
                         mcp_scope=_mcp_scope(request), app_id=request.path_params.get("app_id"))


@router.post("/jobs/{job_id}/readmit")
async def readmit_job(
    job_id: str,
    body: GrantReadmissionRequest,
    request: Request,
    session_mgr: SessionMgr,
    user_id: ScopeUser = None,
    _: str = Depends(verify_token),
):
    from ..wiki.job_controls import control

    return await control(user_id, job_id, "readmit", body.model_dump(), manager=session_mgr,
                         mcp_scope=_mcp_scope(request), app_id=request.path_params.get("app_id"))
