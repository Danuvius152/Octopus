"""Markdown query and maintenance scope, independent of legacy text history."""

from pathlib import PurePosixPath

MARKDOWN_QUERY_CONTRACT = "markdown-v1"
MARKDOWN_SQL = "(lower(d.path) GLOB '*.md' OR lower(d.path) GLOB '*.markdown')"


def is_markdown_path(path: str) -> bool:
    return PurePosixPath(path).suffix.lower() in {".md", ".markdown"}


def is_markdown_maintenance(path: str) -> bool:
    from .store import is_maintainable_text

    return is_markdown_path(path) and is_maintainable_text(path)


def require_markdown_maintenance(path: str) -> None:
    from .store import WikiError

    if not is_markdown_maintenance(path):
        raise WikiError(f"Wiki maintenance requires a Markdown note: {path}", 422)
