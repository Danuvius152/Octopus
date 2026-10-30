"""Health counts and bounded samples describe the same committed snapshot."""

from functools import partial

import pytest

from server.wiki import index_state
from server.wiki.read_response import compact_index
from tests import test_wiki_markdown_navigation as navigation

store = navigation.store
write = navigation.write


@pytest.mark.parametrize("entry", ["health", "search", "documents"])
def test_error_count_and_sample_share_snapshot_while_writer_repairs_documents(store, monkeypatch, entry):
    write(store, "healthy.md", "# Needle\nHealthy evidence.\n")
    paths = [f"broken-{number}.md" for number in range(4)]
    for path in paths:
        write(store, path, "---\ntags: [broken\n---\n# Invalid metadata\n")
    store.refresh()
    before = store.index_state()["index_errors"]
    assert len(before) == 4
    # A concurrent writer must be able to commit while the old read snapshot
    # remains open. DELETE journaling deliberately cannot offer that schedule.
    with store.connect() as db:
        assert db.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"

    summarize = index_state.summary
    commits = []

    def repair_after_count(db, pending):
        state = summarize(db, pending)
        if not commits:
            assert db.in_transaction
            with store.connect() as writer:
                for path in paths:
                    write(store, path, "# Repaired\nValid metadata now.\n")
                    store._index(writer, path)
            commits.append(True)
        return state

    monkeypatch.setattr(index_state, "summary", repair_after_count)
    if entry == "health":
        operation = partial(store.index_state, error_limit=3)
    elif entry == "search":
        operation = partial(store.search, "Needle", view="compact")
    else:
        operation = partial(store.documents, view="compact")

    result = operation()
    assert commits == [True]
    assert result["index_error_count"] == 4
    assert result["index_errors"] == before[:3]
    assert result["index_errors_truncated"] is True
    assert result["index_status"] == "partial"
    following = operation()
    assert following["index_error_count"] == 0
    assert following["index_errors"] == []
    assert not following.get("index_errors_truncated", False)
    assert following["index_status"] == "ready"


@pytest.mark.parametrize("count,system", [(0, None), (0, "System unavailable"), (3, None), (5, None), (0, "e" * 240), (3, "Hidden system failure")])
def test_bounded_health_keeps_exact_total_status_and_full_diagnostics(store, count, system):
    for number in range(count):
        write(store, f"broken-{number}.md", "---\ntags: [broken\n---\n# Invalid metadata\n")
    store.refresh()
    if system:
        with store.connect() as db:
            db.execute("UPDATE index_state SET error=? WHERE singleton=1", (system,))
    full = store.index_state()
    total = count + bool(system)
    assert full["index_error_count"] == total
    assert len(full["index_errors"]) == total
    assert not full.get("index_errors_truncated", False)
    if system:
        assert full["index_errors"][-1] == {"path": "", "error": system}
    bounded = store.index_state(error_limit=3)
    assert bounded["index_error_count"] == total
    assert bounded["index_errors"] == full["index_errors"][:3]
    expected_status = "unavailable" if system else "partial" if count else "ready"
    assert bounded["index_status"] == full["index_status"] == expected_status
    compact = compact_index(bounded)
    assert compact["index_error_count"] == total
    assert compact["index_errors"] == [{**item, "error": item["error"][:160]} for item in full["index_errors"][:3]]
    assert bool(compact.get("index_errors_truncated")) == (total > 3 or bool(system and len(system) > 160))
    empty = store.index_state(error_limit=0)
    assert empty["index_error_count"] == total and empty["index_errors"] == []
    assert bool(empty.get("index_errors_truncated")) == bool(total)
