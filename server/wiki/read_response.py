"""Bound model-facing reads before registering their actual evidence ranges."""

from collections import Counter

from ..mcp_result import model_result_bytes as model_result_bytes
from .line_coordinates import LineCoordinates
from .model_response import fit_prefix, presentation
from .store import WikiError

# Includes the actual FastMCP text result and wrapper, not just body text.
# Transport framing and a client's later context/output limits are separate.
COMPACT_READ_MAX_BYTES = 32768
COMPACT_ERROR_LIMIT = 3
DOCUMENT_FIELDS = ("id", "path", "revision", "title", "kind", "quality", "characters", "stale")
INDEX_FIELDS = ("freshness", "index_epoch", "index_checked_at", "pending_changes", "index_status", "observer_mode")


def validate_view(view):
    if view not in {"full", "compact"}:
        raise WikiError("Unknown read view", 422)


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
    sources = Counter(ref.get("status") for ref in metadata.get("source_refs", []) if ref.get("status") in {"changed", "missing", "unknown"})
    if sources:
        warnings["sources"] = dict(sources)
    if warnings:
        result["warnings"] = warnings
    if document.get("pages"):
        result["page_count"] = len(document["pages"])
    if 'sources' in document:
        result['sources'] = document['sources']
    return result


def compact_index(value):
    result = {key: value[key] for key in INDEX_FIELDS if key in value}
    errors = value.get("index_errors", [])
    samples = errors[:COMPACT_ERROR_LIMIT]
    result["index_errors"] = [{"path": item.get("path", ""), "error": item.get("error", "")[:160]} for item in samples]
    result["index_error_count"] = value["index_error_count"]
    if value.get("index_errors_truncated") or value["index_error_count"] > len(samples) or any(len(item.get("error", "")) > 160 for item in samples):
        result["index_errors_truncated"] = True
    return result


def _fit(build, characters):
    return fit_prefix(build, characters, limit=COMPACT_READ_MAX_BYTES)


def compact_read(document, view="compact"):
    base = (compact_document(document) | compact_index(document) if view == "compact" else presentation(document)) | {"view": view}
    content = document["content"]
    lines = LineCoordinates.passage(document)

    def build(length):
        passage = content[:length]
        shortened = length < len(content)
        return base | {key: document[key] for key in ("page", "start", "line_start")} | {
            "content": passage, "end": document["start"] + length,
            "line_end": lines.line(length),
            "next_start": document["start"] + length if shortened else document["next_start"],
            "status": "partial" if shortened else "ok", "response_budget_exhausted": shortened,
        }

    return _fit(build, len(content))


def compact_batch(batch, view="compact"):
    base = (compact_index(batch) if view == "compact" else presentation(batch)) | {"budget_chars": batch["budget_chars"], "view": view}
    total = batch["characters_returned"]
    originals = {piece["id"]: piece for piece in batch["ranges"]}
    coordinates = {key: LineCoordinates.passage(piece) for key, piece in originals.items()}

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
                                "line_end": coordinates[original["id"]].line(count)}
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
                    line = coordinates[source["id"]].line(position - source["start"])
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
                       "documents": [compact_document(document) if view == "compact" else presentation(document) for document in batch["documents"] if document["id"] in identifiers],
                       "characters_returned": characters - remaining,
                       "response_budget_exhausted": characters < total}

    return _fit(build, total)
