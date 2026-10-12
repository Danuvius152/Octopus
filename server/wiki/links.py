"""Resolve local note links for navigation; a link is never proof of reading."""

import posixpath
import re
import unicodedata
from html.parser import HTMLParser
from pathlib import PurePosixPath
from urllib.parse import unquote, urlsplit

from .extract import blocks


def local_links(text):
    # Code examples are not navigation links.
    text = re.sub(r"(?ms)^\s*(`{3,}|~{3,})[^\n]*\n.*?^\s*\1\s*$", "", text)
    for match in re.finditer(r"\[\[([^\]\n]+)\]\]", text):
        target = match[1].split("|", 1)[0]
        path, _, anchor = target.partition("#")
        yield unquote(path), unquote(anchor), "wikilink"
    for match in re.finditer(r"\]\(([^\s)]+)(?:\s+[^)]*)?\)", text):
        url = urlsplit(match[1].strip("<>"))
        if not url.scheme and not url.netloc and (url.path or url.fragment):
            yield unquote(url.path), unquote(url.fragment), "markdown_link"


def resolve_link(path, parent, documents, *, wiki=False):
    candidates = [posixpath.normpath(posixpath.join(parent, path))]
    if wiki:
        candidates.append(path.lstrip("/"))
    for candidate in candidates:
        for suffix in ("", ".md", ".markdown"):
            found = [doc for doc in documents if doc["path"] == candidate + suffix]
            if len(found) == 1:
                return found[0]
    if wiki and "/" not in path:
        name = PurePosixPath(path).name.removesuffix(".md")
        found = [
            doc
            for doc in documents
            if name
            in {
                PurePosixPath(doc["path"]).stem,
                doc["title"],
                *doc["metadata"].get("aliases", []),
            }
        ]
        if len(found) == 1:
            return found[0]
    return None


def headings(text):
    """Actual Markdown headings with literal current-file coordinates."""
    offset, fence, front = 0, None, text.lstrip("\ufeff").startswith(("---\n", "---\r\n"))
    result = []
    for number, line in enumerate(text.splitlines(keepends=True)):
        stripped = line.strip()
        if front:
            if number > 0 and stripped == "---":
                front = False
        else:
            marker = re.match(r"^\s*(`{3,}|~{3,})", line)
            if marker:
                if fence is None:
                    fence = marker[1]
                elif marker[1][0] == fence[0] and len(marker[1]) >= len(fence):
                    fence = None
            elif fence is None:
                match = re.match(r"^ {0,3}(#{1,6})\s+(.+?)\s*#*\s*$", line)
                if match:
                    result.append({"title": match[2], "level": len(match[1]), "start": offset})
        offset += len(line)
    for index, heading in enumerate(result):
        heading["end"] = next((later["start"] for later in result[index + 1:] if later["level"] <= heading["level"]), len(text))
    return result


def heading_slug(title):
    title = re.sub(r"\[([^]]+)\]\([^)]*\)", r"\1", title)
    return "-".join("".join(char for char in title.casefold() if char.isspace() or char in "_-" or unicodedata.category(char)[0] in {"L", "N"}).split())


def block_span(text, identifier):
    """Cover the whole prose/list block, including a standalone trailing ID."""
    if not re.fullmatch(r"\^[A-Za-z0-9-]+", identifier):
        return None
    # Mask non-prose without moving any current-file coordinates.
    masked = re.sub(
        r"(?ms)\A\ufeff?---\r?\n.*?^---[ \t]*(?:\r?\n|$)|^\s*(`{3,}|~{3,})[^\n]*\n.*?^\s*\1\s*$",
        lambda match: re.sub(r"[^\n]", " ", match[0]), text,
    )
    ranges, offset = [], 0
    for part in blocks(text):
        ranges.append((offset, offset + len(part), masked[offset:offset + len(part)]))
        offset += len(part)
    hits = [index for index, (_, _, part) in enumerate(ranges) if re.search(r"(?m)\^" + re.escape(identifier[1:]) + r"[ \t]*\r?$", part)]
    if len(hits) != 1:
        return None
    index = hits[0]
    end = ranges[index][1]
    if ranges[index][2].strip() == identifier:
        index -= 1
        while index >= 0 and not text[ranges[index][0]:ranges[index][1]].strip():
            index -= 1
        if index < 0:
            return None
    list_marker = r"(?m)^\s*(?:[-+*]|\d+[.)])\s+"
    def list_part(part):
        lines = [line for line in part.splitlines() if line.strip()]
        return bool(lines) and (
            re.match(list_marker, lines[0]) is not None
            # Markdown permits lazy unindented lines after an indented
            # continuation paragraph; its first line determines this chunk.
            or lines[0].startswith((" ", "\t"))
        )

    # A loose list can end in an indented paragraph, with no marker in that
    # final chunk. Include its preceding conditions, including nested items.
    first = index
    saw_marker = re.search(list_marker, ranges[index][2]) is not None
    if list_part(ranges[index][2]):
        while first > 0:
            previous = first - 1
            while previous >= 0 and not ranges[previous][2].strip():
                previous -= 1
            if previous < 0 or not list_part(ranges[previous][2]):
                break
            saw_marker = saw_marker or re.search(list_marker, ranges[previous][2]) is not None
            first = previous
        if saw_marker:
            index = first
    return ranges[index][0], end


def anchor_exists(text, anchor):
    anchor = unquote(anchor)
    if anchor.startswith("^"):
        return block_span(text, anchor) is not None
    seen = {}
    for heading in headings(text):
        title = heading["title"]
        slug = heading_slug(title)
        suffix = seen.get(slug, 0)
        seen[slug] = suffix + 1
        unique = slug + (f"-{suffix}" if suffix else "")
        if anchor.casefold() in {title.casefold(), unique}:
            return True
    class HTMLAnchors(HTMLParser):
        def __init__(self):
            super().__init__()
            self.found = False

        def handle_starttag(self, tag, attrs):
            if any(key in {"id", "name"} and value == anchor for key, value in attrs):
                self.found = True

    parser = HTMLAnchors()
    parser.feed(re.sub(r"(?ms)^\s*(`{3,}|~{3,})[^\n]*\n.*?^\s*\1\s*$|`[^`]*`", "", text))
    return parser.found


def source_span(text, location):
    """Verify an evidence locator, without claiming its semantic relevance."""
    location = location.strip()
    if location in {"全文", "原文全文"} and text.strip():
        return 0, len(text)
    lines = re.fullmatch(r"L(\d+)(?:\s*[-–]\s*L?(\d+))?", location, re.I)
    if lines:
        first, last = int(lines[1]), int(lines[2] or lines[1])
        starts = [0, *(match.end() for match in re.finditer("\n", text))]
        if 1 <= first <= last <= len(starts):
            start, end = starts[first - 1], starts[last] if last < len(starts) else len(text)
            if start < end:
                return start, end
        return None
    title = location.lstrip("#").strip()
    matches = [heading for heading in headings(text) if title.casefold() in {heading["title"].casefold(), heading_slug(heading["title"])}]
    if len(matches) == 1:
        return matches[0]["start"], matches[0]["end"]
    if location.startswith("^"):
        return block_span(text, location)
    return None
