"""Resolve local note links for navigation; a link is never proof of reading."""

import posixpath
import re
from pathlib import PurePosixPath
from urllib.parse import unquote, urlsplit


def local_links(text):
    # Code examples are not navigation links.
    text = re.sub(r"(?ms)^\s*(`{3,}|~{3,})[^\n]*\n.*?^\s*\1\s*$", "", text)
    for match in re.finditer(r"\[\[([^\]\n]+)\]\]", text):
        target = match[1].split("|", 1)[0]
        path, _, anchor = target.partition("#")
        yield path, anchor, "wikilink"
    for match in re.finditer(r"\]\(([^\s)]+)(?:\s+[^)]*)?\)", text):
        url = urlsplit(match[1].strip("<>"))
        if not url.scheme and not url.netloc and url.path:
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
    if wiki:
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
