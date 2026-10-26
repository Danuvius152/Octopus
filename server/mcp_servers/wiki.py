"""Account and live-turn Wiki tools. Authorization is enforced by the host API."""

from typing import Literal

import httpx
from mcp.server.fastmcp import FastMCP, Image

from ..mcp_result import TEXT_RESULT_MAX_BYTES, bound_tool_handler, model_result_bytes
from ..wiki.contracts import (
    CreationRequest,
    GuideModule,
    QueryKind,
    ReadRequest,
    ReadView,
    ReviewObservation,
    ReviewOrdinals,
    Revision,
)
from . import _host


class TaskWiki(FastMCP):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        bound_tool_handler(self, binary_tools=frozenset({"inspect_image"}))

    def tool(self, *args, **kwargs):
        kwargs.setdefault("structured_output", False)
        return super().tool(*args, **kwargs)

    async def list_tools(self):
        from ..mcp_identity import current_scope
        from ..wiki.store import WikiError
        from ..wiki.tasks import tasks
        from ..wiki.workflow import view

        try:
            scope = current_scope()
            task = tasks.call(tasks.require(scope), scope)
        except WikiError:
            return []
        names = view(task).tools
        return [tool for tool in await super().list_tools() if tool.name in names]


mcp = TaskWiki("octopus-wiki")


def call(method: str, path: str, *, params=None, body=None) -> dict:
    response = httpx.request(
        method,
        f"{_host.api_base()}/api/wiki/{path}",
        headers=_host.headers(),
        params=params,
        json=body,
        timeout=120,
    )
    result = response.json()
    if response.is_error:
        return {"error": result.get("detail", result), "status": response.status_code,
                **{key: result[key] for key in ("code", "hint", "response_limit_bytes") if key in result}}
    return result


@mcp.tool()
def search(
    query: str,
    limit: int = 12,
    offset: int = 0,
    kind: QueryKind | None = None,
    include_assets: bool = False,
    directory: str | None = None,
    intent: str | None = None,
    view: str = "compact",
    cursor: str | None = None,
) -> dict:
    """Find Markdown navigation with lookup/question/overview/compare intent.

    section/sections give revision-bound positions for read(); snippets are not
    reading evidence. compact is default; full adds metadata within the same
    byte limit. lexical_partial indicates absent query terms; inspect index
    status/errors before concluding no knowledge. directory is a literal prefix.
    All filters remain Markdown-only; read explicit HTML/TXT originals directly.
    """
    return call(
        "GET", "search", params={k: v for k, v in locals().items() if v is not None}
    )


@mcp.tool()
def read_many(items: list[ReadRequest], budget_chars: int = 24000, view: ReadView = "compact", reuse_delivered: bool = False) -> dict:
    """Read requested current passages once, merging overlaps; no model is called.

    items report status and reference actual content in ranges by range_id and
    start/end. budget_chars counts unique Unicode characters; the complete MCP
    envelope is separately capped at 32768 UTF-8 bytes in both views. Follow
    next_start for partial/budget results; unseen ranges never count as read.
    full adds metadata; oversized metadata needs compact or fewer documents.
    location reads a cited heading, block or lines directly. Set reuse_delivered
    only while this task's previous text is still in context; already_delivered
    items reference those ranges without returning them again. Omit it to recover
    context. No reading is inherited from another task or revision.
    """
    return call("POST", "read-many", body={"items": [item.model_dump() for item in items], "budget_chars": budget_chars, "view": view, "reuse_delivered": reuse_delivered})


@mcp.tool()
def read(
    doc: str,
    start: int = 0,
    length: int = 16000,
    expected_revision: str | None = None,
    page: int | None = None,
    view: ReadView = "compact",
    location: str | None = None,
    source_cursor: str | None = None,
    source_limit: int = 8,
) -> dict:
    """Read one current document slice, not the vault; no model is called.

    start/length are Unicode character offsets. Keep revision and follow
    next_start; only returned content counts as read. Both views cap the full
    MCP envelope at 32768 UTF-8 bytes, independently of requested characters.
    full adds metadata; oversized metadata requires compact.
    location selects cited lines/heading/block; follow next_start within that
    location. sources includes bounded current dependency facts; source_cursor
    continues only those facts and never grants a body read. Long display labels
    are clipped independently; omitted source facts include recovery pointers to
    the current original declaration, never a substitute reading proof.
    """
    return call(
        "GET", "read", params={k: v for k, v in locals().items() if v is not None}
    )


