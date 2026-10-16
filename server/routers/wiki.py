"""Authenticated knowledge API shared by human clients and task-scoped agents."""

from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel

from ..auth import verify_token
from ..deps import ScopeUser, SessionMgr
from ..mcp_identity import verify
from ..wiki import WikiError, registry
from ..wiki.contracts import GuideModule, ReadBatch, ReadView, Revision, Source
from ..wiki.curation import WikiCuration
from ..wiki.history import WikiHistory
from ..wiki.jobs import WikiJobs, worker
from ..wiki.tasks import tasks
from ..workspace import confine

router = APIRouter(prefix="/api/wiki", tags=["wiki"])
app_router = APIRouter(tags=["wiki-app"])


@app_router.api_route(
    "/apps/{app_id}/wiki/{operation:path}",
    methods=["GET", "POST", "PUT"],
    include_in_schema=False,
)
async def app_wiki(request: Request, app_id: str, operation: str):
    import anyio
    from pydantic import ValidationError

    from .applications import _app_for_agent_api

    row = await _app_for_agent_api(request, app_id)
    user_id = row.get("user_id")
    if registry.config(user_id).get("app_id") != app_id:
        raise WikiError("This application is not bound to this wiki", 403)
    handlers: dict[
        tuple[str, str], tuple[Callable[..., Any], type[BaseModel] | None]
    ] = {
        ("GET", "config"): (get_config, None),
        ("GET", "info"): (info, None),
        ("GET", "image"): (image, None),
        ("GET", "search"): (search, None),
        ("GET", "read"): (read, None),
        ("POST", "read_many"): (read_many, ReadBatch),
        ("POST", "read-many"): (read_many, ReadBatch),
        ("GET", "blocks"): (get_blocks, None),
        ("GET", "history"): (history, None),
        ("PUT", "document"): (edit_document, Edit),
        ("POST", "restore"): (restore, Restore),
        ("POST", "publish"): (publish, Publish),
        ("POST", "restructure"): (restructure, Restructure),
        ("POST", "organize"): (organize, Organize),
        ("POST", "revise"): (revise_document, None),
        ("POST", "snapshot"): (snapshot, None),
    }
    handlers |= {
        ("POST", "maintain"): (maintain, Maintenance),
        ("POST", "submit"): (submit, Submission),
        ("POST", "curate"): (curate, Curate),
        ("GET", "jobs"): (list_jobs, None),
        ("GET", "documents"): (documents, None),
        ("GET", "graph"): (graph, None),
        ("GET", "snapshots"): (snapshots, None),
        ("POST", "snapshot/restore"): (restore_snapshot, SnapshotRestore),
    }
    if operation.startswith("jobs/"):
        parts = operation.split("/")
        if len(parts) == 2 and request.method == "GET":
            handlers[("GET", operation)] = (get_job, None)
        elif (
            len(parts) == 3
            and request.method == "POST"
            and parts[2] in {"cancel", "retry"}
        ):
            handlers[("POST", operation)] = (
                cancel_job if parts[2] == "cancel" else retry_job,
                None,
            )
    handler = handlers.get((request.method, operation))
    if handler is None:
        raise WikiError("Knowledge operation not found", 404)
    function, schema = handler
    kwargs = {"request": request, "user_id": user_id, "_": "app"}
    try:
        if operation == "revise":
            from pydantic import TypeAdapter

            kwargs["body"] = TypeAdapter(Revision).validate_python(await request.json())
        elif schema:
            kwargs["body"] = schema.model_validate(await request.json())
        elif operation == "search":
            kwargs |= {
                "query": request.query_params["query"],
                "limit": int(request.query_params.get("limit", 12)),
                "offset": int(request.query_params.get("offset", 0)),
                "kind": request.query_params.get("kind"),
                "directory": request.query_params.get("directory"),
                "intent": request.query_params.get("intent"),
                "view": request.query_params.get("view", "full"),
                "cursor": request.query_params.get("cursor"),
                "include_assets": request.query_params.get(
                    "include_assets", "false"
                ).lower()
                == "true",
            }
        elif operation == "read":
            kwargs |= {
                "doc": request.query_params["doc"],
                "start": int(request.query_params.get("start", 0)),
                "length": int(request.query_params.get("length", 16000)),
                "expected_revision": request.query_params.get("expected_revision"),
                "view": request.query_params.get("view", "full"),
                "page": int(request.query_params["page"])
                if "page" in request.query_params
                else None,
            }
        elif operation == "blocks":
            kwargs["doc"] = request.query_params["doc"]
            kwargs |= {"view": request.query_params.get("view", "full"), "ids": request.query_params.getlist("ids") or None, "expected_revision": request.query_params.get("expected_revision"), "budget_chars": int(request.query_params["budget_chars"]) if "budget_chars" in request.query_params else None, "offset": int(request.query_params.get("offset", 0)), "limit": int(request.query_params["limit"]) if "limit" in request.query_params else None}
        elif operation == "image":
            kwargs["path"] = request.query_params["path"]
        elif operation == "history":
            kwargs |= {
                "path": request.query_params["path"],
                "offset": int(request.query_params.get("offset", 0)),
                "limit": int(request.query_params.get("limit", 50)),
            }
        elif operation == "jobs":
            kwargs |= {
                "offset": int(request.query_params.get("offset", 0)),
                "limit": int(request.query_params.get("limit", 30)),
                "view": request.query_params.get("view", "full"),
                "idempotency_key": request.query_params.get("idempotency_key"),
            }
        elif operation == "documents":
            kwargs |= {
                "kind": request.query_params.get("kind"),
                "quality": request.query_params.get("quality"),
                "source": request.query_params.get("source"),
                "offset": int(request.query_params.get("offset", 0)),
                "limit": int(request.query_params.get("limit", 30)),
                "include_assets": request.query_params.get(
                    "include_assets", "false"
                ).lower()
                == "true",
                "directory": request.query_params.get("directory"),
                "view": request.query_params.get("view", "full"),
                "cursor": request.query_params.get("cursor"),
            }
        elif operation == "graph":
            kwargs["doc"] = request.query_params["doc"]
            kwargs |= {"offset": int(request.query_params.get("offset", 0)), "limit": int(request.query_params.get("limit", 100)), "cursor": request.query_params.get("cursor")}
        elif operation.startswith("jobs/"):
            kwargs["job_id"] = operation.split("/")[1]
            if request.method == "GET":
                kwargs["view"] = request.query_params.get("view", "full")
    except (ValueError, KeyError, ValidationError) as exc:
        raise WikiError(f"Invalid knowledge request: {exc}", 422)
    # The same handlers enforce task grants when an MCP credential reaches this app path.
    import inspect

    if inspect.iscoroutinefunction(function):
        return await function(**kwargs)
    return await anyio.to_thread.run_sync(lambda: function(**kwargs))


