"""Small real-index query counts retain error and pagination contracts."""

from contextlib import contextmanager

import pytest

from tests.test_wiki_markdown_navigation import store as store


@pytest.mark.parametrize("operation", ["documents", "search"])
@pytest.mark.parametrize("view", ["full", "compact"])
def test_navigation_builds_health_once_per_page_with_real_errors(store, monkeypatch, operation, view):
    for index in range(3):
        (store.root / f"note-{index}.md").write_text(f"# Needle {index}\nCurrent evidence.\n")
    for index in range(5):
        (store.root / f"error-{index}.md").write_text("---\ntags: [broken\n---\n# Broken\n")
    store.documents()
    statements = []
    connect = store.connect

    @contextmanager
    def traced():
        with connect() as connection:
            connection.set_trace_callback(statements.append)
            yield connection

    monkeypatch.setattr(store, "connect", traced)
    cursor = None
    for _ in range(2):
        statements.clear()
        kwargs = {"view": view, "limit": 1, "cursor": cursor}
        result = store.documents(**kwargs) if operation == "documents" else store.search("Needle", **kwargs)
        assert result["index_status"] == "partial"
        if view == "compact":
            assert result["index_error_count"] == 5 and len(result["index_errors"]) == 3
        else:
            assert len(result["index_errors"]) == 5
        health = [sql for sql in statements if sql == "SELECT * FROM index_state WHERE singleton=1"]
        assert len(health) == 1, health
        cursor = result["next_cursor"]
        assert cursor is not None
