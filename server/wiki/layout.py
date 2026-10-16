"""Recognize structural Markdown without treating hidden content as headings."""

import re

from markdown_it import MarkdownIt

from . import format
from .store import WikiError

_markdown = MarkdownIt("commonmark")
_INLINE = {
    "text",
    "code_inline",
    "softbreak",
    "hardbreak",
    "em_open",
    "em_close",
    "strong_open",
    "strong_close",
    "link_open",
    "link_close",
}


def headings_only(text: str) -> bool:
    tokens = _markdown.parse(text)
    if not tokens or len(tokens) % 3:
        return False
    covered: set[int] = set()
    for index in range(0, len(tokens), 3):
        opening, inline, closing = tokens[index : index + 3]
        if (
            [opening.type, inline.type, closing.type]
            != ["heading_open", "inline", "heading_close"]
            or opening.level != 0
            or opening.map is None
            or any(child.type not in _INLINE for child in inline.children or [])
        ):
            return False
        covered.update(range(*opening.map))
    # The parser consumes reference definitions without returning a block token.
    # They must not acquire the attribution exemption of a nearby heading.
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    return all(not line.strip() or index in covered for index, line in enumerate(lines))


def _breaks(text: str) -> int:
    return len(re.findall(r"\r\n|\r|\n", text))


def trailing_context(text: str) -> str:
    """Retain the final nonblank character and the whitespace after it."""
    return text[max(0, len(text.rstrip(" \t\r\n")) - 1) :]


def insert_heading(content: str, before: str, after: str, newline: str) -> str:
    """Add only missing block separators, retaining supplied and original bytes."""
    leading = content[: len(content) - len(content.lstrip(" \t\r\n"))]
    trailing = content[len(content.rstrip(" \t\r\n")) :]
    prefix = before[len(before.rstrip(" \t\r\n")) :]
    suffix = after[: len(after) - len(after.lstrip(" \t\r\n"))]
    first = max(0, 2 - _breaks(prefix + leading)) if before else 0
    last = max(0, 2 - _breaks(trailing + suffix)) if after else 0
    return newline * first + content + newline * last


def validate_headings(text: str, fragments: list[tuple[int, str]]) -> None:
    """A fragment must still be a heading in the complete document context."""
    if not fragments:
        return
    actual = {
        (h["start"], h["end"], h["title"], h["level"])
        for h in format.parse(text).headings
    }
    for offset, content in fragments:
        for heading in format.parse(content).headings:
            expected = (
                offset + heading["start"],
                offset + heading["end"],
                heading["title"],
                heading["level"],
            )
            if expected not in actual:
                raise WikiError(
                    "Structural additions must remain headings in the current document context",
                    422,
                )
