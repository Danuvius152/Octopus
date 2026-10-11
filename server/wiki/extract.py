"""Conservative text extraction: originals remain authoritative."""

import re
from html.parser import HTMLParser

SUPPORTED = {".md", ".markdown", ".txt", ".html", ".htm", ".pdf"}


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
    metadata = {
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