@mcp.tool()
def info() -> dict:
    """Get the selected vault identity, curator and current operation scope; no vault summary."""
    return call("GET", "info")


@mcp.tool()
def load_guide(module: GuideModule, known_context_digest: str | None = None) -> dict:
    """Load one needed guide, checking current authority on every call.

    Reuse a module already complete in context with its current guide_digest.
    For another module, pass the prior context_digest as known_context_digest
    only while its common rules and operation card remain in context. Matching
    context omits those shared fields; changed context returns them in full.
    Omit the digest after context loss. A digest never grants worker authority.
    """
    return call("GET", "guide", params={key: value for key, value in locals().items() if value is not None})


@mcp.tool()
def list_documents(
    directory: str | None = None,
    offset: int = 0,
    limit: int = 30,
    kind: QueryKind | None = None,
    include_assets: bool = False,
    quality: str | None = None,
    source: str | None = None,
    view: str = "compact",
    cursor: str | None = None,
) -> dict:
    """Browse compact Markdown navigation; follow next_cursor/next_offset.

    kind and include_assets do not include HTML/TXT or other non-Markdown files.
    Use read() for an explicitly selected HTML/TXT original.
    Explicit view=full keeps indexed metadata within the same byte limit.
    Navigation does not count as reading the document body.
    """
    return call(
        "GET", "documents", params={k: v for k, v in locals().items() if v is not None}
    )


@mcp.tool()
def graph(doc: str, offset: int = 0, limit: int = 100, cursor: str | None = None) -> dict:
    """Read outgoing and reverse relations from the index without changing notes."""
    return call("GET", "graph", params={key: value for key, value in locals().items() if value is not None})


@mcp.tool()
def inspect_image(path: str):
    """Inspect an explicitly selected associated image, preserving its bytes; PDF excluded."""
    response = httpx.get(
        f"{_host.api_base()}/api/wiki/image",
        headers=_host.headers(),
        params={"path": path},
        timeout=30,
    )
    if response.is_error:
        return {"error": response.json().get("detail"), "status": response.status_code}
    return Image(
        data=response.content, format=response.headers["content-type"].split("/")[1]
    )


@mcp.tool()
def maintenance_status(doc: str | None = None, directory: str = "Library", view: Literal["state", "links"] = "state", limit: int = 20, cursor: str | None = None) -> dict:
    """Observe maintenance state without reading bodies or granting writes.

    Defaults to Library review/recovery deltas, omitting pure moves; an explicit
    doc also returns unchanged/path history. Compare identity/revision with accepted
    maintenance only, not ordinary saves. Follow next_cursor; changed state
    invalidates old cursors. ready=false is not a clean result. Observations
    do not count as reading the note. For an actionable daily review copy its
    review object and review_key into maintain, using its exact current path.
    Unchanged moves need no semantic job. view=links with doc observes current
    local links matching known historical receipt paths, including unresolved
    inlinks; it neither certifies move causality nor authorizes those sources.
    """
    return call("GET", "maintenance-status", params={k: v for k, v in locals().items() if v is not None})


@mcp.tool()
def maintenance_select(directory: str = "Library", limit: int | None = None) -> dict:
    """Freeze actionable reviews without reading bodies or granting writes.

    Returns an ID, candidate_count, expiration and one bounded page of complete
    reviews, plus bounded deferred work observed at creation. New enqueues do
    not invalidate its cursor. Follow frozen pages with
    maintenance_selection. Admission still checks current facts and authority.
    Overflow requires a narrower directory, never a silently truncated selection.
    """
    return call("POST", "maintenance-selections", body={"directory": directory,
                **({"limit": limit} if limit is not None else {})})


