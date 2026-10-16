"""Bound model-facing reads before registering their actual evidence ranges."""

from collections import Counter

from mcp.types import CallToolResult, TextContent
from pydantic_core import to_json

from .store import WikiError

# Includes the actual FastMCP text result and wrapper, not just body text.
# Transport framing and a client's later context/output limits are separate.
COMPACT_READ_MAX_BYTES = 32768
DOCUMENT_FIELDS = ("id", "path", "revision", "title", "kind", "quality", "characters", "stale")
INDEX_FIELDS = ("freshness", "index_epoch", "index_checked_at", "pending_changes", "index_status", "observer_mode")


def validate_view(view):
    if view not in {"full", "compact"}:
        raise WikiError("Unknown read view", 422)


def model_result_bytes(value):
    """The installed FastMCP dict conversion and CallToolResult JSON encoding.

    The read tools' plain dict annotation has no structured output schema.
    FastMCP uses pydantic_core's indented JSON in TextContent. Test this against
    its actual converter when upgrading MCP or changing the tool return type.
    """
    result = CallToolResult(
        content=[TextContent(type="text", text=to_json(value, indent=2).decode())],
    )
    return len(result.model_dump_json(by_alias=True, exclude_none=True).encode())


def compact_document(document):
    result = {key: document[key] for key in DOCUMENT_FIELDS if key in document}
    if len(result.get("title", "")) > 240:
        result["title"] = result["title"][:240]
        result["title_truncated"] = True
    metadata = document.get("metadata", {})
    warnings = {key: True for key in ("metadata_error", "frontmatter_error", "error") if metadata.get(key)}
    limitations = metadata.get("limitations") or []
    if limitations:
        if not isinstance(limitations, list):
            limitations = [limitations]
        warnings["limitations"] = [str(item)[:240] for item in limitations[:3]]
        warnings["limitations_count"] = len(limitations)
        if len(limitations) > 3 or any(len(str(item)) > 240 for item in limitations[:3]):
            warnings["limitations_truncated"] = True
    sources = Counter(ref.get("status") for ref in metadata.get("source_refs", []) if ref.get("status") in {"changed", "missing"})
    if sources:
        warnings["sources"] = dict(sources)
    if warnings:
        result["warnings"] = warnings
    if document.get("pages"):
        result["page_count"] = len(document["pages"])
    return result


def compact_index(value):
    result = {key: value[key] for key in INDEX_FIELDS if key in value}
    errors = value.get("index_errors", [])
    result["index_errors"] = [{"path": item.get("path", "")[:160], "error": item.get("error", "")[:160]} for item in errors[:3]]
    result["index_error_count"] = len(errors)
    if len(errors) > 3 or any(len(item.get("path", "")) > 160 or len(item.get("error", "")) > 160 for item in errors[:3]):
        result["index_errors_truncated"] = True
    return result


def _measured(value):
    result = value | {"view": "compact", "response_limit_bytes": COMPACT_READ_MAX_BYTES, "response_bytes": 0}
    while True:
        size = model_result_bytes(result)
        if result["response_bytes"] == size:
            return result
        result["response_bytes"] = size


def _fit(build, characters):
    result = _measured(build(characters))
    if result["response_bytes"] <= COMPACT_READ_MAX_BYTES:
        return result
    low, high, selected = 1, characters - 1, None
    while low <= high:
        middle = (low + high) // 2
        candidate = _measured(build(middle))
        if candidate["response_bytes"] <= COMPACT_READ_MAX_BYTES:
            selected, low = candidate, middle + 1
        else:
            high = middle - 1
    if selected is None:
        raise WikiError("Compact read envelope exceeds its byte limit; request fewer documents or use view=full", 422)
    return selected


def compact_read(document):
    base = compact_document(document) | compact_index(document)
    content = document["content"]

    def build(length):
        passage = content[:length]
        shortened = length < len(content)
        return base | {key: document[key] for key in ("page", "start", "line_start")} | {
            "content": passage, "end": document["start"] + length,
            "line_end": document["line_start"] + passage.count("\n"),
            "next_start": document["start"] + length if shortened else document["next_start"],
            "status": "partial" if shortened else "ok", "response_budget_exhausted": shortened,
        }

    return _fit(build, len(content))


def compact_batch(batch):
    base = compact_index(batch) | {"budget_chars": batch["budget_chars"]}
    total = batch["characters_returned"]
    originals = {piece["id"]: piece for piece in batch["ranges"]}

    def build(characters):
        remaining, ranges, replacements = characters, [], {}
        for original in batch["ranges"]:
            count = min(remaining, len(original["content"]))
            if not count and original["content"]:
                continue
            passage = original["content"][:count]
            end = original["start"] + count
            piece = original | {"id": f"{original['doc']}:{original['revision']}:{original['start']}:{end}",
                                "end": end, "content": passage,
                                "line_end": original["line_start"] + passage.count("\n")}
            ranges.append(piece)
            replacements[original["id"]] = piece
            remaining -= count
        items, empty_ranges = [], {}
        for original in batch["items"]:
            if original["status"] not in {"ok", "partial"}:
                items.append(dict(original))
                continue
            piece = replacements.get(original["range_id"])
            if original["start"] == original["end"]:
                # A merged EOF may lie beyond the returned body prefix. Keep
                # it as an explicit empty range, never move its cursor back.
                if piece is None or piece["end"] < original["end"]:
                    source = originals[original["range_id"]]
                    position = original["start"]
                    identifier = f"{source['doc']}:{source['revision']}:{position}:{position}"
                    line = source["line_start"] + source["content"][:position - source["start"]].count("\n")
                    piece = source | {"id": identifier, "start": position, "end": position,
                                      "content": "", "line_start": line, "line_end": line}
                    empty_ranges[identifier] = piece
                items.append(original | {"range_id": piece["id"]})
                continue
            if piece is None or piece["end"] <= original["start"] < original["end"]:
                items.append({key: value for key, value in original.items() if key not in {"start", "end", "range_id"}} |
                             {"status": "budget_exhausted", "next_start": original["start"]})
                continue
            end = min(original["end"], piece["end"])
            item = original | {"range_id": piece["id"], "end": end}
            if end < original["end"]:
                item |= {"status": "partial", "next_start": end}
            items.append(item)
        ranges.extend(empty_ranges.values())
        identifiers = {piece["doc"] for piece in ranges}
        return base | {"items": items, "ranges": ranges,
                       "documents": [compact_document(document) for document in batch["documents"] if document["id"] in identifiers],
                       "characters_returned": characters - remaining,
                       "response_budget_exhausted": characters < total}

    return _fit(build, total)
