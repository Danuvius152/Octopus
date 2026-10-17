"""Account and live-turn Wiki tools. Authorization is enforced by the host API."""

import json
from typing import Literal

import httpx
from mcp.server.fastmcp import FastMCP, Image

from ..wiki.contracts import (
    GuideModule,
    ReadRequest,
    ReadView,
    ReviewObservation,
    Revision,
)
from . import _host


class TaskWiki(FastMCP):
    async def list_tools(self):
        from ..mcp_identity import current_scope
        from ..wiki.store import WikiError
        from ..wiki.tasks import tasks

        try:
            task = tasks.require(current_scope())
        except WikiError:
            return []
        names = {
            "info",
            "search",
            "read",
            "read_many",
            "list_documents",
            "graph",
            "inspect_image",
            "jobs",
            "load_guide",
            "maintenance_status",
        }
        if task.mode in {"auto", "maintain"}:
            if tasks.is_curator_worker(task):
                names |= {"organize", "publish", "blocks", "restructure", "revise", "load_guide"}
            elif not task.job_id:
                names.add("maintain")
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
        return {"error": result.get("detail", result), "status": response.status_code}
    return result


@mcp.tool()
def search(
    query: str,
    limit: int = 12,
    offset: int = 0,
    kind: str | None = None,
    include_assets: bool = False,
    directory: str | None = None,
    intent: str | None = None,
    view: str = "compact",
    cursor: str | None = None,
) -> dict:
    """Find Markdown navigation; defaults to compact with lookup/question/overview/compare intent.

    All intents, explicit identities, kind and include_assets remain Markdown-only.
    Use read() for an explicitly selected HTML/TXT original.
    Explicit view=full includes all indexed metadata and may be much larger.
    Each document appears once. section.start/end locate evidence for read();
    sections includes extra matched chapters for terms discussed separately.
    pass expected_revision to check freshness. Snippets are navigation only.
    match_mode=lexical_partial means some query words were absent; index_status
    and index_errors distinguish extraction failure from missing knowledge.
    Use directory to narrow an explicitly identified folder; it is a literal
    vault-relative prefix. Preserve named objects when adjusting query wording.
    """
    return call(
        "GET", "search", params={k: v for k, v in locals().items() if v is not None}
    )


@mcp.tool()
def read_many(items: list[ReadRequest], budget_chars: int = 24000, view: ReadView = "compact") -> dict:
    """Read requested current passages once, merging overlaps; no model is called.

    Each item reports ok/partial/changed/not_found/excluded/budget_exhausted.
    Content appears once in ranges; items reference range_id and start/end.
    budget_chars counts unique body Unicode characters. Default compact omits
    full chapter metadata and caps the serialized MCP text result and its
    wrapper at 32768 UTF-8 bytes. This is not a token
    limit or a guarantee against later client truncation. Follow next_start
    after partial/budget results; only returned passages count as read.
    Explicit view=full retains complete metadata and the old body-only budget.
    """
    return call("POST", "read-many", body={"items": [item.model_dump() for item in items], "budget_chars": budget_chars, "view": view})


@mcp.tool()
def read(
    doc: str,
    start: int = 0,
    length: int = 16000,
    expected_revision: str | None = None,
    page: int | None = None,
    view: ReadView = "compact",
) -> dict:
    """Read one current document slice, not the vault; no model is called.

    start/length are Unicode character offsets (default 16000 characters).
    Default compact omits full chapter metadata and bounds the complete MCP
    result at 32768 UTF-8 bytes, so less body may fit. Keep revision and follow
    next_start; only returned content was read. This is not a token limit or
    a guarantee against later client truncation. Use view=full explicitly for
    all metadata with the existing unbounded-envelope behavior.
    """
    return call(
        "GET", "read", params={k: v for k, v in locals().items() if v is not None}
    )


@mcp.tool()
def info() -> dict:
    """Get the selected vault identity, curator and current operation scope; no vault summary."""
    return call("GET", "info")


@mcp.tool()
def load_guide(module: GuideModule) -> dict:
    """Load a needed guide; injected_guides marks the content already injected this turn.

    Reuse available content; reload when that content is missing from context.
    query/retrieval/entry are readable in authorized turns. maintenance
    (patches, sources, recovery), article (metadata/layout) and writing
    (new prose) require a current curator worker assignment.
    """
    return call("GET", "guide", params={"module": module})