def access(
    request: Request,
    *,
    human_only: bool = False,
    write: bool = False,
    publish: bool = False,
):
    from .applications import _presented_token

    scope = verify(_presented_token(request))
    if scope:
        if human_only:
            raise WikiError("This operation requires the account owner", 403)
        return tasks.require(scope, write=write, publish=publish)
    return None


class WikiConfig(BaseModel):
    root: str
    curator_agent_id: str
    app_id: str | None = None


@router.get("/documents")
def documents(
    request: Request,
    kind: str | None = None,
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
    access(request)
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
    access(request)
    return registry.store(user_id).graph(doc, offset, limit, cursor)


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
    root = confine(body.root, user_id=user_id)
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
def info(request: Request, user_id: ScopeUser = None, _: str = Depends(verify_token)):
    from ..wiki.capabilities import CAPABILITIES
    from ..wiki.retention import POLICY

    task = access(request)
    config = registry.config(user_id)
    store = registry.store(user_id)
    store.synchronize()
    return {
        "vault_id": config["vault_id"],
        "curator_agent_id": config["curator_agent_id"],
        "organization": "Library categories, Key note, concept tags, useful links",
        "mode": task.mode if task else "human",
        "history_policy": POLICY,
        "snapshot_creation_enabled": False,
        "capabilities": CAPABILITIES,
        "writable_files": list(task.writable) if task else [],
        "excluded": [
            "PDF processing",
            "Codex Skills",
            "temporary/static assets in default results",
        ],
        **store.index_state(),
    }


@router.get("/guide")
def get_guide(request: Request, module: GuideModule, user_id: ScopeUser = None, _: str = Depends(verify_token)):
    from ..wiki.workflow import load_guide

    task = access(request)
    scope = verify(request.headers.get("authorization", "").split(" ", 1)[-1])
    if task is None or scope is None or task.user_id != user_id:
        raise WikiError("Wiki guides require an active authorized turn", 403)
    return {"module": module, "content": load_guide(module, scope.wiki_task_id)}


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


class SearchResult(BaseModel):
    items: list[SearchItem | CompactSearchItem]
    total: int
    next_offset: int | None
    index_status: str
    index_errors: list[dict[str, str]]
    index_error_count: int | None = None
    index_errors_truncated: bool = False
    match_mode: str
    query_terms: list[str]
    index_epoch: int
    index_checked_at: str | None
    pending_changes: int
    view: str
    intent: str
    next_cursor: str | None


@router.get("/search", response_model=SearchResult)
def search(
    request: Request,
    query: str,
    limit: int = Query(12, ge=1, le=50),
    offset: int = Query(0, ge=0),
    kind: str | None = None,
    include_assets: bool = False,
    directory: str | None = None,
    intent: Literal["lookup", "question", "overview", "compare"] | None = None,
    view: Literal["full", "compact"] = "full",
    cursor: str | None = None,
    user_id: ScopeUser = None,
    _: str = Depends(verify_token),
):
    access(request)
    return registry.store(user_id).search(query, limit, offset, kind, include_assets, directory, intent, view, cursor)


@router.get("/read")
def read(
    request: Request,
    doc: str,
    start: int = Query(0, ge=0),
    length: int = Query(16000, ge=1, le=24000),
    expected_revision: str | None = None,
    page: int | None = Query(None, ge=1),
    view: ReadView = "full",
    user_id: ScopeUser = None,
    _: str = Depends(verify_token),
):
    from ..wiki.read_response import compact_read, validate_view

    task = access(request)
    validate_view(view)
    result = registry.store(user_id).read(
        doc, start=start, length=length, expected_revision=expected_revision, page=page
    )
    if view == "compact":
        result = compact_read(result)
    tasks.record_read(task, result)
    return result


@router.post("/read_many", include_in_schema=False)
@router.post("/read-many", operation_id="wiki_read_many")
def read_many(body: ReadBatch, request: Request, user_id: ScopeUser = None, _: str = Depends(verify_token)):
    from ..wiki.batch_read import read_many

    return read_many(registry.store(user_id), body.items, body.budget_chars, access(request), view=body.view)


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


def curator(request: Request, user_id, path: str):
    task = access(request, publish=True)
    config = registry.config(user_id)
    if task and task.agent_id != config["curator_agent_id"]:
        raise WikiError("Only this account's configured curator may publish", 403)
    tasks.require_path(task, registry.store(user_id), path)
    return task.agent_id if task else "human"


class Publish(Edit):
    kind: str
    sources: list[Source] | None = None
    frontmatter_patch: dict[str, Any] | None = None
    tags: list[str] | None = None
    aliases: list[str] | None = None
    item_doc: str | None = None


@router.post("/publish")
def publish(
    body: Publish,
    request: Request,
    user_id: ScopeUser = None,
    _: str = Depends(verify_token),
):
    actor = curator(request, user_id, body.path)
    task = access(request)
    if task:
        for source in body.sources or []:
            tasks.require_source_read(task, registry.store(user_id), source.model_dump())
    return WikiCuration(registry.store(user_id)).publish(
        body.path,
        body.content,
        body.kind,
        [s.model_dump() for s in body.sources] if body.sources is not None else None,
        body.expected_revision,
        actor,
        body.reason,
        task=task,
        frontmatter_patch=body.frontmatter_patch,
        tags=body.tags,
        aliases=body.aliases,
        item_doc=body.item_doc,
    )


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
    user_id: ScopeUser = None,
    _: str = Depends(verify_token),
):
    task = access(request)
    return WikiCuration(registry.store(user_id)).blocks(doc, view=view, ids=ids, expected_revision=expected_revision, budget_chars=budget_chars, offset=offset, limit=limit, task=task)