@mcp.tool()
def maintenance_selection(selection_id: str, limit: int | None = None, cursor: str | None = None) -> dict:
    """Read a frozen observation page for the same owner/actor/registration.

    Items retain their original ordinal and full review; their current
    eligibility must be rechecked at maintain admission. Expiration rejects
    new admissions but preserves jobs already accepted with this evidence.
    """
    return call("GET", "maintenance-selections/" + selection_id,
                params={key: value for key, value in {"limit": limit, "cursor": cursor}.items() if value is not None})


@mcp.tool()
def maintain(
    idempotency_key: str,
    material: str = "",
    scope: str = "保留原文，补充 Key note、必要标签与关联",
    paths: list[str] | None = None,
    target: str | None = None,
    sources: list[dict] | None = None,
    review: ReviewObservation | None = None,
    selection_id: str | None = None,
    selection_ordinal: int | None = None,
    creation: CreationRequest | None = None,
) -> dict:
    """Submit a durable maintenance request to the configured Weber worker.

    Queues exact Markdown paths or verbatim new material; the returned job state
    is not completion or direct write authority. Citations and HTML/TXT originals
    stay read-only. Keep the original task's selection; no empty full-vault job.
    New material targets Library/*.md or defaults to Library/Inbox.
    Existing-file maintenance grants no new pages. Explicitly requested pages
    use creation={purpose:'requested_knowledge_page',paths:[...]} (at most 16);
    scoped requests must already select them. Auto may omit paths for one stable
    host-selected target. Workers cannot expand the admitted grant.
    Single daily review copies its current review/review_key and exact path,
    with empty material and no new sources/target. Frozen evidence also supplies
    selection_id and selection_ordinal together. Multiple frozen items should
    use maintain_batch. Other purposes keep their original idempotency contract.
    Only missing item evidence needs jobs(view="full", export=true).
    """
    result = call(
        "POST",
        "maintain",
        body={
            "idempotency_key": idempotency_key,
            "material": material,
            "scope": scope,
            "sources": sources or [],
            "paths": paths or [],
            "target": target,
            **({"review": review.model_dump()} if review is not None else {}),
            **({"selection_id": selection_id} if selection_id is not None else {}),
            **({"selection_ordinal": selection_ordinal} if selection_ordinal is not None else {}),
            **({"creation": creation.model_dump()} if creation is not None else {}),
        },
    )
    if isinstance(result.get("status"), int) and result["status"] >= 400:
        return result
    from ..wiki.jobs import WikiJobs

    receipt = WikiJobs.summary(result)
    if review is not None and result.get("payload", {}).get("request", {}).get("reviews"):
        receipt["aggregation_scope"] = "associated_jobs"
        receipt["selected_items"] = [
            {key: item[key] for key in ("doc", "path", "status", "revision", "output_revision", "receipt_id") if key in item}
            for item in result["items"] if item.get("doc") == review.doc
        ]
    reused = [item for item in result["items"] if item.get("reused") is True]
    if reused:
        limits = {"doc": 128, "output": 512, "output_revision": 128}
        receipt["reused_count"] = len(reused)
        receipt["reused_items"] = [
            {key: item[key][:limit] for key, limit in limits.items() if item.get(key)}
            for item in reused[:3]
        ]
        receipt["reused_items_truncated"] = len(reused) > 3 or any(
            len(item.get(key, "")) > limit
            for item in reused[:3]
            for key, limit in limits.items()
        )
    return receipt


@mcp.tool()
def maintain_batch(selection_id: str, ordinals: ReviewOrdinals) -> dict:
    """Submit one to three exact frozen selection ordinals as a checked review batch.

    Copy the selection ID and choose actionable items from its returned page.
    The host rechecks each observation and original write scope atomically.
    Fresh items share one worker context; original review keys remain reusable.
    Existing work is reused, never merged into another running job. jobs report
    whole-job state; selected_items report each requested document's own state.
    This submits work, grants no direct writes and creates no knowledge pages.
    """
    return call("POST", "maintain-batch", body={"selection_id": selection_id, "ordinals": ordinals})


