"""Resolve local note links for navigation; a link is never proof of reading."""

import re
from pathlib import PurePosixPath

from . import format
from .format import heading_slug as heading_slug


def note_name(path):
    name = PurePosixPath(path).name
    for suffix in (".md", ".markdown"):
        if name.casefold().endswith(suffix):
            return name[:-len(suffix)]
    return name


def headings(text: str, *, parsed: format.DocumentFormat | None = None) -> list[dict]:
    """Literal heading sections; a supplied parse belongs to this exact text."""
    return [heading | {"end": heading["section_end"]} for heading in (parsed if parsed is not None else format.parse(text)).headings]


def block_span(text: str, identifier: str, *, parsed: format.DocumentFormat | None = None) -> tuple[int, int] | None:
    """Cover the whole prose/list block, including a standalone trailing ID."""
    return (parsed if parsed is not None else format.parse(text)).block_spans.get(identifier)


def _heading(parsed: format.DocumentFormat, value: str, *, fragment: bool) -> tuple[str, dict | None]:
    """Explicit URI fragments and bare display titles have different priority."""
    value = value.casefold()
    if fragment:
        if value in parsed.ambiguous_heading_slugs:
            return "ambiguous", None
        matches = [heading for heading in parsed.headings if value == heading["slug"]]
        if matches:
            return "resolved", matches[0]
    matches = [heading for heading in parsed.headings if value == heading["title"].casefold()]
    if not matches and not fragment:
        matches = [heading for heading in parsed.headings if value == heading["slug"]]
    if len(matches) == 1:
        return "resolved", matches[0]
    return ("ambiguous" if matches else "missing_anchor"), None


def anchor_status(text: str, anchor: str, *, parsed: format.DocumentFormat | None = None) -> str:
    """The anchor was already URI-decoded; keep its explicit fragment kind."""
    parsed = parsed if parsed is not None else format.parse(text)
    if anchor.startswith("^"):
        return "resolved" if block_span(text, anchor, parsed=parsed) is not None else "missing_anchor"
    if anchor.casefold() in parsed.ambiguous_heading_slugs:
        return "ambiguous"
    if anchor in parsed.html_anchors:
        return "resolved"
    status, _ = _heading(parsed, anchor, fragment=True)
    return status


def anchor_exists(text: str, anchor: str, *, parsed: format.DocumentFormat | None = None) -> bool:
    return anchor_status(text, anchor, parsed=parsed) == "resolved"


def source_span(text: str, location: str, *, parsed: format.DocumentFormat | None = None) -> tuple[int, int] | None:
    """Verify an evidence locator, without claiming its semantic relevance."""
    location = location.strip()
    if location in {"全文", "原文全文"} and text.strip():
        return 0, len(text)
    lines = re.fullmatch(r"L(\d+)(?:\s*[-–]\s*L?(\d+))?", location, re.I)
    if lines:
        first, last = int(lines[1]), int(lines[2] or lines[1])
        from .line_coordinates import LineCoordinates

        starts = [0, *LineCoordinates(text).break_ends]
        if 1 <= first <= last <= len(starts):
            start, end = starts[first - 1], starts[last] if last < len(starts) else len(text)
            if start < end:
                return start, end
        return None
    if location.startswith("^"):
        return block_span(text, location, parsed=parsed)
    parsed = parsed if parsed is not None else format.parse(text)
    _, heading = _heading(parsed, location.removeprefix("#").strip(), fragment=location.startswith("#"))
    return (heading["start"], heading["section_end"]) if heading is not None else None