@mcp.tool()
def list_documents(
    directory: str | None = None,
    offset: int = 0,
    limit: int = 30,
    kind: str | None = None,
    include_assets: bool = False,
    quality: str | None = None,
    source: str | None = None,
    view: str = "compact",
    cursor: str | None = None,
) -> dict:
    """Browse compact Markdown navigation; follow next_cursor/next_offset.

    kind and include_assets do not include HTML/TXT or other non-Markdown files.
    Use read() for an explicitly selected HTML/TXT original.
    Explicit view=full returns all indexed metadata and may
    be much larger. Navigation does not count as reading the document body.
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

    Defaults to bounded Library Markdown deltas; an explicit doc returns its
    state, including unchanged. Compare current identity/revision with accepted
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
def maintain(
    idempotency_key: str,
    material: str = "",
    scope: str = "保留原文，补充 Key note、必要标签与关联",
    paths: list[str] | None = None,
    target: str | None = None,
    sources: list[dict] | None = None,
    review: ReviewObservation | None = None,
) -> dict:
    """Submit a durable maintenance request to the configured Weber worker.

    This queues work; a job ID alone does not mean maintenance is complete.
    Submission gives the caller no direct document-writing capability.
    Material is preserved verbatim; target is a new Library/*.md path, otherwise
    Library/Inbox is used. Sources are read-only citations, not write targets.
    Selected paths must be eligible Markdown; HTML/TXT originals remain read-only.
    In autonomous mode, choose exact paths relevant to the current task.
    Explicit maintenance mode retains the user's selected paths. The user need
    not type /wiki; no material or paths means no full-vault maintenance.
    Returns a bounded submission receipt with the actual current job status;
    use jobs(job_id=..., view="full", export=true) for complete item evidence.
    For ordinary daily review, copy review and its review_key from the current
    maintenance_status item, set paths to that one current path and material
    empty. This checked branch has a fixed minimal-review purpose; it does not
    accept new material, sources or targets. Other explicit purposes retain the
    original idempotency contract. A comparison never grants file selection.
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
        },
    )
    if isinstance(result.get("status"), int) and result["status"] >= 400:
        return result
    from ..wiki.jobs import WikiJobs

    receipt = WikiJobs.summary(result)
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
    cited current passages first. New links use unique vault-path wikilinks.
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
            "relations": relations or [],
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
) -> dict:
    """Publish a necessary NEW Library concept/topic page with actual source references.
    Never create a source card for every Markdown or change related old pages.
    Source objects have doc, revision, location; conflicts retain the proposal.
    """
    return call("POST", "publish", body={key: value for key, value in locals().items() if value is not None or key == "expected_revision"})


@mcp.tool()
def blocks(doc: str, view: str = "full", ids: list[str] | None = None, expected_revision: str | None = None, budget_chars: int | None = None, offset: int = 0, limit: int | None = None) -> dict:
    """Locate current raw blocks with view=locators, then read selected ids.

    Raw positions are Unicode codepoints and hashes cover original UTF-8 bytes.
    Locators bind the document/revision; locator-only output is not a read.
    offset is zero-based; limit is 1..100. Follow next_offset for more blocks.
    """
    return call("GET", "blocks", params={key: value for key, value in locals().items() if value is not None})


@mcp.tool()
def revise(request: Revision) -> dict:
    """Apply exact sparse raw edits, confirm checked sources, or record no_change.

    Current task, assignment, revision, block hash, original words and actual
    read coverage are required. Prior human edits do not bar maintenance.
    Patch cannot replace a whole document. Other words and omitted fields stay.
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
    from ..wiki.evidence import INLINE_JOB_BYTES, capture_jobs
    if export or len(json.dumps(result, ensure_ascii=False).encode("utf-8")) > INLINE_JOB_BYTES:
        from ..mcp_identity import current_scope
        from ..wiki.jobs import WikiJobs
        artifact = capture_jobs(current_scope(), result)
        summary = WikiJobs.summary(result) if job_id and view == "full" else result if job_id else {"count": len(result["items"]), "next_offset": result["next_offset"]}
        return {"summary": summary, "artifact": artifact}
    return result
