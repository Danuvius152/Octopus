"""Explicit-task Wiki tools. Authorization is enforced by the host API."""

import httpx
from mcp.server.fastmcp import FastMCP, Image

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
            "list_documents",
            "graph",
            "inspect_image",
            "jobs",
        }
        if task.mode == "maintain":
            if tasks.is_curator_worker(task):
                names |= {"organize", "publish", "blocks", "restructure"}
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
) -> dict:
    """Find sources in the local Chinese/English chapter index; follow next_offset.

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
def read(
    doc: str,
    start: int = 0,
    length: int = 16000,
    expected_revision: str | None = None,
    page: int | None = None,
) -> dict:
    """Read evidence with a revision and line/page location; follow next_start."""
    return call(
        "GET", "read", params={k: v for k, v in locals().items() if v is not None}
    )


@mcp.tool()
def info() -> dict:
    """Get the selected vault identity, curator and current operation scope; no vault summary."""
    return call("GET", "info")


@mcp.tool()
def list_documents(
    directory: str | None = None,
    offset: int = 0,
    limit: int = 30,
    kind: str | None = None,
    include_assets: bool = False,
) -> dict:
    """Browse a directory when keywords miss; follow next_offset. Assets are opt-in."""
    return call(
        "GET", "documents", params={k: v for k, v in locals().items() if v is not None}
    )


@mcp.tool()
def graph(doc: str) -> dict:
    """Read outgoing and reverse relations from the index without changing notes."""
    return call("GET", "graph", params={"doc": doc})


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
def maintain(
    idempotency_key: str,
    material: str = "",
    scope: str = "保留原文，补充 Key note、必要标签与关联",
    paths: list[str] | None = None,
    target: str | None = None,
    sources: list[dict] | None = None,
) -> dict:
    """Ingest new material OR maintain explicitly named files in a durable Weber task.

    Material is preserved verbatim; target is a new Library/*.md path, otherwise
    Library/Inbox is used. Sources are read-only citations, not write targets.
    Existing paths must be explicitly selected in the user's request. Only
    /wiki maintain grants this action; no scope means no full-vault maintenance.
    """
    return call(
        "POST",
        "maintain",
        body={
            "idempotency_key": idempotency_key,
            "material": material,
            "scope": scope,
            "sources": sources or [],
            "paths": paths or [],
            "target": target,
        },
    )


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
) -> dict:
    """Curator supplements an assigned note, preserving user text and frontmatter.

    Read every chunk of the current note first. Supply a nonempty key_note
    if opening points need supplementing, even if a summary heading exists.
    null preserves trusted existing Weber points; set reuse_existing_summary
    only after judging the user's existing points usable. aliases are a few
    reliable names; native frontmatter stays intact. relations have doc,
    revision, location (L1-L3 or an actual heading), explanation; read their
    cited current passages first. New links use unique vault-path wikilinks.
    Refresh replaces only
    untouched Weber sections; user-edited additions require review. TXT/HTML
    originals stay intact with a sibling Markdown reading note.
    """
    return call(
        "POST",
        "organize",
        body={
            "path": path,
            "expected_revision": expected_revision,
            "reason": reason,
            "key_note": key_note,
            "tags": tags or [],
            "relations": relations or [],
            "sources": sources or [],
            "aliases": aliases,
            "reuse_existing_summary": reuse_existing_summary,
        },
    )


@mcp.tool()
def publish(
    path: str,
    content: str,
    kind: str,
    sources: list[dict],
    expected_revision: str | None,
    reason: str,
) -> dict:
    """Publish a necessary NEW Library concept/topic page with actual source references.
    Never create a source card for every Markdown or change related old pages.
    Source objects have doc, revision, location; conflicts retain the proposal.
    """
    return call("POST", "publish", body=locals())


@mcp.tool()
def blocks(doc: str) -> dict:
    """Read lossless original Markdown blocks before layout changes."""
    return call("GET", "blocks", params={"doc": doc})


@mcp.tool()
def restructure(
    path: str,
    expected_revision: str,
    order: list[str],
    additions: list[dict],
    reason: str,
) -> dict:
    """Reorder every original block once; additions have before, content, sources."""
    return call("POST", "restructure", body=locals())


@mcp.tool()
def jobs(job_id: str | None = None, offset: int = 0, limit: int = 30) -> dict:
    """Inspect maintenance progress and item outcomes."""
    return call(
        "GET",
        f"jobs/{job_id}" if job_id else "jobs",
        params={"offset": offset, "limit": limit},
    )
