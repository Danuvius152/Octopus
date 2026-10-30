"""Current evidence batches with shared content ranges and truthful receipts."""

from .line_coordinates import LineCoordinates
from .store import WikiError, is_maintainable_text


def _end(start, end, covered, budget):
    cursor, spent = start, 0
    for first, last in covered:
        if last <= cursor:
            continue
        gap = max(0, min(first, end) - cursor)
        amount = min(gap, budget - spent)
        cursor += amount
        spent += amount
        if amount < gap or cursor >= end:
            return cursor, spent
        if first <= cursor:
            cursor = min(end, max(cursor, last))
    amount = min(max(0, end - cursor), budget - spent)
    return cursor + amount, spent + amount


def _merge(intervals):
    result = []
    for first, last in sorted(intervals):
        if result and first <= result[-1][1]:
            result[-1] = (result[-1][0], max(last, result[-1][1]))
        else:
            result.append((first, last))
    return result


def read_many(store, requests, budget_chars, task=None, view="full", *, reuse_delivered=False, user_id=None):
    from .history import WikiHistory
    from .read_response import COMPACT_ERROR_LIMIT, compact_batch, validate_view
    from .reading import window
    from .source_details import page as source_page
    from .tasks import tasks

    validate_view(view)
    if not 1 <= len(requests) <= 20 or not 1 <= budget_chars <= 96000:
        raise WikiError("Invalid evidence batch size/budget", 422)
    cache, documents, intervals, items = {}, {}, {}, []
    remaining = budget_chars
    with WikiHistory(store).locked():
        for request in requests:
            item = {"doc": request.doc, "requested_start": request.start, "requested_length": request.length}
            try:
                identity = store.resolve_identity(request.doc)
                cache_key = identity[0] if identity else request.doc
                if cache_key not in cache:
                    document = store.read(request.doc, length=1, index_error_limit=0 if view == "compact" else None)
                    with store.connect() as db:
                        text = db.execute("SELECT text FROM documents WHERE id=?", (document["id"],)).fetchone()[0]
                    cache[cache_key] = (document, text)
                    cache[document["id"]] = (document, text)
                document, text = cache[cache_key]
                item |= {"id": document["id"], "revision": document["revision"]}
                if request.expected_revision and request.expected_revision != document["revision"]:
                    items.append(item | {"status": "changed", "error": "Read the current revision"})
                    continue
                if not is_maintainable_text(document["path"]):
                    items.append(item | {"status": "excluded", "error": document["metadata"].get("asset_reason", "Excluded asset")})
                    continue
                start, requested_end, boundary = window(text, request.start, request.length, request.location)
                delivered = tasks.delivered(task, document) if reuse_delivered else []
                reused = [(max(start, first), min(requested_end, last)) for first, last in delivered
                          if first < requested_end and start < last]
                if reused:
                    item |= {'reused_ranges': reused[:8], 'reused_ranges_complete': len(reused) <= 8}
                first_unseen = tasks.coverage(delivered, start)
                if delivered and first_unseen >= requested_end:
                    items.append(item | {'status': 'already_delivered', 'start': start, 'end': requested_end,
                                         'next_start': requested_end if requested_end < boundary else None})
                    continue
                start = min(max(start, first_unseen), requested_end)
                # Return the next contiguous gap. A later covered interval is
                # referenced, never silently repeated or credited as new text.
                gap_end = min([first for first, _ in delivered if start < first < requested_end], default=requested_end)
                requested_end = gap_end
                end, spent = _end(start, requested_end, intervals.get(document["id"], []), remaining)
                if end == start and start < requested_end:
                    items.append(item | {"status": "budget_exhausted", "next_start": start})
                    continue
                remaining -= spent
                intervals[document["id"]] = _merge([*intervals.get(document["id"], []), (start, end)])
                documents[document["id"]] = {key: value for key, value in document.items() if key not in {"content", "start", "end", "next_start", "line_start", "line_end", "page"}} | {'sources': source_page(store, document, user_id)}
                following = tasks.coverage(delivered, end)
                items.append(item | {"status": "ok" if end == requested_end else "partial", "start": start, "end": end,
                                     "next_start": following if following < boundary else None})
            except WikiError as exc:
                if exc.status in {400, 401, 403, 422}:
                    raise
                items.append(item | {"status": "not_found" if exc.status == 404 else "unavailable", "error": str(exc)})
        ranges = []
        for identifier, selected in intervals.items():
            document, text = cache[identifier]
            lines = LineCoordinates(text)
            for start, end in selected:
                range_id = f"{identifier}:{document['revision']}:{start}:{end}"
                ranges.append({"id": range_id, "doc": identifier, "revision": document["revision"], "start": start, "end": end, "content": text[start:end], "line_start": lines.line(start), "line_end": lines.line(end)})
                for item in items:
                    if item.get("id") == identifier and item["status"] in {"ok", "partial"} and start <= item["start"] <= item["end"] <= end:
                        item["range_id"] = range_id
        result = {"items": items, "documents": list(documents.values()), "ranges": ranges, "characters_returned": budget_chars - remaining, "budget_chars": budget_chars, "freshness": "live", **store.index_state(error_limit=COMPACT_ERROR_LIMIT if view == "compact" else None)}
        if view == "compact" or task is not None:
            result = compact_batch(result, view=view)
        for piece in result["ranges"]:
            document, _ = cache[piece["doc"]]
            tasks.record_read(task, document | {"start": piece["start"], "end": piece["end"], "page": None})
        return result