@router.post("/revise")
def revise_document(body: Revision, request: Request, user_id: ScopeUser = None, _: str = Depends(verify_token)):
    from ..wiki.revise import revise

    store = registry.store(user_id)
    path = store.read(body.doc, length=1)["path"]
    actor = curator(request, user_id, path)
    return revise(WikiCuration(store), body, actor, access(request))


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
    relations: list[Relation] = []
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
    actor = curator(request, user_id, body.path)
    task = access(request)
    tasks.require_read(
        task, registry.store(user_id), body.path, body.expected_revision
    )
    for relation in body.relations:
        tasks.require_source_read(task, registry.store(user_id), relation.model_dump())
    if task:
        for source in body.sources or []:
            tasks.require_source_read(task, registry.store(user_id), source.model_dump())
    return WikiCuration(registry.store(user_id)).organize(
        body.path,
        body.expected_revision,
        body.key_note,
        body.tags,
        [r.model_dump() for r in body.relations],
        [s.model_dump() for s in body.sources] if body.sources is not None else None,
        actor,
        body.reason,
        publication={"job_id": task.job_id, "session_id": task.session_id} if task and task.job_id else None,
        aliases=body.aliases,
        reuse_existing_summary=body.reuse_existing_summary,
        task=task,
        frontmatter_patch=body.frontmatter_patch,
        item_doc=body.item_doc,
    )


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
    actor = curator(request, user_id, body.path)
    task = access(request)
    tasks.require_read(task, registry.store(user_id), body.path, body.expected_revision)
    if task:
        for addition in body.additions:
            for source in addition.sources:
                tasks.require_source_read(task, registry.store(user_id), source.model_dump())
    return WikiCuration(registry.store(user_id)).restructure(
        body.path,
        body.expected_revision,
        body.order,
        [a.model_dump() for a in body.additions],
        actor,
        body.reason,
        task=task,
        frontmatter_patch=body.frontmatter_patch,
        tags=body.tags,
        aliases=body.aliases,
        item_doc=body.item_doc,
    )


