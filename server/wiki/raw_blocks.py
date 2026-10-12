"""Version-bound UTF-8 source coordinates, distinct from extracted offsets."""

from pathlib import Path

from . import format
from .extract import blocks
from .store import WikiError, revision


def load(store, doc, expected_revision=None):
    document = store.read(doc, length=1, expected_revision=expected_revision)
    if Path(document["path"]).suffix.lower() not in {".md", ".markdown", ".txt"}:
        raise WikiError("Raw block editing supports UTF-8 Markdown/text, not extracted HTML", 422)
    raw = store.path(document["path"]).read_bytes()
    if revision(raw) != document["revision"]:
        raise WikiError("Document changed; read current raw blocks", 409)
    try:
        text = raw.decode("utf-8")
    except UnicodeError as exc:
        raise WikiError("Original encoding requires review", 422) from exc
    parsed = format.parse(text)
    document["raw_body_start"] = parsed.body_start
    result, offset = [], 0
    for index, part in enumerate(blocks(text)):
        digest = revision(part.encode())
        section = next((section for section in parsed.sections if section["start"] <= offset < section["end"]), {})
        result.append({"id": str(index), "locator": f"{document['id']}:{document['revision']}:{index}:{digest}", "content": part, "sha256": digest, "start": offset, "end": offset + len(part), "characters": len(part), "title": section.get("title", ""), "role": "metadata" if offset < parsed.body_start else section.get("role", "body")})
        offset += len(part)
    return document, text, result


def read(curation, doc, *, view="full", ids=None, expected_revision=None, budget_chars=None, offset=0, limit=None, task=None):
    from .tasks import tasks

    if view not in {"full", "locators"} or offset < 0 or limit is not None and not 1 <= limit <= 100 or budget_chars is not None and not 1 <= budget_chars <= 96000:
        raise WikiError("Invalid raw block view or budget", 422)
    with curation.history.locked():
        document, text, parts = load(curation.store, doc, expected_revision)
        if ids is not None:
            requested = set(ids)
            if requested - {part["id"] for part in parts}:
                raise WikiError("Raw block not found in current revision", 404)
            parts = [part for part in parts if part["id"] in requested]
        count = len(parts)
        selected = parts[offset:offset + limit] if limit is not None else parts[offset:]
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
        return {"id": document["id"], "path": document["path"], "revision": document["revision"], "view": view, "coordinate_system": "raw_unicode_codepoints", "raw_characters": len(text), "blocks": result, "total": count, "next_offset": next_offset, "budget_exhausted": len(result) < len(selected), "freshness": "live", **curation.store.index_state()}
