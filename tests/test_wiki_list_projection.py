"""Observe the SQLite/Python data boundary as well as navigation semantics."""

import json
import sqlite3
from contextlib import contextmanager

import pytest

from server.wiki.store import WikiError
from tests.test_wiki_markdown_navigation import store as store


def test_compact_list_projects_navigation_without_loading_long_bodies(store, monkeypatch, record_property):
    for path, title in [("a.md", "Alpha"), ("b.md", "Beta")]:
        (store.root / path).write_text(f"---\ndescription: Native description\n---\n# {title}\n" + "long paragraph " * 5000)
    store.refresh()
    with store.connect() as db:
        db.execute("INSERT INTO managed_notes(path,base_hash,regions,metadata,rendered_revision) VALUES(?,?,?,?,?)",
                   ("a.md", "unused", "[]", json.dumps({"description": "Managed description"}), "unused"))
    observed = []
    connect = store.connect

    @contextmanager
    def tracked():
        with connect() as db:
            def row_factory(cursor, values):
                names = [column[0] for column in cursor.description]
                if "managed_metadata" in names:
                    observed.append((names, sum(len(value.encode()) for value in values if isinstance(value, str))))
                return sqlite3.Row(cursor, values)
            db.row_factory = row_factory
            yield db

    monkeypatch.setattr(store, "connect", tracked)
    full = store.documents(view="full", limit=1)
    full_bytes = sum(size for _, size in observed)
    observed.clear()
    first = store.documents(view="compact", limit=1)
    second = store.documents(view="compact", limit=1, cursor=first["next_cursor"])
    assert [page["items"][0]["path"] for page in (first, second)] == ["a.md", "b.md"]
    assert first["items"][0]["description"] == "Managed description"
    assert first["items"][0]["revision"] == full["items"][0]["revision"]
    assert first["total"] == second["total"] == full["total"] == 2
    assert second["next_cursor"] is None
    assert first["index_epoch"] == full["index_epoch"]
    assert first["index_status"] == full["index_status"]
    assert observed and all("text" not in fields and "blocks" not in fields and "pages" not in fields for fields, _ in observed)
    assert sum(size for _, size in observed) < full_bytes / 10
    record_property("full_request_row_bytes", full_bytes)
    record_property("compact_two_pages_row_bytes", sum(size for _, size in observed))
    (store.root / "c.md").write_text("# Changed index\nnew content\n")
    store.refresh()
    with pytest.raises(WikiError) as caught:
        store.documents(view="compact", limit=1, cursor=first["next_cursor"])
    assert caught.value.status == 409
