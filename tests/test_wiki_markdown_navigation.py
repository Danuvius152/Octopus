"""Markdown navigation excludes legacy noise without retiring stored evidence."""

import hashlib
import json
import uuid
from types import SimpleNamespace

import pytest

from server.wiki.batch_read import read_many
from server.wiki.history import WikiHistory
from server.wiki.retrieval import continuation
from server.wiki.store import WikiError, WikiStore

INTENTS = [None, "lookup", "question", "overview", "compare"]
VIEWS = ["full", "compact"]


@pytest.fixture
def store(tmp_path):
    root = tmp_path / "vault"
    root.mkdir()
    value = WikiStore(root, tmp_path / "state", str(uuid.uuid4()))
    yield value
    value.close()


def write(store, path, content):
    target = store.root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)


@pytest.mark.parametrize("intent", INTENTS)
@pytest.mark.parametrize("view", VIEWS)
def test_all_navigation_intents_filter_formats_before_totals_and_pagination(store, intent, view):
    for path in ["one.MD", "two.MarkDown", "legacy.html", "legacy.HTM", "legacy.txt"]:
        write(store, path, "# Realtime\nRealtime visible evidence with calibration conditions.\n")
    for path in ["static/tool.Md", "static/noise.html"]:
        write(store, path, "# Realtime\nRealtime tool evidence.\n")
    for include_assets, expected in [(False, {"one.MD", "two.MarkDown"}), (True, {"one.MD", "two.MarkDown", "static/tool.Md"})]:
        for operation in [store.documents, lambda **kw: store.search("Realtime", intent=intent, **kw)]:
            paths = []
            cursor = None
            while True:
                page = operation(view=view, include_assets=include_assets, limit=1, cursor=cursor)
                assert page["total"] == len(expected)
                assert len(page["items"]) == 1
                paths.extend(item["path"] for item in page["items"])
                cursor = page["next_cursor"]
                if cursor is None:
                    assert page["next_offset"] is None
                    break
            assert len(paths) == len(set(paths)) and set(paths) == expected
            second = operation(view=view, include_assets=include_assets, limit=1, offset=1)
            assert second["items"][0]["path"] == paths[1]
    assert store.documents(kind="asset", include_assets=True)["total"] == 1
    assert store.documents(directory="static", include_assets=True)["total"] == 1


@pytest.mark.parametrize("intent", INTENTS)
@pytest.mark.parametrize("view", VIEWS)
def test_recognized_nonmarkdown_identity_never_falls_back_to_markdown_title(store, intent, view):
    for suffix in ["html", "HTM", "txt"]:
        path = "legacy." + suffix
        write(store, path, "Legacy literal evidence.")
        document = store.read(path)
        uid = "urn:uuid:" + str(uuid.uuid4())
        with store.connect() as db:
            db.execute("INSERT INTO uid_aliases VALUES(?,?)", (uid, document["id"]))
        for index, query in enumerate([path, document["id"], uid.upper()]):
            write(store, f"collision-{suffix}-{index}.md", f"---\ntitle: {query}\n---\n# Collision\n{query} is a separate note.\n")
        for query in [path, document["id"], uid, uid.upper()]:
            result = store.search(query, intent=intent, view=view, include_assets=True)
            assert result["items"] == [] and result["total"] == 0
            assert result["match_mode"] == "exact"
            assert result["next_cursor"] is None and result["next_offset"] is None
            assert store.read(query)["id"] == document["id"]


@pytest.mark.parametrize("intent", [None, "question", "overview", "compare"])
@pytest.mark.parametrize("view", VIEWS)
def test_format_filter_precedes_lexical_relaxation_and_representation_groups(store, intent, view):
    write(store, "legacy.html", "<p>Realtime calibration fully matching original.</p>")
    write(store, "summary.md", "---\nx_wiki: {representation_of: legacy.html}\n---\n# Realtime\nRealtime calibration summary evidence.\n")
    result = store.search("Realtime calibration", intent=intent, view=view)
    assert result["total"] == 1 and result["items"][0]["path"] == "summary.md"
    # A complete HTML match cannot prevent partial Markdown matches from
    # becoming candidates when no Markdown covers every requested term.
    write(store, "legacy.html", "<p>Realtime calibration invisiblefullterm</p>")
    result = store.search("Realtime invisiblefullterm", intent=intent, view=view)
    assert result["total"] == 1 and result["items"][0]["path"] == "summary.md"
    assert result["match_mode"] == ("comparison" if intent == "compare" else "lexical_partial")