@mcp.tool()
def organize(
    path: str,
    expected_revision: str,
    reason: str,
    key_note: str | None = None,
    tags: list[str] | None = None,
    relations: list[dict] | None = None,
    sources: list[dict] | None = None,
    aliases: list[str] | None = None,
    reuse_existing_summary: bool = False,
    frontmatter_patch: dict | None = None,
    item_doc: str | None = None,
) -> dict:
    """Curator adapts an assigned article in place; pass frontmatter_patch for OKF adaptation.

    Read every chunk of the current note first. For OKF adaptation, supply
    key_note only when Key note is missing. null preserves existing points;
    set reuse_existing_summary only after judging them usable. Use revise
    for a specific correction or addition to existing points. aliases are a few
    reliable names; native frontmatter stays intact. relations have doc,
    revision, location (L1-L3 or an actual heading), explanation; read their
    cited current passages first. Omit relations to preserve existing managed
    relations; [] explicitly clears them. New links use unique vault-path wikilinks.
    For an existing navigation page outside the assigned body targets, pass
    only relations to current batch documents: the host appends fixed links,
    preserving its body and metadata. This does not complete a batch item.
    Read its topic context with blocks(view="navigation") first; the host checks
    the whole index for existing links. Generic titles/link-only shells need
    relevant context, not automatic full-index reading.
    Maintain Markdown only. TXT/HTML originals are read-only; explicitly select
    an existing paired Markdown note when that is the maintenance target.
    """
    return call(
        "POST",
        "organize",
        body={
            "path": path,
            "expected_revision": expected_revision,
            "reason": reason,
            "key_note": key_note,
            "tags": tags,
            "relations": relations,
            "sources": sources,
            "aliases": aliases,
            "reuse_existing_summary": reuse_existing_summary,
            "frontmatter_patch": frontmatter_patch,
            "item_doc": item_doc,
        },
    )


@mcp.tool()
def publish(
    path: str,
    content: str,
    kind: str,
    sources: list[dict] | None,
    expected_revision: str | None,
    reason: str,
    frontmatter_patch: dict | None = None,
    tags: list[str] | None = None,
    aliases: list[str] | None = None,
    item_doc: str | None = None,
    reservation_id: str | None = None,
) -> dict:
    """Consume an exact pending new-page reservation with actual source references.
    Pass its reservation_id when available; this cannot authorize a new target.
    After creation use organize/revise on that identity for further changes.
    Never create a source card for every Markdown or change related old pages.
    Source objects have doc, revision, location; conflicts retain the proposal.
    """
    return call("POST", "publish", body={key: value for key, value in locals().items() if value is not None or key == "expected_revision"})


@mcp.tool()
def blocks(doc: str, view: str = "full", ids: list[str] | None = None, expected_revision: str | None = None, budget_chars: int | None = None, offset: int = 0, limit: int | None = None, start: int | None = None, tail: int | None = None) -> dict:
    """Read selected raw blocks directly; tail=2 reads the last two for an append.

    tail (1..100) cannot combine with ids or a nonzero offset.
    On continuation omit tail, keep expected_revision and pass next_offset as
    offset plus next_start as start when present; offsets refer to the full note.
    Raw positions are Unicode codepoints and hashes cover original UTF-8 bytes.
    Locators bind the document/revision; locator-only output is not a read.
    offset is zero-based; limit is 1..100. Follow next_offset for more blocks.
    For partial blocks also pass next_start as start. Full block hash/locator
    and start/end stay unchanged; content_start/end cover the returned raw
    Unicode character range. Only those returned characters count as read.
    view="navigation" returns the actual topic heading and introduction for a
    fixed supporting link, with that view retained in the response. Its
    index_deduplication reports the host's whole-index check at publication;
    follow pagination normally. If it is not enough to judge relevance, read the
    relevant raw section or leave the index unchanged. A link-only shell fails.
    """
    return call("GET", "blocks", params={key: value for key, value in locals().items() if value is not None})


