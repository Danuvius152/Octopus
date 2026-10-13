"""Conservative text extraction: originals remain authoritative."""

import re
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any

from .format import _HEADINGS

SUPPORTED = {".md", ".markdown", ".txt", ".html", ".htm", ".pdf"}
HTML_BODY_VERSION = "html-body-v3"

_HTML_BODY_EXCLUDED = {
    "head",
    "title",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "script",
    "style",
    "noscript",
}
_HTML_VOID_ELEMENTS = {
    "area",
    "base",
    "br",
    "col",
    "embed",
    "hr",
    "img",
    "input",
    "link",
    "meta",
    "param",
    "source",
    "track",
    "wbr",
}


@dataclass
class _HTMLBodyFrame:
    tag: str
    excluded: bool
    heading_parts: list[str] | None = None


class HTMLBodyAnalyzer(HTMLParser):
    """Track complete content regions across feeds, retaining source coordinates.

    The event count records each qualifying piece, even after the document has
    become readable. Indexing can therefore project a single analysis back onto
    source token ranges without resetting enclosing HTML or heading state.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.readable = False
        self.qualifying_events = 0
        self.media_refs: list[dict[str, str]] = []
        self._seen_media_refs: set[tuple[str, str]] = set()
        # Each frame retains its inherited exclusion state. An unmatched end
        # tag cannot release an unrelated heading or hidden ancestor.
        self._stack: list[_HTMLBodyFrame] = []
        self._role = "body"
        self._role_level = 0

    @property
    def _excluded(self) -> bool:
        return bool(self._stack and self._stack[-1].excluded)

    def _heading(self) -> _HTMLBodyFrame | None:
        for frame in reversed(self._stack):
            if frame.heading_parts is not None:
                return frame
            if frame.tag in _HTML_BODY_EXCLUDED:
                break
        return None

    def _qualify(self) -> None:
        self.readable = True
        self.qualifying_events += 1

    def _select_role(self, frame: _HTMLBodyFrame) -> None:
        if frame.heading_parts is None:
            return
        label = " ".join("".join(frame.heading_parts).split()).casefold()
        level = int(frame.tag[1:])
        selected = _HEADINGS.get(label)
        if selected is not None:
            self._role, self._role_level = selected, level
        elif not self._role_level or level <= self._role_level:
            self._role, self._role_level = "body", level

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        excluded = self._excluded or tag in _HTML_BODY_EXCLUDED
        if tag == "br":
            heading = self._heading()
            if heading is not None:
                assert heading.heading_parts is not None
                heading.heading_parts.append("\n")
        if not excluded and self._role not in {"related_notes", "curator_note"}:
            src = next(
                (
                    value.strip()
                    for name, value in attrs
                    if name == "src" and value is not None and value.strip()
                ),
                None,
            )
            media = tag in {"img", "audio", "video"} or (
                tag == "source"
                and any(parent.tag in {"audio", "video"} for parent in self._stack)
            )
            if media and src is not None:
                self._qualify()
                ref = (tag, src)
                if ref not in self._seen_media_refs:
                    self._seen_media_refs.add(ref)
                    self.media_refs.append({"tag": tag, "src": src})
        if tag not in _HTML_VOID_ELEMENTS:
            parts: list[str] | None = (
                []
                if re.fullmatch(r"h[1-6]", tag)
                and not self._excluded
                and not any(
                    frame.tag in {"blockquote", "ul", "ol", "li"}
                    for frame in self._stack
                )
                else None
            )
            self._stack.append(_HTMLBodyFrame(tag, excluded, parts))

    def handle_endtag(self, tag: str) -> None:
        for position in range(len(self._stack) - 1, -1, -1):
            if self._stack[position].tag == tag:
                # A malformed wrapper close must not implicitly end an
                # excluded region. Remove its closed context (including an
                # audio/video parent) while retaining the unclosed region.
                retained_region = next(
                    (
                        child
                        for child in range(position + 1, len(self._stack))
                        if self._stack[child].tag in _HTML_BODY_EXCLUDED
                    ),
                    len(self._stack),
                )
                if tag in _HTML_BODY_EXCLUDED:
                    retained_region = len(self._stack)
                self._select_role(self._stack[position])
                del self._stack[position:retained_region]
                break

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag not in _HTML_VOID_ELEMENTS:
            self.handle_endtag(tag)

    def handle_data(self, data: str) -> None:
        heading = self._heading()
        if heading is not None:
            assert heading.heading_parts is not None
            heading.heading_parts.append(data)
        if (
            not self._excluded
            and self._role not in {"related_notes", "curator_note"}
            and data.strip()
        ):
            self._qualify()


def _parse_html_body(html: str) -> HTMLBodyAnalyzer:
    parser = HTMLBodyAnalyzer()
    parser.feed(html)
    parser.close()
    return parser


def html_has_readable_body(html: str) -> bool:
    """Check HTML content structure, without CSS or browser/media validation."""
    return _parse_html_body(html).readable


class HTMLText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "noscript"}:
            self.hidden += 1
        elif tag in {"p", "div", "br", "li", "h1", "h2", "h3", "tr"}:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in {"script", "style", "noscript"}:
            self.hidden = max(0, self.hidden - 1)
        elif tag in {"p", "div", "li", "h1", "h2", "h3", "tr"}:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def decode(raw: bytes) -> tuple[str, str]:
    encodings = (
        ["utf-16"]
        if raw.startswith((b"\xff\xfe", b"\xfe\xff"))
        else ["utf-8-sig", "gb18030"]
    )
    for encoding in encodings:
        try:
            return raw.decode(encoding), encoding
        except UnicodeError:
            continue
    raise ValueError("Unsupported text encoding; original retained")


def extract(raw: bytes, suffix: str) -> dict:
    metadata: dict[str, Any] = {
        "extractor": "octopus-text-v3",
        "pipeline_version": "octopus-extract-v3",
        "limitations": "Text extraction does not verify image, table or formula semantics.",
    }
    pages: list[dict] = []
    if suffix == ".pdf":
        return {
            "text": "",
            "pages": [],
            "quality": "excluded",
            "metadata": metadata
            | {"exclusion": "PDF content processing is outside the current scope"},
        }
    text, encoding = decode(raw)
    metadata["encoding"] = encoding
    if suffix in {".html", ".htm"}:
        body = _parse_html_body(text)
        metadata.update(
            {
                "html_body_version": HTML_BODY_VERSION,
                "html_body_readable": body.readable,
                "html_media_refs": body.media_refs,
            }
        )
        parser = HTMLText()
        parser.feed(text)
        text = "".join(parser.parts).strip()
    quality = "text" if text.strip() else "needs_review"
    return {"text": text, "pages": pages, "quality": quality, "metadata": metadata}


def blocks(text: str) -> list[str]:
    """Lossless blocks: fenced code/math/frontmatter are indivisible."""
    result: list[str] = []
    current: list[str] = []
    fence = None
    for line in text.splitlines(keepends=True):
        marker = re.match(r"^\s*(`{3,}|~{3,}|\$\$)", line)
        if not current and not result and line.strip().lstrip("\ufeff") == "---":
            fence = "---"
        elif fence == "---" and line.strip() == "---":
            fence = None
        elif marker:
            if fence is None:
                fence = marker[1]
            elif marker[1][0] == fence[0] and len(marker[1]) >= len(fence):
                fence = None
        current.append(line)
        if not fence and not line.strip():
            result.append("".join(current))
            current = []
    if current:
        result.append("".join(current))
    return result
