"""Version-bound UTF-8 source coordinates, distinct from extracted offsets."""

from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path

from . import format
from .extract import blocks
from .store import WikiError, revision


@dataclass(frozen=True)
class RawBlock:
    index: int
    start: int
    end: int
    digest: str
    title: str
    role: str


@dataclass(frozen=True)
class RawCoordinates:
    body_start: int
    blocks: tuple[RawBlock, ...]


def snapshot(store, doc, expected_revision=None):
    document, raw = store.read_snapshot(doc, length=1, expected_revision=expected_revision)
    if Path(document["path"]).suffix.lower() not in {".md", ".markdown", ".txt"}:
        raise WikiError("Raw block editing supports UTF-8 Markdown/text, not extracted HTML", 422)
    try:
        text = raw.decode("utf-8")
    except UnicodeError as exc:
        raise WikiError("Original encoding requires review", 422) from exc
    return document, text


def coordinates(store, document, text):
    with store._lifecycle:
        cache = store._raw_coordinates
        if cache is None:
            cache = store._raw_coordinates = OrderedDict()
        cached = cache.get(document["revision"])
        if cached is not None:
            cache.move_to_end(document["revision"])
            return cached
    parsed = format.parse(text)
    sections = iter(parsed.sections)
    section = next(sections, None)
    result, offset = [], 0
    for index, part in enumerate(blocks(text, parsed=parsed)):
        while section is not None and section["end"] <= offset:
            section = next(sections, None)
        selected = section if section is not None and section["start"] <= offset else {}
        result.append(RawBlock(index, offset, offset + len(part), revision(part.encode()),
                               selected.get("title", ""), "metadata" if offset < parsed.body_start else selected.get("role", "body")))
        offset += len(part)
    current = RawCoordinates(parsed.body_start, tuple(result))
    with store._lifecycle:
        cache[document["revision"]] = current
        cache.move_to_end(document["revision"])
        while len(cache) > 4:
            cache.popitem(last=False)
    return current


def project(document, text, part):
    return {"id": str(part.index), "locator": f"{document['id']}:{document['revision']}:{part.index}:{part.digest}",
            "content": text[part.start:part.end], "sha256": part.digest, "start": part.start, "end": part.end,
            "characters": part.end - part.start, "title": part.title, "role": part.role}


def read(curation, doc, *, view="full", ids=None, expected_revision=None, budget_chars=None, offset=0, limit=None, start=None, tail=None, task=None):
    from .tasks import tasks

    if view not in {"full", "locators"} or offset < 0 or limit is not None and not 1 <= limit <= 100 or budget_chars is not None and not 1 <= budget_chars <= 96000:
        raise WikiError("Invalid raw block view or budget", 422)
    if start is not None and (start < 0 or view != "full"):
        raise WikiError("Raw start requires a nonnegative full-content position", 422)
    if tail is not None and (isinstance(tail, bool) or not isinstance(tail, int) or not 1 <= tail <= 100 or offset or ids is not None):
        raise WikiError("tail selects 1..100 final blocks and cannot combine with offset or ids", 422)
    with curation.history.locked():
        document, text = snapshot(curation.store, doc, expected_revision)
        from .read_response import INDEX_FIELDS

        state = {key: document[key] for key in (*INDEX_FIELDS, "index_errors") if key in document}
        parts = coordinates(curation.store, document, text).blocks
        if ids is not None:
            requested = set(ids)
            if requested - {str(part.index) for part in parts}:
                raise WikiError("Raw block not found in current revision", 404)
            parts = tuple(part for part in parts if str(part.index) in requested)
        count = len(parts)
        if tail is not None:
            offset = max(0, count - tail)
        selected = [project(document, text, part) for part in (parts[offset:offset + limit] if limit is not None else parts[offset:])]
        if task is not None or start is not None:
            from .model_response import fit_prefix, presentation

            if start is not None and (not selected or not selected[0]["start"] <= start <= selected[0]["end"]):
                raise WikiError("Raw start must be within the first selected block", 422)
            header = {"id": document["id"], "path": document["path"], "revision": document["revision"],
                      "view": view, "coordinate_system": "raw_unicode_codepoints", "raw_characters": len(text),
                      "total": count, "freshness": "live", **state}
            projected = presentation(selected)

            def build(amount):
                remaining, returned = amount, []
                for index, part in enumerate(projected):
                    if view == "locators":
                        if len(returned) == amount:
                            break
                        returned.append({key: value for key, value in part.items() if key != "content"})
                        continue
                    first = start if index == 0 and start is not None else part["start"]
                    length = min(remaining, part["end"] - first)
                    if not length and first < part["end"]:
                        break
                    last = first + length
                    returned.append(part | {"content": text[first:last], "content_start": first,
                                            "content_end": last, "content_complete": first == part["start"] and last == part["end"]})
                    remaining -= length
                    if last < part["end"]:
                        break
                partial = bool(returned and view == "full" and returned[-1]["content_end"] < returned[-1]["end"])
                following = offset + len(returned) - (1 if partial else 0)
                return header | {"blocks": returned, "returned_count": len(returned),
                                 "next_offset": following if following < count else None,
                                 "next_start": returned[-1]["content_end"] if partial else None,
                                 "budget_exhausted": partial or len(returned) < len(selected)}

            available = sum(part["end"] - (start if index == 0 and start is not None else part["start"]) for index, part in enumerate(selected))
            capacity = len(selected) if view == "locators" else min(available, budget_chars or 96000)
            result = fit_prefix(build, capacity)
            if view == "full":
                for part in result["blocks"]:
                    tasks.record_raw_read(task, document, part["content_start"], part["content_end"], len(text))
            return result
        remaining, result = budget_chars, []
        for part in selected:
            item = dict(part)
            if view == "locators":
                item.pop("content")
            else:
                if remaining is not None and remaining < len(part["content"]):
                    break
                if remaining is not None:
                    remaining -= len(part["content"])
                tasks.record_raw_read(task, document, part["start"], part["end"], len(text))
            result.append(item)
        next_offset = offset + len(result) if offset + len(result) < count else None
        return {"id": document["id"], "path": document["path"], "revision": document["revision"], "view": view, "coordinate_system": "raw_unicode_codepoints", "raw_characters": len(text), "blocks": result, "total": count, "next_offset": next_offset, "budget_exhausted": len(result) < len(selected), "freshness": "live", **state}
