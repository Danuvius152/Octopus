"""Owned additions are replaceable; all other text belongs to the user."""

import json
import uuid

from . import format
from .store import WikiError, revision


def original_text(text, regions):
    for region in regions:
        raw = region["raw"]
        if text.count(raw) != 1:
            raise WikiError(
                "A user edited a Weber section; preserve it and review before refreshing",
                409,
            )
        text = text.replace(raw, "", 1)
    return text


def region(role, content, metadata):
    record = {"id": uuid.uuid4().hex, "author": "Weber 整理", "role": role, **metadata}
    encoded = json.dumps(record, ensure_ascii=False).replace("-->", "--\\u003e")
    if "<!-- octopus-section" in content or "<!-- /octopus-section" in content:
        raise WikiError("Section control markers cannot be supplied as content", 422)
    raw = f"\n\n<!-- octopus-section {encoded} -->\n{content}\n<!-- /octopus-section -->\n\n"
    return {"role": role, "raw": raw}


def top_offset(text: str, *, parsed: format.DocumentFormat | None = None) -> int:
    parsed = parsed if parsed is not None else format.parse(text)
    offset = parsed.body_start
    title = next((heading for heading in parsed.headings if heading["level"] == 1 and heading["depth"] == 0), None)
    if title is not None and not text[offset:title["start"]].strip(" \t\r\n"):
        return title["end"]
    return offset


def managed_metadata(store, path, raw):
    with store.connect() as db:
        row = db.execute("SELECT * FROM managed_notes WHERE path=?", (path,)).fetchone()
    if row is None:
        return {}, False
    metadata = json.loads(row["metadata"])
    try:
        base = original_text(raw.decode("utf-8"), json.loads(row["regions"]))
        stale = revision(base.encode()) != row["base_hash"]
    except (WikiError, UnicodeError):
        stale = True
    return metadata | {
        "organized_revision": row["rendered_revision"],
        "needs_refresh": stale,
    }, stale