@mcp.tool()
def revise(request: Revision) -> dict:
    """Apply exact sparse raw edits, confirm checked sources, or record no_change.

    Current task, assignment, revision, block hash, original words and actual
    read coverage are required. Prior human edits do not bar maintenance.
    Patch cannot replace a whole document. Other words and omitted fields stay.
    Confirm repeated source resources by their original location. Confirmation
    records checked versions in its receipt, preserving article bytes/revision;
    source_refs reports confirmed_revision separately from the original revision.
    readback reports surviving final changes from this job, including structural
    context; follow block_ids on the final revision when required. Confirmation
    adds no new body readback but cannot erase earlier modifications.
    """
    return call("POST", "revise", body=request.model_dump())


@mcp.tool()
def restructure(
    path: str,
    expected_revision: str,
    order: list[str],
    additions: list[dict],
    reason: str,
    frontmatter_patch: dict | None = None,
    tags: list[str] | None = None,
    aliases: list[str] | None = None,
    item_doc: str | None = None,
) -> dict:
    """Reorder every original block once; additions have before, content, sources.

    Heading-only additions insert structure without visible attribution prose.
    New explanations retain attribution; original bytes and private history stay.
    """
    return call("POST", "restructure", body=locals())


@mcp.tool()
def cancel_job(job_id: str, expected_attempt: int | None = None, expected_vault_id: str | None = None) -> dict:
    """Close writes for this job; pending/unconfirmed means execution or recording is not yet settled.

    Supply the attempt and vault from the current job to reject a stale control.
    Previously accepted content and operation receipts are retained.
    """
    return call("POST", f"jobs/{job_id}/cancel", body={"expected_attempt": expected_attempt, "expected_vault_id": expected_vault_id})


@mcp.tool()
def retry_job(job_id: str, expected_attempt: int | None = None, expected_vault_id: str | None = None) -> dict:
    """Resume unfinished work after retirement; completed jobs return their original result.

    Unconfirmed original recording is verified without starting another model.
    Cancellation pending/unconfirmed blocks retry. Original receipts and reread budgets remain.
    An accepted continuation returns to this dispatching request; old chat subscriptions
    do not own its result. Do not pre-review the note merely to resume retained work.
    """
    return call("POST", f"jobs/{job_id}/retry", body={"expected_attempt": expected_attempt, "expected_vault_id": expected_vault_id})


@mcp.tool()
def jobs(job_id: str | None = None, offset: int = 0, limit: int = 30,
         idempotency_key: str | None = None, view: Literal["compact", "full"] = "compact",
         export: bool = False) -> dict:
    """Navigate jobs with small summaries; filter exact idempotency keys on the server.

    Use job_id and view=full for item outcomes. export=true saves the complete
    selected result to a private JSON file and returns its path, bytes and hash.
    Oversize results are exported automatically. Read needed fields from that
    file with a short program; never embed the returned JSON in a shell command.
    """
    if job_id and idempotency_key is not None:
        return {"error": "Choose job_id or idempotency_key", "status": 422}
    result = call(
        "GET",
        f"jobs/{job_id}" if job_id else "jobs",
        params={"offset": offset, "limit": limit, "view": view,
                **({"idempotency_key": idempotency_key} if idempotency_key is not None else {})},
    )
    if isinstance(result.get("status"), int) and result["status"] >= 400:
        return result
    from ..wiki.evidence import capture_jobs
    if export or model_result_bytes(result) > TEXT_RESULT_MAX_BYTES:
        from ..mcp_identity import current_scope
        from ..wiki.jobs import WikiJobs
        artifact = capture_jobs(current_scope(), result)
        summary = WikiJobs.summary(result, exact_key=True) if job_id and view == "full" else result if job_id else {"count": len(result["items"]), "next_offset": result["next_offset"]}
        if job_id and model_result_bytes({"summary": summary, "artifact": artifact}) > TEXT_RESULT_MAX_BYTES:
            # Keep the complete exact key in the authorized artifact rather
            # than returning a clipped key or losing the artifact reference.
            summary = {"id": result["id"], "status": result["status"], "summary_omitted": True,
                       "hint": "Read the artifact for the complete job, including its exact request key."}
        return {"summary": summary, "artifact": artifact}
    return result
