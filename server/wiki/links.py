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


def anchor_exists(text: str, anchor: str, *, parsed: format.DocumentFormat | None = None) -> bool:
    parsed = parsed if parsed is not None else format.parse(text)
    if anchor.startswith("^"):
        return block_span(text, anchor, parsed=parsed) is not None
    for heading in parsed.headings:
        if anchor.casefold() in {heading["title"].casefold(), heading["slug"]}:
            return True
    return anchor in parsed.html_anchors


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
    title = location.lstrip("#").strip().casefold()
    current = (parsed if parsed is not None else format.parse(text)).headings
    # An explicit fragment selects its slug projection; a plain repeated title
    # remains ambiguous rather than silently citing the first occurrence.
    if location.startswith("#"):
        matches = [heading for heading in current if title == heading["slug"]]
        if len(matches) == 1:
            return matches[0]["start"], matches[0]["section_end"]
    matches = [heading for heading in current if title in {heading["title"].casefold(), heading["slug"]}]
    if len(matches) == 1:
        return matches[0]["start"], matches[0]["section_end"]
    return None