class Submission(BaseModel):
    idempotency_key: str
    material: str
    scope: str
    sources: list[Source] = []


class Maintenance(BaseModel):
    idempotency_key: str
    material: str = ""
    scope: str = "保留原文，补充 Key note、必要标签与关联"
    paths: list[str] = []
    target: str | None = None
    sources: list[Source] = []
    created_changes: dict[str, str] = {}


@router.post("/maintain")
def maintain(
    body: Maintenance,
    request: Request,
    user_id: ScopeUser = None,
    _: str = Depends(verify_token),
):
    task = access(request, write=True)
    if task and task.job_id:
        raise WikiError(
            "Finish the assigned job; workers cannot enqueue more maintenance", 403
        )
    tasks.require_selection(task, body.paths)
    return WikiJobs(user_id).maintain(
        body.idempotency_key,
        body.material,
        body.scope,
        body.paths,
        body.target,
        [s.model_dump() for s in body.sources],
        task.agent_id if task else "human",
        body.created_changes,
    )


@router.post("/submit")
def submit(
    body: Submission,
    request: Request,
    user_id: ScopeUser = None,
    _: str = Depends(verify_token),
):
    task = access(request, write=True)
    if task and task.job_id:
        raise WikiError("Workers cannot enqueue more maintenance", 403)
    result = WikiJobs(user_id).submit(
        body.idempotency_key,
        body.material,
        body.scope,
        [s.model_dump() for s in body.sources],
        task.agent_id if task else "human",
    )
    return result


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
    access(request, human_only=True)
    import anyio

    result = await anyio.to_thread.run_sync(
        lambda: WikiJobs(user_id).curate(body.idempotency_key, body.scope)
    )
    worker.wake(user_id)
    return result


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
    access(request)
    return WikiJobs(user_id).list(offset, limit, view=view, idempotency_key=idempotency_key)


@router.get("/jobs/{job_id}")
def get_job(
    job_id: str,
    request: Request,
    view: Literal["compact", "full"] = "full",
    user_id: ScopeUser = None,
    _: str = Depends(verify_token),
):
    access(request)
    if view not in {"compact", "full"}:
        raise WikiError("Invalid maintenance view", 422)
    result = WikiJobs(user_id).get(job_id)
    return WikiJobs.summary(result) if view == "compact" else result


@router.post("/jobs/{job_id}/cancel")
async def cancel_job(
    job_id: str,
    request: Request,
    user_id: ScopeUser = None,
    _: str = Depends(verify_token),
):
    access(request, write=True)
    import anyio

    jobs = await anyio.to_thread.run_sync(lambda: WikiJobs(user_id))
    return await jobs.cancel(job_id, worker.manager)


@router.post("/jobs/{job_id}/retry")
def retry_job(
    job_id: str,
    request: Request,
    user_id: ScopeUser = None,
    _: str = Depends(verify_token),
):
    access(request, write=True)
    return WikiJobs(user_id).retry(job_id)