@pytest.mark.parametrize("view", VIEWS)
def test_old_same_epoch_navigation_cursors_are_rejected_but_graph_is_unchanged(store, view):
    write(store, "one.md", "# Realtime\nRealtime [legacy](legacy.html) [text](legacy.txt).\n")
    write(store, "two.md", "# Realtime\nRealtime evidence.\n")
    write(store, "legacy.html", "<p>Legacy evidence.</p>")
    write(store, "legacy.txt", "Legacy text evidence.")
    epoch = store.index_state()["index_epoch"]
    old_filters = [
        {"query": "Realtime", "kind": None, "assets": False, "directory": None, "intent": None, "view": view},
        {"kind": None, "quality": None, "source": None, "assets": False, "directory": None, "view": view},
    ]
    for operation, filters in zip([lambda **kw: store.search("Realtime", **kw), store.documents], old_filters, strict=True):
        fingerprint = hashlib.sha256(json.dumps(filters, sort_keys=True).encode()).hexdigest()
        with pytest.raises(WikiError) as error:
            operation(view=view, cursor=continuation(1, epoch, fingerprint))
        assert error.value.status == 422
        first = operation(view=view, limit=1)
        assert operation(view=view, cursor=first["next_cursor"], limit=1)["items"]
    graph = store.graph("one.md", limit=1)
    second = store.graph("one.md", limit=1, cursor=graph["next_cursor"])
    assert graph["total"] == second["total"] == 2
    assert {node["path"] for page in [graph, second] for node in page["nodes"]} == {"one.md", "legacy.html", "legacy.txt"}


def test_legacy_batch_reads_history_and_restore_remain_compatible(store):
    history = WikiHistory(store)
    for path, before, after in [("legacy.html", b"<p>Old HTML.</p>", b"<p>Current HTML.</p>"), ("legacy.txt", b"Old text.", b"Current text.")]:
        created = history.write(path, before, None, "human", "create")
        edited = history.write(path, after, created["revision"], "human", "edit")
        current = store.read(path)
        request = SimpleNamespace(doc=current["id"], start=0, length=100, expected_revision=current["revision"])
        result = read_many(store, [request], 100, view="compact")
        assert result["items"][0]["status"] == "ok"
        assert result["ranges"][0]["content"] == current["content"]
        assert len(history.history(path)["items"]) == 2
        history.restore(edited["change_id"], current["revision"], "before", "human")
        assert store.path(path).read_bytes() == before
        assert store.read(current["id"])["revision"] == created["revision"]
        assert store.search(path)["total"] == 0


def test_compact_navigation_bounds_errors_but_full_keeps_complete_diagnostics(store, monkeypatch):
    write(store, "one.md", "# Realtime\nRealtime evidence.\n")
    state = store.index_state() | {"index_errors": [{"path": "p" * 200 + str(index), "error": "e" * 200} for index in range(5)]}
    monkeypatch.setattr(store, "index_state", lambda: state)
    for operation in [store.documents, lambda **kw: store.search("Realtime", **kw)]:
        compact = operation(view="compact")
        assert len(compact["index_errors"]) == 3
        assert compact["index_error_count"] == 5 and compact["index_errors_truncated"] is True
        assert all(len(item["path"]) == len(item["error"]) == 160 for item in compact["index_errors"])
        assert operation(view="full")["index_errors"] == state["index_errors"]


def test_joined_navigation_metadata_preserves_overlay_and_order_without_per_item_connections(store, monkeypatch):
    for index in range(4):
        path = f"note-{index}.md"
        write(store, path, "---\ntags: [native, common]\naliases: [Native, Common]\ndescription: native\n---\n# Realtime\nRealtime evidence.\n")
        document = store.read(path)
        supplied = {"tags": ["common", "managed"], "aliases": ["Common", "Managed"], "description": "managed"}
        with store.connect() as db:
            db.execute("INSERT INTO managed_notes VALUES(?,?,?,?,?)", (path, document["revision"], document["revision"], "{}", json.dumps(supplied)))
    with store.connect() as db:
        row = db.execute("SELECT * FROM documents WHERE path='note-0.md'").fetchone()
        joined = db.execute("SELECT d.*,m.metadata AS managed_metadata FROM documents d LEFT JOIN managed_notes m ON m.path=d.path WHERE d.path='note-0.md'").fetchone()
    expected = store.indexed_metadata(row)
    assert expected == store.indexed_metadata(joined)
    assert expected["description"] == "managed"
    assert expected["tags"] == ["native", "common", "managed"]
    assert expected["aliases"] == ["Native", "Common", "Managed"]
    original = store.indexed_metadata
    seen = []

    def metadata(row):
        assert "managed_metadata" in row.keys()
        seen.append(row["path"])
        return original(row)

    monkeypatch.setattr(store, "indexed_metadata", metadata)
    for operation in [store.documents, lambda **kw: store.search("Realtime", **kw), lambda **kw: store.search("note-0.md", **kw)]:
        for view in VIEWS:
            assert operation(view=view)["items"]
    assert len(seen) == 18
